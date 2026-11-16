"""Асинхронная обертка над синхронным pyicloud + персистентные сессии в MongoDB.

Все сетевые/блокирующие вызовы pyicloud выполняются через asyncio.to_thread().
На один аккаунт — один asyncio.Lock (requests.Session не потокобезопасна).
"""
import asyncio
import hashlib
import io
import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from icloud_bot.legacy_repair import LegacyRepairError, RepairPyiCloudService as PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2SARequiredException,
    PyiCloudAPIResponseException,
    PyiCloudException,
    PyiCloudFailedLoginException,
    PyiCloudNoDevicesException,
)

from icloud_bot.config import CHINA_MAINLAND, SESSION_ROOT
from icloud_bot.db import Database, decrypt_text

log = logging.getLogger(__name__)

# status(additional) вернет эти поля из content устройства без лишних вызовов:
# один refresh_client() на вызов status() обновляет сразу ВСЕ устройства аккаунта.
EXTRA_STATUS_FIELDS = [
    "batteryStatus",
    "location",
    "locationCapable",
    "lostModeCapable",
    "deviceModel",
    "isLocating",
]

DEVICE_STATUS_TEXT = {
    "200": "🟢 онлайн",
    "201": "🔴 офлайн",
    "203": "🟡 ожидание ответа",
    "204": "⚪ не зарегистрировано",
}


class AuthError(Exception):
    """Ошибка входа/сессии iCloud, понятная пользователю."""


class ReauthRequired(AuthError):
    """Нужен интерактивный вход (2FA/2SA) — воркер сам это не пройдет."""


