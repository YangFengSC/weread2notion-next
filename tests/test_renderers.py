from weread2notion_next.renderers import content_hash, date_prop, item_key, note_sort_key


def test_content_hash_is_stable_for_key_order():
    left = content_hash({"b": 2, "a": 1})
    right = content_hash({"a": 1, "b": 2})
    assert left == right


def test_item_key_is_stable():
    assert item_key("book1", "bookmark", "mark1") == "book:book1:bookmark:mark1"


def test_note_sort_key_uses_chapter_index_then_range_start():
    chapters = {9: {"chapterIdx": 2}}
    row = {"chapterUid": 9, "range": "45-60", "createTime": 10}
    assert note_sort_key(row, chapters).startswith("0000000002:0000000045")


def test_date_prop_only_adds_timezone_for_datetimes():
    assert date_prop("2026-01-01") == {"date": {"start": "2026-01-01"}}
    assert date_prop("2026-01-01 12:30:00") == {
        "date": {"start": "2026-01-01 12:30:00", "time_zone": "Asia/Shanghai"}
    }
