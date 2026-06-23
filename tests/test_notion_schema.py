from datetime import datetime

import pytest
from httpx import Headers
from notion_client.errors import APIResponseError

from weread2notion_next.notion_schema import (
    AUTHOR_DB,
    DAY_DB,
    MONTH_DB,
    NotionConfigError,
    NotionWorkspace,
    READ_RECORD_DB,
    WEEK_DB,
    YEAR_DB,
    should_retry_notion_exception,
)


def test_transient_notion_api_errors_are_retried():
    exc = APIResponseError("service_unavailable", 503, "down", Headers(), "")

    assert should_retry_notion_exception(exc) is True


def test_validation_notion_api_errors_are_not_retried():
    exc = APIResponseError("validation_error", 400, "bad", Headers(), "")

    assert should_retry_notion_exception(exc) is False

class FakeClient:
    def __init__(self, properties):
        self.properties = properties

    def request(self, path, method="GET", body=None):
        if path == "data_sources/db-author":
            raise APIResponseError("object_not_found", 404, "not found", Headers(), "")
        if path == "databases/db-author":
            return {"data_sources": [{"id": "ds-author"}]}
        if path == "data_sources/ds-author":
            return {"properties": self.properties}
        raise AssertionError(f"unexpected request: {path}")


def test_env_database_id_is_normalized_to_data_source_id(monkeypatch):
    monkeypatch.setenv("NOTION_AUTHOR_DATA_SOURCE_ID", "db-author")
    workspace = NotionWorkspace(FakeClient({"名称": {"type": "title"}}), "38317f12e1658016be5dc3f66f794434")

    assert workspace.normalized_env_data_source_ids() == {AUTHOR_DB: "ds-author"}


def test_env_id_must_match_expected_schema(monkeypatch):
    monkeypatch.setenv("NOTION_AUTHOR_DATA_SOURCE_ID", "db-author")
    workspace = NotionWorkspace(FakeClient({"Name": {"type": "title"}}), "38317f12e1658016be5dc3f66f794434")

    with pytest.raises(NotionConfigError):
        workspace.normalized_env_data_source_ids()


class DailyWorkspace(NotionWorkspace):
    def __init__(self):
        self.created_properties = None
        self._daily_read_page_cache = None
        self._read_record_page_cache = {}
        self._period_page_cache = {}

    def get_or_create_period_page(self, database_name, title, start, end, duration_minutes=None):
        return f"{database_name}:{title}"

    def query_all(self, data_source_id, filter=None, sorts=None):
        return []

    def db(self, name):
        return name

    def data_source_parent(self, name):
        return {"type": "data_source_id", "data_source_id": name}

    def create_page(self, parent, properties, **kwargs):
        self.created_properties = properties
        return {"id": "day-page"}

    def upsert_read_record(self, timestamp, duration, dry_run=False):
        self.read_record_properties = {
            "时间戳": {"number": timestamp},
            "时长": {"number": round(duration / 60, 2)},
        }


def test_daily_read_time_is_written_as_minutes():
    workspace = DailyWorkspace()

    workspace.upsert_daily_read_time(timestamp=1767225600, duration=120)

    assert workspace.created_properties["日期"]["date"]["start"] == "2026-01-01"
    assert workspace.created_properties["时间戳"]["number"] == 1767225600
    assert workspace.created_properties["时长"]["number"] == 2
    assert workspace.read_record_properties["时长"]["number"] == 2


class RollupWorkspace(NotionWorkspace):
    def __init__(self):
        self.rollups = []

    def db(self, name):
        return name

    def query_all(self, data_source_id, filter=None, sorts=None):
        if data_source_id != DAY_DB:
            return []
        return [
            {
                "properties": {
                    "日期": {"type": "date", "date": {"start": "2026-01-01"}},
                    "时长": {"type": "number", "number": 10},
                }
            },
            {
                "properties": {
                    "日期": {"type": "date", "date": {"start": "2026-01-02"}},
                    "时长": {"type": "number", "number": 15},
                }
            },
        ]

    def get_or_create_period_page(self, database_name, title, start, end, duration_minutes=None):
        self.rollups.append((database_name, title, duration_minutes))
        return f"{database_name}:{title}"


def test_reading_time_rollups_write_week_month_and_year_duration():
    workspace = RollupWorkspace()

    workspace.refresh_reading_time_rollups()

    assert (YEAR_DB, "2026", 25.0) in workspace.rollups
    assert (MONTH_DB, "2026年01月", 25.0) in workspace.rollups
    assert (WEEK_DB, "2026年第1周", 25.0) in workspace.rollups


class CachedPeriodWorkspace(NotionWorkspace):
    def __init__(self):
        self._period_page_cache = {(YEAR_DB, "2026"): "year-page"}
        self.updated_properties = None

    def update_page(self, **kwargs):
        self.updated_properties = kwargs["properties"]


def test_cached_period_page_is_updated_when_duration_is_provided():
    workspace = CachedPeriodWorkspace()

    page_id = workspace.get_or_create_period_page(
        YEAR_DB,
        "2026",
        start=datetime(2026, 1, 1),
        end=datetime(2026, 12, 31),
        duration_minutes=42,
    )

    assert page_id == "year-page"
    assert workspace.updated_properties["时长"]["number"] == 42
