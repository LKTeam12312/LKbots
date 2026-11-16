"""Точка входа. Запуск ИЗ КОРНЯ репозитория: python icloud_bot/bot.py"""
import asyncio
import logging
import sys
from contextlib import suppress
from pathlib import Path

# --- bootstrap sys.path -------------------------------------------------------
# При запуске `python icloud_bot/bot.py` sys.path[0] = icloud_bot/, а не корень
# репозитория. Добавляем корень, чтобы абсолютные импорты icloud_bot.* работали.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# -----------------------------------------------------------------------------

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from icloud_bot.config import BOT_TOKEN, validate_config  # noqa: E402
from icloud_bot.db import Database  # noqa: E402
from icloud_bot.handlers import AccessMiddleware, router  # noqa: E402
from icloud_bot.icloud_client import ICloudManager  # noqa: E402
from icloud_bot.worker import MonitorWorker  # noqa: E402

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("pyicloud").setLevel(logging.WARNING)
    validate_config()

    db = Database()
    await db.init()
    manager = ICloudManager(db)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # DI: эти объекты aiogram передает в хендлеры по именам аргументов.
    dp["db"] = db
    dp["manager"] = manager

    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)

    worker = MonitorWorker(bot, db, manager)
    worker_task = asyncio.create_task(worker.run(), name="icloud-monitor-worker")

    try:
        # Long Polling. aiogram сам регистрирует обработчики SIGINT/SIGTERM
        # (handle_signals=True по умолчанию) и корректно завершает polling;
        # сессию бота он тоже закрывает сам (close_bot_session=True).
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.stop()
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        await db.close()
        log.info("Graceful shutdown завершен: воркер остановлен, MongoDB закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