def device_token(device_id: str) -> str:
    """Короткий стабильный токен для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.sha1(device_id.encode()).hexdigest()[:12]


class ICloudManager:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._apis: dict = {}       # account_id -> авторизованный PyiCloudService
        self._pending: dict = {}    # account_id -> api в процессе 2FA/2SA
        self._fmip: dict = {}       # account_id -> FindMyiPhoneServiceManager (кеш)
        self._locks: dict = {}      # account_id -> asyncio.Lock
        self._device_ids: dict = {} # account_id -> {token: полный device id}
        self._2sa_devices: dict = {}  # account_id -> выбранный trusted device (dict)

    def _lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    @staticmethod
    def _session_dir(apple_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", apple_id)
        return SESSION_ROOT / safe

    # ---------- персистентность сессий: /tmp <-> MongoDB (zip в bson.Binary) ----------

    async def _restore_session(self, account_id: str, apple_id: str) -> Path:
        directory = self._session_dir(apple_id)
        # pyicloud делает только одиночный mkdir — создаем родителей сами.
        directory.mkdir(parents=True, exist_ok=True)
        blob = await self._db.load_session_blob(account_id)
        if blob:
            def _unpack() -> None:
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    # ZipFile.extractall сам отсекает абсолютные пути и "..".
                    zf.extractall(directory)

            await asyncio.to_thread(_unpack)
            log.info("Сессия %s восстановлена из MongoDB", apple_id)
        return directory

    async def _save_session(self, account_id: str, apple_id: str) -> None:
        directory = self._session_dir(apple_id)
        if not directory.exists():
            return

        def _pack() -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for file in sorted(directory.rglob("*")):
                    if file.is_file():
                        zf.write(file, file.relative_to(directory).as_posix())
            return buf.getvalue()

        blob = await asyncio.to_thread(_pack)
        await self._db.save_session_blob(account_id, blob)
        log.debug("Сессия %s сохранена в MongoDB (%d байт)", apple_id, len(blob))

    # ---------- вход и проверки безопасности ----------

    async def login(self, doc: dict) -> str:
        """Полный вход. Возвращает 'ok' | '2fa' | '2sa'. Бросает AuthError."""
        account_id = str(doc["_id"])
        async with self._lock(account_id):
            return await self._login_locked(doc)

    async def _login_locked(self, doc: dict) -> str:
        account_id = str(doc["_id"])
        apple_id = doc["apple_id"]
        self.invalidate(account_id)
        directory = await self._restore_session(account_id, apple_id)
        try:
            password = decrypt_text(doc["password"])
        except Exception as exc:
            raise AuthError("Не удалось расшифровать пароль — проверьте ENCRYPTION_KEY.") from exc
        try:
            api = await asyncio.to_thread(
                PyiCloudService,
                apple_id,
                password,
                cookie_directory=str(directory),
                with_family=False,  # только собственные устройства аккаунта
                china_mainland=CHINA_MAINLAND,
            )
        except LegacyRepairError as exc:
            raise AuthError(str(exc)) from None
        except PyiCloudFailedLoginException as exc:
            raise AuthError(f"Apple отклонила вход (неверный пароль или блокировка): {exc}") from exc
        except PyiCloudAPIResponseException as exc:
            raise AuthError(f"Ошибка API iCloud при входе: {exc}") from exc
        except PyiCloudException as exc:
            raise AuthError(f"Ошибка pyicloud при входе: {exc}") from exc
        return await self._route_auth_state(doc, api)

    async def _route_auth_state(self, doc: dict, api: PyiCloudService) -> str:
        account_id = str(doc["_id"])
        try:
            requires_2fa = await asyncio.to_thread(getattr, api, "requires_2fa")
            requires_2sa = await asyncio.to_thread(getattr, api, "requires_2sa")
        except (KeyError, PyiCloudException) as exc:
            raise AuthError(f"Не удалось определить состояние аутентификации: {exc}") from exc

        if requires_2fa or requires_2sa:
            self._pending[account_id] = api
            # scnt/session_id уже записаны pyicloud на диск — сохраняем сразу.
            await self._save_session(account_id, doc["apple_id"])
            return "2fa" if requires_2fa else "2sa"

        # Confirm actual Find My access, not merely completion of a repair screen.
        if not api.data.get("dsInfo", {}).get("dsid"):
            raise AuthError("Apple не вернула dsid после входа. Сессия не помечена как активная.")
        try:
            fmip = await asyncio.to_thread(getattr, api, "devices")
        except PyiCloudNoDevicesException:
            fmip = None
        except PyiCloud2SARequiredException as exc:
            raise ReauthRequired("Find My требует подтверждения входа.") from exc
        except PyiCloudException as exc:
            raise AuthError("Вход выполнен, но доступ к Find My не подтвержден. Попробуйте ручной вход.") from exc
        self._pending.pop(account_id, None)
        self._apis[account_id] = api
        if fmip is not None:
            self._fmip[account_id] = fmip
        await self._save_session(account_id, doc["apple_id"])
        await self._db.update_fields(account_id, is_active=True, last_error=None)
        return "ok"

    async def _ensure_api(self, doc: dict) -> PyiCloudService:
        """Вызывать только под захваченным локом аккаунта."""
        account_id = str(doc["_id"])
        if account_id in self._apis:
            return self._apis[account_id]
        status = await self._login_locked(doc)
        if status != "ok":
            raise ReauthRequired(
                "Требуется подтверждение входа (2FA/2SA). Откройте аккаунт и нажмите «🔐 Войти»."
            )
        return self._apis[account_id]

    async def validate_2fa(self, account_id: str, code: str) -> bool:
        async with self._lock(account_id):
            api = self._pending.get(account_id)
            doc = await self._db.get_account(account_id)
            if api is None or doc is None:
                raise ReauthRequired("Сессия входа устарела — нажмите «🔐 Войти» заново.")
            ok = await asyncio.to_thread(api.validate_2fa_code, code)
            if not ok:
                return False
            trusted = await asyncio.to_thread(getattr, api, "is_trusted_session")
            if not trusted:
                trusted = await asyncio.to_thread(api.trust_session)
                if not trusted:
                    log.warning("trust_session() вернул False для %s", doc["apple_id"])
            # Сразу сохраняем обновленную папку сессии в MongoDB.
            await self._save_session(account_id, doc["apple_id"])
            self._pending.pop(account_id, None)
            self._apis[account_id] = api
            await self._db.update_fields(account_id, is_active=True, last_error=None)
            return True

    async def get_trusted_devices(self, account_id: str) -> list:
        api = self._pending.get(account_id)
        if api is None:
            raise ReauthRequired("Сессия входа устарела — нажмите «🔐 Войти» заново.")
        return await asyncio.to_thread(getattr, api, "trusted_devices") or []

    async def send_2sa_code(self, account_id: str, index: int) -> bool:
        async with self._lock(account_id):
            api = self._pending.get(account_id)
            if api is None:
                raise ReauthRequired("Сессия входа устарела — нажмите «🔐 Войти» заново.")
            devices = await asyncio.to_thread(getattr, api, "trusted_devices") or []
            if not devices or not 0 <= index < len(devices):
                raise AuthError("Доверенное устройство не найдено.")
            self._2sa_devices[account_id] = devices[index]
            return await asyncio.to_thread(api.send_verification_code, devices[index])

    async def validate_2sa(self, account_id: str, code: str) -> str:
        """Возвращает 'ok' | 'bad_code' | '2fa' (Apple может запросить 2FA после 2SA)."""
        async with self._lock(account_id):
            api = self._pending.get(account_id)
            device = self._2sa_devices.get(account_id)
            doc = await self._db.get_account(account_id)
            if api is None or device is None or doc is None:
                raise ReauthRequired("Сессия входа устарела — нажмите «🔐 Войти» заново.")
            ok = await asyncio.to_thread(api.validate_verification_code, device, code)
            if not ok:
                return "bad_code"
            await self._save_session(account_id, doc["apple_id"])
            if await asyncio.to_thread(getattr, api, "requires_2fa"):
                return "2fa"
            self._pending.pop(account_id, None)
            self._2sa_devices.pop(account_id, None)
            self._apis[account_id] = api
            await self._db.update_fields(account_id, is_active=True, last_error=None)
            return "ok"

    # ---------- устройства Find My ----------

    def invalidate(self, account_id: str) -> None:
        self._apis.pop(account_id, None)
        self._fmip.pop(account_id, None)

    def is_authorized(self, account_id: str) -> bool:
        """Есть ли живая авторизованная сессия (для воркера)."""
        return account_id in self._apis

    async def _get_fmip_locked(self, doc: dict):
        account_id = str(doc["_id"])
        fmip = self._fmip.get(account_id)
        if fmip is None:
            api = await self._ensure_api(doc)
            try:
                # ВАЖНО: у pyicloud каждый доступ к api.devices создает новый
                # менеджер и делает refresh — кешируем его на аккаунт.
                fmip = await asyncio.to_thread(getattr, api, "devices")
            except PyiCloudNoDevicesException:
                return None
            except PyiCloud2SARequiredException as exc:
                self.invalidate(account_id)
                raise ReauthRequired("Сессия перестала быть доверенной — нужен вход с кодом.") from exc
            self._fmip[account_id] = fmip
        return fmip

    def _snapshot(self, account_id: str, device, status: dict) -> dict:
        device_id = device["id"]
        token = device_token(device_id)
        self._device_ids.setdefault(account_id, {})[token] = device_id
        loc = status.get("location") or {}
        battery = status.get("batteryLevel")
        return {
            "id": device_id,
            "token": token,
            "name": status.get("name") or device["name"],
            "model": status.get("deviceDisplayName") or status.get("deviceModel") or "—",
            "status_code": str(status.get("deviceStatus") or ""),
            "battery": float(battery) if battery is not None else None,
            "battery_status": status.get("batteryStatus") or "Unknown",
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
            "location_time": loc.get("timeStamp"),
            "location_capable": bool(status.get("locationCapable")),
            "lost_mode_capable": bool(status.get("lostModeCapable")),
        }

    async def fetch_devices(self, doc: dict) -> list:
        account_id = str(doc["_id"])
        async with self._lock(account_id):
            fmip = await self._get_fmip_locked(doc)
            if fmip is None:
                return []
            snapshots = []
            for device in list(fmip.values()):
                try:
                    status = await asyncio.to_thread(device.status, EXTRA_STATUS_FIELDS)
                except PyiCloudNoDevicesException:
                    continue
                except PyiCloud2SARequiredException as exc:
                    self.invalidate(account_id)
                    raise ReauthRequired("Сессия перестала быть доверенной — нужен вход с кодом.") from exc
                snapshots.append(self._snapshot(account_id, device, status))
            return snapshots

    async def _resolve_device_locked(self, doc: dict, token: str):
        account_id = str(doc["_id"])
        fmip = await self._get_fmip_locked(doc)
        if fmip is None:
            raise AuthError("У аккаунта нет устройств в Find My.")
        mapping = self._device_ids.setdefault(account_id, {})
        device_id = mapping.get(token)
        if device_id is None:
            # Карта токенов могла потеряться после рестарта — пересобираем.
            for device in fmip.values():
                mapping[device_token(device["id"])] = device["id"]
            device_id = mapping.get(token)
        if device_id is None:
            raise AuthError("Устройство не найдено — обновите список устройств.")
        try:
            return fmip[device_id]
        except KeyError as exc:
            raise AuthError("Устройство больше не привязано к аккаунту.") from exc

    async def device_location(self, doc: dict, token: str) -> Optional[dict]:
        async with self._lock(str(doc["_id"])):
            device = await self._resolve_device_locked(doc, token)
            try:
                loc = await asyncio.to_thread(device.location)
            except KeyError:
                return None
            except PyiCloud2SARequiredException as exc:
                self.invalidate(str(doc["_id"]))
                raise ReauthRequired("Сессия перестала быть доверенной — нужен вход с кодом.") from exc
            return loc if isinstance(loc, dict) else None

    async def play_sound(self, doc: dict, token: str) -> None:
        async with self._lock(str(doc["_id"])):
            device = await self._resolve_device_locked(doc, token)
            await asyncio.to_thread(device.play_sound)

    async def lost_mode(self, doc: dict, token: str, phone: str, text: str) -> None:
        async with self._lock(str(doc["_id"])):
            device = await self._resolve_device_locked(doc, token)
            await asyncio.to_thread(device.lost_device, phone, text)

    # ---------- для фонового воркера ----------

    async def poll(self, doc: dict) -> list:
        snapshots = await self.fetch_devices(doc)
        if snapshots:
            # pyicloud обновляет cookies/session-файл после каждого запроса —
            # освежаем zip в MongoDB, чтобы пережить редеплой Railway.
            async with self._lock(str(doc["_id"])):
                await self._save_session(str(doc["_id"]), doc["apple_id"])
        return snapshots

    async def forget(self, account_id: str, apple_id: str) -> None:
        self.invalidate(account_id)
        self._pending.pop(account_id, None)
        self._2sa_devices.pop(account_id, None)
        self._device_ids.pop(account_id, None)
        await asyncio.to_thread(shutil.rmtree, self._session_dir(apple_id), ignore_errors=True)
