import asyncio
import logging
import os
import json
import time
import datetime
import html
import copy
from PIL import Image, ImageDraw, ImageFont
import io
import pymongo  # Добавили импорт pymongo

from aiogram import Bot, Dispatcher, F
from aiogram.methods import DeleteWebhook
from aiogram import types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.dispatcher.middlewares.base import BaseMiddleware

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN is required')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def parse_id_set(value: str) -> set[int]:
    result = set()
    for item in value.replace(';', ',').split(','):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                logging.warning('Некорректный ID в ADMIN_IDS: %s', item)
    return result


ADMIN_IDS = parse_id_set(os.environ.get('ADMIN_IDS', ''))

# Ссылка на мини-приложение (веб-версия LK TEAM)
MINI_APP_URL = os.environ.get('MINI_APP_URL', 'https://l-kbots.vercel.app').strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

IMG_MAIN     = os.path.join(BASE_DIR, 'images', 'menu_main.jpg')
IMG_PROFILE  = os.path.join(BASE_DIR, 'images', 'profile.jpg')
IMG_SETTINGS = os.path.join(BASE_DIR, 'images', 'settings.jpg')
IMG_EDU      = os.path.join(BASE_DIR, 'images', 'education.jpg')
IMG_ABOUT    = os.path.join(BASE_DIR, 'images', 'about.jpg')
IMG_DEY      = os.path.join(BASE_DIR, 'images', 'deystvie.png')

VIDEO_VHOD_3  = os.path.join(BASE_DIR, 'images', 'vhod3.MP4')

# =========================================================
# ПРЕМИУМ ЭМОДЗИ
# =========================================================
E = {
    'star': ('5239767843333105620', '⭐'),
    'fire': ('5314313344111159198', '🔥'),
    'rocket': ('5241883001913699341', '🚀'),
    'money': ('5251013304144858624', '💰'),
    'trophy': ('5226431245918942763', '🏆'),
}

def emo(name: str) -> str:
    if name in E:
        emoji_id, fallback = E[name]
        return f"<tg-emoji emoji-id='{emoji_id}'>{fallback}</tg-emoji>"
    return "❓"

# =========================================================
# FSM СОСТОЯНИЯ
# =========================================================
class AdminStates(StatesGroup):
    wait_base_profits  = State()
    wait_base_cash     = State()
    wait_cash          = State()
    wait_lock          = State()
    wait_ref           = State()
    wait_tag           = State()
    wait_broadcast     = State()

# =========================================================
# БАЗА ДАННЫХ MONGO DB (ВМЕСТО JSON И БЭКАПОВ)
# =========================================================
MONGO_URL = (os.environ.get("MONGO_URL") or "").strip()
MONGO_DB_NAME = "tg_bot_database"

# Единственная production-база. Без URI бот не имеет права запускаться с пустым профилем.
if not MONGO_URL:
    raise RuntimeError("MONGO_URL is required; refusing to start without the production database")

# Страховка от мусора ${{...}} в интерфейсе Railway
if "${{" in MONGO_URL:
    MONGO_URL = MONGO_URL.split("}")[-1].strip()

MONGO_CONNECT_ATTEMPTS = 5


def connect_to_mongo():
    """Connect with bounded retries; never continue with an unavailable database."""
    last_error = None
    for attempt in range(1, MONGO_CONNECT_ATTEMPTS + 1):
        candidate = None
        try:
            candidate = pymongo.MongoClient(
                MONGO_URL,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=10000,
                socketTimeoutMS=20000,
                retryWrites=True,
            )
            candidate.admin.command("ping")
            logging.info("✅ MongoDB connection established on attempt %s", attempt)
            return candidate
        except Exception as exc:
            last_error = exc
            if candidate is not None:
                candidate.close()
            if attempt < MONGO_CONNECT_ATTEMPTS:
                delay = min(2 ** (attempt - 1), 8)
                logging.warning(
                    "MongoDB unavailable (attempt %s/%s); retrying in %ss",
                    attempt,
                    MONGO_CONNECT_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
    raise RuntimeError("MongoDB is unavailable; refusing to start the bot") from last_error


client = connect_to_mongo()
db = client[MONGO_DB_NAME]
users_col = db["users"]
settings_col = db["settings"]
applications_col = db["applications"]
blacklist_col = db["blacklist"]
known_users_col = db["known_users"]
admin_actions_col = db["admin_actions"]

try:
    users_col.create_index('user_id', unique=True)
    users_col.create_index('username_lower', sparse=True)
    known_users_col.create_index('user_id', unique=True)
    known_users_col.create_index('username_lower')
    admin_actions_col.create_index([('chat_id', 1), ('created_at', -1)])
except Exception as e:
    logging.warning('Не удалось создать индексы MongoDB: %s', e)

global_settings = {
    'base_profits': 0,
    'base_cash': 0.0,
    'mentor_link': 'https://t.me/+Dw1yqXJJU1FmNmNi'
}

def load_db():
    """Load production data with retries; an empty fallback is forbidden."""
    global global_settings
    last_error = None
    for attempt in range(1, MONGO_CONNECT_ATTEMPTS + 1):
        try:
            loaded_settings = copy.deepcopy(global_settings)
            db_settings = settings_col.find_one({"type": "global_settings"})
            if db_settings:
                db_settings.pop("_id", None)
                db_settings.pop("type", None)
                loaded_settings.update(db_settings)

            loaded_users = {}
            for user in users_col.find():
                user_id = int(user["user_id"])
                user.pop("_id", None)
                user.pop("user_id", None)
                loaded_users[user_id] = user

            existing_collections = db.list_collection_names()
            app_count = applications_col.count_documents({}) if "applications" in existing_collections else 0
            ban_count = blacklist_col.count_documents({}) if "blacklist" in existing_collections else 0
            global_settings = loaded_settings
            logging.info(
                "✅ Production MongoDB loaded: %s users, %s applications, %s blacklisted",
                len(loaded_users),
                app_count,
                ban_count,
            )
            return loaded_users
        except Exception as exc:
            last_error = exc
            if attempt < MONGO_CONNECT_ATTEMPTS:
                delay = min(2 ** (attempt - 1), 8)
                logging.warning(
                    "MongoDB load failed (attempt %s/%s); retrying in %ss: %s",
                    attempt,
                    MONGO_CONNECT_ATTEMPTS,
                    delay,
                    exc,
                )
                time.sleep(delay)

    logging.critical("🛑 Production MongoDB could not be loaded after retries: %s", last_error)
    raise RuntimeError("MongoDB load failed; refusing to start with empty user data") from last_error

import concurrent.futures
# Одна очередь сохраняет снимки строго по порядку: старые записи больше не могут
# завершиться после новых и вернуть пользователю устаревшие данные.
_db_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dbsave")

def _execute_write(description, operation, attempts=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                delay = min(2 ** (attempt - 1), 4)
                logging.warning(
                    "%s failed (attempt %s/%s); retrying in %ss: %s",
                    description,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
    raise RuntimeError(f"{description} failed after retries") from last_error


def _save_db_sync(settings_payload, user_updates):
    """Persist only explicit fields; never write a full stale user snapshot."""
    try:
        if settings_payload:
            _execute_write(
                "settings update",
                lambda: settings_col.update_one(
                    {"type": "global_settings"},
                    {"$set": settings_payload},
                    upsert=True,
                ),
            )

        for user_id, fields in user_updates.items():
            if not fields:
                continue
            result = _execute_write(
                f"user {user_id} update",
                lambda user_id=user_id, fields=fields: users_col.update_one(
                    {"user_id": int(user_id)},
                    {"$set": fields},
                    upsert=False,
                ),
            )
            if result.matched_count != 1:
                raise RuntimeError(f"User {user_id} disappeared before save")
        logging.info("✅ MongoDB fields saved: %s user(s)", len(user_updates))
    except Exception:
        logging.exception("⚠️ Failed to persist MongoDB changes")

def save_db(user_fields=None, settings_fields=None):
    """Queue a deep-copied field-level update in strict order.

    user_fields maps user_id to an iterable of field names. Requiring explicit
    names prevents bot cache snapshots from overwriting fresh Mini App edits.
    """
    user_fields = user_fields or {}
    settings_fields = set(settings_fields or ())
    settings_payload = {
        field: copy.deepcopy(global_settings[field])
        for field in settings_fields
        if field in global_settings
    }
    user_updates = {}
    for raw_user_id, field_names in user_fields.items():
        user_id = int(raw_user_id)
        cached = user_data.get(user_id)
        if not isinstance(cached, dict):
            raise RuntimeError(f"Cannot save unknown user {user_id}")
        payload = {
            field: copy.deepcopy(cached[field])
            for field in set(field_names)
            if field in cached
        }
        if payload:
            user_updates[user_id] = payload

    if settings_payload or user_updates:
        _db_executor.submit(_save_db_sync, settings_payload, user_updates)

user_data = load_db()
applications = {}
chat_message_log = {}
_known_user_cache = {}

# =========================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С ЧЕРНЫМ СПИСКОМ
# =========================================================
# Кэш чёрного списка в памяти — раньше is_blacklisted() дергал MongoDB
# на КАЖДОЕ сообщение/нажатие кнопки (через init_user), что и давало
# основную часть задержки. Теперь база читается один раз при старте,
# а дальше только сверяемся с множеством в памяти.
try:
    blacklisted_ids = {doc["user_id"] for doc in blacklist_col.find({}, {"user_id": 1})}
    logging.info(f"🚫 Загружено {len(blacklisted_ids)} ID в кэш blacklist.")
except Exception as e:
    blacklisted_ids = set()
    logging.error(f"⚠️ Не удалось загрузить blacklist в кэш: {e}")

def is_blacklisted(user_id: int) -> bool:
    """Проверяет находится ли пользователь в blacklist (без похода в базу)."""
    return user_id in blacklisted_ids

def add_to_blacklist(user_id: int, reason: str = "Забан админом"):
    """Добавляет пользователя в blacklist и удаляет его данные"""
    try:
        blacklisted_ids.add(user_id)  # сразу обновляем кэш в памяти

        # Добавляем в blacklist
        blacklist_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id,
                "banned_at": datetime.datetime.now().isoformat(),
                "reason": reason
            }},
            upsert=True
        )
        
        # Удаляем из users
        users_col.delete_one({"user_id": user_id})
        if user_id in user_data:
            del user_data[user_id]
        
        # Удаляем заявку
        applications_col.delete_one({"user_id": user_id})
        if user_id in applications:
            del applications[user_id]
        
        logging.info(f"🚫 Пользователь {user_id} добавлен в blacklist")
        return True
    except Exception as e:
        logging.error(f"⚠️ Ошибка при добавлении в blacklist: {e}")
        return False


RANKS_CONFIG = {
    'student':  {'name': 'Студент',               'price': 0,     'emoji_id': '5348215481683688355'},
    'champion': {'name': 'Чемпион',               'price': 1000,  'emoji_id': '5345892905103932200'},
    'wolf':     {'name': 'Волк с Уолл-Стрит',    'price': 3000,  'emoji_id': '5323559637805443196'},
    'legend':   {'name': 'Легенда',               'price': 5000,  'emoji_id': '5400077452301056814'},
    'boss':     {'name': 'Босс Верховного стола', 'price': 10000, 'emoji_id': '5348306023889254367'},
}

# Миграция старых рангов
rank_migration = {
    '🎓 Студент': 'Студент',
    '🏆 Чемпион': 'Чемпион',
    '🐺 Волк с Уолл-Стрит': 'Волк с Уолл-Стрит',
    '🌌 Легенда': 'Легенда',
    '👑 Босс Верховного стола': 'Босс Верховного стола',
}
migrated_user_ids = []
for uid, info in user_data.items():
    old_rank = info.get('rank', '')
    if old_rank in rank_migration:
        info['rank'] = rank_migration[old_rank]
        migrated_user_ids.append(uid)
if migrated_user_ids:
    save_db(user_fields={uid: {"rank"} for uid in migrated_user_ids})

