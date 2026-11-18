"""Хендлеры Telegram: меню, FSM добавления аккаунта, 2FA/2SA, устройства."""
import html
import logging
import re
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pymongo.errors import DuplicateKeyError
from pyicloud.exceptions import PyiCloudException

from icloud_bot.config import ADMIN_IDS
from icloud_bot.db import Database, decrypt_text
from icloud_bot.icloud_client import (
    DEVICE_STATUS_TEXT,
    AuthError,
    ICloudManager,
    ReauthRequired,
)

log = logging.getLogger(__name__)
router = Router(name="icloud_bot")

WELCOME = (
    "🍏 <b>iCloud Monitor</b>\n\n"
    "Управление аккаунтами Apple ID и мониторинг устройств Find My.\n"
    "Доступ разрешен только администраторам.\n\n"
    "Команды: /add — добавить аккаунт, /accounts — список, /cancel — отмена."
)

BATTERY_STATUS_TEXT = {
    "Charging": "⚡️ заряжается",
    "NotCharging": "🔋 не заряжается",
    "FullyCharged": "✅ заряжено",
    "Unknown": "",
}


class AccessMiddleware(BaseMiddleware):
    """Пускаем только пользователей из ADMIN_IDS (остальных молча игнорируем)."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in ADMIN_IDS:
            return None
        return await handler(event, data)


# ---------- FSM ----------

class AddAccount(StatesGroup):
    apple_id = State()
    password = State()
    questions = State()


class Auth2FA(StatesGroup):
    code = State()


class Auth2SA(StatesGroup):
    code = State()


class LostMode(StatesGroup):
    phone = State()
    text = State()


# ---------- callback-фабрики (данные компактнее лимита 64 байта) ----------

class AccountCB(CallbackData, prefix="acc"):
    action: str
    account_id: str = ""


class DeviceCB(CallbackData, prefix="dev"):
    action: str
    account_id: str
    token: str


class TwoSACB(CallbackData, prefix="twa"):
    account_id: str
    index: int


# ---------- клавиатуры и тексты ----------

def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить аккаунт", callback_data=AccountCB(action="add").pack())
    b.button(text="📋 Мои аккаунты", callback_data=AccountCB(action="list").pack())
    b.adjust(1)
    return b.as_markup()


def accounts_kb(docs: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for doc in docs:
        mark = "🟢" if doc.get("is_active") else "⚪️"
        b.button(
            text=f"{mark} {doc['apple_id']}",
            callback_data=AccountCB(action="sel", account_id=str(doc["_id"])).pack(),
        )
    b.button(text="➕ Добавить", callback_data=AccountCB(action="add").pack())
    b.button(text="⬅️ Меню", callback_data=AccountCB(action="menu").pack())
    b.adjust(1)
    return b.as_markup()


def account_kb(doc: dict) -> InlineKeyboardMarkup:
    aid = str(doc["_id"])
    b = InlineKeyboardBuilder()
    b.button(text="📱 Устройства", callback_data=AccountCB(action="dev", account_id=aid).pack())
    b.button(text="🔐 Войти / проверить сессию", callback_data=AccountCB(action="login", account_id=aid).pack())
    b.button(
        text="⏸ Деактивировать" if doc.get("is_active") else "▶️ Сделать активным",
        callback_data=AccountCB(action="act", account_id=aid).pack(),
    )
    b.button(
        text="🛰 Трекинг: ВКЛ ✅" if doc.get("tracking_enabled") else "🛰 Трекинг: ВЫКЛ",
        callback_data=AccountCB(action="trk", account_id=aid).pack(),
    )
    if doc.get("security_questions"):
        b.button(text="❓ Контрольные вопросы", callback_data=AccountCB(action="qa", account_id=aid).pack())
    b.button(text="🗑 Удалить", callback_data=AccountCB(action="del", account_id=aid).pack())
    b.button(text="⬅️ К списку", callback_data=AccountCB(action="list").pack())
    b.adjust(1)
    return b.as_markup()


def device_kb(account_id: str, snap: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📍 Геопозиция", callback_data=DeviceCB(action="loc", account_id=account_id, token=snap["token"]).pack())
    b.button(text="🔊 Сигнал", callback_data=DeviceCB(action="snd", account_id=account_id, token=snap["token"]).pack())
    b.button(text="🚨 Режим пропажи", callback_data=DeviceCB(action="lost", account_id=account_id, token=snap["token"]).pack())
    b.adjust(2, 1)
    return b.as_markup()


def account_card_text(doc: dict) -> str:
    last = doc.get("last_check_at")
    last_str = last.strftime("%d.%m %H:%M UTC") if isinstance(last, datetime) else "—"
    lines = [
        f"🍏 <b>{html.escape(doc['apple_id'])}</b>",
        f"Активен (воркер): {'да 🟢' if doc.get('is_active') else 'нет ⚪️'}",
        f"Трекинг координат: {'ВКЛ 🛰' if doc.get('tracking_enabled') else 'ВЫКЛ'}",
        f"Сессия в MongoDB: {'есть ✅' if doc.get('session_data') else 'нет'}",
        f"Известных устройств: {len(doc.get('known_devices') or [])}",
        f"Последняя проверка: {last_str}",
    ]
    if doc.get("last_error"):
        lines.append(f"⚠️ {html.escape(str(doc['last_error'])[:200])}")
    return "\n".join(lines)


def device_text(snap: dict) -> str:
    battery = f"{snap['battery'] * 100:.0f}%" if snap.get("battery") is not None else "—"
    bs = BATTERY_STATUS_TEXT.get(snap.get("battery_status") or "", "")
    lines = [
        f"📱 <b>{html.escape(snap['name'])}</b>",
        f"Модель: {html.escape(snap['model'])}",
        f"Статус: {DEVICE_STATUS_TEXT.get(snap['status_code'], snap['status_code'] or '—')}",
        f"Батарея: {battery} {bs}".rstrip(),
    ]
    if snap.get("lat") is not None and snap.get("lon") is not None:
        lines.append(f"Координаты: <code>{snap['lat']:.6f}, {snap['lon']:.6f}</code>")
    return "\n".join(lines)


async def safe_edit(cb: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup]) -> None:
    if isinstance(cb.message, Message):
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(text, reply_markup=kb)


def cb_message(cb: CallbackQuery) -> Optional[Message]:
    return cb.message if isinstance(cb.message, Message) else None


# ---------- команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME, reply_markup=main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_kb())


@router.message(Command("accounts"))
async def cmd_accounts(message: Message, db: Database) -> None:
    docs = await db.list_accounts()
    if not docs:
        await message.answer("Аккаунтов пока нет.", reply_markup=main_menu_kb())
        return
    await message.answer("📋 Аккаунты Apple ID:", reply_markup=accounts_kb(docs))


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccount.apple_id)
    await message.answer("Введите Apple ID (email):")


# ---------- навигация ----------

@router.callback_query(AccountCB.filter(F.action == "menu"))
async def cb_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(cb, WELCOME, main_menu_kb())
    await cb.answer()


@router.callback_query(AccountCB.filter(F.action == "list"))
async def cb_list(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    docs = await db.list_accounts()
    text = "📋 Аккаунты Apple ID:" if docs else "Аккаунтов пока нет."
    await safe_edit(cb, text, accounts_kb(docs))
    await cb.answer()


@router.callback_query(AccountCB.filter(F.action == "sel"))
async def cb_select(cb: CallbackQuery, callback_data: AccountCB, state: FSMContext, db: Database) -> None:
    await state.clear()
    doc = await db.get_account(callback_data.account_id)
    if doc is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await safe_edit(cb, account_card_text(doc), account_kb(doc))
    await cb.answer()


@router.callback_query(AccountCB.filter(F.action == "act"))
async def cb_toggle_active(cb: CallbackQuery, callback_data: AccountCB, db: Database) -> None:
    doc = await db.get_account(callback_data.account_id)
    if doc is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    new_value = not bool(doc.get("is_active"))
    await db.set_active(callback_data.account_id, new_value)
    doc = await db.get_account(callback_data.account_id)
    await safe_edit(cb, account_card_text(doc), account_kb(doc))
    await cb.answer("Мониторинг включен" if new_value else "Мониторинг остановлен")


@router.callback_query(AccountCB.filter(F.action == "trk"))
async def cb_toggle_tracking(cb: CallbackQuery, callback_data: AccountCB, db: Database) -> None:
    doc = await db.get_account(callback_data.account_id)
    if doc is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    new_value = await db.toggle_tracking(callback_data.account_id)
    doc = await db.get_account(callback_data.account_id)
    await safe_edit(cb, account_card_text(doc), account_kb(doc))
    await cb.answer("Трекинг включен" if new_value else "Трекинг выключен")


@router.callback_query(AccountCB.filter(F.action == "qa"))
async def cb_security_questions(cb: CallbackQuery, callback_data: AccountCB, db: Database) -> None:
    doc = await db.get_account(callback_data.account_id)
    msg = cb_message(cb)
    if doc is None or msg is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    qa = doc.get("security_questions") or {}
    if not qa:
        await cb.answer("Ответы не сохранены", show_alert=True)
        return
    lines = []
    for question, answer in qa.items():
        try:
            plain = decrypt_text(answer)
        except Exception:
            plain = "⚠️ не удалось расшифровать (сменился ENCRYPTION_KEY?)"
        lines.append(f"• {html.escape(question)}: <tg-spoiler>{html.escape(plain)}</tg-spoiler>")
    await msg.answer("❓ Контрольные вопросы (нажмите, чтобы раскрыть):\n" + "\n".join(lines))
    await cb.answer()


@router.callback_query(AccountCB.filter(F.action == "del"))
async def cb_delete(cb: CallbackQuery, callback_data: AccountCB, db: Database) -> None:
    doc = await db.get_account(callback_data.account_id)
    if doc is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, удалить", callback_data=AccountCB(action="dely", account_id=callback_data.account_id).pack())
    b.button(text="⬅️ Нет", callback_data=AccountCB(action="sel", account_id=callback_data.account_id).pack())
    b.adjust(2)
    await safe_edit(cb, f"Удалить <b>{html.escape(doc['apple_id'])}</b>? Сессия и данные будут стерты.", b.as_markup())
    await cb.answer()


@router.callback_query(AccountCB.filter(F.action == "dely"))
async def cb_delete_yes(cb: CallbackQuery, callback_data: AccountCB, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    doc = await db.get_account(callback_data.account_id)
    if doc is not None:
        await manager.forget(callback_data.account_id, doc["apple_id"])
        await db.delete_account(callback_data.account_id)
    await state.clear()
    docs = await db.list_accounts()
    text = "🗑 Аккаунт удален.\n\n📋 Аккаунты Apple ID:" if docs else "🗑 Аккаунт удален. Аккаунтов больше нет."
    await safe_edit(cb, text, accounts_kb(docs))
    await cb.answer("Удалено")


# ---------- добавление аккаунта (FSM) ----------

@router.callback_query(AccountCB.filter(F.action == "add"))
async def cb_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccount.apple_id)
    await safe_edit(cb, "Введите Apple ID (email):", None)
    await cb.answer()


@router.message(AddAccount.apple_id, F.text)
async def add_apple_id(message: Message, state: FSMContext, db: Database) -> None:
    apple_id = message.text.strip().lower()
    if "@" not in apple_id or " " in apple_id:
        await message.answer("Это не похоже на Apple ID. Введите email:")
        return
    if await db.get_by_apple_id(apple_id):
        await state.clear()
        await message.answer("Такой аккаунт уже есть в базе.", reply_markup=main_menu_kb())
        return
    await state.update_data(apple_id=apple_id)
    await state.set_state(AddAccount.password)
    await message.answer("Введите пароль Apple ID (сообщение с паролем будет удалено):")


@router.message(AddAccount.password, F.text)
async def add_password(message: Message, state: FSMContext) -> None:
    with suppress(Exception):
        await message.delete()
    await state.update_data(password=message.text.strip())
    await state.set_state(AddAccount.questions)
    await message.answer(
        "Пароль принят (хранится только в зашифрованном виде Fernet).\n\n"
        "Если у аккаунта есть контрольные вопросы, пришлите их строками «вопрос=ответ».\n"
        "Если их нет — /skip"
    )


@router.message(AddAccount.questions, Command("skip"))
async def add_skip_questions(message: Message, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    await _finalize_add(message, state, db, manager, {})


@router.message(AddAccount.questions, F.text)
async def add_questions(message: Message, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    qa = {}
    for line in message.text.splitlines():
        if "=" in line:
            question, answer = line.split("=", 1)
            question, answer = question.strip(), answer.strip()
            if question and answer:
                qa[question] = answer
    with suppress(Exception):
        await message.delete()
    if not qa:
        await message.answer("Не распознал пары «вопрос=ответ». Повторите или /skip")
        return
    await _finalize_add(message, state, db, manager, qa)


async def _finalize_add(message: Message, state: FSMContext, db: Database, manager: ICloudManager, qa: dict) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        doc = await db.add_account(data["apple_id"], data["password"], qa)
    except DuplicateKeyError:
        await message.answer("Такой аккаунт уже существует.", reply_markup=main_menu_kb())
        return
    await message.answer(f"✅ Аккаунт <b>{html.escape(doc['apple_id'])}</b> добавлен. Пробую войти…")
    await _start_login(message, state, db, manager, doc)


# ---------- вход / 2FA / 2SA ----------

@router.callback_query(AccountCB.filter(F.action == "login"))
async def cb_login(cb: CallbackQuery, callback_data: AccountCB, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    doc = await db.get_account(callback_data.account_id)
    msg = cb_message(cb)
    if doc is None or msg is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await cb.answer("Пробую войти…")
    await _start_login(msg, state, db, manager, doc)


async def _start_login(message: Message, state: FSMContext, db: Database, manager: ICloudManager, doc: dict) -> None:
    account_id = str(doc["_id"])
    try:
        status = await manager.login(doc)
    except AuthError as exc:
        await db.update_fields(account_id, last_error=str(exc))
        await message.answer(f"❌ {html.escape(str(exc))}", reply_markup=account_kb(doc))
        return
    if status == "2fa":
        await state.set_state(Auth2FA.code)
        await state.update_data(account_id=account_id)
        await message.answer("🔐 Apple требует 2FA. Введите 6-значный код с доверенного устройства:")
        return
    if status == "2sa":
        await _send_2sa_devices(message, db, manager, doc)
        return
    fresh = await db.get_account(account_id) or doc
    await message.answer(
        "✅ Вход выполнен, сессия сохранена в MongoDB. Аккаунт активен — воркер начнет мониторинг.",
        reply_markup=account_kb(fresh),
    )


async def _send_2sa_devices(message: Message, db: Database, manager: ICloudManager, doc: dict) -> None:
    account_id = str(doc["_id"])
    try:
        devices = await manager.get_trusted_devices(account_id)
    except (AuthError, PyiCloudException) as exc:
        await message.answer(f"❌ Не удалось получить доверенные устройства: {html.escape(str(exc))}")
        return
    if not devices:
        await message.answer("❌ Apple не вернула доверенных устройств для 2SA.")
        return
    b = InlineKeyboardBuilder()
    for index, device in enumerate(devices):
        label = device.get("deviceName") or f"SMS на {device.get('phoneNumber', '?')}"
        b.button(text=f"📲 {label}", callback_data=TwoSACB(account_id=account_id, index=index).pack())
    b.adjust(1)
    hint = ""
    if doc.get("security_questions"):
        hint = "\n\nℹ️ Ответы на контрольные вопросы сохранены в профиле (кнопка «❓» в карточке аккаунта)."
    await message.answer(
        "🔐 Требуется двухэтапная проверка (2SA). Выберите устройство для отправки кода:" + hint,
        reply_markup=b.as_markup(),
    )


@router.callback_query(TwoSACB.filter())
async def cb_2sa_choice(cb: CallbackQuery, callback_data: TwoSACB, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    doc = await db.get_account(callback_data.account_id)
    msg = cb_message(cb)
    if doc is None or msg is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    try:
        ok = await manager.send_2sa_code(callback_data.account_id, callback_data.index)
    except (AuthError, PyiCloudException) as exc:
        await cb.answer("Ошибка отправки кода", show_alert=True)
        await msg.answer(f"❌ {html.escape(str(exc))}")
        return
    if not ok:
        await cb.answer("Apple не отправила код", show_alert=True)
        return
    await state.set_state(Auth2SA.code)
    await state.update_data(account_id=callback_data.account_id)
    await msg.answer("📨 Код отправлен. Введите полученный код:")
    await cb.answer()


@router.message(Auth2FA.code, F.text.regexp(r"^\d{6}$"))
async def on_2fa_code(message: Message, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    data = await state.get_data()
    account_id = data.get("account_id", "")
    doc = await db.get_account(account_id)
    if doc is None:
        await state.clear()
        await message.answer("Аккаунт не найден.", reply_markup=main_menu_kb())
        return
    with suppress(Exception):
        await message.delete()
    try:
        ok = await manager.validate_2fa(account_id, message.text.strip())
    except (AuthError, PyiCloudException) as exc:
        await state.clear()
        await message.answer(f"❌ {html.escape(str(exc))}", reply_markup=main_menu_kb())
        return
    if not ok:
        await message.answer("❌ Неверный код. Попробуйте еще раз (6 цифр) или /cancel.")
        return
    await state.clear()
    fresh = await db.get_account(account_id) or doc
    await message.answer(
        "✅ 2FA подтверждена, сессия доверенная и сохранена в MongoDB. Мониторинг активен.",
        reply_markup=account_kb(fresh),
    )


@router.message(Auth2FA.code, F.text)
async def on_2fa_bad_format(message: Message) -> None:
    await message.answer("Нужен 6-значный код, например 123456. /cancel — отмена.")


@router.message(Auth2SA.code, F.text.regexp(r"^\d{4,6}$"))
async def on_2sa_code(message: Message, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    data = await state.get_data()
    account_id = data.get("account_id", "")
    doc = await db.get_account(account_id)
    if doc is None:
        await state.clear()
        await message.answer("Аккаунт не найден.", reply_markup=main_menu_kb())
        return
    with suppress(Exception):
        await message.delete()
    try:
        result = await manager.validate_2sa(account_id, message.text.strip())
    except (AuthError, PyiCloudException) as exc:
        await state.clear()
        await message.answer(f"❌ {html.escape(str(exc))}", reply_markup=main_menu_kb())
        return
    if result == "bad_code":
        await message.answer("❌ Неверный код. Попробуйте еще раз или /cancel.")
        return
    if result == "2fa":
        await state.set_state(Auth2FA.code)
        await message.answer("🔐 Apple дополнительно требует 2FA. Введите 6-значный код:")
        return
    await state.clear()
    fresh = await db.get_account(account_id) or doc
    await message.answer(
        "✅ 2SA подтверждена, сессия сохранена в MongoDB. Мониторинг активен.",
        reply_markup=account_kb(fresh),
    )


@router.message(Auth2SA.code, F.text)
async def on_2sa_bad_format(message: Message) -> None:
    await message.answer("Нужен цифровой код из SMS/устройства. /cancel — отмена.")


# ---------- устройства ----------

@router.callback_query(AccountCB.filter(F.action == "dev"))
async def cb_devices(cb: CallbackQuery, callback_data: AccountCB, db: Database, manager: ICloudManager) -> None:
    doc = await db.get_account(callback_data.account_id)
    msg = cb_message(cb)
    if doc is None or msg is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await cb.answer("Запрашиваю устройства…")
    account_id = str(doc["_id"])
    try:
        snapshots = await manager.fetch_devices(doc)
    except ReauthRequired as exc:
        await msg.answer(f"🔐 {html.escape(str(exc))}", reply_markup=account_kb(doc))
        return
    except (AuthError, PyiCloudException) as exc:
        manager.invalidate(account_id)
        await msg.answer(f"❌ Ошибка Find My: {html.escape(str(exc))}")
        return
    if not snapshots:
        await msg.answer("У аккаунта нет устройств в Find My.")
        return
    await msg.answer(f"📱 Устройства <b>{html.escape(doc['apple_id'])}</b> — {len(snapshots)} шт.")
    for snap in snapshots:
        await msg.answer(device_text(snap), reply_markup=device_kb(account_id, snap))


@router.callback_query(DeviceCB.filter(F.action == "loc"))
async def cb_device_location(cb: CallbackQuery, callback_data: DeviceCB, bot: Bot, db: Database, manager: ICloudManager) -> None:
    doc = await db.get_account(callback_data.account_id)
    msg = cb_message(cb)
    if doc is None or msg is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await cb.answer("Запрашиваю геопозицию…")
    try:
        loc = await manager.device_location(doc, callback_data.token)
    except (AuthError, PyiCloudException) as exc:
        manager.invalidate(callback_data.account_id)
        await msg.answer(f"❌ {html.escape(str(exc))}")
        return
    if not loc or loc.get("latitude") is None or loc.get("longitude") is None:
        await msg.answer("📍 Геопозиция сейчас недоступна (устройство офлайн или локация не определена).")
        return
    kwargs = {}
    accuracy = loc.get("horizontalAccuracy")
    if isinstance(accuracy, (int, float)) and 0 < accuracy <= 1500:
        kwargs["horizontal_accuracy"] = float(accuracy)
    await bot.send_location(
        chat_id=msg.chat.id,
        latitude=float(loc["latitude"]),
        longitude=float(loc["longitude"]),
        **kwargs,
    )
    ts = loc.get("timeStamp")
    when = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
        if isinstance(ts, (int, float))
        else "неизвестно"
    )
    await msg.answer(f"🕒 Обновлено: {when}")


@router.callback_query(DeviceCB.filter(F.action == "snd"))
async def cb_device_sound(cb: CallbackQuery, callback_data: DeviceCB, db: Database, manager: ICloudManager) -> None:
    doc = await db.get_account(callback_data.account_id)
    msg = cb_message(cb)
    if doc is None or msg is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    try:
        await manager.play_sound(doc, callback_data.token)
    except (AuthError, PyiCloudException) as exc:
        manager.invalidate(callback_data.account_id)
        await cb.answer("Ошибка", show_alert=True)
        await msg.answer(f"❌ {html.escape(str(exc))}")
        return
    await cb.answer("🔊 Звуковой сигнал отправлен на устройство", show_alert=True)


@router.callback_query(DeviceCB.filter(F.action == "lost"))
async def cb_device_lost(cb: CallbackQuery, callback_data: DeviceCB, state: FSMContext, db: Database) -> None:
    doc = await db.get_account(callback_data.account_id)
    msg = cb_message(cb)
    if doc is None or msg is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await state.set_state(LostMode.phone)
    await state.update_data(account_id=callback_data.account_id, token=callback_data.token)
    await msg.answer("🚨 Режим пропажи.\nШаг 1/2: введите номер телефона для связи (например +79001234567):")
    await cb.answer()


@router.message(LostMode.phone, F.text)
async def lost_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    if not re.fullmatch(r"\+?[\d\s\-()]{5,20}", phone):
        await message.answer("Не похоже на номер телефона. Повторите (например +79001234567) или /cancel:")
        return
    await state.update_data(phone=phone)
    await state.set_state(LostMode.text)
    await message.answer("Шаг 2/2: введите текст сообщения на экране устройства (или /skip для стандартного):")


@router.message(LostMode.text, Command("skip"))
async def lost_skip_text(message: Message, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    await _finish_lost(message, state, db, manager, "This iPhone has been lost. Please call me.")


@router.message(LostMode.text, F.text)
async def lost_text(message: Message, state: FSMContext, db: Database, manager: ICloudManager) -> None:
    await _finish_lost(message, state, db, manager, message.text.strip())


async def _finish_lost(message: Message, state: FSMContext, db: Database, manager: ICloudManager, text: str) -> None:
    data = await state.get_data()
    await state.clear()
    doc = await db.get_account(data.get("account_id", ""))
    if doc is None:
        await message.answer("Аккаунт не найден.", reply_markup=main_menu_kb())
        return
    try:
        await manager.lost_mode(doc, data["token"], data["phone"], text)
    except (AuthError, PyiCloudException) as exc:
        manager.invalidate(str(doc["_id"]))
        await message.answer(f"❌ {html.escape(str(exc))}")
        return
    await message.answer("🚨 Режим пропажи включен: устройство заблокировано, сообщение и номер показаны на экране.")


# ---------- fallback ----------

@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer("Используйте меню ниже или /start.", reply_markup=main_menu_kb())
