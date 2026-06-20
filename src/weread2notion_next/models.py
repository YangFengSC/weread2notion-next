from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Book:
    book_id: str
    title: str
    sort: int | float
    author: str = ""
    cover: str = ""
    isbn: str = ""
    rating: int | float | None = None
    categories: tuple[str, ...] = ()
    url: str = ""
    progress: float | None = None
    reading_time: int | float | None = None
    reading_days: int | float | None = None
    status: str = ""
    finished_at: str = ""
    started_at: str = ""
    last_read_at: str = ""
    intro: str = ""
    shelf_category: str = ""
    source_type: str = "book"
    is_top: bool = False
    is_secret: bool = False
    note_count: int | float | None = None
    review_count: int | float | None = None
    bookmark_count: int | float | None = None
    my_rating: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncItem:
    item_key: str
    book_id: str
    item_type: str
    weread_id: str
    content: str
    sort_key: str
    hash_payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncState:
    item_key: str
    book_id: str
    item_type: str
    weread_id: str
    block_id: str
    content_hash: str
    sort_key: str
    page_id: str | None = None


@dataclass
class SyncStats:
    books_seen: int = 0
    books_synced: int = 0
    books_skipped: int = 0
    items_added: int = 0
    items_updated: int = 0
    items_deleted: int = 0
    items_unchanged: int = 0

    def summary(self) -> str:
        return (
            f"books seen={self.books_seen}, synced={self.books_synced}, skipped={self.books_skipped}; "
            f"items added={self.items_added}, updated={self.items_updated}, "
            f"deleted={self.items_deleted}, unchanged={self.items_unchanged}"
        )


@dataclass(frozen=True)
class ReadTimeBucket:
    timestamp: int
    duration: int