# =========================================================
# РЕФЕРАЛЬНЫЕ ПРОЦЕНТЫ
# =========================================================
def get_ref_percent(referrer_id: int) -> float:
    count = user_data.get(referrer_id, {}).get('ref_count_l1', 0)
    if count >= 10: return 0.15
    elif count >= 5: return 0.10
    else: return 0.05

async def remember_user(user: types.User, chat_id: int | None = None):
    """Keeps a safe username -> Telegram ID cache for group-scoped commands."""
    if not user:
        return
    payload = {
        'user_id': user.id,
        'username': user.username,
        'username_lower': user.username.lower() if user.username else None,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'is_bot': user.is_bot,
        'updated_at': datetime.datetime.now(datetime.timezone.utc),
    }
    normalized_username = payload['username_lower']
    if user.id in user_data:
        cached = user_data[user.id]
        if cached.get('username_lower') != normalized_username:
            cached['username'] = user.username
            cached['username_lower'] = normalized_username
            save_db(user_fields={user.id: {"username", "username_lower"}})

    cache_key = (user.id, chat_id)
    signature = (user.username, user.first_name, user.last_name, user.is_bot)
    if _known_user_cache.get(cache_key) == signature:
        return

    update = {'$set': payload}
    if chat_id is not None:
        update['$addToSet'] = {'chat_ids': chat_id}
    try:
        await asyncio.to_thread(known_users_col.update_one, {'user_id': user.id}, update, upsert=True)
        _known_user_cache[cache_key] = signature
    except Exception as e:
        logging.warning('Не удалось обновить кэш пользователя %s: %s', user.id, e)


async def resolve_worker_id(username: str):
    """Находит воркера локально или одним индексированным запросом вместо N Telegram API вызовов."""
    normalized = (username or '').lstrip('@').strip().lower()
    if not normalized:
        return None

    for uid, data in user_data.items():
        stored = data.get('username_lower')
        if not stored and data.get('username'):
            stored = str(data['username']).lower()
        if stored == normalized:
            return int(uid)

    try:
        record = await asyncio.to_thread(
            known_users_col.find_one,
            {'username_lower': normalized},
            {'user_id': 1, 'username': 1},
        )
        if record:
            uid = int(record['user_id'])
            if uid in user_data:
                user_data[uid]['username'] = record.get('username') or normalized
                user_data[uid]['username_lower'] = normalized
                return uid

        record = await asyncio.to_thread(
            users_col.find_one,
            {'username_lower': normalized},
            {'user_id': 1},
        )
        if record and int(record['user_id']) in user_data:
            return int(record['user_id'])
    except Exception as e:
        logging.warning('Не удалось найти @%s в кэше пользователей: %s', normalized, e)
    return None


async def get_worker_card_identity(user_id: int, username_fallback: str):
    """Берёт тег и ранг прямо из MongoDB, чтобы карточка не использовала устаревший кэш."""
    data = user_data.get(user_id, {})
    fresh = None
    try:
        fresh = await asyncio.to_thread(
            users_col.find_one,
            {'user_id': int(user_id)},
            {'tag': 1, 'rank': 1},
        )
    except Exception as e:
        logging.warning('Не удалось обновить тег/ранг пользователя %s: %s', user_id, e)

    if fresh:
        for field in ('tag', 'rank'):
            value = fresh.get(field)
            if value not in (None, ''):
                data[field] = value

    tag = str(data.get('tag') or username_fallback).strip() or username_fallback
    rank = str(data.get('rank') or 'Студент').strip() or 'Студент'
    return rank, tag


def init_user(chat_id: int, first_name: str, username: str | None = None):
    """Return the canonical MongoDB profile without ever resetting an existing user."""
    if is_blacklisted(chat_id):
        return {
            'name': first_name,
            'cash': 0.0,
            'status_approved': False,
            'blocked': True,
        }

    if chat_id in user_data:
        data = user_data[chat_id]
        identity_update = {}
        if data.get('name') != first_name:
            data['name'] = first_name
            identity_update['name'] = first_name
        if username and data.get('username') != username:
            data['username'] = username
            data['username_lower'] = username.lower()
            identity_update['username'] = username
            identity_update['username_lower'] = username.lower()
        if identity_update:
            users_col.update_one({'user_id': int(chat_id)}, {'$set': identity_update}, upsert=False)
        return data

    # A historical approved application restores access, but never overwrites money,
    # locks, histories, rank, tag, referrals, or any other existing profile field.
    approved_before = applications_col.find_one(
        {'user_id': int(chat_id), 'status': 'approved'},
        {'_id': 1},
    ) is not None
    defaults = {
        'name': first_name,
        'username': username,
        'username_lower': username.lower() if username else None,
        'cash': 0.0,
        'profits': 0,
        'profit_history': [],
        'locks': 0,
        'lock_history': [],
        'tag': 'NOTSCAM',
        'rank': RANKS_CONFIG['student']['name'],
        'joined_at': time.time(),
        'status_approved': (chat_id in ADMIN_IDS) or approved_before,
        'blocked': False,
        'referrer_l1': None,
        'referrer_l2': None,
        'referrer_l3': None,
        'ref_count_l1': 0, 'ref_money_l1': 0.0,
        'ref_count_l2': 0, 'ref_money_l2': 0.0,
        'ref_count_l3': 0, 'ref_money_l3': 0.0,
    }
    identity = {
        'name': first_name,
        'username': username,
        'username_lower': username.lower() if username else None,
    }
    try:
        document = users_col.find_one_and_update(
            {'user_id': int(chat_id)},
            {'$setOnInsert': defaults, '$set': identity},
            upsert=True,
            return_document=pymongo.ReturnDocument.AFTER,
        )
    except Exception as e:
        logging.exception("Не удалось загрузить или создать профиль %s", chat_id)
        raise RuntimeError("MongoDB profile operation failed") from e

    if not document:
        raise RuntimeError(f"MongoDB did not return profile {chat_id}")
    document.pop('_id', None)
    document.pop('user_id', None)
    user_data[chat_id] = document
    return document

def register_referral(new_user_id: int, referrer_id: int):
    """Привязывает реферальную цепочку к новому пользователю."""
    if new_user_id == referrer_id:
        return
    if referrer_id not in user_data:
        return

    data = user_data[new_user_id]

    # L1 — прямой реферер
    data['referrer_l1'] = referrer_id
    user_data[referrer_id]['ref_count_l1'] += 1

    # L2 — реферер реферера
    l2 = user_data[referrer_id].get('referrer_l1')
    if l2 and l2 in user_data:
        data['referrer_l2'] = l2
        user_data[l2]['ref_count_l2'] += 1

        # L3 — реферер реферера реферера
        l3 = user_data[l2].get('referrer_l1')
        if l3 and l3 in user_data:
            data['referrer_l3'] = l3
            user_data[l3]['ref_count_l3'] += 1

    changed_fields = {
        new_user_id: {"referrer_l1", "referrer_l2", "referrer_l3"},
        referrer_id: {"ref_count_l1"},
    }
    if data.get("referrer_l2") in user_data:
        changed_fields[data["referrer_l2"]] = {"ref_count_l2"}
    if data.get("referrer_l3") in user_data:
        changed_fields[data["referrer_l3"]] = {"ref_count_l3"}
    save_db(user_fields=changed_fields)

def accrue_ref_bonuses(worker_id: int, amount: float):
    data = user_data.get(worker_id, {})
    l1 = data.get('referrer_l1')
    if l1 and l1 in user_data:
        percent = get_ref_percent(l1)
        bonus = round(amount * percent, 2)
        user_data[l1]['ref_money_l1'] = round(user_data[l1].get('ref_money_l1', 0.0) + bonus, 2)
        save_db(user_fields={l1: {"ref_money_l1"}})

# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================
def get_status(profits):
    if profits == 0: return "Новичок 💤"
    elif 1 <= profits <= 5: return "Воркер 🛠"
    elif 6 <= profits <= 15: return "Профи 🔥"
    else: return "Топ-Воркер 👑"

def get_top_place(chat_id):
    sorted_users = sorted(user_data.items(), key=lambda x: x[1]['cash'], reverse=True)
    for index, (uid, _) in enumerate(sorted_users):
        if uid == chat_id:
            return index + 1
    return "N/A"

