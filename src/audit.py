from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    """解析ISO时间；无时区后缀的朴素时间按UTC处理，Z后缀转换为+00:00。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间必须是ISO8601字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("时间不是有效的ISO8601格式") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_iso(value: str) -> str:
    """统一为秒级UTC ISO字符串，使生效区间与测量时刻可按字符串比较。"""
    return parse_iso(value).astimezone(timezone.utc).replace(microsecond=0).isoformat()


def calculate_hash(previous_hash: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256((previous_hash + ":").encode("utf-8") + raw).hexdigest()


def make_entry(action: str, entity_type: str, entity_id: int, actor: str,
               detail: dict, previous_hash: str) -> dict:
    payload = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor": actor,
        "detail": detail,
        "created_at": utc_now(),
    }
    return dict(payload, previous_hash=previous_hash,
                entry_hash=calculate_hash(previous_hash, payload))
