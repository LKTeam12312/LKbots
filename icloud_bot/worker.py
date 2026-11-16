"""Фоновой воркер: опрос активных аккаунтов раз в POLL_INTERVAL (>= 60 c)."""
import asyncio
import html
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from pyicloud.exceptions import PyiCloudException

from icloud_bot.config import ADMIN_IDS, MOVE_THRESHOLD_DEG, POLL_INTERVAL
from icloud_bot.db import Database
from icloud_bot.icloud_client import (
    DEVICE_STATUS_TEXT,
    AuthError,
    ICloudManager,
    ReauthRequired,
)

log = logging.getLogger(__name__)


def _status_text(code: str) -> str:
    return DEVICE_STATUS_TEXT.get(code, code or "—")


def _moved(prev: dict, snap: dict) -> bool:
    plat, plon = prev.get("lat"), prev.get("lon")
    lat, lon = snap.get("lat"), snap.get("lon")
    if None in (plat, plon, lat, lon):
        return False
    return abs(lat - plat) > MOVE_THRESHOLD_DEG or abs(lon - plon) > MOVE_THRESHOLD_DEG


class MonitorWorker:
    def __init__(self, bot: Bot, db: Database, manager: ICloudManager) -> None:
        self._bot = bot
        self._db = db
        self._manager = manager
        self._stop = asyncio.Event()
        self._paused: set = set()  # аккаунты, ждущие ручного входа (не дергаем Apple)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("Воркер мониторинга запущен, интервал %s c", POLL_INTERVAL)
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Ошибка цикла мониторинга")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            log.info("Воркер мониторинга остановлен")
            raise

    async def _tick(self) -> None:
        for doc in await self._db.list_active():
            if self._stop.is_set():
                break
            await self._check_account(doc)
            await asyncio.sleep(3)  # разносим запросы разных аккаунтов

    async def _check_account(self, doc: dict) -> None:
        account_id = str(doc["_id"])
        apple_id = doc["apple_id"]

        # Антифрод: если вход сломан, ждем ручного «🔐 Войти», а не пытаемся
        # логиниться в Apple каждый цикл.
        if account_id in self._paused and not self._manager.is_authorized(account_id):
            return

        try:
            snapshots = await self._manager.poll(doc)
        except ReauthRequired as exc:
            await self._pause(account_id, apple_id, str(exc), reauth=True)
            return
        except AuthError as exc:
            await self._pause(account_id, apple_id, str(exc), reauth=False)
            return
        except PyiCloudException as exc:
            # Сетевые/временные ошибки API: пробуем снова на следующем цикле.
            self._manager.invalidate(account_id)
            log.warning("Ошибка опроса %s: %s", apple_id, exc)
            await self._db.update_fields(account_id, last_error=str(exc))
            return

        self._paused.discard(account_id)
        if not snapshots:
            # Пустой ответ не должен затирать known_devices.
            await self._db.update_fields(account_id, last_check_at=datetime.now(timezone.utc), last_error=None)
            return

        known = set(doc.get("known_devices") or [])
        prev_states = doc.get("device_states") or {}
        tracking = bool(doc.get("tracking_enabled"))
        # Первый опрос аккаунта: молча запоминаем устройства, чтобы не спамить.
        first_run = not known and not prev_states

        current_ids = []
        new_states = {}
        for snap in snapshots:
            device_id = snap["id"]
            current_ids.append(device_id)
            new_states[device_id] = {
                "name": snap["name"],
                "status": snap["status_code"],
                "lat": snap["lat"],
                "lon": snap["lon"],
                "battery": snap["battery"],
            }
            name = html.escape(snap["name"])
            account = html.escape(apple_id)

            if device_id not in known:
                if not first_run:
                    await self._notify(
                        f"🆕 <b>Новое устройство</b> в аккаунте <b>{account}</b>:\n"
                        f"{name} — {html.escape(snap['model'])}, {_status_text(snap['status_code'])}"
                    )
                continue

            if not tracking:
                continue

            prev = prev_states.get(device_id) or {}
            if prev.get("status") and snap["status_code"] and prev["status"] != snap["status_code"]:
                await self._notify(
                    f"🔄 Статус <b>{name}</b> ({account}): "
                    f"{_status_text(prev['status'])} → {_status_text(snap['status_code'])}"
                )
            if _moved(prev, snap):
                await self._notify(
                    f"📍 <b>{name}</b> ({account}) сместилось:\n"
                    f"<code>{snap['lat']:.6f}, {snap['lon']:.6f}</code>\n"
                    "https:" + f"//maps.google.com/?q={snap['lat']},{snap['lon']}"
                )

        await self._db.update_monitor_state(account_id, current_ids, new_states)

    async def _pause(self, account_id: str, apple_id: str, error: str, reauth: bool) -> None:
        self._manager.invalidate(account_id)
        await self._db.update_fields(account_id, last_error=error)
        if account_id in self._paused:
            return
        self._paused.add(account_id)
        reason = (
            "нужна повторная авторизация (2FA/2SA). Нажмите «🔐 Войти» в карточке аккаунта."
            if reauth
            else "ошибка входа. Проверьте пароль (при необходимости удалите и добавьте аккаунт заново)."
        )
        await self._notify(
            f"🔐 <b>{html.escape(apple_id)}</b>: {reason}\n⚠️ {html.escape(error[:300])}"
        )

    async def _notify(self, text: str) -> None:
        for admin_id in ADMIN_IDS:
            try:
                await self._bot.send_message(admin_id, text)
            except TelegramAPIError as exc:
                log.warning("Не удалось уведомить админа %s: %s", admin_id, exc)
