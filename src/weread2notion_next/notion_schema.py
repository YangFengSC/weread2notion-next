from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from notion_client import Client
from notion_client.errors import APIResponseError
from retrying import retry

from .models import Book, SyncItem, SyncState
from .renderers import (
    MANAGED_HEADING,
    checkbox_prop,
    callout,
    content_hash,
    date_prop,
    divider,
    external_icon,
    heading,
    item_block,
    multi_select_prop,
    number_prop,
    relation_prop,
    rich_text_prop,
    select_prop,
    title_prop,
    updated_at_iso,
    url_prop,
)

NOTION_VERSION = "2026-03-11"
DATABASE_PREFIX = "微信读书数据库"
LEGACY_DATABASE_PREFIXES = ("WeReadNext v3", "WeReadenext v3")


def database_name(suffix: str) -> str:
    return f"{DATABASE_PREFIX} {suffix}"


BOOK_DB = database_name("书架")
BOOKMARK_DB = database_name("划线")
REVIEW_DB = database_name("笔记")
CHAPTER_DB = database_name("章节")
AUTHOR_DB = database_name("作者")
CATEGORY_DB = database_name("分类")
DAY_DB = database_name("日")
WEEK_DB = database_name("周")
MONTH_DB = database_name("月")
YEAR_DB = database_name("年")
READ_RECORD_DB = database_name("阅读记录")
STATE_DB = database_name("同步状态")
SETTING_DB = database_name("设置")
HOME_PAGE_TITLE = "微信读书"
LEGACY_HOME_PAGE_TITLE = "WeReadNext 首页"
DATA_PAGE_TITLE = "系统数据"
HOME_PAGE_VERSION_TEXT = "微信读书首页版本：6"
BOOK_ICON = "https://www.notion.so/icons/book_gray.svg"
TAG_ICON = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON = "https://www.notion.so/icons/user-circle-filled_gray.svg"
SYNC_ICON = "https://www.notion.so/icons/refresh_gray.svg"
TARGET_ICON_URL = "https://www.notion.so/icons/target_gray.svg"
TRANSIENT_NOTION_ERROR_CODES = {
    "bad_gateway",
    "gateway_timeout",
    "internal_server_error",
    "rate_limited",
    "request_timeout",
    "service_unavailable",
}
TRANSIENT_NOTION_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def should_retry_notion_exception(exc: Exception) -> bool:
    if not isinstance(exc, APIResponseError):
        return True

    code = getattr(exc, "code", None)
    if hasattr(code, "value"):
        code = code.value
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return code in TRANSIENT_NOTION_ERROR_CODES or status in TRANSIENT_NOTION_STATUS_CODES


NOTION_RETRY = {
    "stop_max_attempt_number": 5,
    "wait_exponential_multiplier": 1000,
    "wait_exponential_max": 15000,
    "retry_on_exception": should_retry_notion_exception,
}
ENV_DATA_SOURCE_MAPPING = {
    BOOK_DB: "NOTION_BOOK_DATA_SOURCE_ID",
    BOOKMARK_DB: "NOTION_BOOKMARK_DATA_SOURCE_ID",
    REVIEW_DB: "NOTION_REVIEW_DATA_SOURCE_ID",
    CHAPTER_DB: "NOTION_CHAPTER_DATA_SOURCE_ID",
    AUTHOR_DB: "NOTION_AUTHOR_DATA_SOURCE_ID",
    CATEGORY_DB: "NOTION_CATEGORY_DATA_SOURCE_ID",
    DAY_DB: "NOTION_DAY_DATA_SOURCE_ID",
    WEEK_DB: "NOTION_WEEK_DATA_SOURCE_ID",
    MONTH_DB: "NOTION_MONTH_DATA_SOURCE_ID",
    YEAR_DB: "NOTION_YEAR_DATA_SOURCE_ID",
    READ_RECORD_DB: "NOTION_READ_RECORD_DATA_SOURCE_ID",
}
SELECT_OPTIONS = {
    (BOOK_DB, "阅读状态"): [
        ("想读", "gray"),
        ("在读", "blue"),
        ("已读", "green"),
        ("收藏", "purple"),
        ("已完结", "green"),
    ],
    (BOOK_DB, "来源"): [
        ("book", "blue"),
        ("album", "purple"),
        ("mp", "orange"),
    ],
    (BOOK_DB, "我的评分"): [
        ("1 星", "red"),
        ("2 星", "orange"),
        ("3 星", "yellow"),
        ("4 星", "blue"),
        ("5 星", "green"),
    ],
}


class NotionConfigError(RuntimeError):
    pass


def extract_notion_id(value: str | None) -> str:
    if not value:
        raise NotionConfigError("Missing NOTION_PAGE")
    match = re.search(
        r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
        value,
        re.IGNORECASE,
    )
    if not match:
        raise NotionConfigError("NOTION_PAGE must be a Notion page URL or page id")
    return match.group(1)


def normalize_notion_id(value: str | None) -> str:
    return (value or "").replace("-", "").lower()


def client_from_env() -> Client:
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise NotionConfigError("Missing NOTION_TOKEN")
    return Client(auth=token, notion_version=NOTION_VERSION)