def get_time_in_bot(chat_id):
    delta_seconds = time.time() - user_data[chat_id]['joined_at']
    days = int(delta_seconds // 86400)
    if days % 10 == 1 and days % 100 != 11: return f"{days} день"
    elif 2 <= days % 10 <= 4 and (days % 100 < 10 or days % 100 >= 20): return f"{days} дня"
    else: return f"{days} дней"

def is_group(message: types.Message):
    return message.chat.type in ['group', 'supergroup']

async def is_tg_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False


async def is_group_owner_or_project_admin(chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status == 'creator'
    except Exception:
        return False


async def resolve_group_target(message: types.Message):
    """Resolve a target by reply, numeric ID or a username already seen by the bot."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2:
        return None
    value = parts[1].strip().split()[0]

    if value.lstrip('-').isdigit():
        target_id = int(value)
    else:
        username = value.lstrip('@').lower()
        record = await asyncio.to_thread(
            known_users_col.find_one,
            {'username_lower': username, 'chat_ids': message.chat.id},
        )
        if not record:
            return None
        target_id = int(record['user_id'])

    try:
        member = await bot.get_chat_member(message.chat.id, target_id)
        return member.user
    except Exception:
        return None


def audit_admin_action(chat_id: int, actor_id: int, target_id: int, action: str, success: bool, error: str | None = None):
    try:
        payload = {
            'chat_id': chat_id,
            'actor_id': actor_id,
            'target_id': target_id,
            'action': action,
            'success': success,
            'error': error,
            'created_at': datetime.datetime.now(datetime.timezone.utc),
        }
        _db_executor.submit(admin_actions_col.insert_one, payload)
    except Exception as e:
        logging.warning('Не удалось записать действие администратора: %s', e)


class UserTrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message) and event.from_user:
            group_id = event.chat.id if is_group(event) else None
            await remember_user(event.from_user, group_id)
        return await handler(event, data)


dp.message.outer_middleware(UserTrackingMiddleware())

# =========================================================
# КЛАВИАТУРЫ
# =========================================================
def get_main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='👤 Мой профиль',      callback_data='menu_profile',  icon_custom_emoji_id='5974226780059756316'),
        types.InlineKeyboardButton(text=' Настройки',        callback_data='menu_settings', icon_custom_emoji_id='5771449289972650710'),
    )
    builder.row(
        types.InlineKeyboardButton(text=' Система обучения', callback_data='menu_edu',      icon_custom_emoji_id='6028435952299413210'),
        types.InlineKeyboardButton(text=' О проекте',        callback_data='menu_about',    icon_custom_emoji_id='5334544901428229844'),
    )
    builder.row(
        types.InlineKeyboardButton(text='🚀 Открыть в приложении', web_app=types.WebAppInfo(url=MINI_APP_URL))
    )
    return builder.as_markup()

def get_profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='Мои профиты', callback_data='my_profits', icon_custom_emoji_id='5417924076503062111'),
        types.InlineKeyboardButton(text='Мои локи',    callback_data='my_locks', icon_custom_emoji_id='5289722755871162900'),
    )
    builder.row(types.InlineKeyboardButton(text='Реф система', callback_data='ref_system', icon_custom_emoji_id='5298877105000439431'))
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_main', icon_custom_emoji_id='5352759161945867747'))
    return builder.as_markup()

def get_back_main():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_main', icon_custom_emoji_id='5352759161945867747'))
    return builder.as_markup()

def get_back_profile():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_profile', icon_custom_emoji_id='5352759161945867747'))
    return builder.as_markup()

def get_back_settings():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_settings', icon_custom_emoji_id='5352759161945867747'))
    return builder.as_markup()

def get_cancel_markup():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_cancel_step'))
    return builder.as_markup()

async def update_menu_screen(call, img_path, text, keyboard):
    try:
        with open(img_path, 'rb') as photo:
            await call.message.edit_media(
                media=types.InputMediaPhoto(
                    media=types.BufferedInputFile(photo.read(), filename='img.jpg'),
                    caption=text,
                    parse_mode="HTML"
                ),
                reply_markup=keyboard
            )
    except FileNotFoundError:
        await call.message.edit_caption(caption=text, reply_markup=keyboard, parse_mode="HTML")

# =========================================================
# JOIN HANDLER
# =========================================================
@dp.my_chat_member()
async def on_bot_join(update: types.ChatMemberUpdated):
    if update.new_chat_member.status not in ['member', 'administrator', 'creator']:
        return
    if update.chat.type in ['group', 'supergroup']:
        from_user = update.from_user
        await remember_user(from_user, update.chat.id)
        if not await is_group_owner_or_project_admin(update.chat.id, update.from_user.id):
            try:
                group_name = update.chat.title or f"Группа {update.chat.id}"
                group_username = update.chat.username
                
                # Формируем ссылку на группу
                if group_username:
                    group_link = f"https://t.me/{group_username}"
                else:
                    # Для приватных групп используем ссылку по ID
                    group_link = f"https://t.me/c/{str(update.chat.id).replace('-100', '')}"
                
                # Формируем username пользователя с @
                user_mention = f"@{from_user.username}" if from_user.username else "нет username"
                
                # Создаём сообщение с информацией
                notification_text = (
                    f"⚠️ <b>ПОПЫТКА ДОБАВЛЕНИЯ БОТА!</b>\n\n"
                    f"👤 <b>Пользователь:</b> {from_user.first_name}\n"
                    f"👉 <b>Username:</b> {user_mention}\n"
                    f"🆔 <b>ID:</b> <code>{from_user.id}</code>\n\n"
                    f"📍 <b>Группа:</b> {group_name}\n"
                    f"🔗 <b>Ссылка:</b> <a href='{group_link}'>Перейти в группу</a>\n"
                    f"📌 <b>ID группы:</b> <code>{update.chat.id}</code>\n\n"
                    f"✅ Бот автоматически покинул группу"
                )
                
                try:
                    await bot.leave_chat(update.chat.id)
                except Exception as migrate_error:
                    # Игнорируем ошибку миграции группы
                    logging.warning(f"⚠️ Ошибка при выходе (может быть миграция группы): {migrate_error}")
                
                # Отправляем уведомление всем админам
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, notification_text, parse_mode="HTML")
                    except Exception as e:
                        logging.error(f"⚠️ Не удалось отправить уведомление админу {admin_id}: {e}")
                
                logging.warning(f"🚫 Бот покинул группу {update.chat.id} - добавил неавторизованный пользователь {update.from_user.id}")
            except Exception as e:
                logging.error(f"⚠️ Ошибка при обработке события присоединения к группе: {e}")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await remember_user(message.from_user)
    args = message.text.split(maxsplit=1)
    ref_id = None
    if len(args) > 1:
        try:
            ref_id = int(args[1].removeprefix('ref_'))
        except ValueError:
            pass

    data = init_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    
    # Проверяем blacklist
    if data.get('blocked'):
        await message.answer("⛔ <b>Вы заблокированы в этом боте.</b>\n\nПопытка обхода черного списка будет записана.", parse_mode="HTML")
        logging.warning(f"🚫 Попытка входа заблокированного пользователя: {message.chat.id}")
        return

    # Привязываем реферала только если пользователь новый и реферер существует
    if ref_id and ref_id in user_data and data.get('referrer_l1') is None and ref_id != message.chat.id:
        register_referral(message.chat.id, ref_id)

    if not data['status_approved']:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text='📝 Подать заявку', callback_data='apply_start'))
        await message.answer(
            "🔒 <b>Доступ ограничен.</b>\nЧтобы войти в бота LK TEAM, вам необходимо подать заявку на рассмотрение администрации.",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        return

    try:
        with open(IMG_MAIN, 'rb') as photo:
            await message.answer_photo(
                photo=types.BufferedInputFile(photo.read(), filename='img.jpg'),
                caption="<b>Добро пожаловать в главное меню!</b> Выберите интересующий раздел:",
                reply_markup=get_main_menu_keyboard(),
                parse_mode="HTML"
            )
    except FileNotFoundError:
        await message.answer(
            "<b>Добро пожаловать в главное меню!</b> Выберите интересующий раздел:",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )

# =========================================================
# /bc — РАССЫЛКА
# =========================================================
@dp.message(Command("bc"))
async def cmd_broadcast(message: types.Message):
    if message.chat.type != 'private': return
    if message.from_user.id not in ADMIN_IDS: return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Укажи текст рассылки.\nПример: /bc Привет всем воркерам!")
        return

    broadcast_text = parts[1]
    targets = [uid for uid, info in user_data.items() if info.get('status_approved') and uid != message.from_user.id]

    if not targets:
        await message.reply("📭 Нет пользователей для рассылки.")
        return

    sent = 0
    failed = 0
    for uid in targets:
        try:
            await bot.send_message(uid, f"📢 <b>Сообщение от администрации LK TEAM:</b>\n\n{broadcast_text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n📨 Отправлено: <b>{sent}</b>\n❌ Не доставлено: <b>{failed}</b>",
        parse_mode="HTML"
    )

# =========================================================
# /admin
# =========================================================
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='👥 Список пользователей', callback_data='admin_list_users'))
    builder.row(types.InlineKeyboardButton(text='📩 Список заявок',        callback_data='admin_list_apps'))
    builder.row(types.InlineKeyboardButton(text='⚙️ Настройки проекта',   callback_data='admin_project_settings'))
    await message.answer("⚙️ <b>Панель администратора LK TEAM</b>\nВыберите действие:", reply_markup=builder.as_markup(), parse_mode="HTML")


# =========================================================
# УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ КОНКРЕТНОЙ ГРУППЫ
# =========================================================
@dp.message(Command("setadmin"))
async def cmd_setadmin(message: types.Message):
    if not is_group(message):
        await message.reply("⛔ Команда работает только в группе.")
        return
    await remember_user(message.from_user, message.chat.id)

    if not await is_group_owner_or_project_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Выдать администратора может только владелец группы.")
        return

    target = await resolve_group_target(message)
    if not target:
        await message.reply(
            "❌ Пользователь не найден в этой группе.\n\n"
            "Использование: <code>/setadmin @username</code> или ответьте командой на его сообщение.",
            parse_mode="HTML",
        )
        return
    if target.is_bot or target.id == message.from_user.id:
        await message.reply("⛔ Нельзя назначить выбранного пользователя.")
        return

    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(message.chat.id, me.id)
        if bot_member.status != 'administrator' or not getattr(bot_member, 'can_promote_members', False):
            await message.reply(
                "⚠️ Сначала выдайте боту право <b>«Добавление администраторов»</b>.",
                parse_mode="HTML",
            )
            return

        await bot.promote_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            is_anonymous=False,
            can_manage_chat=True,
            can_delete_messages=bool(getattr(bot_member, 'can_delete_messages', False)),
            can_manage_video_chats=bool(getattr(bot_member, 'can_manage_video_chats', False)),
            can_restrict_members=bool(getattr(bot_member, 'can_restrict_members', False)),
            can_promote_members=bool(getattr(bot_member, 'can_promote_members', False)),
            can_change_info=bool(getattr(bot_member, 'can_change_info', False)),
            can_invite_users=bool(getattr(bot_member, 'can_invite_users', False)),
            can_post_stories=bool(getattr(bot_member, 'can_post_stories', False)),
            can_edit_stories=bool(getattr(bot_member, 'can_edit_stories', False)),
            can_delete_stories=bool(getattr(bot_member, 'can_delete_stories', False)),
            can_pin_messages=bool(getattr(bot_member, 'can_pin_messages', False)),
            can_manage_topics=bool(getattr(bot_member, 'can_manage_topics', False)),
            can_manage_tags=bool(getattr(bot_member, 'can_manage_tags', False)),
            can_send_welcome_messages=bool(getattr(bot_member, 'can_send_welcome_messages', False)),
        )
        try:
            await bot.set_chat_administrator_custom_title(message.chat.id, target.id, "LK Admin")
        except Exception:
            pass

        await remember_user(target, message.chat.id)
        audit_admin_action(message.chat.id, message.from_user.id, target.id, 'promote', True)
        mention = f"@{html.escape(target.username)}" if target.username else html.escape(target.full_name)
        await message.reply(
            f"✅ <b>{mention} назначен администратором</b>\n\n"
            f"Права действуют только в группе «{html.escape(message.chat.title or 'Без названия')}».\n"
            "Выданы все права, которыми располагает бот.",
            parse_mode="HTML",
        )
    except Exception as e:
        audit_admin_action(message.chat.id, message.from_user.id, target.id, 'promote', False, str(e))
        logging.exception("Не удалось назначить администратора")
        await message.reply("⚠️ Telegram не разрешил назначение. Проверьте права бота и пользователя.")


@dp.message(Command("deladmin"))
async def cmd_deladmin(message: types.Message):
    if not is_group(message):
        await message.reply("⛔ Команда работает только в группе.")
        return
    if not await is_group_owner_or_project_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Снять администратора может только владелец группы.")
        return

    target = await resolve_group_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение администратора или укажите известный @username.")
        return

    try:
        member = await bot.get_chat_member(message.chat.id, target.id)
        if member.status == 'creator':
            await message.reply("⛔ Нельзя снять права у владельца группы.")
            return
        await bot.promote_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            is_anonymous=False,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
            can_pin_messages=False,
            can_manage_topics=False,
            can_manage_tags=False,
            can_send_welcome_messages=False,
        )
        audit_admin_action(message.chat.id, message.from_user.id, target.id, 'demote', True)
        mention = f"@{html.escape(target.username)}" if target.username else html.escape(target.full_name)
        await message.reply(f"✅ Права администратора сняты у <b>{mention}</b> в этой группе.", parse_mode="HTML")
    except Exception as e:
        audit_admin_action(message.chat.id, message.from_user.id, target.id, 'demote', False, str(e))
        logging.exception("Не удалось снять администратора")
        await message.reply("⚠️ Не удалось снять права. Проверьте, что этого администратора назначил бот.")


@dp.message(Command("admins"))
async def cmd_admins(message: types.Message):
    if not is_group(message):
        return
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        lines = []
        for member in admins:
            user = member.user
            label = f"@{html.escape(user.username)}" if user.username else html.escape(user.full_name)
            role = "владелец" if member.status == 'creator' else "администратор"
            lines.append(f"• {label} — {role}")
        await message.reply("<b>Администрация группы</b>\n\n" + "\n".join(lines), parse_mode="HTML")
    except Exception:
        await message.reply("⚠️ Не удалось получить список администраторов.")

# =========================================================
# /mamont
# =========================================================
@dp.message(Command("mamont"))
async def cmd_mamont(message: types.Message):
    try:
        if os.path.exists(VIDEO_VHOD_3):
            await message.answer_video(
                video=types.FSInputFile(VIDEO_VHOD_3)
            )
        else:
            await message.answer("⚠️ Видео не найдено на сервере.")
    except Exception as e:
        await message.answer(f'Ошибка: {e}')

# =========================================================
# /price — ПРАЙС ЛИСТ IPHONE
# =========================================================
@dp.message(Command("price"))
async def cmd_price(message: types.Message):
    if not is_group(message): return
    price_text = (
        "👑 iPHONE ПРАЙС 👑\n\n"
        "17 Pro Max — 50 000₽\n17 Pro — 45 000₽\n17 — 45 000₽\n17 Air — 37 000₽\n"
        "16 Pro Max — 29 000₽\n16 Pro — 20 000₽\n16 Plus — 15 000₽\n16 — 15 000₽\n"
        "15 Pro Max — 19 000₽\n15 Pro — 14 000₽\n15 Plus — 10 000₽\n15 — 10 000₽\n"
        "14 Pro Max — 10 000₽\n14 Pro — 9 000₽\n14 Plus — 8 000₽\n14 — 8 000₽\n"
        "13 Pro Max — 9 000₽\n13 Pro — 8 000₽\n13 — 6 000₽\n13 mini — 4 000₽\n"
        "12 Pro Max — 6 000₽\n12 Pro — 5 000₽\n12 — 3 000₽\n12 mini — 2 000₽\n"
        "11 Pro Max — 4 000₽\n11 Pro — 3 500₽\n11 — 3 000₽\nXS / XR / X — 500₽"
    )
    await message.reply(f"<tg-spoiler>{price_text}</tg-spoiler>", parse_mode="HTML")

@dp.message(Command("pasta"))
async def cmd_pasta(message: types.Message):
    if not is_group(message): return
    pasta_text = (
        "<b><tg-emoji emoji-id='5220214598585568818'>⚠️</tg-emoji> Ваш Телефон под угрозой<tg-emoji emoji-id='5220214598585568818'>⚠️</tg-emoji>\n"
        "Это устройство теперь полностью под моим контролем. Ты даже не представляешь, с кем имеешь дело. "
        "Я обладаю полным доступом к устройству и могу:\n"
        "1. Немедленно удалить все данные, с невозможностью их восстановления.\n"
        "2. Установить такой пароль, который невозможно будет взломать.\n"
        "3. Заблокировать телефон до состояния бесполезного устройства.\n\n"
        "Сейчас ты не сможешь скачать, установить или восстановить ничего.\n\n"
        "У тебя есть два выбора:\n\n"
        "Попрощаться с телефоном, так как он тебе больше не пригодится.\n\n"
        "Заплатить, чтобы я снял блокировку.\n\n"
        "Не забывай: я знаю, где ты находишься. Один неверный шаг — и твой телефон навсегда потеряет всякую ценность.\n\n"
        "Решай — платишь или прощаешься с устройством.\n"
        "Для связи пиши админу @iCloud_BAN</b>"
    )
    await message.reply(f"{pasta_text}", parse_mode="HTML")

@dp.message(Command("rules"))
async def cmd_rules(message: types.Message):
    if not is_group(message): return
    rules_text = (
        "<b>LK TEAM — Порядок и усилия</b>\n\n⚖️ <b>Правила нашего проекта:</b>\n"
        "<blockquote>Запрещено:\n1. Пытаться обмануть администрацию в разных аспектах\n"
        "2. Неадекватное поведение\n3. Реклама сторонних проектов/услуг\n4. Попрошайничество\n"
        "5. Распространение запрещённых материалов\n6. Дезинформация о проектах\n"
        "7. Отправка шокирующего контента\n8. Необходимо находится в чате для выплаты\n"
        "9. В чате запрещены темы о политике\n10. Воркеру который заблокирован, профит не выплачивается\n"
        "11. Продажа каких либо услуг/товаров без согласования\n12. Оскорблять администрацию в любом виде\n"
        "13. Коммерция и предоставление любых типов услуг запрещена\n14. Репорт багов обязателен.</blockquote>"
    )
    await message.reply(rules_text, parse_mode="HTML")

# =========================================================
# /mp — ПРОФИЛЬ ВОРКЕРА
# =========================================================
@dp.message(Command("mp"))
async def cmd_mp(message: types.Message):
    if not is_group(message): return
    uid = message.from_user.id
    data = init_user(uid, message.from_user.first_name)
    if not data.get('status_approved'):
        await message.reply("🔒 У вас нет доступа к боту.")
        return
    joined_date = datetime.datetime.fromtimestamp(data.get('joined_at', time.time())).strftime("%d.%m.%Y")
    username_str = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    text = (
        f"<tg-emoji emoji-id='5275979556308674886'>👤</tg-emoji> <b>Профиль воркера</b>\n\n"
        f"<tg-emoji emoji-id='5411554197621463099'>📛</tg-emoji> Имя: {message.from_user.first_name}\n"
        f"<tg-emoji emoji-id='5014902839575577394'>🆔</tg-emoji> Username: {username_str}\n"
        f"<tg-emoji emoji-id='5258476306152038031'>🔒</tg-emoji> Локов: {data.get('locks', 0)}\n"
        f"<tg-emoji emoji-id='5224257782013769471'>💰</tg-emoji> Заработано: {data.get('cash', 0.0):g}$\n"
        f"<tg-emoji emoji-id='5413879192267805083'>📅</tg-emoji> В команде с: {joined_date}"
    )
    await message.reply(text, parse_mode="HTML")

# =========================================================
# КОМАНДЫ ДЛЯ ГРУПП
# =========================================================
@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    if not is_group(message): return
    sorted_users = sorted(
        [(uid, info) for uid, info in user_data.items() if info.get('status_approved')],
        key=lambda x: x[1].get('locks', 0), reverse=True
    )
    if not sorted_users:
        await message.reply("📭 Список пуст.")
        return
    medals = {
        1: "<tg-emoji emoji-id='5244961448525848230'>🥇</tg-emoji>",
        2: "<tg-emoji emoji-id='5242293676834579345'>🥈</tg-emoji>",
        3: "<tg-emoji emoji-id='5242652525647127686'>🥉</tg-emoji>",
        4: "<tg-emoji emoji-id='5242287453426969423'>4️⃣</tg-emoji>",
        5: "<tg-emoji emoji-id='5242407832770340528'>5️⃣</tg-emoji>",
        6: "<tg-emoji emoji-id='5242669447818277073'>6️⃣</tg-emoji>",
        7: "<tg-emoji emoji-id='5242663134216350272'>7️⃣</tg-emoji>",
        8: "<tg-emoji emoji-id='5242497782270418294'>8️⃣</tg-emoji>",
        9: "<tg-emoji emoji-id='5242286371095211663'>9️⃣</tg-emoji>",
        10: "<tg-emoji emoji-id='5244961448525848230'>🔟</tg-emoji><tg-emoji emoji-id='5242380641332393116'>🔟</tg-emoji>",
    }
    text = "<tg-emoji emoji-id='5226431245918942763'>🏆</tg-emoji> <b>ТОП ВОРКЕРОВ LK TEAM</b>\n"
    text += "━━━━━━━━━━━━━━━━━\n\n"
    for i, (uid, info) in enumerate(sorted_users[:10], 1):
        medal = medals.get(i, f"<b>{i}.</b>")
        locks = info.get('locks', 0)
        cash = info.get('cash', 0.0)
        name = info.get('name', 'Воркер')
        text += f"{medal} <b>{name}</b>\n"
        text += f"     🔒 {locks} локов • 💰 {cash:g}$\n\n"
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    if not is_group(message): return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы могут использовать эту команду.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("❌ Укажи параметр.\nПримеры:\n/clear 50\n/clear 3м")
        return
    arg = parts[1].lower()
    cid = message.chat.id
    now = time.time()
    log = chat_message_log.get(cid, [])
    if arg.endswith('м'):
        try: minutes = int(arg[:-1])
        except ValueError:
            await message.reply("❌ Неверный формат. Пример: /clear 5м")
            return
        cutoff_time = now - minutes * 60
        to_delete = [mid for mid, ts in log if ts >= cutoff_time]
        mode_text = f"за последние {minutes} мин."
    else:
        try: count = int(arg)
        except ValueError:
            await message.reply("❌ Неверный формат. Пример: /clear 50 или /clear 3м")
            return
        to_delete = [mid for mid, ts in log[-count:]]
        mode_text = f"последних {count} шт."
    to_delete.append(message.message_id)
    to_delete = list(set(to_delete))
    if len(to_delete) <= 1:
        await bot.delete_message(cid, message.message_id)
        try: await bot.send_message(message.from_user.id, "📭 Нет сообщений для удаления в логе.")
        except Exception: pass
        return
    deleted = 0
    failed = 0
    for mid in to_delete:
        try:
            await bot.delete_message(cid, mid)
            deleted += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    deleted_set = set(to_delete)
    if cid in chat_message_log:
        chat_message_log[cid] = [(mid, ts) for mid, ts in chat_message_log[cid] if mid not in deleted_set]
    report = (f"🗑 <b>Очистка завершена</b>\n\n📋 Режим: {mode_text}\n✅ Удалено: {deleted}\n"
              f"❌ Не удалось: {failed} (старше 48ч или уже удалены)")
    try: await bot.send_message(message.from_user.id, report, parse_mode="HTML")
    except Exception: pass

def parse_mute_duration(arg):
    arg = arg.lower()
    if arg.endswith('м') or arg.endswith('m'):
        try: return int(arg[:-1]) * 60
        except ValueError: return None
    elif arg.endswith('ч') or arg.endswith('h'):
        try: return int(arg[:-1]) * 3600
        except ValueError: return None
    elif arg.endswith('д') or arg.endswith('d'):
        try: return int(arg[:-1]) * 86400
        except ValueError: return None
    return None

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message):
    if not is_group(message): return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы чата могут использовать эту команду.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Используй команду как реплай.\nПример: /mute 10м спам")
        return
    target = message.reply_to_message.from_user
    if await is_tg_admin(message.chat.id, target.id):
        await message.reply("⛔ Нельзя замутить администратора.")
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2:
        await message.reply("❌ Укажи время.\nПримеры: /mute 10м спам")
        return
    duration_sec = parse_mute_duration(parts[1])
    if duration_sec is None:
        await message.reply("❌ Неверный формат времени.\nПримеры: 10м, 2ч, 1д")
        return
    reason = parts[2] if len(parts) > 2 else "причина не указана"
    until_ts = int(time.time()) + duration_sec
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=types.ChatPermissions(can_send_messages=False),
            until_date=until_ts
        )
        await message.reply(
            f"🔇 <b>Пользователь замучен</b>\n\n👤 {target.first_name} (<code>{target.id}</code>)\n"
            f"⏱ Время: <b>{parts[1]}</b>\n📝 Причина: <b>{reason}</b>", parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"⚠️ Не удалось замутить.\n<code>{e}</code>", parse_mode="HTML")

@dp.message(Command("kick"))
async def cmd_kick(message: types.Message):
    if not is_group(message): return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы чата могут использовать эту команду.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Используй команду как реплай.\nПример: /kick причина")
        return
    target = message.reply_to_message.from_user
    if await is_tg_admin(message.chat.id, target.id):
        await message.reply("⛔ Нельзя выгнать администратора.")
        return
    parts = message.text.strip().split(maxsplit=1)
    reason = parts[1] if len(parts) > 1 else "причина не указана"
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await asyncio.sleep(1)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(
            f"🚫 <b>Пользователь выгнан</b>\n\n👤 {target.first_name} (<code>{target.id}</code>)\n📝 Причина: <b>{reason}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"⚠️ Не удалось выгнать.\n<code>{e}</code>", parse_mode="HTML")

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not is_group(message): return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы чата могут использовать эту команду.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Используй команду как реплай.\nПример: /ban причина")
        return
    target = message.reply_to_message.from_user
    if await is_tg_admin(message.chat.id, target.id):
        await message.reply("⛔ Нельзя забанить администратора.")
        return
    parts = message.text.strip().split(maxsplit=1)
    reason = parts[1] if len(parts) > 1 else "причина не указана"
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.reply(
            f"🚫 <b>Пользователь забанен</b>\n\n👤 {target.first_name} (<code>{target.id}</code>)\n📝 Причина: <b>{reason}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"⚠️ Не удалось забанить.\n<code>{e}</code>", parse_mode="HTML")

@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    if not is_group(message): return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы чата могут использовать эту команду.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Используй команду как реплай.\nПример: /unmute причина")
        return
    target = message.reply_to_message.from_user
    parts = message.text.strip().split(maxsplit=1)
    reason = parts[1] if len(parts) > 1 else "причина не указана"
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=types.ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True
            )
        )
        await message.reply(
            f"🔊 <b>Пользователь размучен</b>\n\n👤 {target.first_name} (<code>{target.id}</code>)\n📝 Причина: <b>{reason}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"⚠️ Не удалось размутить.\n<code>{e}</code>", parse_mode="HTML")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not is_group(message): return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы чата могут использовать эту команду.")
        return
    parts = message.text.strip().split(maxsplit=2)
    target_id = None
    target_name = None
    reason = "причина не указана"
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        target_id = target.id
        target_name = target.first_name
        if len(parts) > 1: reason = parts[1]
    elif len(parts) >= 2:
        try:
            target_id = int(parts[1])
            target_name = f"ID {target_id}"
            reason = parts[2] if len(parts) > 2 else reason
        except ValueError:
            await message.reply("❌ Укажи числовой ID или сделай реплай.")
            return
    else:
        await message.reply("❌ Используй реплай или укажи ID.\nПример: /unban 123456789 причина")
        return
    try:
        await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
        await message.reply(
            f"✅ <b>Пользователь разбанен</b>\n\n👤 {target_name} (<code>{target_id}</code>)\n📝 Причина: <b>{reason}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"⚠️ Не удалось разбанить.\n<code>{e}</code>", parse_mode="HTML")

# =========================================================
# /pluslokhgasdgasdu
# =========================================================
LOCK_CHAT_ID = -1004485193806
PROFIT_CHAT_ID = -1004409257430

def _generate_profit_card_sync(rank: str, worker_tag: str, service: str, amount: float, share: float) -> io.BytesIO:
    template_path = os.path.join(BASE_DIR, 'images', 'profit_card.jpg')
    img = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    font_path = os.path.join(BASE_DIR, 'images', 'font.ttf')
    
    try:
        font_small = ImageFont.truetype(font_path, 28)
        font_large = ImageFont.truetype(font_path, 34)
    except:
        font_small = ImageFont.load_default()
        font_large = ImageFont.load_default()

    fill = (255, 255, 255, 255)

    def draw_text_in_box(text, cx, cy, font, max_width):
        f = font
        size = f.size
        while True:
            bbox = draw.textbbox((0, 0), text, font=f)
            w = bbox[2] - bbox[0]
            if w <= max_width or size <= 14:
                break
            size -= 2
            try:
                f = ImageFont.truetype(font_path, size)
            except:
                break
        draw.text((cx, cy), text, font=f, fill=fill, anchor="mm")

    draw_text_in_box(rank,           231,  383, font_small, 200)
    draw_text_in_box(worker_tag,     699,  385, font_small, 200)
    draw_text_in_box(service,        1173, 385, font_small, 200)
    draw_text_in_box(f"{amount:g}$", 439,  597, font_large, 220)
    draw_text_in_box(f"{share:g}$",  1024, 594, font_large, 220)

    output = io.BytesIO()
    img = img.convert("RGB")
    img.save(output, format="JPEG", quality=95)
    output.seek(0)
    return output


async def generate_profit_card(rank: str, worker_tag: str, service: str, amount: float, share: float) -> io.BytesIO:
    return await asyncio.to_thread(_generate_profit_card_sync, rank, worker_tag, service, amount, share)


@dp.message(Command("botban"))
async def cmd_botban(message: types.Message):
    """Команда для админов: /botban @username или /botban ID - забанить пользователя в боте"""
    # Работает только в ЛС админов
    if message.chat.type != 'private':
        await message.reply("⛔ Эта команда работает только в личных сообщениях боту.")
        return
    
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Доступно только администраторам.")
        return
    
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Формат: /botban @username или /botban ID\n\nПримеры:\n/botban @john123\n/botban 123456789")
        return
    
    user_arg = parts[1].lstrip('@').strip()
    target_id = None
    target_username = None
    
    # Пытаемся распарсить как ID
    try:
        target_id = int(user_arg)
    except ValueError:
        target_id = await resolve_worker_id(user_arg)
        target_username = user_arg if target_id is not None else None
    
    if target_id is None:
        await message.reply(f"❌ Пользователь не найден: {user_arg}")
        return
    
    # Добавляем в blacklist
    if add_to_blacklist(target_id, f"Забанен админом {message.from_user.id}"):
        await message.reply(f"✅ Пользователь {target_id} забанен в боте.")
    else:
        await message.reply(f"❌ Ошибка при добавлении в blacklist.")

@dp.message(Command("deal"))
async def cmd_deal(message: types.Message):
    if message.chat.type != 'private': return
    if message.from_user.id not in ADMIN_IDS: return

    parts = message.text.strip().split(maxsplit=3)
    if len(parts) < 4:
        await message.reply("❌ Формат: /deal @username сумма + (или −)\n\nПримеры:\n/deal @john123 100.5 +\n/deal @john123 100.5 −")
        return

    username_arg = parts[1].lstrip('@').lower()
    try:
        amount = float(parts[2])
    except ValueError:
        await message.reply("❌ Неверная сумма.")
        return
    
    deal_type = parts[3].strip()
    if deal_type not in ['+', '-', '−']:  # Поддерживаем оба варианта минуса
        await message.reply("❌ Тип сделки должен быть + или −\n\n➕ + = 100% выплата\n➖ − = 60% выплата")
        return
    
    # Определяем процент выплаты
    if deal_type == '+':
        percentage = 1.0  # 100%
        percentage_text = "100%"
    else:  # '-' или '−'
        percentage = 0.6  # 60%
        percentage_text = "60%"
    
    target_id = await resolve_worker_id(username_arg)
    if target_id is None:
        await message.reply(f"❌ Пользователь @{username_arg} не найден в базе.")
        return
    
    # Считаем долю на основе выбранного процента
    share = round(amount * percentage, 2)
    user_data[target_id]['cash'] = round(user_data[target_id].get('cash', 0.0) + share, 2)
    user_data[target_id]['profits'] = user_data[target_id].get('profits', 0) + 1
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if 'profit_history' not in user_data[target_id]:
        user_data[target_id]['profit_history'] = []
    user_data[target_id]['profit_history'].append({'amount': amount, 'share': share, 'date': date_str, 'deal_type': deal_type})
    accrue_ref_bonuses(target_id, share)

    save_db(user_fields={target_id: {"cash", "profits", "profit_history"}})
    rank, tag = await get_worker_card_identity(target_id, username_arg)
    service = 'Прямик' if deal_type == '+' else 'iCloud'
    try:
        card = await generate_profit_card(rank, tag, service, amount, share)
        await bot.send_photo(
            PROFIT_CHAT_ID,
            photo=types.BufferedInputFile(card.read(), filename='profit.jpg'),
        )
    except Exception as e:
        await message.reply(f"⚠️ Карточка не отправлена: {e}")
    await message.reply(
        f"✅ <b>Профит добавлен!</b>\n\n👤 @{username_arg}\n💰 Общая сумма: {amount:g}$\n➕ Доля воркера: <b>{share:g}$</b>\n"
        f"📅 {date_str}\n💵 Касса: {user_data[target_id]['cash']:g}$",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_id,
            f"🎉 <b>Вам начислен профит!</b>\n💰 +{share:g}$\n📅 {date_str}\n💵 Касса: {user_data[target_id]['cash']:g}$",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.message(Command("profit"))
async def cmd_profit(message: types.Message):
    # Работает только в ЛС или в LOCK_CHAT_ID (где /pluslokhgasdgasdu)
    if message.chat.type != 'private' and message.chat.id != LOCK_CHAT_ID:
        return
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.strip().split(maxsplit=3)
    if len(parts) < 4:
        await message.reply("❌ Формат: /profit username сумма +/−\n\n➕ + = 100% выплата\n➖ − = 60% выплата")
        return
    username_arg = parts[1].lstrip('@').lower()
    try:
        amount = float(parts[2])
    except ValueError:
        await message.reply("❌ Неверная сумма.")
        return
    
    deal_type = parts[3].strip()
    if deal_type not in ['+', '-', '−']:  # Поддерживаем оба варианта минуса
        await message.reply("❌ Тип сделки должен быть + или −\n\n➕ + = 100% выплата\n➖ − = 60% выплата")
        return
    
    # Определяем процент выплаты
    if deal_type == '+':
        percentage = 1.0  # 100%
        percentage_text = "100%"
    else:  # '-' или '−'
        percentage = 0.6  # 60%
        percentage_text = "60%"
    
    target_id = await resolve_worker_id(username_arg)
    if target_id is None:
        await message.reply(f"❌ Пользователь @{username_arg} не найден в базе.")
        return
    
    # Считаем долю на основе выбранного процента
    share = round(amount * percentage, 2)
    user_data[target_id]['cash'] = round(user_data[target_id].get('cash', 0.0) + share, 2)
    user_data[target_id]['profits'] = user_data[target_id].get('profits', 0) + 1
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if 'profit_history' not in user_data[target_id]:
        user_data[target_id]['profit_history'] = []
    user_data[target_id]['profit_history'].append({'amount': amount, 'share': share, 'date': date_str, 'deal_type': deal_type})
    accrue_ref_bonuses(target_id, share)

    save_db(user_fields={target_id: {"cash", "profits", "profit_history"}})
    rank, tag = await get_worker_card_identity(target_id, username_arg)
    service = 'Прямик' if deal_type == '+' else 'iCloud'
    try:
        card = await generate_profit_card(rank, tag, service, amount, share)
        await bot.send_photo(
            PROFIT_CHAT_ID,
            photo=types.BufferedInputFile(card.read(), filename='profit.jpg'),
        )
    except Exception as e:
        await message.reply(f"⚠️ Карточка не отправлена: {e}")
    await message.reply(
        f"✅ <b>Профит добавлен!</b>\n\n👤 @{username_arg}\n💰 Общая сумма: {amount:g}$\n➕ Доля воркера: <b>{share:g}$</b>\n"
        f"📅 {date_str}\n💵 Касса: {user_data[target_id]['cash']:g}$",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_id,
            f"🎉 <b>Вам начислен профит!</b>\n💰 +{share:g}$\n📅 {date_str}\n💵 Касса: {user_data[target_id]['cash']:g}$",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.message(Command("pluslokhgasdgasdu"))
async def cmd_plus_lock(message: types.Message):
    if message.chat.id != LOCK_CHAT_ID: return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы чата могут использовать эту команду.")
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("❌ Формат: /pluslokhgasdgasdu @username название модели")
        return
    username_arg = parts[1].lstrip('@').lower()
    model_name = parts[2]
    target_id = await resolve_worker_id(username_arg)
    target_data = user_data.get(target_id) if target_id is not None else None
    if target_id is None:
        await message.reply(f"❌ Пользователь @{username_arg} не найден в базе.")
        return
    target_data['locks'] = target_data.get('locks', 0) + 1
    if 'lock_history' not in target_data:
        target_data['lock_history'] = []
    date_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    target_data['lock_history'].append({'model': model_name, 'date': date_str})
    save_db(user_fields={target_id: {"locks", "lock_history"}})
    await message.reply(
        f"🔒 <b>Лок добавлен!</b>\n\n👤 Воркер: @{username_arg}\n📱 Модель: <b>{model_name}</b>\n"
        f"📅 Дата: {date_str}\n🔒 Всего локов: <b>{target_data['locks']}</b>", parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_id,
            f"🔒 <b>Вам добавлен лок!</b>\n\n📱 Модель: <b>{model_name}</b>\n"
            f"📅 Дата: {date_str}\n🔒 Всего локов: <b>{target_data['locks']}</b>", parse_mode="HTML"
        )
    except Exception:
        pass

# =========================================================
# CALLBACK — ЗАЯВКИ
# =========================================================
@dp.callback_query(F.data == 'apply_start')
async def cb_apply_start(call: types.CallbackQuery):
    applications[call.message.chat.id] = {'submitted': False}
    rules_text = (
        "<b>LK TEAM — Порядок и усилия</b>\n\nМы создали комфортную среду для работы. Придерживайтесь правил:\n\n"
        "⚖️ <b>Правила проекта:</b>\n<blockquote>1. Не обманывать администрацию\n2. Адекватное поведение\n"
        "3. Без спама и рекламы\n4. Блокированным воркерам выплаты не производятся.</blockquote>\n\n"
        "<b>Вы ознакомились и согласились с правилами проекта ✔️</b>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='Принять правила', callback_data='apply_q1'))
    await call.message.edit_text(rules_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == 'apply_q1')
async def cb_apply_q1(call: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='От друга',      callback_data='apply_q2_friend', icon_custom_emoji_id='5298668674532538341'),
        types.InlineKeyboardButton(text='Реклама',       callback_data='apply_q2_ads', icon_custom_emoji_id='5895226695505417875'),
    )
    builder.row(
        types.InlineKeyboardButton(text='TT/Shorts/Reels', callback_data='apply_q2_tt', icon_custom_emoji_id='5215399343246289016'),
        types.InlineKeyboardButton(text='Другое',          callback_data='apply_q2_other', icon_custom_emoji_id='5242329690135356589'),
    )
    await call.message.edit_text("Откуда узнали о LK Team?", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith('apply_q2_'))
async def cb_apply_q2(call: types.CallbackQuery):
    cid = call.message.chat.id
    source_map = {'friend': 'От друга', 'ads': 'Реклама', 'tt': 'TT/Shorts/Reels', 'other': 'Другое'}
    if cid not in applications: applications[cid] = {}
    applications[cid]['source'] = source_map[call.data.split('_')[2]]
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='Нет опыта', callback_data='apply_q3_noexp'),
        types.InlineKeyboardButton(text='Есть опыт', callback_data='apply_q3_exp'),
    )
    await call.message.edit_text("Имеется ли у вас опыт работы в данной сфере?", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith('apply_q3_'))
async def cb_apply_q3(call: types.CallbackQuery):
    cid = call.message.chat.id
    exp_map = {'noexp': 'Нет опыта', 'exp': 'Есть опыт'}
    if cid not in applications: applications[cid] = {}
    applications[cid]['experience'] = exp_map[call.data.split('_')[2]]
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='1-3ч',      callback_data='apply_final_1'),
        types.InlineKeyboardButton(text='3-6ч',      callback_data='apply_final_2'),
        types.InlineKeyboardButton(text='6ч и более',callback_data='apply_final_3'),
    )
    await call.message.edit_text("Какое время Вы готовы уделять работе в день?", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith('apply_final_'))
async def cb_apply_final(call: types.CallbackQuery):
    cid = call.message.chat.id
    time_map = {'1': '1-3ч', '2': '3-6ч', '3': '6ч и более'}
    if cid not in applications: applications[cid] = {}
    applications[cid]['time'] = time_map[call.data.split('_')[2]]
    app = applications[cid]
    final_text = (
        f"Ваша заявка:\nОткуда узнали: {app.get('source', '-')}\nОпыт: {app.get('experience', '-')}\n"
        f"Время для работы: {app.get('time', '-')}\n\n<blockquote>Просим не дублировать заявку. За спам — блокировка.</blockquote>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='✅ Отправить заявку',   callback_data='apply_submit'),
        types.InlineKeyboardButton(text='❌ Отменить',           callback_data='apply_cancel'),
    )
    await call.message.edit_text(final_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == 'apply_submit')
async def cb_apply_submit(call: types.CallbackQuery):
    cid = call.message.chat.id
    if cid not in applications:
        await call.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    app = applications[cid]
    app['submitted'] = True
    app['submitted_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    app['user_id'] = cid
    
    # Сохраняем заявку в MongoDB
    try:
        user_info = user_data.get(cid, {})
        applications_col.update_one(
            {"user_id": cid},
            {"$set": {
                "user_id": cid,
                "user_name": user_info.get('name', 'Unknown'),
                "source": app.get('source', '-'),
                "experience": app.get('experience', '-'),
                "time": app.get('time', '-'),
                "submitted_at": app['submitted_at'],
                "status": "pending"
            }},
            upsert=True
        )
    except Exception as e:
        logging.error(f"⚠️ Ошибка при сохранении заявки: {e}")
    
    await call.message.edit_text(
        "✅ <b>Ваша заявка отправлена на рассмотрение!</b>\n\nАдминистрация рассмотрит вашу заявку в течение суток.",
        parse_mode="HTML",
        reply_markup=None
    )
    
    # Отправляем уведомление админам
    u_name = user_data.get(cid, {}).get('name', f"ID: {cid}")
    admin_notify = (
        f"📩 <b>Новая заявка!</b>\n\n👤 <b>Пользователь:</b> {u_name} (ID: <code>{cid}</code>)\n"
        f"• Откуда: {app.get('source', '-')}\n• Опыт: {app.get('experience', '-')}\n• Время: {app.get('time', '-')}"
    )
    for admin_id in ADMIN_IDS:
        try:
            builder = InlineKeyboardBuilder()
            builder.row(
                types.InlineKeyboardButton(text='✅ Принять',   callback_data=f'admin_accept_{cid}'),
                types.InlineKeyboardButton(text='❌ Отклонить', callback_data=f'admin_decline_{cid}'),
            )
            await bot.send_message(admin_id, admin_notify, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as e:
            logging.error(f"⚠️ Не удалось отправить уведомление админу {admin_id}: {e}")
    
    await call.answer()

@dp.callback_query(F.data == 'apply_cancel')
async def cb_apply_cancel(call: types.CallbackQuery):
    applications.pop(call.message.chat.id, None)
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='📝 Подать заявку', callback_data='apply_start'))
    await call.message.edit_text("❌ Заявка отменена.", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith('admin_accept_'))
async def cb_admin_accept(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    
    target_id = int(call.data.split('_')[2])
    admin_id = call.from_user.id
    
    # Проверяем была ли уже обработана заявка
    app_record = applications_col.find_one({"user_id": target_id})
    
    if app_record and app_record.get('status') and app_record.get('status') != 'pending':
        # Заявка уже была обработана
        status = app_record.get('status')
        admin_who_decided = app_record.get('admin_decided_by')
        decided_at = app_record.get('decided_at')
        
        # Получаем имя админа
        try:
            admin_user = await bot.get_chat(admin_who_decided)
            admin_name = admin_user.username or admin_user.first_name
        except:
            admin_name = f"ID {admin_who_decided}"
        
        msg = f"⚠️ <b>Эта заявка была уже рассмотрена!</b>\n\n👤 <b>Админ:</b> @{admin_name}\n✅ <b>Решение:</b> {'Одобрена' if status == 'approved' else 'Отклонена'}\n🕐 <b>Время:</b> {decided_at}"
        await call.message.edit_text(msg, parse_mode="HTML")
        await call.answer()
        return
    
    # Сначала гарантированно сохраняем доступ в users, и только потом закрываем заявку.
    # Так approved-заявка больше не может остаться без активного профиля.
    try:
        if target_id not in user_data:
            known = known_users_col.find_one({'user_id': target_id}) or {}
            init_user(
                target_id,
                known.get('first_name') or f'ID: {target_id}',
                known.get('username'),
            )
        joined_at = time.time()
        result = users_col.update_one(
            {'user_id': target_id},
            {'$set': {'status_approved': True, 'joined_at': joined_at}},
            upsert=False,
        )
        if result.matched_count != 1:
            raise RuntimeError(f'Profile {target_id} was not saved')
        user_data[target_id]['status_approved'] = True
        user_data[target_id]['joined_at'] = joined_at

        applications_col.update_one(
            {'user_id': target_id},
            {'$set': {
                'status': 'approved',
                'admin_decided_by': admin_id,
                'decided_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }},
            upsert=True,
        )
    except Exception:
        logging.exception('Не удалось одобрить заявку пользователя %s', target_id)
        await call.answer('База временно недоступна. Данные не изменены, повторите позже.', show_alert=True)
        return

    applications.pop(target_id, None)
    await call.message.edit_text(f"✅ Заявка {target_id} одобрена.")
    await bot.send_message(target_id, "🎉 Ваша заявка одобрена! Нажмите /start для входа.")
    await call.answer()

@dp.callback_query(F.data.startswith('admin_decline_'))
async def cb_admin_decline(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    
    target_id = int(call.data.split('_')[2])
    admin_id = call.from_user.id
    
    # Проверяем была ли уже обработана заявка
    app_record = applications_col.find_one({"user_id": target_id})
    
    if app_record and app_record.get('status') and app_record.get('status') != 'pending':
        # Заявка уже была обработана
        status = app_record.get('status')
        admin_who_decided = app_record.get('admin_decided_by')
        decided_at = app_record.get('decided_at')
        
        # Получаем имя админа
        try:
            admin_user = await bot.get_chat(admin_who_decided)
            admin_name = admin_user.username or admin_user.first_name
        except:
            admin_name = f"ID {admin_who_decided}"
        
        msg = f"⚠️ <b>Эта заявка была уже рассмотрена!</b>\n\n👤 <b>Админ:</b> @{admin_name}\n❌ <b>Решение:</b> {'Одобрена' if status == 'approved' else 'Отклонена'}\n🕐 <b>Время:</b> {decided_at}"
        await call.message.edit_text(msg, parse_mode="HTML")
        await call.answer()
        return
    
    # Сохраняем решение в MongoDB
    applications_col.update_one(
        {"user_id": target_id},
        {"$set": {
            "status": "declined",
            "admin_decided_by": admin_id,
            "decided_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }},
        upsert=True
    )
    
    applications.pop(target_id, None)
    await call.message.edit_text(f"❌ Заявка {target_id} отклонена.")
    await bot.send_message(target_id, "❌ Ваша заявка отклонена.")
    await call.answer()

# =========================================================
# CALLBACK — ВХОД/ВЫХОД
# =========================================================
# =========================================================
# CALLBACK — ADMIN ПАНЕЛЬ
# =========================================================
@dp.callback_query(F.data == 'admin_main')
async def cb_admin_main(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='👥 Список пользователей', callback_data='admin_list_users'))
    builder.row(types.InlineKeyboardButton(text='📩 Список заявок',        callback_data='admin_list_apps'))
    builder.row(types.InlineKeyboardButton(text='⚙️ Настройки проекта',   callback_data='admin_project_settings'))
    await call.message.edit_text("⚙️ <b>Панель администратора LK TEAM</b>\nВыберите действие:", reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == 'admin_list_apps')
async def cb_admin_list_apps(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    active_apps = {uid: app for uid, app in applications.items() if app.get('submitted') and not user_data.get(uid, {}).get('status_approved', False)}
    builder = InlineKeyboardBuilder()
    if not active_apps:
        builder.row(types.InlineKeyboardButton(text='Заявок пока нет 📁', callback_data='dummy'))
    else:
        for uid, app in active_apps.items():
            u_name = user_data.get(uid, {}).get('name', f"ID: {uid}")
            builder.row(types.InlineKeyboardButton(text=f"📩 Заявка от {u_name}", callback_data=f"adm_view_app_{uid}"))
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='admin_main', icon_custom_emoji_id='5352759161945867747'))
    await call.message.edit_text("📩 <b>Список активных заявок:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith('adm_view_app_'))
async def cb_adm_view_app(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    target_id = int(call.data.split('_')[3])
    app = applications.get(target_id)
    if not app:
        await call.message.edit_text("⚠️ Заявка не найдена или уже обработана.")
        return
    u_name = user_data.get(target_id, {}).get('name', f"ID: {target_id}")
    admin_text = (
        f"📩 <b>Анкета кандидата</b>\n\n👤 <b>Пользователь:</b> {u_name} (ID: <code>{target_id}</code>)\n"
        f"• Откуда: {app.get('source', '-')}\n• Опыт: {app.get('experience', '-')}\n• Время: {app.get('time', '-')}"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='✅ Принять',   callback_data=f'admin_accept_{target_id}'),
        types.InlineKeyboardButton(text='❌ Отклонить', callback_data=f'admin_decline_{target_id}'),
    )
    builder.row(types.InlineKeyboardButton(text='‹ К списку заявок', callback_data='admin_list_apps'))
    await call.message.edit_text(admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == 'admin_project_settings')
async def cb_admin_project_settings(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    text = (
        "⚙️ <b>Глобальные настройки проекта LK TEAM:</b>\n\n"
        f"📈 <b>Стартовые профиты:</b> {global_settings['base_profits']} шт.\n"
        f"💰 <b>Стартовая касса:</b> {global_settings['base_cash']:,.0f} $\n"
        f"👨‍🏫 <b>Ссылка на наставника:</b> {global_settings['mentor_link']} (Статичная)"
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='📈 Изменить базовые профиты', callback_data='adm_set_b_profits'))
    builder.row(types.InlineKeyboardButton(text='💰 Изменить базовую кассу',   callback_data='adm_set_b_cash'))
    builder.row(types.InlineKeyboardButton(text='  Назад',                     callback_data='admin_main', icon_custom_emoji_id='5352759161945867747'))
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == 'admin_cancel_step')
async def cb_admin_cancel_step(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='‹ Вернуться в меню', callback_data='admin_main'))
    await call.message.edit_text("❌ Действие отменено.", reply_markup=builder.as_markup())
    await call.answer()

@dp.callback_query(F.data == 'adm_set_b_profits')
async def cb_adm_set_b_profits(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminStates.wait_base_profits)
    await call.message.edit_text("Введите новое базовое количество профитов:", reply_markup=get_cancel_markup())
    await call.answer()

@dp.callback_query(F.data == 'adm_set_b_cash')
async def cb_adm_set_b_cash(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminStates.wait_base_cash)
    await call.message.edit_text("Введите новую базовую кассу в $:", reply_markup=get_cancel_markup())
    await call.answer()

USERS_PER_PAGE = 10

@dp.callback_query(F.data.startswith('admin_list_users'))
async def cb_admin_list_users(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return

    # Определяем номер страницы из callback_data вида 'admin_list_users' или 'admin_list_users_2'
    parts = call.data.split('_')
    page = int(parts[-1]) if parts[-1].isdigit() else 0

    builder = InlineKeyboardBuilder()
    users_list = list(user_data.items())

    if not users_list:
        builder.row(types.InlineKeyboardButton(text='Список пуст', callback_data='dummy'))
    else:
        total_pages = (len(users_list) - 1) // USERS_PER_PAGE + 1
        page = max(0, min(page, total_pages - 1))
        start = page * USERS_PER_PAGE
        end = start + USERS_PER_PAGE

        for uid, info in users_list[start:end]:
            builder.row(types.InlineKeyboardButton(text=f"👤 {info['name']} (ID: {uid})", callback_data=f"adm_usr_{uid}"))

        nav_row = []
        if page > 0:
            nav_row.append(types.InlineKeyboardButton(text='◀ Пред.', callback_data=f'admin_list_users_{page - 1}'))
        if page < total_pages - 1:
            nav_row.append(types.InlineKeyboardButton(text='След. ▶', callback_data=f'admin_list_users_{page + 1}'))
        if nav_row:
            builder.row(*nav_row)

    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='admin_main', icon_custom_emoji_id='5352759161945867747'))
    await call.message.edit_text("👥 <b>Выберите пользователя:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith('adm_usr_'))
async def cb_adm_usr(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    target_id = int(call.data.split('_')[2])
    t = user_data[target_id]
    card = (
        f"⚙️ <b>Управление воркером:</b> {t['name']}\n🆔 <b>ID:</b> <code>{target_id}</code>\n\n"
        f"💰 <b>Касса:</b> {t['cash']:g} $\n├ Профитов: {t['profits']}\n└ Локов: {t['locks']}\n\n"
        f"👥 <b>Рефералы L1:</b> {t['ref_count_l1']} чел.\n└ Доход: {t['ref_money_l1']:.2f} USDT"
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='💰 Изменить Кассу',     callback_data=f"adm_edit_cash_{target_id}"))
    builder.row(types.InlineKeyboardButton(text='🔒 Изменить Локи',      callback_data=f"adm_edit_lock_{target_id}"))
    builder.row(types.InlineKeyboardButton(text='👥 Изменить Рефералов', callback_data=f"adm_edit_ref_{target_id}"))
    builder.row(types.InlineKeyboardButton(text='‹ К списку',            callback_data='admin_list_users'))
    await call.message.edit_text(card, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith('adm_edit_cash_'))
async def cb_adm_edit_cash(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    target_id = int(call.data.split('_')[3])
    await state.set_state(AdminStates.wait_cash)
    await state.update_data(target_id=target_id)
    await call.message.edit_text("Введите сумму для добавления (+/-):", reply_markup=get_cancel_markup())
    await call.answer()

@dp.callback_query(F.data.startswith('adm_edit_lock_'))
async def cb_adm_edit_lock(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    target_id = int(call.data.split('_')[3])
    await state.set_state(AdminStates.wait_lock)
    await state.update_data(target_id=target_id)
    await call.message.edit_text("Введите новое точное число локов:", reply_markup=get_cancel_markup())
    await call.answer()

@dp.callback_query(F.data.startswith('adm_edit_ref_'))
async def cb_adm_edit_ref(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS: return
    target_id = int(call.data.split('_')[3])
    await state.set_state(AdminStates.wait_ref)
    await state.update_data(target_id=target_id)
    await call.message.edit_text("Укажите через пробел: рефералы и USDT (Пример: 5 150):", reply_markup=get_cancel_markup())
    await call.answer()

# =========================================================
# CALLBACK — ОСНОВНОЕ МЕНЮ
# =========================================================
@dp.callback_query(F.data == 'menu_main')
async def cb_menu_main(call: types.CallbackQuery):
    data = init_user(call.message.chat.id, call.message.chat.first_name)
    if not data['status_approved']:
        await call.answer("🔒 Доступ ограничен! Подайте заявку.", show_alert=True)
        return
    await update_menu_screen(call, IMG_MAIN, "<b>Добро пожаловать в главное меню!</b> Выберите интересующий раздел:", get_main_menu_keyboard())
    await call.answer()

@dp.callback_query(F.data == 'menu_profile')
async def cb_menu_profile(call: types.CallbackQuery):
    cid = call.message.chat.id
    data = init_user(cid, call.message.chat.first_name)
    if not data['status_approved']:
        await call.answer("🔒 Доступ ограничен!", show_alert=True)
        return
    status = get_status(data['profits'])
    top_place = get_top_place(cid)
    time_spent = get_time_in_bot(cid)
    rank_emoji_id = next((v['emoji_id'] for v in RANKS_CONFIG.values() if v['name'] == data['rank']), '5348215481683688355')
    profile_text = (
        f"👤 <b>Твой профиль {data['name']}</b> 🫶\n\n🏆 <b>Статистика:</b>\n"
        f"├ Твоя касса: {data['cash']:g} $\n├ Кол-во профитов: {data['profits']} профитов\n"
        f"├ Кол-во локов: {data['locks']} локов\n├ Место в топе: # {top_place}\n\n"
        f"⚙️ <b>Информация и текущие настройки</b>\n├ Тег в профитах: {data['tag']}\n"
        f"├ Статус: {status}\n├ Ранг: <tg-emoji emoji-id='{rank_emoji_id}'>🎓</tg-emoji> {data['rank']}\n\n⏳ <b>В команде:</b> {time_spent}"
    )
    await update_menu_screen(call, IMG_PROFILE, profile_text, get_profile_keyboard())
    await call.answer()

@dp.callback_query(F.data == 'my_profits')
async def cb_my_profits(call: types.CallbackQuery):
    cid = call.message.chat.id
    data = init_user(cid, call.message.chat.first_name)
    if len(data.get('profit_history', [])) == 0:
        await call.answer("У вас пока нет профитов", show_alert=True)
        return
    text = "💰 <b>История ваших профитов:</b>\n\n"
    for i, p in enumerate(data['profit_history'], 1):
        text += f"{i}. <b>+{p['amount']:g}$</b> <i>({p['date']})</i>\n"
    await call.message.edit_caption(caption=text, reply_markup=get_back_profile(), parse_mode="HTML")
    await call.answer()

LOCKS_PER_PAGE = 15

@dp.callback_query(F.data == 'my_locks')
async def cb_my_locks(call: types.CallbackQuery):
    cid = call.message.chat.id
    data = init_user(cid, call.message.chat.first_name)
    if data['locks'] == 0:
        await call.answer("У вас пока нет локов", show_alert=True)
        return
    await cb_my_locks_page(call, 0)

async def cb_my_locks_page(call: types.CallbackQuery, page: int):
    cid = call.message.chat.id
    data = init_user(cid, call.message.chat.first_name)
    history = data.get('lock_history', [])
    
    if not history:
        text = f"🔒 <b>Ваши локи:</b> {data['locks']} шт.\n\nИстория локов пуста."
        await call.message.edit_caption(caption=text, reply_markup=get_back_profile(), parse_mode="HTML")
        await call.answer()
        return
    
    # Разворачиваем историю в обратном порядке для вывода
    reversed_history = list(reversed(history))
    total_pages = (len(reversed_history) - 1) // LOCKS_PER_PAGE + 1
    page = max(0, min(page, total_pages - 1))
    
    start = page * LOCKS_PER_PAGE
    end = start + LOCKS_PER_PAGE
    
    text = f"🔒 <b>Ваши локи:</b> {data['locks']} шт.\n\n"
    text += "<b>📋 История локов:</b>\n"
    
    for i, entry in enumerate(reversed_history[start:end], start + 1):
        text += f"{i}. 📱 <b>{entry['model']}</b> — {entry['date']}\n"
    
    builder = InlineKeyboardBuilder()
    
    nav_row = []
    if page > 0:
        nav_row.append(types.InlineKeyboardButton(text='◀ Пред.', callback_data=f'my_locks_page_{page - 1}'))
    if page < total_pages - 1:
        nav_row.append(types.InlineKeyboardButton(text='След. ▶', callback_data=f'my_locks_page_{page + 1}'))
    
    if nav_row:
        builder.row(*nav_row)
    
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_profile', icon_custom_emoji_id='5352759161945867747'))
    
    await call.message.edit_caption(caption=text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith('my_locks_page_'))
async def cb_my_locks_page_nav(call: types.CallbackQuery):
    page = int(call.data.split('_')[3])
    await cb_my_locks_page(call, page)

@dp.callback_query(F.data == 'ref_system')
async def cb_ref_system(call: types.CallbackQuery):
    cid = call.message.chat.id
    data = init_user(cid, call.message.chat.first_name)
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={cid}"
    total_ref_money = data['ref_money_l1'] + data['ref_money_l2'] + data['ref_money_l3']
    text = (
        f"📡 <b>Реферальная система:</b>\n\n🏆 <b>Вы получаете выплату с профитов ваших рефералов</b>\n\n"
        f"📊 <b>Уровень 1 (5%):</b>\n• Рефералов: {data['ref_count_l1']}\n• Заработано: {data['ref_money_l1']:.2f} USDT\n\n"
        f"📊 <b>Уровень 2 (10%):</b>\n• Рефералов: {data['ref_count_l2']}\n• Заработано: {data['ref_money_l2']:.2f} USDT\n\n"
        f"📊 <b>Уровень 3 (15%):</b>\n• Рефералов: {data['ref_count_l3']}\n• Заработано: {data['ref_money_l3']:.2f} USDT\n\n"
        f"💲 <b>Общий доход: {total_ref_money:.2f} USDT</b>\n\n🔗 <b>Ваша ссылка:</b>\n<code>{ref_link}</code>"
    )
    await call.message.edit_caption(caption=text, reply_markup=get_back_profile(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == 'menu_settings')
async def cb_menu_settings(call: types.CallbackQuery):
    data = init_user(call.message.chat.id, call.message.chat.first_name)
    if not data['status_approved']:
        await call.answer("🔒 Доступ ограничен!", show_alert=True)
        return
    settings_text = (
        f"⚙️ <b>Настройки профиля</b>\n\n"
        f"👤 <b>Текущий тег:</b> <code>{data['tag']}</code>\n\n"
        f"Тег используется при выплате профитов для идентификации."
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='✏️ Изменить тег', callback_data='settings_change_tag', icon_custom_emoji_id='5771449289972650710'))
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_main', icon_custom_emoji_id='5352759161945867747'))
    await update_menu_screen(call, IMG_SETTINGS, settings_text, builder.as_markup())
    await call.answer()

@dp.callback_query(F.data == 'settings_change_tag')
async def cb_settings_change_tag(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.wait_tag)
    await call.message.edit_caption(caption="Введите новый тег (не более 20 символов):", reply_markup=get_back_settings(), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == 'menu_edu')
async def cb_menu_edu(call: types.CallbackQuery):
    data = init_user(call.message.chat.id, call.message.chat.first_name)
    if not data['status_approved']:
        await call.answer("🔒 Доступ ограничен!", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='👨‍🏫 Связаться с наставником', url=global_settings['mentor_link']))
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_main', icon_custom_emoji_id='5352759161945867747'))
    await update_menu_screen(call, IMG_MAIN, "<tg-emoji emoji-id='5224437668129026620'>🎓</tg-emoji> <b>Система обучения LK TEAM</b>\n\nЕсли у вас возникли вопросы — нажмите кнопку ниже:", builder.as_markup())
    await call.answer()

@dp.callback_query(F.data == 'menu_about')
async def cb_menu_about(call: types.CallbackQuery):
    data = init_user(call.message.chat.id, call.message.chat.first_name)
    if not data['status_approved']:
        await call.answer("🔒 Доступ ограничен!", show_alert=True)
        return
    real_profits = sum(int(u.get('profits', 0)) for u in user_data.values())
    real_cash    = sum(float(u.get('cash', 0.0)) for u in user_data.values())
    total_profits = int(global_settings['base_profits']) + real_profits
    total_cash    = float(global_settings['base_cash']) + real_cash
    about_text = (
        f"<tg-emoji emoji-id='5447410659077661506'>🌐</tg-emoji> <b>ИНФОРМАЦИЯ О ПРОЕКТЕ LK TEAM</b>\n\n"
        f"<tg-emoji emoji-id='5372917041193828849'>🚀</tg-emoji> Мы работаем 20.06.2026, за это время сделаны {total_profits} профитов на сумму {total_cash:,.0f} $\n\n"
        f"<tg-emoji emoji-id='5472250091332993630'>💸</tg-emoji> <b>Выплаты</b>\n├ Прямой перевод - 100%\n└ Сервисы - 60%\n\nСостояние работы бота: <tg-emoji emoji-id='5206607081334906820'>✅</tg-emoji>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='Общий чат',       url='https://t.me/+C8p0KE54ZudlNzM6', icon_custom_emoji_id='5443038326535759644'),
        types.InlineKeyboardButton(text='Мануалы',         url='https://t.me/+WZhhjJ1b8uk1YzUy', icon_custom_emoji_id='5278753302023004775'),
    )
    builder.row(
        types.InlineKeyboardButton(text='Канал профитов',  url='https://t.me/+kxuAJ546U0M4Mjdi', icon_custom_emoji_id='5391292736647209211'),
        types.InlineKeyboardButton(text='Правила',         callback_data='about_rules', icon_custom_emoji_id='5278753302023004775'),
    )
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_main', icon_custom_emoji_id='5352759161945867747'))
    await update_menu_screen(call, IMG_ABOUT, about_text, builder.as_markup())
    await call.answer()

@dp.message(Command("call"))
async def cmd_call(message: types.Message):
    if not is_group(message): return
    if not await is_tg_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Только администраторы могут использовать эту команду.")
        return
    try:
        members_text = ""
        for uid, info in user_data.items():
            if info.get('status_approved'):
                members_text += f'<a href="tg://user?id={uid}">​</a>'
        if not members_text:
            await message.reply("📭 Нет пользователей.")
            return
        await message.reply(f"📢 {members_text}Внимание!", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"⚠️ Ошибка: {e}")



@dp.callback_query(F.data == 'about_rules')
async def cb_about_rules(call: types.CallbackQuery):
    rules_text = (
        "<b>LK TEAM — Порядок и усилия</b>\n\n⚖️ <b>Правила нашего проекта:</b>\n"
        "<blockquote>Запрещено:\n1. Пытаться обмануть администрацию в разных аспектах\n"
        "2. Неадекватное поведение\n3. Реклама сторонних проектов/услуг\n4. Попрошайничество\n"
        "5. Распространение запрещённых материалов\n6. Дезинформация о проектах\n"
        "7. Отправка шокирующего контента\n8. Необходимо находится в чате для выплаты\n"
        "9. В чате запрещены темы о политике\n10. Воркеру который заблокирован, профит не выплачивается\n"
        "11. Продажа каких либо услуг/товаров без согласования\n12. Оскорблять администрацию в любом виде\n"
        "13. Коммерция и предоставление любых типов услуг запрещена\n14. Репорт багов обязателен.</blockquote>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_about', icon_custom_emoji_id='5352759161945867747'))
    await update_menu_screen(call, IMG_ABOUT, rules_text, builder.as_markup())
    await call.answer()

# =========================================================
# FSM — ОБРАБОТКА ВВОДА
# =========================================================
@dp.message(AdminStates.wait_base_profits)
async def fsm_base_profits(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    try:
        val = int(message.text)
        global_settings['base_profits'] = val
        save_db(settings_fields={"base_profits"})
        await state.clear()
        await message.answer(f"✅ Базовые профиты изменены на: <b>{val}</b>", parse_mode="HTML")
    except ValueError:
        await message.answer("⚠️ Введите целое число.")

@dp.message(AdminStates.wait_base_cash)
async def fsm_base_cash(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    try:
        val = float(message.text)
        global_settings['base_cash'] = val
        save_db(settings_fields={"base_cash"})
        await state.clear()
        await message.answer(f"✅ Базовая касса изменена на: <b>{val:,.0f} $</b>", parse_mode="HTML")
    except ValueError:
        await message.answer("⚠️ Введите число.")

@dp.message(AdminStates.wait_cash)
async def fsm_cash(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data_state = await state.get_data()
    target_id = data_state.get('target_id')
    try:
        amount = float(message.text)
        user_data[target_id]['cash'] += amount
        if amount > 0:
            user_data[target_id]['profits'] += 1
            date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            user_data[target_id]['profit_history'].append({'amount': amount, 'date': date_str})
            # Начисляем реферальные бонусы
            accrue_ref_bonuses(target_id, amount)
        user_data[target_id]['locks'] = int(user_data[target_id]['cash'] // 500)
        save_db(user_fields={target_id: {"cash", "profits", "profit_history", "locks"}})
        await state.clear()
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text='👤 Вернуться в карточку', callback_data=f"adm_usr_{target_id}"))
        await message.answer(f"✅ Изменено! Баланс: {user_data[target_id]['cash']:g}$", reply_markup=builder.as_markup())
        if amount > 0:
            await bot.send_message(target_id, f"🎉 Начислен профит: <b>+{amount:g}$</b>!", parse_mode="HTML")
    except ValueError:
        await message.answer("⚠️ Введите корректное число.")

@dp.message(AdminStates.wait_lock)
async def fsm_lock(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data_state = await state.get_data()
    target_id = data_state.get('target_id')
    try:
        val = int(message.text)
        user_data[target_id]['locks'] = val
        save_db(user_fields={target_id: {"locks"}})
        await state.clear()
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text='👤 Вернуться в карточку', callback_data=f"adm_usr_{target_id}"))
        await message.answer(f"✅ Локи изменены на: {val}", reply_markup=builder.as_markup())
    except ValueError:
        await message.answer("⚠️ Введите целое число.")

@dp.message(AdminStates.wait_ref)
async def fsm_ref(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data_state = await state.get_data()
    target_id = data_state.get('target_id')
    try:
        parts = message.text.split()
        user_data[target_id]['ref_count_l1'] = int(parts[0])
        user_data[target_id]['ref_money_l1']  = float(parts[1])
        save_db(user_fields={target_id: {"ref_count_l1", "ref_money_l1"}})
        await state.clear()
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text='👤 Вернуться в карточку', callback_data=f"adm_usr_{target_id}"))
        await message.answer("✅ Данные рефералов обновлены!", reply_markup=builder.as_markup())
    except Exception:
        await message.answer("⚠️ Ошибка формата. Пример: 5 150.5")

@dp.message(AdminStates.wait_tag)
async def fsm_tag(message: types.Message, state: FSMContext):
    cid = message.chat.id
    tag = (message.text or '').strip()
    if not tag or len(tag) > 20 or any(ord(char) < 32 for char in tag):
        await message.answer("⚠️ Тег должен содержать от 1 до 20 обычных символов.")
        return
    if cid not in user_data:
        await state.clear()
        await message.answer("⚠️ Профиль не найден. Сначала нажмите /start.")
        return
    try:
        await asyncio.to_thread(
            users_col.update_one,
            {'user_id': int(cid)},
            {'$set': {'tag': tag}},
            upsert=False,
        )
    except Exception as e:
        logging.exception("Не удалось сохранить тег пользователя %s", cid)
        await message.answer("⚠️ Не удалось сохранить тег. Попробуйте ещё раз.")
        return
    user_data[cid]['tag'] = tag
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='  Назад', callback_data='menu_settings', icon_custom_emoji_id='5352759161945867747'))
    await message.answer(f"✅ Тег изменен на: <b>{html.escape(tag)}</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    
@dp.message(F.chat.type.in_({'group', 'supergroup'}))
async def track_all_messages(message: types.Message):
    cid = message.chat.id
    if cid not in chat_message_log:
        chat_message_log[cid] = []
    chat_message_log[cid].append((message.message_id, message.date.timestamp()))
    cutoff = time.time() - 48 * 3600
    chat_message_log[cid] = [(mid, ts) for mid, ts in chat_message_log[cid] if ts >= cutoff]

# =========================================================
# ФОНОВАЯ СИНХРОНИЗАЦИЯ С MONGODB
# =========================================================
def _fetch_all_users_blocking():
    """Синхронный (блокирующий) запрос к MongoDB. Вызывается только через to_thread,
    чтобы не тормозить event loop бота.
    Тянем только те поля, которые реально может поменять мини-приложение (tag, rank) —
    это на порядок меньше данных и быстрее, чем выкачивать целиком profit_history/lock_history
    у каждого пользователя каждые несколько секунд."""
    result = {}
    cursor = users_col.find({}, {"user_id": 1, "tag": 1, "rank": 1})
    for user in cursor:
        uid = int(user["user_id"])
        result[uid] = {k: v for k, v in user.items() if k in ("tag", "rank")}
    return result

async def sync_from_db_loop():
    """Периодически подтягивает изменения тега/ранга из MongoDB в оперативную память бота —
    чтобы правки, сделанные напрямую в базе (например, из мини-приложения), доходили и до бота.
    Запрос лёгкий (только 2 поля) и выполняется в отдельном потоке (asyncio.to_thread),
    поэтому бот продолжает нормально отвечать на сообщения во время синхронизации."""
    while True:
        await asyncio.sleep(5)
        try:
            fresh_users = await asyncio.to_thread(_fetch_all_users_blocking)
            for uid, fresh in fresh_users.items():
                if uid in user_data:
                    user_data[uid].update(fresh)
        except Exception as e:
            logging.warning(f"⚠️ Ошибка фоновой синхронизации с MongoDB: {e}")

# =========================================================
# ЗАПУСК
# =========================================================
async def main():
    await bot(DeleteWebhook(drop_pending_updates=True))
    await bot.set_my_commands(
        [
            types.BotCommand(command='setadmin', description='Выдать администратора группы'),
            types.BotCommand(command='deladmin', description='Снять администратора группы'),
            types.BotCommand(command='admins', description='Список администраторов'),
            types.BotCommand(command='mute', description='Замутить пользователя'),
            types.BotCommand(command='unmute', description='Снять мут'),
            types.BotCommand(command='kick', description='Выгнать пользователя'),
            types.BotCommand(command='ban', description='Заблокировать пользователя'),
            types.BotCommand(command='unban', description='Разблокировать пользователя'),
            types.BotCommand(command='clear', description='Очистить сообщения'),
        ],
        scope=types.BotCommandScopeAllGroupChats(),
    )
    asyncio.create_task(sync_from_db_loop())
    logging.info("Бот запущен на aiogram, MongoDB активна")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])

if __name__ == '__main__':
    asyncio.run(main())
