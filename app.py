import hashlib
import hmac
import json
import logging
import os
import time
from functools import wraps
from urllib.parse import parse_qsl

import pymongo
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_file, send_from_directory


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URL = (os.getenv("MONGO_URL") or "").strip()
MONGO_DB_NAME = "tg_bot_database"
if not MONGO_URL:
    raise RuntimeError("MONGO_URL is required; Mini App must use the production bot database")
MINI_APP_AUTH_TTL = int(os.getenv("MINI_APP_AUTH_TTL", "86400"))

if MONGO_URL and "${{" in MONGO_URL:
    MONGO_URL = MONGO_URL.split("}")[-1]

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
mongo = pymongo.MongoClient(
    MONGO_URL,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=20000,
    retryWrites=True,
)
db = mongo[MONGO_DB_NAME]
users_col = db["users"]

RANKS_CONFIG = {
    "Студент": 0,
    "Чемпион": 1000,
    "Волк с Уолл-Стрит": 3000,
    "Легенда": 5000,
    "Босс Верховного стола": 10000,
}

PUBLIC_USER_FIELDS = {
    "_id": 0,
    "user_id": 1,
    "name": 1,
    "tag": 1,
    "rank": 1,
    "cash": 1,
    "profits": 1,
    "locks": 1,
    "profit_history": 1,
    "lock_history": 1,
    "ref_count_l1": 1,
    "ref_count_l2": 1,
    "ref_count_l3": 1,
    "ref_money_l1": 1,
    "ref_money_l2": 1,
    "ref_money_l3": 1,
    "joined_at": 1,
    "status_approved": 1,
}


def api_error(message, status=400):
    return jsonify({"success": False, "error": message}), status


def validate_telegram_init_data(init_data: str):
    """Validate Telegram Mini App initData and return the authenticated user."""
    if not BOT_TOKEN or not init_data:
        return None

    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash", "")
        auth_date = int(values.get("auth_date", "0"))
        if not received_hash or abs(int(time.time()) - auth_date) > MINI_APP_AUTH_TTL:
            return None

        data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received_hash, expected_hash):
            return None

        user = json.loads(values.get("user", "{}"))
        if not isinstance(user.get("id"), int):
            return None
        return user
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def telegram_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        init_data = auth_header[4:].strip() if auth_header.lower().startswith("tma ") else ""
        user = validate_telegram_init_data(init_data)
        if not user:
            return api_error("Откройте приложение через Telegram", 401)
        g.telegram_user = user
        g.user_id = int(user["id"])
        try:
            access = users_col.find_one({"user_id": g.user_id}, {"status_approved": 1})
        except pymongo.errors.PyMongoError:
            return api_error("База данных временно недоступна", 503)
        if not access or not access.get("status_approved"):
            return api_error("Доступ к приложению ещё не одобрен", 403)
        return view(*args, **kwargs)

    return wrapped


def serialize_user(user):
    if not user:
        return None
    user.pop("_id", None)
    return user


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def serve_index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/<path:filename>")
def serve_static(filename):
    if filename.startswith("api/"):
        return api_error("Метод не найден", 404)
    candidate = os.path.join(BASE_DIR, filename)
    if os.path.isfile(candidate):
        return send_from_directory(BASE_DIR, filename)
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/health", methods=["GET"])
def health():
    try:
        mongo.admin.command("ping")
        return jsonify({"status": "ok"})
    except pymongo.errors.PyMongoError:
        return jsonify({"status": "degraded"}), 503


@app.route("/api/me", methods=["GET"])
@telegram_auth
def get_me():
    try:
        user = users_col.find_one({"user_id": g.user_id}, PUBLIC_USER_FIELDS)
        if not user:
            return api_error("Профиль не найден. Сначала запустите бота", 404)
        return jsonify({"success": True, "data": serialize_user(user)})
    except pymongo.errors.PyMongoError:
        logging.exception("Failed to load user %s", g.user_id)
        return api_error("База данных временно недоступна", 503)


@app.route("/api/me/update", methods=["POST"])
@telegram_auth
def update_me():
    try:
        body = request.get_json(silent=True) or {}
        update_fields = {}

        if "tag" in body:
            tag = str(body["tag"]).strip()
            if not tag or len(tag) > 20 or any(ord(char) < 32 for char in tag):
                return api_error("Тег должен содержать от 1 до 20 обычных символов")
            update_fields["tag"] = tag

        if "rank" in body:
            rank = str(body["rank"]).strip()
            if rank not in RANKS_CONFIG:
                return api_error("Некорректный ранг")

            user = users_col.find_one({"user_id": g.user_id}, {"cash": 1})
            if not user:
                return api_error("Профиль не найден", 404)
            balance = float(user.get("cash", 0))
            required = RANKS_CONFIG[rank]
            if balance < required:
                return api_error(f"Для этого ранга нужен баланс от ${required:,.0f}")
            update_fields["rank"] = rank

        if not update_fields:
            return api_error("Нет данных для обновления")

        result = users_col.update_many({"user_id": g.user_id}, {"$set": update_fields})
        if not result.matched_count:
            return api_error("Профиль не найден", 404)
        return jsonify({"success": True, "data": update_fields})
    except pymongo.errors.PyMongoError:
        logging.exception("Failed to update user %s", g.user_id)
        return api_error("Не удалось сохранить изменения", 503)


@app.route("/api/leaderboard/<string:kind>", methods=["GET"])
@telegram_auth
def leaderboard(kind):
    if kind not in {"profits", "balance", "locks"}:
        return api_error("Неизвестный рейтинг", 404)
    sort_field = {"profits": "profits", "balance": "cash", "locks": "locks"}[kind]
    projection = {"_id": 0, "user_id": 1, "name": 1, "tag": 1, "rank": 1, sort_field: 1}
    try:
        users = list(
            users_col.find({"status_approved": True}, projection)
            .sort(sort_field, pymongo.DESCENDING)
            .limit(10)
        )
        return jsonify({"success": True, "data": users})
    except pymongo.errors.PyMongoError:
        logging.exception("Failed to load leaderboard")
        return api_error("Не удалось загрузить рейтинг", 503)


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return api_error("Метод не найден", 404)
    return send_file(os.path.join(BASE_DIR, "index.html"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
