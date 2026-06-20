from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .models import SyncItem

MANAGED_HEADING = "微信读书同步"
TIMEZONE = "Asia/Shanghai"


def rich_text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content[:2000]}}]


def title_prop(content: str) -> dict[str, Any]:
    return {"title": rich_text(content)}


def rich_text_prop(content: str) -> dict[str, Any]:
    return {"rich_text": rich_text(content)}


def number_prop(value: int | float | None) -> dict[str, Any]:
    return {"number": value}


def checkbox_prop(value: bool) -> dict[str, Any]:
    return {"checkbox": bool(value)}


def url_prop(value: str) -> dict[str, Any]:
    return {"url": value or None}


def select_prop(value: str) -> dict[str, Any]:
    return {"select": {"name": value} if value else None}


def status_prop(value: str) -> dict[str, Any]:
    return {"status": {"name": value} if value else None}


def multi_select_prop(values: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return {"multi_select": [{"name": value} for value in values if value]}


def relation_prop(page_ids: list[str]) -> dict[str, Any]:
    return {"relation": [{"id": page_id} for page_id in page_ids if page_id]}


def date_prop(value: str) -> dict[str, Any]:
    if not value:
        return {"date": None}
    body = {"start": value}
    if "T" in value or " " in value:
        body["time_zone"] = TIMEZONE
    return {"date": body}


def external_icon(url: str) -> dict[str, Any]:
    return {"type": "external", "external": {"url": url}}


def emoji_icon(emoji: str) -> dict[str, Any]:
    return {"type": "emoji", "emoji": emoji}


def heading(level: int, content: str) -> dict[str, Any]:
    block_type = "heading_1" if level == 1 else "heading_2" if level == 2 else "heading_3"
    return {
        "type": block_type,
        block_type: {
            "rich_text": rich_text(content),
            "color": "default",
            "is_toggleable": False,
        },
    }


def divider() -> dict[str, Any]:
    return {"type": "divider", "divider": {}}


def callout(content: str, emoji: str = "〰️", color: str = "default") -> dict[str, Any]:
    return {
        "type": "callout",
        "callout": {
            "rich_text": rich_text(content),
            "icon": emoji_icon(emoji),
            "color": color,
        },
    }


def quote(content: str) -> dict[str, Any]:
    return {"type": "quote", "quote": {"rich_text": rich_text(content), "color": "default"}}


def content_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def item_key(book_id: str, item_type: str, weread_id: str) -> str:
    return f"book:{book_id}:{item_type}:{weread_id}"


def range_start(item: dict[str, Any]) -> int:
    raw = item.get("range") or ""
    try:
        return int(str(raw).split("-")[0] or 0)
    except (TypeError, ValueError):
        return 0


def note_sort_key(item: dict[str, Any], chapters: dict[Any, dict[str, Any]] | None = None) -> str:
    chapter_uid = item.get("chapterUid", 1)
    chapter = (chapters or {}).get(chapter_uid) or (chapters or {}).get(str(chapter_uid)) or {}
    chapter_idx = chapter.get("chapterIdx", chapter_uid or 0)
    return f"{int(chapter_idx):010d}:{range_start(item):010d}:{item.get('createTime') or 0}"


def item_block(sync_item: SyncItem) -> dict[str, Any]:
    if sync_item.item_type == "chapter":
        return heading(sync_item.metadata.get("level", 1), sync_item.content)
    if sync_item.item_type == "review":
        return callout(sync_item.content, emoji="✍️")
    return callout(sync_item.content, emoji="〰️")


def updated_at_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
