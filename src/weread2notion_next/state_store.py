from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from notion_client.errors import APIResponseError

from .models import SyncItem, SyncState
from .notion_schema import BOOKMARK_DB, CHAPTER_DB, REVIEW_DB, NotionWorkspace, property_value
from .renderers import date_prop, number_prop, rich_text_prop, title_prop

DEFAULT_CACHE_DIR = Path(".weread2notion-cache")
DEFAULT_DB_NAME = "state.sqlite3"
ORPHAN_PREFIX = "__orphan__:"


class StateStore:
    def __init__(self, workspace: NotionWorkspace, cache_dir: Path | None = None, db_path: Path | None = None):
        self.workspace = workspace
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        configured_db = os.getenv("WEREAD_STATE_DB")
        self.db_path = db_path or (Path(configured_db) if configured_db else self.cache_dir / DEFAULT_DB_NAME)
        self._property_cache: dict[str, set[str]] = {}
        self._item_record_cache: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}

    def list_book_states(self, book_id: str) -> dict[str, SyncState]:
        self.ensure_schema()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT item_key, book_id, item_type, weread_id, block_id, content_hash, sort_key
                FROM sync_state
                WHERE book_id = ?
                """,
                (book_id,),
            ).fetchall()
        states = {
            row["item_key"]: SyncState(
                item_key=row["item_key"],
                book_id=row["book_id"],
                item_type=row["item_type"],
                weread_id=row["weread_id"],
                block_id=row["block_id"],
                content_hash=row["content_hash"],
                sort_key=row["sort_key"],
            )
            for row in rows
        }
        self.write_cache(book_id, [state.__dict__ for state in states.values()])
        return states

    def replace_book_states(self, book_id: str, states: dict[str, SyncState], dry_run: bool = False) -> None:
        if dry_run:
            return
        self.ensure_schema()
        with self.connect() as db:
            db.execute("DELETE FROM sync_state WHERE book_id = ?", (book_id,))
            for state in states.values():
                if state.item_key.startswith(ORPHAN_PREFIX):
                    continue
                self.upsert_state_row(db, state)

    def upsert_state(self, state: SyncState, book_page_id: str, dry_run: bool = False) -> None:
        if dry_run or state.item_key.startswith(ORPHAN_PREFIX):
            return
        self.ensure_schema()
        with self.connect() as db:
            self.upsert_state_row(db, state)

    def delete_state(self, state: SyncState, dry_run: bool = False) -> None:
        if dry_run or state.item_key.startswith(ORPHAN_PREFIX):
            return
        self.ensure_schema()
        with self.connect() as db:
            db.execute("DELETE FROM sync_state WHERE item_key = ?", (state.item_key,))

    def upsert_item_record(self, state: SyncState, book_page_id: str, item: SyncItem, dry_run: bool = False) -> None:
        if dry_run or not self.should_write_item_records():
            return
        database_name = {"bookmark": BOOKMARK_DB, "review": REVIEW_DB, "chapter": CHAPTER_DB}.get(state.item_type)
        if not database_name:
            return
        id_prop = "bookmarkId" if state.item_type == "bookmark" else "reviewId" if state.item_type == "review" else "chapterUid"
        properties = {
            "Name": title_prop(item.content),
            "bookId": rich_text_prop(state.book_id),
            id_prop: rich_text_prop(state.weread_id),
            "blockId": rich_text_prop(state.block_id),
            "sortKey": rich_text_prop(state.sort_key),
            "书籍": {"relation": [{"id": book_page_id}]},
        }
        properties.update(item_metadata_properties(item))
        properties = self.filter_existing_properties(database_name, properties)
        existing = self.get_cached_item_record(database_name, state.item_type, state.book_id, state.weread_id)
        if existing:
            try:
                self.workspace.update_page(page_id=existing["id"], properties=properties)
            except APIResponseError as exc:
                if "is not a property that exists" not in str(exc):
                    raise
                self.workspace.repair_schema()
                self.workspace.update_page(page_id=existing["id"], properties=properties)
            return
        try:
            created = self.workspace.create_page(parent=self.workspace.data_source_parent(database_name), properties=properties)
        except APIResponseError as exc:
            if "is not a property that exists" not in str(exc):
                raise
            self.workspace.repair_schema()
            created = self.workspace.create_page(parent=self.workspace.data_source_parent(database_name), properties=self.filter_existing_properties(database_name, properties))
        self.cache_item_record(database_name, state.item_type, state.book_id, state.weread_id, created)

    def delete_item_record(self, state: SyncState, dry_run: bool = False) -> None:
        if dry_run or not self.should_write_item_records():
            return
        database_name = {"bookmark": BOOKMARK_DB, "review": REVIEW_DB, "chapter": CHAPTER_DB}.get(state.item_type)
        if not database_name:
            return
        existing = self.get_cached_item_record(database_name, state.item_type, state.book_id, state.weread_id)
        if existing:
            self.workspace.delete_block(existing["id"], dry_run=dry_run)

    def should_write_item_records(self) -> bool:
        return os.getenv("WEREAD_WRITE_ITEM_RECORDS", "1") != "0"

    def filter_existing_properties(self, database_name: str, properties: dict[str, Any]) -> dict[str, Any]:
        if database_name not in self._property_cache:
            try:
                response = self.workspace.request(path=f"data_sources/{self.workspace.db(database_name)}", method="GET")
            except Exception:
                return properties
            self._property_cache[database_name] = set((response.get("properties") or {}).keys())
        existing = self._property_cache[database_name]
        return {key: value for key, value in properties.items() if key in existing}

    def get_cached_item_record(self, database_name: str, item_type: str, book_id: str, weread_id: str) -> dict[str, Any] | None:
        cache_key = (database_name, item_type, book_id)
        if cache_key not in self._item_record_cache:
            id_prop = "bookmarkId" if item_type == "bookmark" else "reviewId" if item_type == "review" else "chapterUid"
            rows = self.workspace.query_all(
                self.workspace.db(database_name),
                filter={"property": "bookId", "rich_text": {"equals": book_id}},
            )
            self._item_record_cache[cache_key] = {
                str(property_value((row.get("properties") or {}).get(id_prop))): row
                for row in rows
                if property_value((row.get("properties") or {}).get(id_prop)) is not None
            }
        return self._item_record_cache[cache_key].get(str(weread_id))

    def cache_item_record(
        self,
        database_name: str,
        item_type: str,
        book_id: str,
        weread_id: str,
        page: dict[str, Any],
    ) -> None:
        cache_key = (database_name, item_type, book_id)
        self._item_record_cache.setdefault(cache_key, {})[str(weread_id)] = page

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def ensure_schema(self) -> None:
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    item_key TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    weread_id TEXT NOT NULL,
                    block_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    sort_key TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_sync_state_book_id ON sync_state(book_id)")

    def upsert_state_row(self, db: sqlite3.Connection, state: SyncState) -> None:
        db.execute(
            """
            INSERT INTO sync_state (
                item_key, book_id, item_type, weread_id, block_id, content_hash, sort_key, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_key) DO UPDATE SET
                book_id = excluded.book_id,
                item_type = excluded.item_type,
                weread_id = excluded.weread_id,
                block_id = excluded.block_id,
                content_hash = excluded.content_hash,
                sort_key = excluded.sort_key,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                state.item_key,
                state.book_id,
                state.item_type,
                state.weread_id,
                state.block_id,
                state.content_hash,
                state.sort_key,
            ),
        )

    def write_cache(self, book_id: str, payload: Any) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{book_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def read_cache(self, book_id: str) -> Any | None:
        try:
            path = self.cache_dir / f"{book_id}.json"
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


def item_metadata_properties(item: SyncItem) -> dict[str, Any]:
    metadata = item.metadata or {}
    properties: dict[str, Any] = {}
    if metadata.get("range") is not None:
        properties["range"] = rich_text_prop(str(metadata.get("range") or ""))
    if item.item_type != "chapter" and metadata.get("chapterUid") is not None:
        properties["chapterUid"] = number_prop(to_number(metadata.get("chapterUid")))
    if metadata.get("createTime") is not None:
        properties["Date"] = date_prop(timestamp_to_iso(metadata.get("createTime")))
    if metadata.get("style") is not None:
        properties["style"] = number_prop(to_number(metadata.get("style")))
    if metadata.get("colorStyle") is not None:
        properties["colorStyle"] = number_prop(to_number(metadata.get("colorStyle")))
    if metadata.get("star") is not None:
        properties["star"] = number_prop(to_number(metadata.get("star")))
    if metadata.get("abstract") is not None:
        properties["abstract"] = rich_text_prop(str(metadata.get("abstract") or ""))
    if metadata.get("chapterIdx") is not None:
        properties["chapterIdx"] = number_prop(to_number(metadata.get("chapterIdx")))
    if metadata.get("level") is not None:
        properties["level"] = number_prop(to_number(metadata.get("level")))
    if metadata.get("updateTime") is not None:
        properties["updateTime"] = number_prop(to_number(metadata.get("updateTime")))
    return properties


def to_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def timestamp_to_iso(value: Any) -> str:
    number = to_number(value)
    if not number:
        return ""
    return datetime.utcfromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
