"""MongoDB (motor): аккаунты, Fernet-шифрование, бинарные сессии."""
import logging
from datetime import datetime, timezone
from typing import Optional

from bson import Binary, ObjectId
from cryptography.fernet import Fernet
from motor.motor_asyncio import AsyncIOMotorClient

from icloud_bot.config import ACCOUNTS_COLLECTION, DB_NAME, ENCRYPTION_KEY, MONGO_URL

log = logging.getLogger(__name__)

_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(ENCRYPTION_KEY.encode())
    return _fernet


def encrypt_text(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_text(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    """Тонкая обертка над коллекцией icloud_monitor.accounts."""

    def __init__(self) -> None:
        self._client = AsyncIOMotorClient(MONGO_URL, tz_aware=True, serverSelectionTimeoutMS=15000)
        self.accounts = self._client[DB_NAME][ACCOUNTS_COLLECTION]

    async def init(self) -> None:
        await self._client.admin.command("ping")
        await self.accounts.create_index("apple_id", unique=True)
        log.info("MongoDB подключена, база %s", DB_NAME)

    async def close(self) -> None:
        self._client.close()

    @staticmethod
    def _oid(account_id: str) -> Optional[ObjectId]:
        try:
            return ObjectId(account_id)
        except Exception:
            return None

    async def add_account(self, apple_id: str, password: str, security_questions: dict) -> dict:
        now = _utcnow()
        doc = {
            "apple_id": apple_id.strip().lower(),
            "password": encrypt_text(password),
            "security_questions": {q: encrypt_text(a) for q, a in security_questions.items()},
            "is_active": False,
            "tracking_enabled": False,
            "known_devices": [],
            "device_states": {},
            "session_data": None,
            "session_updated_at": None,
            "last_check_at": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        result = await self.accounts.insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc

    async def get_account(self, account_id: str) -> Optional[dict]:
        oid = self._oid(account_id)
        if oid is None:
            return None
        return await self.accounts.find_one({"_id": oid})

    async def get_by_apple_id(self, apple_id: str) -> Optional[dict]:
        return await self.accounts.find_one({"apple_id": apple_id.strip().lower()})

    async def list_accounts(self) -> list:
        return await self.accounts.find().sort("apple_id", 1).to_list(length=100)

    async def list_active(self) -> list:
        return await self.accounts.find({"is_active": True}).to_list(length=100)

    async def update_fields(self, account_id: str, **fields) -> None:
        oid = self._oid(account_id)
        if oid is None:
            return
        fields["updated_at"] = _utcnow()
        await self.accounts.update_one({"_id": oid}, {"$set": fields})

    async def set_active(self, account_id: str, value: bool) -> None:
        await self.update_fields(account_id, is_active=value)

    async def toggle_tracking(self, account_id: str) -> bool:
        doc = await self.get_account(account_id)
        new_value = not bool(doc and doc.get("tracking_enabled"))
        await self.update_fields(account_id, tracking_enabled=new_value)
        return new_value

    async def delete_account(self, account_id: str) -> None:
        oid = self._oid(account_id)
        if oid is not None:
            await self.accounts.delete_one({"_id": oid})

    async def save_session_blob(self, account_id: str, blob: bytes) -> None:
        await self.update_fields(account_id, session_data=Binary(blob), session_updated_at=_utcnow())

    async def load_session_blob(self, account_id: str) -> Optional[bytes]:
        oid = self._oid(account_id)
        if oid is None:
            return None
        doc = await self.accounts.find_one({"_id": oid}, {"session_data": 1})
        data = doc.get("session_data") if doc else None
        return bytes(data) if data else None

    async def update_monitor_state(self, account_id: str, known_devices: list, device_states: dict) -> None:
        await self.update_fields(
            account_id,
            known_devices=known_devices,
            device_states=device_states,
            last_check_at=_utcnow(),
            last_error=None,
        )
