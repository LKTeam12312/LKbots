"""Persistent Nedobonus contest data in the existing tg_bot_database.

All contest records live in the separate ``nedobonus`` collection. Nothing is
written to users, and a Telegram channel post is counted at most once.
"""

import datetime as dt
import re
import uuid


SOURCE_CHANNEL_ID = -1004192767524
EVENT_GROUP_ID = -1004398908256
TEST_GROUP_ID = -1004485193806
DURATION = dt.timedelta(days=10)
UTC = dt.timezone.utc

MODEL_RE = re.compile(r"(?im)^\s*[┣├┗└]?\s*(?:📱\s*)?Модель\s*:\s*(.+?)\s*$")
WORKER_RE = re.compile(r"(?im)^\s*[┣├┗└]?\s*(?:👷\s*)?Воркер\s*:\s*@([A-Za-z0-9_]{5,32})\s*$")
SUCCESS_RE = re.compile(r"(?im)^\s*[┣├┗└]?\s*(?:🎉\s*)?Отличная работа!\s*$")


def parse_lock_notice(text):
    """Return (username without @, arbitrary device model) or None."""
    text = text or ""
    model = MODEL_RE.search(text)
    worker = WORKER_RE.search(text)
    if not (model and worker and SUCCESS_RE.search(text)):
        return None
    device = model.group(1).strip()
    if not device or len(device) > 120:
        return None
    return worker.group(1), device


def now_utc():
    return dt.datetime.now(UTC)


def as_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def format_date(value):
    return as_utc(value).strftime("%d.%m.%Y %H:%M UTC")


def normalize_prize(raw):
    if isinstance(raw, dict):
        kind = raw.get("type")
        value = raw.get("value")
        if kind == "text" and isinstance(value, str) and value.strip():
            return {"type": "text", "value": value.strip()[:100]}
        if kind == "money":
            try:
                return {"type": "money", "value": max(0.0, float(value))}
            except (TypeError, ValueError):
                pass
    try:
        return {"type": "money", "value": max(0.0, float(raw))}
    except (TypeError, ValueError):
        return {"type": "money", "value": 0.0}


def prizes(collection):
    doc = collection.find_one({"_id": "prizes"}) or {}
    values = doc.get("amounts") or []
    return [normalize_prize(values[i] if i < len(values) else 0) for i in range(10)]


def set_prize(collection, place, kind, value):
    if not 1 <= place <= 10 or kind not in {"money", "text"}:
        raise ValueError("Invalid prize")
    if kind == "money":
        value = float(value)
        if not 0 <= value <= 1_000_000_000:
            raise ValueError("Invalid prize amount")
        value = round(value, 2)
    else:
        value = str(value).strip()
        if not 1 <= len(value) <= 100 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid prize name")
    current = prizes(collection)
    current[place - 1] = {"type": kind, "value": value}
    collection.update_one({"_id": "prizes"}, {"$set": {"amounts": current}}, upsert=True)


def event(collection):
    return collection.find_one({"_id": "event"})


def start(collection, started_at=None, prize_snapshot=None):
    """Atomically start a new event; never replace an active one."""
    from pymongo import ReturnDocument

    started_at = as_utc(started_at or now_utc())
    collection.update_one({"_id": "event"}, {"$setOnInsert": {"status": "idle"}}, upsert=True)
    snapshot = {
        "status": "active", "event_id": uuid.uuid4().hex,
        "started_at": started_at, "ends_at": started_at + DURATION,
        "ended_at": None, "announced": False,
        "prizes": [normalize_prize(p) for p in prize_snapshot] if prize_snapshot is not None else prizes(collection),
    }
    return collection.find_one_and_update(
        {"_id": "event", "status": {"$ne": "active"}, "announced": {"$ne": False}},
        {"$set": snapshot}, return_document=ReturnDocument.AFTER,
    )


