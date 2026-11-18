"""Optional Apple security-upgrade deferral, NOT a security-question solver.

Protocol reference: fastlane/spaceship/upgrade_2fa_later_client.rb.
Scoped to this service instance; no global monkeypatches. pyicloud==2.6.5.
"""
import logging
import os
from importlib.metadata import version
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from pyicloud import PyiCloudService
from pyicloud.session import PyiCloudSession

log = logging.getLogger(__name__)


class LegacyRepairError(RuntimeError):
    """Sanitized repair failure, safe to display without response/token dumps."""


def _required(headers, name, stage):
    value = headers.get(name)
    if not value:
        raise LegacyRepairError(f"Apple repair [{stage}]: отсутствует {name}. Нужен новый вход в браузере.")
    return value


def _json_object(response, stage):
    try:
        body = response.json()
    except ValueError:
        raise LegacyRepairError(f"Apple repair [{stage}]: ожидался JSON.") from None
    if not isinstance(body, dict):
        raise LegacyRepairError(f"Apple repair [{stage}]: неизвестный формат ответа.")
    if body.get("serviceErrors") or body.get("validationErrors"):
        raise LegacyRepairError(f"Apple repair [{stage}]: сервер отклонил действие.")
    return body


def complete_optional_repair(session, initial):
    """Run the documented deferral flow once, preserving separate IDMS contexts.

    Uses raw requests because pyicloud normally discards 412 as a login error.
    Raw requests still ingest cookies and persist session state. Temporary repair
    tokens are removed from session.data afterwards, including on failure.
    """
    location = _required(initial.headers, "Location", "signin")
    target = urlsplit(urljoin("https://idmsa.apple.com/", location))
    if (target.scheme != "https" or target.hostname not in {"appleid.apple.com", "account.apple.com"}
            or target.username or target.password or target.port not in (None, 443)
            or not target.path.startswith("/widget/account/")):
        raise LegacyRepairError("Apple repair: неизвестный адрес widget; токены не отправлены.")
    widget = parse_qs(target.query).get("widgetKey", [None])[0]
    if not widget:
        raise LegacyRepairError("Apple repair: widgetKey отсутствует в Location.")
    sid = _required(initial.headers, "X-Apple-ID-Session-Id", "signin")
    initial_scnt = _required(initial.headers, "scnt", "signin")
    repair_token = _required(initial.headers, "X-Apple-Repair-Session-Token", "signin")
    account_origin = urlunsplit((target.scheme, target.netloc, "", "", ""))
    original_state = dict(session.data)
    final = None

    def send(stage, method, url, headers, expected, **kwargs):
        # No automatic redirect following: never leak auth headers to a new host.
        try:
            response = session.request_raw(method, url, headers=headers, timeout=30,
                                           allow_redirects=False, **kwargs)
        except Exception:
            raise LegacyRepairError(f"Apple repair [{stage}]: сетевая ошибка; автоматический повтор отключён.") from None
        log.info("Apple optional repair: %s HTTP %s", stage, response.status_code)
        if response.status_code not in expected:
            raise LegacyRepairError(f"Apple repair [{stage}]: HTTP {response.status_code}. Повторный вход остановлен.")
        return response

    try:
        shell = send("widget", "GET", urlunsplit(target._replace(fragment="")),
                     {"Accept": "text/html", "Referer": "https://www.icloud.com/"}, {200})
        common = {
            "X-Apple-ID-Session-Id": sid,
            "X-Apple-Skip-Repair-Attributes": "[]",
            "X-Apple-Widget-Key": widget,
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript",
            "Origin": account_origin,
            "Referer": account_origin + "/",
        }
        options = send("options", "GET", account_origin + "/account/manage/repair/options",
                       dict(common, **{"scnt": _required(shell.headers, "scnt", "widget"),
                                      "X-Apple-Session-Token": repair_token}), {200})
        data = _json_object(options, "options")
        steps = data.get("requiredSteps")
        if data.get("securityQuestions") or data.get("mandatory") is True:
            raise LegacyRepairError("Apple требует отдельную проверку личности; этот модуль не отвечает на вопросы.")
        if (data.get("repairAttribute") != "hsa2_enrollment"
                or not isinstance(steps, list) or not steps
                or any(step != "hsa2_enrollment" for step in steps)):
            raise LegacyRepairError("Apple repair: это не необязательное предложение hsa2_enrollment; остановлено.")
        current_token = _required(options.headers, "X-Apple-Session-Token", "options")
        later = send("setuplater", "GET", account_origin + "/account/security/upgrade/setuplater",
                     dict(common, **{"scnt": _required(options.headers, "scnt", "options"),
                                    "X-Apple-Session-Token": current_token}), {200, 204})
        if later.content:
            later_data = _json_object(later, "setuplater")
            if later_data.get("mandatory") is True or later_data.get("accountAlreadyEnrolled") is True:
                raise LegacyRepairError("Apple не разрешила отложить enrollment; автоматическая обработка остановлена.")
        # fastlane uses ORIGINAL signin scnt here, not the last repair scnt.
        final_headers = {
            "scnt": initial_scnt,
            "X-Apple-ID-Session-Id": sid,
            "X-Apple-Repair-Session-Token": current_token,
            "X-Apple-Widget-Key": widget,
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json;charset=utf-8",
            "Origin": "https://idmsa.apple.com",
            "Referer": "https://idmsa.apple.com/",
        }
        result = send("complete", "POST", "https://idmsa.apple.com/appleauth/auth/repair/complete",
                      final_headers, {204}, data=b"")
        _required(result.headers, "X-Apple-Session-Token", "complete")
        final = result
        return result
    finally:
        # Do not persist a repair-context token as an ordinary iCloud token.
        session.data.clear()
        session.data.update(original_state)
        if final is not None:
            session._update_session_data(initial)
            session._update_session_data(final)
        session._save_session_data()


class RepairSession(PyiCloudSession):
    def _handle_request_error(self, status_code, response):
        parsed = urlsplit(response.url)
        enabled = os.getenv("ICLOUD_ALLOW_OPTIONAL_REPAIR", "1").lower() in {"1", "true", "yes"}
        if (enabled and status_code == 412 and parsed.scheme == "https"
                and parsed.hostname == "idmsa.apple.com"
                and parsed.path == "/appleauth/auth/signin/complete"):
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("authType") == "sa":
                return complete_optional_repair(self, response)
        return super()._handle_request_error(status_code, response)


class RepairPyiCloudService(PyiCloudService):
    def __init__(self, *args, **kwargs):
        installed = version("pyicloud")
        if installed != "2.6.5":
            raise LegacyRepairError("Нужен pyicloud==2.6.5 из requirements.txt; переустановите зависимости.")
        authenticate = kwargs.pop("authenticate", True)
        super().__init__(*args, authenticate=False, **kwargs)
        old = self._session
        self._session = RepairSession(
            self, client_id=self._client_id, cookie_directory=old._cookie_directory,
            verify=old.verify, headers=dict(old.headers),
        )
        old.close()
        if authenticate:
            self.authenticate()
