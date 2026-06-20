import pytest
from httpx import Headers
from notion_client.errors import APIResponseError

from weread2notion_next.notion_schema import AUTHOR_DB, DAY_DB, NotionConfigError, NotionWorkspace


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
        self._period_page_cache = {}

    def get_or_create_period_page(self, database_name, title, start, end):
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


def test_daily_read_time_is_written_as_minutes():
    workspace = DailyWorkspace()

    workspace.upsert_daily_read_time(timestamp=1767225600, duration=120)

    assert workspace.created_properties["日期"]["date"]["start"] == "2026-01-01"
    assert workspace.created_properties["时间戳"]["number"] == 1767225600
    assert workspace.created_properties["时长"]["number"] == 2
