from __future__ import annotations

from dataclasses import dataclass

from .models import Book, SyncItem, SyncState, SyncStats
from .notion_schema import NotionConfigError, NotionWorkspace, required_database_names
from .renderers import content_hash
from .state_store import StateStore
from .weread_gateway import WeReadService


@dataclass
class DiffPlan:
    add: list[SyncItem]
    update: list[tuple[SyncItem, SyncState]]
    delete: list[SyncState]
    unchanged: list[SyncItem]


def plan_diff(items: list[SyncItem], states: dict[str, SyncState]) -> DiffPlan:
    current = {item.item_key: item for item in items}
    add: list[SyncItem] = []
    update: list[tuple[SyncItem, SyncState]] = []
    unchanged: list[SyncItem] = []
    for item in items:
        state = states.get(item.item_key)
        digest = content_hash(item.hash_payload)
        if not state:
            add.append(item)
        elif state.content_hash != digest or state.sort_key != item.sort_key:
            update.append((item, state))
        else:
            unchanged.append(item)
    delete = [state for key, state in states.items() if key not in current]
    return DiffPlan(add=add, update=update, delete=delete, unchanged=unchanged)


class SyncEngine:
    def __init__(self, weread: WeReadService, workspace: NotionWorkspace, state_store: StateStore | None = None):
        self.weread = weread
        self.workspace = workspace
        self.state_store = state_store or StateStore(workspace)

    def init(self) -> dict[str, str]:
        return self.workspace.ensure_template()

    def doctor(self) -> list[str]:
        checks = self.workspace.doctor()
        if not self.workspace.data_source_ids():
            self.workspace.find_databases()
        return checks

    def sync(
        self,
        dry_run: bool = False,
        force: bool = False,
        limit: int | None = None,
        reading_time: bool = False,
        reading_years: list[int] | None = None,
        all_reading_years: bool = False,
        books_only: bool = False,
    ) -> SyncStats:
        self.workspace.require_template()
        stats = SyncStats()
        latest_sort = self.workspace.latest_sort()
        books = self.weread.list_books()
        if limit is not None and limit > 0:
            books = books[-limit:]
        stats.books_seen = len(books)
        for book in books:
            if not force and book.sort <= latest_sort and self.workspace.get_book_page(book.book_id):
                stats.books_skipped += 1
                continue
            enriched = self.weread.enrich_book(book)
            if books_only:
                self.workspace.upsert_book(enriched, dry_run=dry_run)
                stats.books_synced += 1
            else:
                self.sync_book(enriched, stats, dry_run=dry_run)
        if not dry_run and reading_time:
            self.sync_reading_time(
                dry_run=dry_run,
                years=reading_years,
                all_years=all_reading_years,
            )
        return stats

    def sync_reading_time(
        self,
        dry_run: bool = False,
        years: list[int] | None = None,
        all_years: bool = False,
    ) -> None:
        if not hasattr(self.weread, "list_daily_read_times"):
            return
        if all_years and hasattr(self.weread, "list_reading_years"):
            years = self.weread.list_reading_years()
        years = years or [None]
        for year in years:
            for bucket in self.weread.list_daily_read_times(year):
                self.workspace.upsert_daily_read_time(bucket.timestamp, bucket.duration, dry_run=dry_run)

    def sync_book(self, book: Book, stats: SyncStats | None = None, dry_run: bool = False) -> SyncStats:
        stats = stats or SyncStats(books_seen=1)
        book_page_id = self.workspace.upsert_book(book, dry_run=dry_run)
        managed_anchor = self.workspace.ensure_managed_area(book_page_id, dry_run=dry_run)
        states = self.state_store.list_book_states(book.book_id)
        items = self.weread.list_sync_items(book.book_id) if self.should_sync_items(book, states) else []
        if states and self.should_recover_states(book_page_id):
            states = {}
        if not states and not book_page_id.startswith("dry-run-book-"):
            states = self.workspace.recover_states_from_managed_area(book_page_id, items)
            self.state_store.replace_book_states(book.book_id, states, dry_run=dry_run)
        diff = plan_diff(items, states)
        ordered_existing = {state.item_key: state for state in states.values()}
        add_keys = {item.item_key for item in diff.add}
        add_items = [item for item in items if item.item_key in add_keys]
        block_ids = self.workspace.append_item_blocks(book_page_id, add_items, dry_run=dry_run)
        for item, block_id in zip(add_items, block_ids):
            state = SyncState(
                item_key=item.item_key,
                book_id=item.book_id,
                item_type=item.item_type,
                weread_id=item.weread_id,
                block_id=block_id,
                content_hash=content_hash(item.hash_payload),
                sort_key=item.sort_key,
            )
            self.state_store.upsert_state(state, book_page_id, dry_run=dry_run)
            self.state_store.upsert_item_record(state, book_page_id, item, dry_run=dry_run)
        for item, state in diff.update:
            self.workspace.update_item_block(state.block_id, item, dry_run=dry_run)
            state.content_hash = content_hash(item.hash_payload)
            state.sort_key = item.sort_key
            self.state_store.upsert_state(state, book_page_id, dry_run=dry_run)
            self.state_store.upsert_item_record(state, book_page_id, item, dry_run=dry_run)
        for item in diff.unchanged:
            state = states[item.item_key]
            self.state_store.upsert_item_record(state, book_page_id, item, dry_run=dry_run)
        for state in diff.delete:
            self.workspace.delete_block(state.block_id, dry_run=dry_run)
            self.state_store.delete_item_record(state, dry_run=dry_run)
            self.state_store.delete_state(state, dry_run=dry_run)
        stats.books_synced += 1
        stats.items_added += len(diff.add)
        stats.items_updated += len(diff.update)
        stats.items_deleted += len(diff.delete)
        stats.items_unchanged += len(diff.unchanged)
        return stats

    def should_recover_states(self, book_page_id: str) -> bool:
        if book_page_id.startswith("dry-run-book-"):
            return True
        return not self.workspace.managed_item_blocks(book_page_id)

    def should_sync_items(self, book: Book, states: dict[str, SyncState]) -> bool:
        if book.source_type != "book":
            return bool(states)
        counts = (book.note_count, book.review_count, book.bookmark_count)
        if all(count is None for count in counts):
            return True
        return bool(states) or any((count or 0) > 0 for count in counts)
