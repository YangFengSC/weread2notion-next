from dataclasses import replace

from weread2notion_next.models import Book, SyncItem, SyncState
from weread2notion_next.renderers import content_hash
from weread2notion_next.sync_engine import SyncEngine, plan_diff


def make_item(key="k1", content="hello", item_type="bookmark", sort_key="0001"):
    return SyncItem(
        item_key=key,
        book_id="book1",
        item_type=item_type,
        weread_id=key,
        content=content,
        sort_key=sort_key,
        hash_payload={"content": content},
    )


def make_book():
    return Book(book_id="book1", title="Book", sort=100)


class FakeWeRead:
    def __init__(self, items):
        self.items = items

    def list_sync_items(self, book_id):
        return list(self.items)


class FakeWorkspace:
    def __init__(self):
        self.appended = []
        self.updated = []
        self.deleted = []
        self.managed_blocks = []

    def upsert_book(self, book, dry_run=False):
        return "book-page"

    def ensure_managed_area(self, page_id, dry_run=False):
        return "managed-anchor"

    def append_item_block(self, page_id, after, item, dry_run=False):
        block_id = f"block-{item.weread_id}"
        self.appended.append((page_id, after, item.item_key, dry_run))
        return block_id

    def append_item_blocks(self, page_id, items, dry_run=False):
        block_ids = []
        for item in items:
            block_id = f"block-{item.weread_id}"
            self.appended.append((page_id, "batch", item.item_key, dry_run))
            block_ids.append(block_id)
            if not dry_run:
                self.managed_blocks.append({"id": block_id, "type": "callout"})
        return block_ids

    def recover_states_from_managed_area(self, page_id, items):
        return {}

    def managed_item_blocks(self, page_id):
        return list(self.managed_blocks)

    def update_item_block(self, block_id, item, dry_run=False):
        self.updated.append((block_id, item.content, dry_run))

    def delete_block(self, block_id, dry_run=False):
        self.deleted.append((block_id, dry_run))
        if not dry_run:
            self.managed_blocks = [block for block in self.managed_blocks if block["id"] != block_id]


class FakeStateStore:
    def __init__(self):
        self.states = {}
        self.records = []
        self.deleted = []
        self.record_deletes = []

    def list_book_states(self, book_id):
        return dict(self.states)

    def upsert_state(self, state, book_page_id, dry_run=False):
        if not dry_run:
            self.states[state.item_key] = state

    def upsert_item_record(self, state, book_page_id, item, dry_run=False):
        if not dry_run:
            self.records.append((state.item_key, item.content))

    def delete_item_record(self, state, dry_run=False):
        if not dry_run:
            self.record_deletes.append(state.item_key)

    def delete_state(self, state, dry_run=False):
        if not dry_run:
            self.deleted.append(state.item_key)
            self.states.pop(state.item_key, None)

    def replace_book_states(self, book_id, states, dry_run=False):
        if not dry_run:
            self.states = {key: state for key, state in states.items() if not key.startswith("__orphan__:")}


def test_plan_diff_detects_add_update_delete_and_unchanged():
    same = make_item("same", "same")
    changed = make_item("changed", "new")
    stale = SyncState("stale", "book1", "bookmark", "stale", "block-stale", "old", "0001")
    states = {
        "same": SyncState("same", "book1", "bookmark", "same", "block-same", content_hash(same.hash_payload), same.sort_key),
        "changed": SyncState("changed", "book1", "bookmark", "changed", "block-changed", "old", changed.sort_key),
        "stale": stale,
    }
    diff = plan_diff([same, changed, make_item("new", "new")], states)
    assert [item.item_key for item in diff.add] == ["new"]
    assert [item.item_key for item, _ in diff.update] == ["changed"]
    assert [state.item_key for state in diff.delete] == ["stale"]
    assert [item.item_key for item in diff.unchanged] == ["same"]


def test_sync_book_is_idempotent_on_second_run():
    items = [make_item("a", "A"), make_item("b", "B", sort_key="0002")]
    workspace = FakeWorkspace()
    store = FakeStateStore()
    engine = SyncEngine(FakeWeRead(items), workspace, store)

    first = engine.sync_book(make_book())
    second = engine.sync_book(make_book())

    assert first.items_added == 2
    assert second.items_added == 0
    assert second.items_unchanged == 2
    assert len(workspace.appended) == 2
    assert len(store.records) == 4


def test_sync_book_updates_changed_item_without_appending_duplicate():
    item = make_item("a", "A")
    workspace = FakeWorkspace()
    store = FakeStateStore()
    SyncEngine(FakeWeRead([item]), workspace, store).sync_book(make_book())

    changed = replace(item, content="A changed", hash_payload={"content": "A changed"})
    stats = SyncEngine(FakeWeRead([changed]), workspace, store).sync_book(make_book())

    assert stats.items_updated == 1
    assert stats.items_added == 0
    assert len(workspace.appended) == 1
    assert workspace.updated == [("block-a", "A changed", False)]


def test_sync_book_deletes_only_missing_state_blocks():
    first_items = [make_item("a", "A"), make_item("b", "B", sort_key="0002")]
    workspace = FakeWorkspace()
    store = FakeStateStore()
    SyncEngine(FakeWeRead(first_items), workspace, store).sync_book(make_book())

    stats = SyncEngine(FakeWeRead([first_items[0]]), workspace, store).sync_book(make_book())

    assert stats.items_deleted == 1
    assert workspace.deleted == [("block-b", False)]
    assert store.deleted == ["b"]
    assert store.record_deletes == ["b"]


def test_dry_run_does_not_mutate_state():
    item = make_item("a", "A")
    workspace = FakeWorkspace()
    store = FakeStateStore()
    stats = SyncEngine(FakeWeRead([item]), workspace, store).sync_book(make_book(), dry_run=True)

    assert stats.items_added == 1
    assert store.states == {}
    assert workspace.appended == [("book-page", "batch", "a", True)]


def test_sync_book_recovers_missing_sqlite_state_from_managed_blocks():
    item = make_item("a", "A")
    workspace = FakeWorkspace()
    store = FakeStateStore()
    recovered = SyncState("a", "book1", "bookmark", "a", "block-a", content_hash(item.hash_payload), item.sort_key)
    workspace.recover_states_from_managed_area = lambda page_id, items: {"a": recovered}

    stats = SyncEngine(FakeWeRead([item]), workspace, store).sync_book(make_book())

    assert stats.items_added == 0
    assert stats.items_unchanged == 1
    assert workspace.appended == []
    assert store.states["a"].block_id == "block-a"


def test_sync_book_ignores_sqlite_state_when_current_managed_area_is_empty():
    item = make_item("a", "A")
    workspace = FakeWorkspace()
    store = FakeStateStore()
    store.states["a"] = SyncState("a", "book1", "bookmark", "a", "old-block-a", content_hash(item.hash_payload), item.sort_key)

    stats = SyncEngine(FakeWeRead([item]), workspace, store).sync_book(make_book())

    assert stats.items_added == 1
    assert stats.items_unchanged == 0
    assert workspace.appended == [("book-page", "batch", "a", False)]
    assert store.states["a"].block_id == "block-a"
