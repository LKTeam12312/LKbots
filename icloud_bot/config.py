"""Конфигурация: все секреты читаются только из переменных окружения."""
import os
from pathlib import Path

# Локальная разработка: подхватываем .env, если он есть (на Railway не нужен).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN: str = os.getenv("ICLOUD_BOT_TOKEN", "").strip()
MONGO_URL: str = os.getenv("MONGO_URL", "").strip()
ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "").strip()

DB_NAME = "icloud_monitor"
ACCOUNTS_COLLECTION = "accounts"

# Корень для восстановления сессий pyicloud (эфемерная ФС Railway -> /tmp).
SESSION_ROOT = Path(os.getenv("SESSION_ROOT", "/tmp/sessions"))

# Регион Apple ID "Китай" (опционально).
CHINA_MAINLAND: bool = os.getenv("ICLOUD_CHINA_MAINLAND", "0").lower() in {"1", "true", "yes"}

# Порог "существенного" смещения координат (~55 м), чтобы не спамить GPS-дрожью.
MOVE_THRESHOLD_DEG = 0.0005


def _parse_admin_ids(raw: str) -> frozenset:
    ids = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise RuntimeError(f"ADMIN_IDS содержит не число: {chunk!r}") from exc
    return frozenset(ids)


ADMIN_IDS: frozenset = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))


def _poll_interval() -> int:
    try:
        value = int(os.getenv("POLL_INTERVAL", "75"))
    except ValueError:
        value = 75
    # Антифрод Apple: строго не чаще одного опроса в 60 секунд.
    return max(60, value)


POLL_INTERVAL: int = _poll_interval()


def validate_config() -> None:
    """Fail-fast проверка конфигурации при старте сервиса."""
    missing = [
        name
        for name, value in (
            ("ICLOUD_BOT_TOKEN", BOT_TOKEN),
            ("MONGO_URL", MONGO_URL),
            ("ENCRYPTION_KEY", ENCRYPTION_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS пуст: укажите хотя бы один Telegram user id через запятую.")
    from cryptography.fernet import Fernet

    try:
        Fernet(ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise RuntimeError(
            "ENCRYPTION_KEY не является валидным Fernet-ключом. Сгенерируйте: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from exc