def add_lock(collection, active, channel_id, message_id, username, model, user_id=None, timestamp=None, source_id=SOURCE_CHANNEL_ID):
    """Atomic per-user update; one channel message contributes exactly one lock."""
    timestamp = as_utc(timestamp or now_utc())
    if (not active or active.get("status") != "active"
            or channel_id != source_id
            or timestamp < as_utc(active["started_at"])
            or timestamp >= as_utc(active["ends_at"])
            or now_utc() >= as_utc(active["ends_at"])):
        return False
    username_lower = username.lower()
    key = f"{channel_id}:{message_id}"
    entry_id = f"entry:{active['event_id']}:{username_lower}"
    lock = {"message_id": key, "model": model, "at": timestamp}
    # One document update makes deduplication and count change indivisible.
    # A concurrent upsert of the same username may hit the unique _id index;
    # retry against the document inserted by the other update.
    from pymongo.errors import DuplicateKeyError

    update = {
        "$setOnInsert": {"kind": "entry", "event_id": active["event_id"],
                         "username_lower": username_lower,
                         **({"user_id": None} if user_id is None else {})},
        "$set": {"username": username, **({"user_id": user_id} if user_id is not None else {})},
        "$addToSet": {"message_keys": key},
        "$push": {"locks": lock},
        "$inc": {"count": 1},
    }
    for attempt in range(3):
        try:
            result = collection.update_one(
                {"_id": entry_id, "message_keys": {"$ne": key}}, update, upsert=True,
            )
            return bool(result.modified_count or result.upserted_id)
        except DuplicateKeyError:
            if collection.find_one({"_id": entry_id, "message_keys": key}, {"_id": 1}):
                return False
            if attempt == 2:
                raise
    return False


def leaderboard(collection, active):
    if not active:
        return []
    rows = list(collection.find(
        {"kind": "entry", "event_id": active["event_id"]},
        {"_id": 0, "username": 1, "user_id": 1, "count": 1, "locks": 1},
    ))
    merged = {}
    for row in rows:
        identity = ("id", row["user_id"]) if row.get("user_id") is not None else ("username", row["username"].lower())
        if identity in merged:
            target = merged[identity]
            target["locks"].extend(row.get("locks", []))
            target["count"] += row.get("count", 0)
            target["username"] = row["username"]
        else:
            merged[identity] = row
    rows = list(merged.values())
    rows.sort(key=lambda row: (-row.get("count", 0),
                               min((as_utc(lock["at"]) for lock in row.get("locks", [])), default=dt.datetime.max.replace(tzinfo=UTC)),
                               row.get("username", "").lower()))
    return rows


def public_payload(collection, user_id, username=None):
    """Safe data for an already authenticated Mini App visitor."""
    current = event(collection)
    rows = leaderboard(collection, current)
    normalized = (username or "").lstrip("@").lower()
    position, own_count = None, 0
    for index, row in enumerate(rows, 1):
        if row.get("user_id") == user_id or (normalized and row["username"].lower() == normalized):
            position, own_count = index, row["count"]
            break
    return {
        "status": current.get("status", "idle") if current else "idle",
        "started_at": as_utc(current["started_at"]).isoformat() if current and current.get("started_at") else None,
        "ends_at": as_utc(current["ends_at"]).isoformat() if current and current.get("ends_at") else None,
        "ended_at": as_utc(current["ended_at"]).isoformat() if current and current.get("ended_at") else None,
        "prizes": [normalize_prize(p) for p in current.get("prizes", [])] if current else prizes(collection),
        "me": {"count": own_count, "position": position},
        "leaders": [
            {"position": index, "username": row["username"], "count": row["count"]}
            for index, row in enumerate(rows, 1)
        ],
    }


def finish(collection, ended_at=None):
    from pymongo import ReturnDocument

    ended_at = as_utc(ended_at or now_utc())
    return collection.find_one_and_update(
        {"_id": "event", "status": "active"},
        {"$set": {"status": "ended", "ended_at": ended_at, "announced": False}},
        return_document=ReturnDocument.AFTER,
    )