class NotionWorkspace:
    def __init__(self, client: Client, page_id: str):
        self.client = client
        self.page_id = extract_notion_id(page_id)
        self._data_source_ids: dict[str, str] = {}
        self._simple_page_cache: dict[tuple[str, str], str] = {}
        self._home_page_id: str | None = None
        self._database_parent_page_id: str | None = None
        self._period_page_cache: dict[tuple[str, str], str] = {}
        self._daily_read_page_cache: dict[int, dict[str, Any]] | None = None

    @classmethod
    def from_env(cls) -> "NotionWorkspace":
        return cls(client_from_env(), os.getenv("NOTION_PAGE"))

    def doctor(self) -> list[str]:
        checks: list[str] = []
        self.retrieve_page(page_id=self.page_id)
        checks.append("Notion page access: ok")
        try:
            self.request(path=f"blocks/{self.page_id}/children", method="GET")
            checks.append(f"Notion API version {NOTION_VERSION}: ok")
        except APIResponseError as exc:
            raise NotionConfigError(f"Notion API check failed: {exc}") from exc
        pinned = self.normalized_env_data_source_ids(strict=False)
        if pinned:
            checks.append(f"Usable pinned data sources: {len(pinned)}/{len(required_database_names())}")
        existing = {**pinned, **self.find_databases()}
        missing = [name for name in required_database_names() if name not in existing]
        if missing:
            checks.append(f"Template missing: {', '.join(missing)}")
        else:
            checks.append("Template databases: ok")
        duplicates = self.duplicate_database_titles()
        if duplicates:
            detail = ", ".join(f"{name} x{len(ids)}" for name, ids in duplicates.items())
            checks.append(f"Duplicate template databases found: {detail}")
        else:
            checks.append("Duplicate template databases: none")
        return checks

    def ensure_template(self) -> dict[str, str]:
        pinned = self.normalized_env_data_source_ids(strict=False)
        self._data_source_ids = {**pinned, **self.find_databases()}
        for name in (AUTHOR_DB, CATEGORY_DB):
            if name not in self._data_source_ids:
                self._data_source_ids[name] = self.create_simple_database(name)
        if BOOK_DB not in self._data_source_ids:
            self._data_source_ids[BOOK_DB] = self.create_book_database()
        for name in (YEAR_DB, MONTH_DB, WEEK_DB):
            if name not in self._data_source_ids:
                self._data_source_ids[name] = self.create_period_database(name)
        if DAY_DB not in self._data_source_ids:
            self._data_source_ids[DAY_DB] = self.create_day_database()
        if READ_RECORD_DB not in self._data_source_ids:
            self._data_source_ids[READ_RECORD_DB] = self.create_read_record_database()
        for name in (BOOKMARK_DB, REVIEW_DB, CHAPTER_DB):
            if name not in self._data_source_ids:
                self._data_source_ids[name] = self.create_item_database(name)
        self.ensure_database_titles()
        self.repair_schema()
        return dict(self._data_source_ids)

    def require_template(self) -> dict[str, str]:
        pinned = self.normalized_env_data_source_ids(strict=False)
        self._data_source_ids = {**pinned, **self.find_databases()}
        missing = [name for name in required_database_names() if name not in self._data_source_ids]
        if missing:
            raise NotionConfigError(
                "Notion template is not ready. Run `weread2notion-next init` once, "
                "Missing: " + ", ".join(missing)
            )
        self.repair_schema()
        return dict(self._data_source_ids)

    def repair_schema(self) -> None:
        for name, expected in expected_properties().items():
            if name not in self._data_source_ids:
                continue
            response = self.request(path=f"data_sources/{self._data_source_ids[name]}", method="GET")
            properties = response.get("properties") or {}
            updates = {
                prop_name: schema_for_property_type(prop_type)
                for prop_name, prop_type in expected.items()
                if prop_name not in properties
            }
            updates.update(select_option_updates(name, properties))
            if updates:
                self.update_data_source(data_source_id=self._data_source_ids[name], properties=updates)

    def ensure_views(self) -> None:
        view_specs = [
            (BOOK_DB, "书架画廊", "gallery", None, [{"property": "Sort", "direction": "descending"}]),
            (BOOK_DB, "最近阅读", "table", None, [{"property": "最后阅读时间", "direction": "descending"}]),
            (BOOK_DB, "按阅读状态", "board", None, [{"property": "Sort", "direction": "descending"}]),
            (BOOK_DB, "在读", "gallery", {"property": "阅读状态", "select": {"equals": "在读"}}, [{"property": "最后阅读时间", "direction": "descending"}]),
            (BOOK_DB, "已读", "gallery", {"property": "阅读状态", "select": {"equals": "已读"}}, [{"property": "时间", "direction": "descending"}]),
            (BOOK_DB, "有笔记", "gallery", {"or": [{"property": "划线数", "number": {"greater_than": 0}}, {"property": "笔记数", "number": {"greater_than": 0}}]}, [{"property": "Sort", "direction": "descending"}]),
            (BOOKMARK_DB, "最新划线", "list", None, [{"property": "Date", "direction": "descending"}]),
            (REVIEW_DB, "最新笔记", "list", None, [{"property": "Date", "direction": "descending"}]),
            (CHAPTER_DB, "章节目录", "table", None, [{"property": "sortKey", "direction": "ascending"}]),
            (DAY_DB, "日统计", "table", None, [{"property": "时间戳", "direction": "descending"}]),
            (WEEK_DB, "周统计", "table", None, [{"property": "日期", "direction": "descending"}]),
            (MONTH_DB, "月统计", "table", None, [{"property": "日期", "direction": "descending"}]),
            (YEAR_DB, "年统计", "table", None, [{"property": "日期", "direction": "descending"}]),
        ]
        for database_name, view_name, view_type, view_filter, sorts in view_specs:
            self.ensure_view(self.db(database_name), view_name, view_type, view_filter, sorts)

    def ensure_view(
        self,
        data_source_id: str,
        name: str,
        view_type: str,
        view_filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            existing = self.list_views(data_source_id=data_source_id)
            for view in existing.get("results") or []:
                view_name = view.get("name")
                if not view_name and view.get("id"):
                    view_name = self.retrieve_view(view_id=view["id"]).get("name")
                if view_name == name:
                    return
            self.create_view(
                data_source_id=data_source_id,
                database_id=self.database_id_for_data_source(data_source_id),
                name=name,
                type=view_type,
                filter=view_filter,
                sorts=sorts,
            )
        except APIResponseError:
            return

    def ensure_homepage(self, page_id: str | None = None) -> str:
        page_id = page_id or self.find_or_create_homepage()
        texts = {block_plain_text(child) for child in self.list_block_children(page_id)}
        if HOME_PAGE_VERSION_TEXT not in texts:
            self.rebuild_homepage(page_id)
        self.refresh_homepage_stats(page_id)
        return page_id

    def find_or_create_homepage(self) -> str:
        if self._home_page_id:
            return self._home_page_id
        for child in self.list_block_children(self.page_id):
            if child.get("type") != "child_page":
                continue
            title = (child.get("child_page") or {}).get("title")
            if title == HOME_PAGE_TITLE:
                self._home_page_id = child["id"]
                return child["id"]
            if title == LEGACY_HOME_PAGE_TITLE:
                self.update_page(page_id=child["id"], properties={"title": title_prop(HOME_PAGE_TITLE)})
                self._home_page_id = child["id"]
                return child["id"]
        response = self.create_page(
            parent={"type": "page_id", "page_id": self.page_id},
            properties={"title": title_prop(HOME_PAGE_TITLE)},
            icon={"type": "emoji", "emoji": "📚"},
        )
        self._home_page_id = response["id"]
        return response["id"]

    def find_or_create_data_page(self, home_page_id: str) -> str:
        if self._database_parent_page_id:
            return self._database_parent_page_id
        for child in self.list_block_children(home_page_id):
            if child.get("type") == "child_page" and (child.get("child_page") or {}).get("title") == DATA_PAGE_TITLE:
                self._database_parent_page_id = child["id"]
                return child["id"]
        response = self.create_page(
            parent={"type": "page_id", "page_id": home_page_id},
            properties={"title": title_prop(DATA_PAGE_TITLE)},
            icon={"type": "emoji", "emoji": "🗄️"},
        )
        self._database_parent_page_id = response["id"]
        return response["id"]

    def ensure_database_location(self) -> None:
        if not self._database_parent_page_id:
            return
        for data_source_id in self._data_source_ids.values():
            try:
                database_id = self.database_id_for_data_source(data_source_id)
                database = self.request(path=f"databases/{database_id}", method="GET")
            except APIResponseError:
                continue
            parent = database.get("parent") or {}
            if normalize_notion_id(parent.get("page_id")) == normalize_notion_id(self._database_parent_page_id):
                continue
            self.client.databases.update(
                database_id=database_id,
                parent={"type": "page_id", "page_id": self._database_parent_page_id},
            )

    def cleanup_legacy_root_blocks(self) -> None:
        legacy_prefixes = {
            "WeReadNext 仪表盘",
            "同步入口：运行 weread2notion-next sync 后",
            "书架画廊  完整书架、阅读状态、书架分类",
            "划线  所有划线摘录",
            "笔记  个人想法和书评",
            "章节  章节目录和顺序",
        }
        children = self.list_block_children(self.page_id)
        should_delete_divider = any(
            any(block_plain_text(child).startswith(prefix) for prefix in legacy_prefixes)
            for child in children
        )
        for child in children:
            text = block_plain_text(child)
            if any(text.startswith(prefix) for prefix in legacy_prefixes):
                self.delete_notion_block(child["id"])
                continue
            if should_delete_divider and child.get("type") == "divider":
                self.delete_notion_block(child["id"])
                should_delete_divider = False

    def rebuild_homepage(self, page_id: str) -> None:
        for child in self.list_block_children(page_id):
            if child.get("type") == "child_page" and (child.get("child_page") or {}).get("title") == DATA_PAGE_TITLE:
                continue
            self.delete_notion_block(child["id"])
        self.append_children(
            block_id=page_id,
            children=[
                heading(1, "微信读书"),
                callout(
                    "本页由 weread2notion-next 维护：书籍、分类、划线、笔记和章节会随 sync 更新。",
                    emoji="📚",
                    color="blue_background",
                ),
                callout(HOME_PAGE_VERSION_TEXT, emoji="🔧"),
                heading(2, "统计概览"),
                callout("书籍总数：0", emoji="📖"),
                callout("阅读状态：在读 0 / 已读 0 / 想读 0", emoji="✅"),
                callout("笔记资产：划线 0 / 笔记 0 / 章节 0", emoji="✍️"),
                callout("阅读记录：0 天 / 0分", emoji="⏳"),
                callout("资料维度：作者 0 / 分类 0", emoji="🏷️"),
            ],
        )
        sections = [
            ("阅读时长", "按天记录阅读时长，后续热力图和周/月/年统计都从这里生成。", "首页 · 阅读时长"),
            ("书架", "完整书架画廊，适合按封面浏览。", "首页 · 书架画廊"),
            ("最近阅读", "按最后阅读时间排序，适合继续读。", "首页 · 最近阅读"),
            ("有笔记", "只看有划线或笔记的书。", "首页 · 有笔记"),
            ("分类入口", "从分类进入书架。", "首页 · 分类入口"),
            ("摘录与笔记", "最新划线和个人笔记。", None),
        ]
        specs = {spec[1]: spec for spec in homepage_view_specs()}
        for title, description, view_name in sections:
            self.append_children(block_id=page_id, children=[heading(2, title), callout(description, emoji="🔗")])
            if view_name:
                database_name, name, view_type, view_filter, sorts = specs[view_name]
                self.create_linked_view(self.db(database_name), page_id, name, view_type, view_filter, sorts)
        for view_name in ("首页 · 最新划线", "首页 · 最新笔记"):
            database_name, name, view_type, view_filter, sorts = specs[view_name]
            self.create_linked_view(self.db(database_name), page_id, name, view_type, view_filter, sorts)
        self.move_data_page_to_bottom(page_id)

    def move_data_page_to_bottom(self, home_page_id: str) -> None:
        children = self.list_block_children(home_page_id)
        data_pages = [
            child
            for child in children
            if child.get("type") == "child_page" and (child.get("child_page") or {}).get("title") == DATA_PAGE_TITLE
        ]
        if not data_pages:
            return
        data_page = data_pages[0]
        if children and children[-1].get("id") == data_page["id"]:
            return
        response = self.create_page(
            parent={"type": "page_id", "page_id": home_page_id},
            properties={"title": title_prop(DATA_PAGE_TITLE)},
            icon={"type": "emoji", "emoji": "🗄️"},
        )
        new_data_page_id = response["id"]
        old_data_page_id = data_page["id"]
        self._database_parent_page_id = new_data_page_id
        for data_source_id in self._data_source_ids.values():
            try:
                database_id = self.database_id_for_data_source(data_source_id)
                self.client.databases.update(
                    database_id=database_id,
                    parent={"type": "page_id", "page_id": new_data_page_id},
                )
            except APIResponseError:
                continue
        self.update_page(page_id=old_data_page_id, in_trash=True)

    def delete_homepage_linked_views(self) -> None:
        names = {spec[1] for spec in homepage_view_specs()}
        for database_name in {spec[0] for spec in homepage_view_specs()}:
            try:
                existing = self.list_views(data_source_id=self.db(database_name))
            except APIResponseError:
                continue
            for view in existing.get("results") or []:
                try:
                    detail = self.retrieve_view(view_id=view["id"])
                except APIResponseError:
                    continue
                if detail.get("name") in names:
                    try:
                        self.delete_view(view_id=view["id"])
                    except APIResponseError:
                        continue

    def ensure_linked_view(
        self,
        data_source_id: str,
        page_id: str,
        name: str,
        view_type: str,
        view_filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            existing = self.list_views(data_source_id=data_source_id)
            for view in existing.get("results") or []:
                view_name = view.get("name")
                if not view_name and view.get("id"):
                    view_name = self.retrieve_view(view_id=view["id"]).get("name")
                if view_name == name:
                    return
            self.create_view(
                data_source_id=data_source_id,
                name=name,
                type=view_type,
                filter=view_filter,
                sorts=sorts,
                create_database={"parent": {"type": "page_id", "page_id": page_id}},
            )
        except APIResponseError:
            return

    def create_linked_view(
        self,
        data_source_id: str,
        page_id: str,
        name: str,
        view_type: str,
        view_filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.create_view(
            data_source_id=data_source_id,
            name=name,
            type=view_type,
            filter=view_filter,
            sorts=sorts,
            create_database={"parent": {"type": "page_id", "page_id": page_id}},
        )

    def refresh_homepage_stats(self, page_id: str) -> None:
        stats = self.homepage_stats()
        desired = {
            "书籍总数：": f"书籍总数：{stats['books']}",
            "阅读状态：": (
                f"阅读状态：在读 {stats['reading']} / 已读 {stats['finished']} / 想读 {stats['wish']}"
            ),
            "笔记资产：": (
                f"笔记资产：划线 {stats['bookmarks']} / 笔记 {stats['reviews']} / 章节 {stats['chapters']}"
            ),
            "阅读记录：": f"阅读记录：{stats['read_days']} 天 / {format_duration(stats['read_seconds'])}",
            "资料维度：": f"资料维度：作者 {stats['authors']} / 分类 {stats['categories']}",
        }
        seen: set[str] = set()
        for child in self.list_block_children(page_id):
            text = block_plain_text(child)
            for prefix, content in desired.items():
                if text.startswith(prefix):
                    seen.add(prefix)
                    self.update_callout_text(child["id"], content)
        missing = [callout(content, emoji=stats_icon(prefix)) for prefix, content in desired.items() if prefix not in seen]
        if missing:
            self.append_children(block_id=page_id, children=missing)

    def homepage_stats(self) -> dict[str, int]:
        books = self.query_all(self.db(BOOK_DB))
        status_counts: dict[str, int] = {}
        for row in books:
            status = property_value((row.get("properties") or {}).get("阅读状态")) or ""
            status_counts[status] = status_counts.get(status, 0) + 1
        days = self.query_all(self.db(DAY_DB))
        read_seconds = sum(property_value((row.get("properties") or {}).get("时长")) or 0 for row in days)
        return {
            "books": len(books),
            "reading": status_counts.get("在读", 0),
            "finished": status_counts.get("已读", 0),
            "wish": status_counts.get("想读", 0),
            "bookmarks": len(self.query_all(self.db(BOOKMARK_DB))),
            "reviews": len(self.query_all(self.db(REVIEW_DB))),
            "chapters": len(self.query_all(self.db(CHAPTER_DB))),
            "read_days": len([row for row in days if (property_value((row.get("properties") or {}).get("时长")) or 0) >= 60]),
            "read_seconds": int(read_seconds),
            "authors": len(self.query_all(self.db(AUTHOR_DB))),
            "categories": len(self.query_all(self.db(CATEGORY_DB))),
        }

    def upsert_daily_read_time(self, timestamp: int, duration: int, dry_run: bool = False) -> None:
        if dry_run:
            return
        dt = datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=8)
        day_title = dt.strftime("%Y年%m月%d日")
        minutes = round((duration or 0) / 60, 2)
        existing = self.daily_read_pages_by_timestamp().get(timestamp)
        if existing:
            existing_minutes = property_value((existing.get("properties") or {}).get("时长"))
            if existing_minutes == minutes:
                return
        year_id = self.get_or_create_period_page(YEAR_DB, dt.strftime("%Y"), period_start(dt, "year"), period_end(dt, "year"))
        month_id = self.get_or_create_period_page(MONTH_DB, dt.strftime("%Y年%m月"), period_start(dt, "month"), period_end(dt, "month"))
        week_year, week_number, _ = dt.isocalendar()
        week_id = self.get_or_create_period_page(
            WEEK_DB,
            f"{week_year}年第{week_number}周",
            period_start(dt, "week"),
            period_end(dt, "week"),
        )
        properties = {
            "标题": title_prop(day_title),
            "日期": date_prop(dt.strftime("%Y-%m-%d")),
            "时间戳": number_prop(timestamp),
            "时长": number_prop(minutes),
            "年": relation_prop([year_id]),
            "月": relation_prop([month_id]),
            "周": relation_prop([week_id]),
        }
        if existing:
            self.update_page(page_id=existing["id"], properties=properties)
            self._daily_read_page_cache[timestamp] = {**existing, "properties": properties}
            return
        created = self.create_page(parent=self.data_source_parent(DAY_DB), properties=properties)
        self._daily_read_page_cache[timestamp] = {**created, "properties": properties}

    def daily_read_pages_by_timestamp(self) -> dict[int, dict[str, Any]]:
        if self._daily_read_page_cache is None:
            self._daily_read_page_cache = {}
            for row in self.query_all(self.db(DAY_DB)):
                timestamp = property_value((row.get("properties") or {}).get("时间戳"))
                if timestamp is not None:
                    self._daily_read_page_cache[int(timestamp)] = row
        return self._daily_read_page_cache

    def get_or_create_period_page(self, database_name: str, title: str, start: datetime, end: datetime) -> str:
        cache_key = (database_name, title)
        if cache_key in self._period_page_cache:
            return self._period_page_cache[cache_key]
        results = self.query_all(self.db(database_name), filter={"property": "标题", "title": {"equals": title}})
        properties = {
            "标题": title_prop(title),
            "日期": {"date": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")}},
        }
        if results:
            self.update_page(page_id=results[0]["id"], properties=properties)
            self._period_page_cache[cache_key] = results[0]["id"]
            return results[0]["id"]
        page_id = self.create_page(parent=self.data_source_parent(database_name), properties=properties)["id"]
        self._period_page_cache[cache_key] = page_id
        return page_id

    def normalized_env_data_source_ids(self, strict: bool = True) -> dict[str, str]:
        raw = env_data_source_ids()
        normalized: dict[str, str] = {}
        for name, notion_id in raw.items():
            env_name = ENV_DATA_SOURCE_MAPPING[name]
            try:
                data_source_id = self.resolve_data_source_id(notion_id)
            except APIResponseError as exc:
                if not strict:
                    continue
                raise NotionConfigError(
                    f"{env_name} is not accessible or is not a Notion database/data source id: {notion_id}"
                ) from exc
            response = self.request(path=f"data_sources/{data_source_id}", method="GET")
            if response.get("in_trash"):
                if not strict:
                    continue
                raise NotionConfigError(
                    f"{env_name} points to a database/data source in Notion trash: {notion_id}. "
                    "Restore it or remove this line from .env and run `weread2notion-next init`."
                )
            expected = expected_properties().get(name, {})
            properties = response.get("properties") or {}
            if not all(properties.get(prop_name, {}).get("type") == prop_type for prop_name, prop_type in expected.items()):
                if not strict:
                    continue
                raise NotionConfigError(
                    f"{env_name} points to an incompatible database/data source: {notion_id}. "
                    f"Expected schema for {name}."
                )
            normalized[name] = data_source_id
        return normalized

    def find_databases(self) -> dict[str, str]:
        found: dict[str, str] = {}
        self._scan_databases(self.page_id, found)
        missing = [name for name in required_database_names() if name not in found]
        if missing:
            found.update(self.search_databases(missing))
        return found

    def duplicate_database_titles(self) -> dict[str, list[str]]:
        ids_by_name = {name: set() for name in required_database_names()}
        self._collect_database_ids(self.page_id, ids_by_name)
        return {name: sorted(ids) for name, ids in ids_by_name.items() if len(ids) > 1}

    def _collect_database_ids(self, block_id: str, ids_by_name: dict[str, set[str]]) -> None:
        for child in self.list_block_children(block_id):
            if child.get("type") == "child_database":
                title = child["child_database"]["title"]
                canonical = canonical_database_name(title)
                if canonical in ids_by_name:
                    data_source_id = self.resolve_data_source_id(child["id"], prefer_data_source=False)
                    if self.is_schema_compatible(canonical, data_source_id):
                        ids_by_name[canonical].add(data_source_id)
            if child.get("has_children"):
                self._collect_database_ids(child["id"], ids_by_name)

    def _scan_databases(self, block_id: str, found: dict[str, str]) -> None:
        for child in self.list_block_children(block_id):
            if child.get("type") == "child_database":
                title = child["child_database"]["title"]
                canonical = canonical_database_name(title)
                if canonical and canonical not in found:
                    data_source_id = self.resolve_data_source_id(child["id"], prefer_data_source=False)
                    if self.is_schema_compatible(canonical, data_source_id):
                        found[canonical] = data_source_id
            if child.get("has_children"):
                self._scan_databases(child["id"], found)

    def search_databases(self, names: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for name in names:
            responses = []
            for query in database_search_names(name):
                try:
                    responses.append(
                        self.client.search(
                            query=query,
                            filter={"property": "object", "value": "data_source"},
                            page_size=25,
                        )
                    )
                except APIResponseError:
                    continue
            fallback_id = None
            for response in responses:
                for result in response.get("results") or []:
                    title = data_source_title(result)
                    if canonical_database_name(title) != name:
                        continue
                    data_source_id = result.get("id")
                    if not data_source_id:
                        continue
                    if self.is_schema_compatible(name, data_source_id):
                        if self.is_database_under_page(result, self.page_id):
                            found[name] = data_source_id
                            break
                        fallback_id = fallback_id or data_source_id
                if name in found:
                    break
            if name not in found and fallback_id:
                found[name] = fallback_id
        return found

    def ensure_database_titles(self) -> None:
        for name, data_source_id in self._data_source_ids.items():
            title = rich_title(name)
            try:
                data_source = self.request(path=f"data_sources/{data_source_id}", method="GET")
                if data_source_title(data_source) != name:
                    self.update_data_source(data_source_id=data_source_id, title=title)
                database_id = self.database_id_for_data_source(data_source_id)
                database = self.request(path=f"databases/{database_id}", method="GET")
                if database_title(database) != name:
                    self.client.databases.update(database_id=database_id, title=title)
            except APIResponseError:
                continue

    def is_database_under_page(self, data_source: dict[str, Any], page_id: str) -> bool:
        parent = data_source.get("parent") or {}
        database_id = parent.get("database_id") if parent.get("type") == "database_id" else None
        if not database_id:
            return False
        try:
            database = self.request(path=f"databases/{database_id}", method="GET")
        except APIResponseError:
            return False
        database_parent = database.get("parent") or {}
        return normalize_notion_id(database_parent.get("page_id")) == normalize_notion_id(page_id)

    def is_schema_compatible(self, name: str, data_source_id: str) -> bool:
        expected = core_properties().get(name)
        if not expected:
            return False
        try:
            response = self.request(path=f"data_sources/{data_source_id}", method="GET")
        except APIResponseError:
            return False
        if response.get("in_trash"):
            return False
        properties = response.get("properties") or {}
        return all(properties.get(prop_name, {}).get("type") == prop_type for prop_name, prop_type in expected.items())

    def resolve_data_source_id(self, notion_id: str, prefer_data_source: bool = True) -> str:
        if prefer_data_source:
            try:
                self.request(path=f"data_sources/{notion_id}", method="GET")
                return notion_id
            except APIResponseError as error:
                code = getattr(error.code, "value", error.code)
                if code not in {"object_not_found", "validation_error"}:
                    raise

        try:
            database = self.request(path=f"databases/{notion_id}", method="GET")
            sources = database.get("data_sources") or []
            if sources:
                return sources[0]["id"]
        except APIResponseError as error:
            code = getattr(error.code, "value", error.code)
            if code not in {"object_not_found", "validation_error"}:
                raise

        if not prefer_data_source:
            self.request(path=f"data_sources/{notion_id}", method="GET")
            return notion_id
        raise NotionConfigError(f"Could not resolve Notion database/data source id: {notion_id}")

    def created_data_source_id(self, response: dict[str, Any]) -> str:
        sources = response.get("data_sources") or []
        if sources:
            return sources[0]["id"]
        return self.resolve_data_source_id(response["id"])

    def data_source_parent(self, name: str) -> dict[str, str]:
        return {"type": "data_source_id", "data_source_id": self.db(name)}

    def relation_schema(self, target_name: str) -> dict[str, Any]:
        return {
            "relation": {
                "data_source_id": self.db(target_name),
                "single_property": {},
            }
        }

    def list_block_children(self, block_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        start_cursor = None
        while True:
            response = self.list_children(
                block_id=block_id, start_cursor=start_cursor, page_size=100
            )
            results.extend(response.get("results") or [])
            if not response.get("has_more"):
                return results
            start_cursor = response.get("next_cursor")

    def create_simple_database(self, name: str) -> str:
        response = self.create_database_with_data_source(
            name=name,
            icon=external_icon(USER_ICON if name == AUTHOR_DB else TAG_ICON),
            properties={"名称": {"title": {}}},
        )
        return self.created_data_source_id(response)

    def create_database_with_data_source(
        self, name: str, icon: dict[str, Any], properties: dict[str, Any]
    ) -> dict[str, Any]:
        title = rich_title(name)
        return self.request(
            path="databases",
            method="POST",
            body={
                "parent": {"type": "page_id", "page_id": self.database_parent_page_id()},
                "title": title,
                "icon": icon,
                "initial_data_source": {
                    "title": title,
                    "properties": properties,
                },
            },
        )

    def database_parent_page_id(self) -> str:
        return self._database_parent_page_id or self.page_id

    @retry(**NOTION_RETRY)
    def request(self, path: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.client.request(path=path, method=method, body=body)

    @retry(**NOTION_RETRY)
    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self.client.pages.retrieve(page_id=page_id)

    @retry(**NOTION_RETRY)
    def create_page(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.pages.create(**kwargs)

    @retry(**NOTION_RETRY)
    def update_page(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.pages.update(**kwargs)

    @retry(**NOTION_RETRY)
    def append_children(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.blocks.children.append(**kwargs)

    @retry(**NOTION_RETRY)
    def list_children(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.blocks.children.list(**kwargs)

    @retry(**NOTION_RETRY)
    def update_block(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.blocks.update(**kwargs)

    def update_callout_text(self, block_id: str, content: str) -> None:
        self.update_block(
            block_id=block_id,
            callout={
                "rich_text": [{"type": "text", "text": {"content": content}}],
                "icon": {"type": "emoji", "emoji": "📊"},
                "color": "default",
            },
        )

    @retry(**NOTION_RETRY)
    def delete_notion_block(self, block_id: str) -> dict[str, Any]:
        return self.client.blocks.delete(block_id=block_id)

    @retry(**NOTION_RETRY)
    def update_data_source(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.data_sources.update(**kwargs)

    @retry(**NOTION_RETRY)
    def list_views(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.views.list(**kwargs)

    @retry(**NOTION_RETRY)
    def retrieve_view(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.views.retrieve(**kwargs)

    @retry(**NOTION_RETRY)
    def create_view(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        return self.client.views.create(**kwargs)

    @retry(**NOTION_RETRY)
    def delete_view(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.views.delete(**kwargs)

    def create_book_database(self) -> str:
        properties: dict[str, Any] = {
            "书名": {"title": {}},
            "BookId": {"rich_text": {}},
            "ISBN": {"rich_text": {}},
            "链接": {"url": {}},
            "作者": self.relation_schema(AUTHOR_DB),
            "Sort": {"number": {}},
            "评分": {"number": {}},
            "分类": self.relation_schema(CATEGORY_DB),
            "阅读状态": select_schema(SELECT_OPTIONS[(BOOK_DB, "阅读状态")]),
            "阅读时长": {"number": {}},
            "阅读进度": {"number": {"format": "percent"}},
            "阅读天数": {"number": {}},
            "时间": {"date": {}},
            "开始阅读时间": {"date": {}},
            "最后阅读时间": {"date": {}},
            "简介": {"rich_text": {}},
            "书架分类": {"select": {}},
            "我的评分": {"select": {}},
            "来源": select_schema(SELECT_OPTIONS[(BOOK_DB, "来源")]),
            "置顶": {"checkbox": {}},
            "私密": {"checkbox": {}},
            "划线数": {"number": {}},
            "笔记数": {"number": {}},
            "书签数": {"number": {}},
        }
        response = self.create_database_with_data_source(
            name=BOOK_DB,
            icon=external_icon(BOOK_ICON),
            properties=properties,
        )
        return self.created_data_source_id(response)

    def create_item_database(self, name: str) -> str:
        id_property = "bookmarkId" if name == BOOKMARK_DB else "reviewId" if name == REVIEW_DB else "chapterUid"
        properties: dict[str, Any] = {
            "Name": {"title": {}},
            "bookId": {"rich_text": {}},
            id_property: {"rich_text": {}},
            "blockId": {"rich_text": {}},
            "sortKey": {"rich_text": {}},
            "书籍": self.relation_schema(BOOK_DB),
        }
        if name in (BOOKMARK_DB, REVIEW_DB):
            properties.update(
                {
                    "range": {"rich_text": {}},
                    "chapterUid": {"number": {}},
                    "Date": {"date": {}},
                }
            )
        if name == BOOKMARK_DB:
            properties.update({"style": {"number": {}}, "colorStyle": {"number": {}}})
        if name == REVIEW_DB:
            properties.update({"star": {"number": {}}, "abstract": {"rich_text": {}}})
        if name == CHAPTER_DB:
            properties.update(
                {
                    "chapterIdx": {"number": {}},
                    "level": {"number": {}},
                    "updateTime": {"number": {}},
                }
            )
        response = self.create_database_with_data_source(
            name=name,
            icon=external_icon(TAG_ICON),
            properties=properties,
        )
        return self.created_data_source_id(response)

    def create_period_database(self, name: str) -> str:
        response = self.create_database_with_data_source(
            name=name,
            icon=external_icon(TARGET_ICON_URL),
            properties={
                "标题": {"title": {}},
                "日期": {"date": {}},
                "时长": {"number": {}},
            },
        )
        return self.created_data_source_id(response)

    def create_day_database(self) -> str:
        response = self.create_database_with_data_source(
            name=DAY_DB,
            icon=external_icon(TARGET_ICON_URL),
            properties={
                "标题": {"title": {}},
            "日期": {"date": {}},
            "时间戳": {"number": {}},
            "时长": {"number": {}},
            "年": self.relation_schema(YEAR_DB),
            "月": self.relation_schema(MONTH_DB),
            "周": self.relation_schema(WEEK_DB),
            },
        )
        return self.created_data_source_id(response)

    def create_read_record_database(self) -> str:
        response = self.create_database_with_data_source(
            name=READ_RECORD_DB,
            icon=external_icon(TARGET_ICON_URL),
            properties={
                "标题": {"title": {}},
                "日期": {"date": {}},
                "时间戳": {"number": {}},
                "时长": {"number": {}},
                "书架": self.relation_schema(BOOK_DB),
            },
        )
        return self.created_data_source_id(response)

    def create_state_database(self) -> str:
        response = self.create_database_with_data_source(
            name=STATE_DB,
            icon=external_icon(SYNC_ICON),
            properties={
                "item_key": {"title": {}},
                "book_id": {"rich_text": {}},
                "item_type": {"select": {}},
                "weread_id": {"rich_text": {}},
                "block_id": {"rich_text": {}},
                "content_hash": {"rich_text": {}},
                "sort_key": {"rich_text": {}},
                "last_seen_at": {"date": {}},
                "书籍": self.relation_schema(BOOK_DB),
            },
        )
        return self.created_data_source_id(response)

    def create_setting_database(self) -> str:
        response = self.create_database_with_data_source(
            name=SETTING_DB,
            icon=external_icon(SYNC_ICON),
            properties={
                "标题": {"title": {}},
                "值": {"rich_text": {}},
                "说明": {"rich_text": {}},
            },
        )
        return self.created_data_source_id(response)

    def data_source_ids(self) -> dict[str, str]:
        if not self._data_source_ids:
            pinned = self.normalized_env_data_source_ids(strict=False)
            self._data_source_ids = {**pinned, **self.find_databases()}
        return self._data_source_ids

    def db(self, name: str) -> str:
        ids = self.data_source_ids()
        if name not in ids:
            raise NotionConfigError(f"Missing Notion database: {name}. Run init first.")
        return ids[name]

    def query_all(self, data_source_id: str, filter: dict[str, Any] | None = None, sorts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        start_cursor = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = self.request(
                path=f"data_sources/{data_source_id}/query",
                method="POST",
                body=body,
            )
            results.extend(response.get("results") or [])
            if not response.get("has_more"):
                return results
            start_cursor = response.get("next_cursor")

    def latest_sort(self) -> int | float:
        results = self.query_all(
            self.db(BOOK_DB),
            filter={"property": "Sort", "number": {"is_not_empty": True}},
            sorts=[{"property": "Sort", "direction": "descending"}],
        )
        if not results:
            return 0
        return property_value(results[0]["properties"].get("Sort")) or 0

    def get_book_page(self, book_id: str) -> dict[str, Any] | None:
        results = self.query_all(
            self.db(BOOK_DB),
            filter={"property": "BookId", "rich_text": {"equals": book_id}},
        )
        return results[0] if results else None

    def data_source_url(self, name: str) -> str:
        try:
            response = self.request(path=f"data_sources/{self.db(name)}", method="GET")
            return response.get("url") or ""
        except APIResponseError:
            return ""

    def database_id_for_data_source(self, data_source_id: str) -> str:
        response = self.request(path=f"data_sources/{data_source_id}", method="GET")
        parent = response.get("parent") or {}
        if parent.get("type") == "database_id" and parent.get("database_id"):
            return parent["database_id"]
        raise NotionConfigError(f"Could not find parent database for data source: {data_source_id}")

    def get_item_record(self, item_type: str, book_id: str, weread_id: str) -> dict[str, Any] | None:
        database_name = {"bookmark": BOOKMARK_DB, "review": REVIEW_DB, "chapter": CHAPTER_DB}.get(item_type)
        if not database_name:
            return None
        id_property = "bookmarkId" if item_type == "bookmark" else "reviewId" if item_type == "review" else "chapterUid"
        results = self.query_all(
            self.db(database_name),
            filter={
                "and": [
                    {"property": "bookId", "rich_text": {"equals": book_id}},
                    {"property": id_property, "rich_text": {"equals": weread_id}},
                ]
            },
        )
        return results[0] if results else None

    def upsert_book(self, book: Book, dry_run: bool = False) -> str:
        existing = self.get_book_page(book.book_id)
        if dry_run:
            return existing["id"] if existing else f"dry-run-book-{book.book_id}"
        author_ids = [self.get_or_create_simple_page(AUTHOR_DB, name, USER_ICON) for name in book.author.split(" ") if name]
        category_ids = [self.get_or_create_simple_page(CATEGORY_DB, name, TAG_ICON) for name in book.categories]
        properties = {
            "书名": title_prop(book.title),
            "BookId": rich_text_prop(book.book_id),
            "ISBN": rich_text_prop(book.isbn),
            "链接": url_prop(book.url),
            "作者": relation_prop(author_ids),
            "Sort": number_prop(book.sort),
            "评分": number_prop(book.rating),
            "分类": relation_prop(category_ids),
            "阅读状态": select_prop(book.status),
            "阅读时长": number_prop(book.reading_time),
            "阅读进度": number_prop(book.progress),
            "阅读天数": number_prop(book.reading_days),
            "时间": date_prop(book.finished_at),
            "开始阅读时间": date_prop(book.started_at),
            "最后阅读时间": date_prop(book.last_read_at),
            "简介": rich_text_prop(book.intro),
            "书架分类": select_prop(book.shelf_category),
            "我的评分": select_prop(book.my_rating),
            "来源": select_prop(book.source_type),
            "置顶": checkbox_prop(book.is_top),
            "私密": checkbox_prop(book.is_secret),
            "划线数": number_prop(book.note_count),
            "笔记数": number_prop(book.review_count),
            "书签数": number_prop(book.bookmark_count),
        }
        properties = {key: value for key, value in properties.items() if value.get(next(iter(value))) is not None}
        icon = external_icon(book.cover or BOOK_ICON)
        cover = external_cover(book.cover)
        if existing:
            kwargs: dict[str, Any] = {"page_id": existing["id"], "properties": properties, "icon": icon}
            if cover:
                kwargs["cover"] = cover
            return self.update_page(**kwargs)["id"]
        kwargs = {
            "parent": self.data_source_parent(BOOK_DB),
            "properties": properties,
            "icon": icon,
        }
        if cover:
            kwargs["cover"] = cover
        return self.create_page(**kwargs)["id"]

    def get_or_create_simple_page(self, database_name: str, name: str, icon_url: str) -> str:
        cache_key = (database_name, name)
        if cache_key in self._simple_page_cache:
            return self._simple_page_cache[cache_key]
        results = self.query_all(self.db(database_name), filter={"property": "名称", "title": {"equals": name}})
        if results:
            self._simple_page_cache[cache_key] = results[0]["id"]
            return results[0]["id"]
        results = self.query_all(self.db(database_name), filter={"property": "名称", "title": {"contains": name}})
        for row in results:
            if property_value((row.get("properties") or {}).get("名称")) == name:
                self._simple_page_cache[cache_key] = row["id"]
                return row["id"]
        page_id = self.create_page(
            parent=self.data_source_parent(database_name),
            icon=external_icon(icon_url),
            properties={"名称": title_prop(name)},
        )["id"]
        self._simple_page_cache[cache_key] = page_id
        return page_id

    def ensure_managed_area(self, page_id: str, dry_run: bool = False) -> str:
        if dry_run and page_id.startswith("dry-run-"):
            return f"dry-run-managed-{page_id}"
        for child in self.list_block_children(page_id):
            if block_plain_text(child) == MANAGED_HEADING:
                return child["id"]
        if dry_run:
            return f"dry-run-managed-{page_id}"
        response = self.append_children(
            block_id=page_id,
            children=[divider(), heading(2, MANAGED_HEADING)],
        )
        return response["results"][-1]["id"]

    def append_item_block(self, page_id: str, after: str, item: SyncItem, dry_run: bool = False) -> str:
        if dry_run:
            return f"dry-run-block-{item.weread_id}"
        response = self.append_children(
            block_id=page_id,
            children=[item_block(item)],
        )
        return response["results"][0]["id"]

    def append_item_blocks(self, page_id: str, items: list[SyncItem], dry_run: bool = False) -> list[str]:
        if dry_run:
            return [f"dry-run-block-{item.weread_id}" for item in items]
        block_ids: list[str] = []
        for index in range(0, len(items), 100):
            chunk = items[index : index + 100]
            response = self.append_children(
                block_id=page_id,
                children=[item_block(item) for item in chunk],
            )
            block_ids.extend(block["id"] for block in response.get("results") or [])
        return block_ids

    def update_item_block(self, block_id: str, item: SyncItem, dry_run: bool = False) -> None:
        if dry_run:
            return
        block = item_block(item)
        block_type = block["type"]
        self.update_block(block_id=block_id, **{block_type: block[block_type]})

    def delete_block(self, block_id: str, dry_run: bool = False) -> None:
        if dry_run:
            return
        self.delete_notion_block(block_id=block_id)

    def recover_states_from_managed_area(self, page_id: str, items: list[SyncItem]) -> dict[str, SyncState]:
        pending: dict[str, list[SyncItem]] = {}
        for item in items:
            pending.setdefault(item.content, []).append(item)

        states: dict[str, SyncState] = {}
        for block in self.managed_item_blocks(page_id):
            if block.get("type") not in {"callout", "heading_1", "heading_2", "heading_3"}:
                continue
            text = block_plain_text(block)
            queue = pending.get(text) or []
            item = queue.pop(0) if queue else None
            if item:
                states[item.item_key] = SyncState(
                    item_key=item.item_key,
                    book_id=item.book_id,
                    item_type=item.item_type,
                    weread_id=item.weread_id,
                    block_id=block["id"],
                    content_hash=content_hash(item.hash_payload),
                    sort_key=item.sort_key,
                )
                continue

            item_key = f"__orphan__:{block['id']}"
            states[item_key] = SyncState(
                item_key=item_key,
                book_id="",
                item_type="orphan",
                weread_id=block["id"],
                block_id=block["id"],
                content_hash="",
                sort_key="",
            )
        return states

    def managed_item_blocks(self, page_id: str) -> list[dict[str, Any]]:
        children = self.list_block_children(page_id)
        for index, child in enumerate(children):
            if block_plain_text(child) == MANAGED_HEADING:
                return children[index + 1 :]
        return []


def required_database_names() -> tuple[str, ...]:
    return (
        BOOK_DB,
        BOOKMARK_DB,
        REVIEW_DB,
        CHAPTER_DB,
        AUTHOR_DB,
        CATEGORY_DB,
        DAY_DB,
        WEEK_DB,
        MONTH_DB,
        YEAR_DB,
        READ_RECORD_DB,
    )


def database_suffix(name: str) -> str:
    prefix = f"{DATABASE_PREFIX} "
    if name.startswith(prefix):
        return name[len(prefix) :]
    for legacy_prefix in LEGACY_DATABASE_PREFIXES:
        legacy = f"{legacy_prefix} "
        if name.startswith(legacy):
            return name[len(legacy) :]
    return name


def legacy_database_names(name: str) -> tuple[str, ...]:
    suffix = database_suffix(name)
    return tuple(f"{prefix} {suffix}" for prefix in LEGACY_DATABASE_PREFIXES)


def database_search_names(name: str) -> tuple[str, ...]:
    return (name, *legacy_database_names(name))


def canonical_database_name(title: str) -> str | None:
    if title in required_database_names():
        return title
    for name in required_database_names():
        if title in legacy_database_names(name):
            return name
    return None


def env_data_source_ids() -> dict[str, str]:
    return {name: value for name, env_name in ENV_DATA_SOURCE_MAPPING.items() if (value := os.getenv(env_name))}


def has_all_required_data_sources(data_source_ids: dict[str, str]) -> bool:
    return all(name in data_source_ids for name in required_database_names())


def expected_properties() -> dict[str, dict[str, str]]:
    return {
        AUTHOR_DB: {"名称": "title"},
        CATEGORY_DB: {"名称": "title"},
        BOOK_DB: {
            "书名": "title",
            "BookId": "rich_text",
            "Sort": "number",
            "阅读状态": "select",
            "阅读时长": "number",
            "阅读进度": "number",
            "阅读天数": "number",
            "时间": "date",
            "开始阅读时间": "date",
            "最后阅读时间": "date",
            "简介": "rich_text",
            "书架分类": "select",
            "我的评分": "select",
            "来源": "select",
            "置顶": "checkbox",
            "私密": "checkbox",
            "划线数": "number",
            "笔记数": "number",
            "书签数": "number",
        },
        BOOKMARK_DB: {
            "Name": "title",
            "bookId": "rich_text",
            "bookmarkId": "rich_text",
            "blockId": "rich_text",
            "sortKey": "rich_text",
            "range": "rich_text",
            "chapterUid": "number",
            "Date": "date",
            "style": "number",
            "colorStyle": "number",
        },
        REVIEW_DB: {
            "Name": "title",
            "bookId": "rich_text",
            "reviewId": "rich_text",
            "blockId": "rich_text",
            "sortKey": "rich_text",
            "range": "rich_text",
            "chapterUid": "number",
            "Date": "date",
            "star": "number",
            "abstract": "rich_text",
        },
        CHAPTER_DB: {
            "Name": "title",
            "bookId": "rich_text",
            "chapterUid": "rich_text",
            "blockId": "rich_text",
            "sortKey": "rich_text",
            "chapterIdx": "number",
            "level": "number",
            "updateTime": "number",
        },
        DAY_DB: {
            "标题": "title",
            "日期": "date",
            "时间戳": "number",
            "时长": "number",
        },
        WEEK_DB: {"标题": "title", "日期": "date", "时长": "number"},
        MONTH_DB: {"标题": "title", "日期": "date", "时长": "number"},
        YEAR_DB: {"标题": "title", "日期": "date", "时长": "number"},
        READ_RECORD_DB: {
            "标题": "title",
            "日期": "date",
            "时间戳": "number",
            "时长": "number",
        },
        STATE_DB: {
            "item_key": "title",
            "book_id": "rich_text",
            "item_type": "select",
            "block_id": "rich_text",
            "content_hash": "rich_text",
        },
        SETTING_DB: {"标题": "title", "值": "rich_text"},
    }


def core_properties() -> dict[str, dict[str, str]]:
    return {
        AUTHOR_DB: {"名称": "title"},
        CATEGORY_DB: {"名称": "title"},
        BOOK_DB: {"书名": "title", "BookId": "rich_text"},
        BOOKMARK_DB: {
            "Name": "title",
            "bookId": "rich_text",
            "bookmarkId": "rich_text",
            "blockId": "rich_text",
        },
        REVIEW_DB: {
            "Name": "title",
            "bookId": "rich_text",
            "reviewId": "rich_text",
            "blockId": "rich_text",
        },
        CHAPTER_DB: {
            "Name": "title",
            "bookId": "rich_text",
            "chapterUid": "rich_text",
            "blockId": "rich_text",
        },
        DAY_DB: {"标题": "title", "时间戳": "number"},
        WEEK_DB: {"标题": "title"},
        MONTH_DB: {"标题": "title"},
        YEAR_DB: {"标题": "title"},
        READ_RECORD_DB: {"标题": "title", "时间戳": "number"},
    }


def select_schema(options: list[tuple[str, str]]) -> dict[str, Any]:
    return {"select": {"options": [{"name": name, "color": color} for name, color in options]}}


def select_option_updates(database_name: str, properties: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for (option_database, property_name), desired_options in SELECT_OPTIONS.items():
        if option_database != database_name:
            continue
        property_schema = properties.get(property_name) or {}
        if property_schema.get("type") != "select":
            continue
        existing_options = (property_schema.get("select") or {}).get("options") or []
        existing_by_name = {option.get("name"): option for option in existing_options if option.get("name")}
        merged = [
            {"name": option["name"], "color": option.get("color", "default")}
            for option in existing_options
            if option.get("name")
        ]
        changed = False
        for option_name, color in desired_options:
            if option_name not in existing_by_name:
                merged.append({"name": option_name, "color": color})
                changed = True
        if changed:
            updates[property_name] = {"select": {"options": merged}}
    return updates


def homepage_view_specs() -> list[
    tuple[str, str, str, dict[str, Any] | None, list[dict[str, Any]] | None]
]:
    return [
        (BOOK_DB, "首页 · 书架画廊", "gallery", None, [{"property": "Sort", "direction": "descending"}]),
        (DAY_DB, "首页 · 阅读时长", "table", None, [{"property": "时间戳", "direction": "descending"}]),
        (BOOK_DB, "首页 · 最近阅读", "table", None, [{"property": "最后阅读时间", "direction": "descending"}]),
        (
            BOOK_DB,
            "首页 · 有笔记",
            "gallery",
            {
                "or": [
                    {"property": "划线数", "number": {"greater_than": 0}},
                    {"property": "笔记数", "number": {"greater_than": 0}},
                ]
            },
            [{"property": "Sort", "direction": "descending"}],
        ),
        (CATEGORY_DB, "首页 · 分类入口", "table", None, None),
        (BOOKMARK_DB, "首页 · 最新划线", "list", None, [{"property": "Date", "direction": "descending"}]),
        (REVIEW_DB, "首页 · 最新笔记", "list", None, [{"property": "Date", "direction": "descending"}]),
    ]


def stats_icon(prefix: str) -> str:
    return {
        "书籍总数：": "📖",
        "阅读状态：": "✅",
        "笔记资产：": "✍️",
        "阅读记录：": "⏳",
        "资料维度：": "🏷️",
    }.get(prefix, "📊")


def format_duration(seconds: int | float) -> str:
    seconds = int(seconds or 0)
    hours = seconds // 3600
    minutes = seconds % 3600 // 60
    if hours:
        return f"{hours}时{minutes}分"
    return f"{minutes}分"


def period_start(value: datetime, period: str) -> datetime:
    if period == "year":
        return value.replace(month=1, day=1)
    if period == "month":
        return value.replace(day=1)
    if period == "week":
        return value - timedelta(days=value.weekday())
    return value


def period_end(value: datetime, period: str) -> datetime:
    start = period_start(value, period)
    if period == "year":
        return start.replace(year=start.year + 1) - timedelta(days=1)
    if period == "month":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1) - timedelta(days=1)
        return start.replace(month=start.month + 1) - timedelta(days=1)
    if period == "week":
        return start + timedelta(days=6)
    return value


def schema_for_property_type(prop_type: str) -> dict[str, Any]:
    if prop_type == "title":
        return {"title": {}}
    if prop_type == "rich_text":
        return {"rich_text": {}}
    if prop_type == "number":
        return {"number": {}}
    if prop_type == "select":
        return {"select": {}}
    if prop_type == "checkbox":
        return {"checkbox": {}}
    if prop_type == "date":
        return {"date": {}}
    if prop_type == "url":
        return {"url": {}}
    raise NotionConfigError(f"Unsupported schema property type: {prop_type}")


def property_value(prop: dict[str, Any] | None) -> Any:
    if not prop:
        return None
    prop_type = prop.get("type")
    value = prop.get(prop_type)
    if prop_type in {"title", "rich_text"}:
        return value[0]["plain_text"] if value else None
    if prop_type in {"select", "status"}:
        return value.get("name") if value else None
    if prop_type == "date":
        return value.get("start") if value else None
    return value


def block_plain_text(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    body = block.get(block_type) or {}
    return "".join(part.get("plain_text", "") for part in body.get("rich_text") or [])


def data_source_title(data_source: dict[str, Any]) -> str:
    return "".join(part.get("plain_text", "") for part in data_source.get("title") or [])


def database_title(database: dict[str, Any]) -> str:
    return "".join(part.get("plain_text", "") for part in database.get("title") or [])


def rich_title(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


def external_cover(url: str | None) -> dict[str, Any] | None:
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    return {"type": "external", "external": {"url": str(url)}}


def link_paragraph(label: str, description: str, url: str) -> dict[str, Any]:
    label_text: dict[str, Any] = {"content": label}
    if url:
        label_text["link"] = {"url": url}
    rich_text: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": label_text,
            "annotations": {"bold": True},
        },
        {"type": "text", "text": {"content": f"  {description}"}},
    ]
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": rich_text,
            "color": "default",
        },
    }
