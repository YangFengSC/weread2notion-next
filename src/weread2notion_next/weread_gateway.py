from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

import requests
from retrying import retry

from .models import Book, ReadTimeBucket, SyncItem
from .renderers import content_hash, item_key, note_sort_key

WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.3"


class WeReadGatewayError(RuntimeError):
    pass


class WeReadGatewayClient:
    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise WeReadGatewayError("Missing WEREAD_API_KEY")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def request(self, api_name: str, **kwargs: Any) -> dict[str, Any]:
        payload = {"api_name": api_name, "skill_version": WEREAD_SKILL_VERSION, **kwargs}
        response = self.session.post(WEREAD_GATEWAY_URL, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("upgrade_info"):
            raise WeReadGatewayError(f"WeRead skill needs upgrade: {data['upgrade_info']}")
        if data.get("errcode", 0) != 0:
            raise WeReadGatewayError(f"WeRead request failed: {api_name}, response={data}")
        return data

    def notebooks(self) -> list[dict[str, Any]]:
        books: list[dict[str, Any]] = []
        has_more = 1
        last_sort = None
        while has_more:
            params: dict[str, Any] = {"count": 100}
            if last_sort is not None:
                params["lastSort"] = last_sort
            data = self.request("/user/notebooks", **params)
            batch = data.get("books") or []
            books.extend(batch)
            has_more = data.get("hasMore", 0)
            last_sort = batch[-1].get("sort") if batch else None
            if not batch:
                break
        return sorted(books, key=lambda item: item.get("sort") or 0)

    def shelf(self) -> dict[str, Any]:
        return self.request("/shelf/sync")

    def bookmark_list(self, book_id: str) -> list[dict[str, Any]]:
        data = self.request("/book/bookmarklist", bookId=book_id)
        return data.get("updated") or []

    def review_list(self, book_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        reviews_data: list[dict[str, Any]] = []
        has_more = 1
        synckey = 0
        while has_more:
            data = self.request("/review/list/mine", bookid=book_id, synckey=synckey, count=100)
            batch = data.get("reviews") or []
            reviews_data.extend(batch)
            has_more = data.get("hasMore", 0)
            synckey = data.get("synckey", 0)
            if not batch:
                break
        summaries = [row for row in reviews_data if (row.get("review") or {}).get("type") == 4]
        reviews = [(row.get("review") or {}) for row in reviews_data if (row.get("review") or {}).get("type") == 1]
        return summaries, reviews

    def book_info(self, book_id: str) -> dict[str, Any]:
        return self.request("/book/info", bookId=book_id)

    def read_info(self, book_id: str) -> dict[str, Any]:
        return self.request("/book/getprogress", bookId=book_id)

    def chapter_info(self, book_id: str) -> dict[Any, dict[str, Any]]:
        data = self.request("/book/chapterinfo", bookId=book_id)
        chapters = data.get("chapters") or []
        return {item["chapterUid"]: item for item in chapters if "chapterUid" in item}

    def readdata_detail(self, mode: str = "annually", base_time: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": mode}
        if base_time:
            params["baseTime"] = base_time
        return self.request("/readdata/detail", **params)


class WeReadService:
    def __init__(self, client: WeReadGatewayClient):
        self.client = client

    def list_books(self) -> list[Book]:
        shelf = self.client.shelf()
        notebooks_by_book_id = self.notebooks_by_book_id()
        archive_by_book_id = self.archive_by_book_id(shelf.get("archive") or [])
        result: list[Book] = []
        for row in shelf.get("books") or []:
            book_id = row.get("bookId")
            if not book_id:
                continue
            note_row = notebooks_by_book_id.get(str(book_id), {})
            result.append(self.book_from_shelf_row(row, note_row, archive_by_book_id.get(str(book_id), "")))
        for row in shelf.get("albums") or []:
            album = row.get("albumInfo") or {}
            extra = row.get("albumInfoExtra") or {}
            album_id = album.get("albumId")
            if not album_id:
                continue
            result.append(
                Book(
                    book_id=f"album:{album_id}",
                    title=album.get("name") or "",
                    sort=extra.get("lectureReadUpdateTime") or album.get("updateTime") or 0,
                    author=album.get("authorName") or "",
                    cover=album.get("cover") or "",
                    categories=("有声书",),
                    progress=1 if album.get("finish") else None,
                    status="已完结" if album.get("finish") else album.get("finishStatus") or "",
                    intro=album.get("intro") or "",
                    source_type="album",
                    is_top=bool(extra.get("isTop")),
                    is_secret=bool(extra.get("secret")),
                    raw=row,
                )
            )
        if shelf.get("mp"):
            result.append(
                Book(
                    book_id="mp:articles",
                    title="文章收藏",
                    sort=0,
                    categories=("文章收藏",),
                    status="收藏",
                    source_type="mp",
                    is_secret=True,
                    raw=shelf.get("mp") or {},
                )
            )
        if result:
            return sorted(result, key=lambda item: item.sort or 0)

        for row in notebooks_by_book_id.values():
            book = row.get("book") or row
            book_id = book.get("bookId") or row.get("bookId")
            if not book_id:
                continue
            categories = tuple((item or {}).get("title") for item in (book.get("categories") or []) if (item or {}).get("title"))
            result.append(
                Book(
                    book_id=str(book_id),
                    title=book.get("title") or "",
                    sort=row.get("sort") or book.get("sort") or 0,
                    author=book.get("author") or "",
                    cover=(book.get("cover") or "").replace("/s_", "/t7_"),
                    categories=categories,
                    url=weread_url(str(book_id)),
                    note_count=row.get("noteCount"),
                    review_count=row.get("reviewCount"),
                    bookmark_count=row.get("bookmarkCount"),
                    raw=row,
                )
            )
        return result

    def notebooks_by_book_id(self) -> dict[str, dict[str, Any]]:
        rows = self.client.notebooks()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            book = row.get("book") or row
            book_id = book.get("bookId") or row.get("bookId")
            if book_id:
                result[str(book_id)] = row
        return result

    def archive_by_book_id(self, archive_rows: list[dict[str, Any]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for archive in archive_rows:
            name = archive.get("name") or ""
            for book_id in archive.get("bookIds") or []:
                result[str(book_id)] = name
        return result

    def book_from_shelf_row(self, row: dict[str, Any], note_row: dict[str, Any], shelf_category: str) -> Book:
        book_id = str(row.get("bookId"))
        categories = []
        if row.get("category"):
            categories.append(row.get("category"))
        book = note_row.get("book") or {}
        for category in book.get("categories") or []:
            title = (category or {}).get("title")
            if title and title not in categories:
                categories.append(title)
        return Book(
            book_id=book_id,
            title=row.get("title") or book.get("title") or "",
            sort=row.get("readUpdateTime") or row.get("updateTime") or note_row.get("sort") or 0,
            author=row.get("author") or book.get("author") or "",
            cover=((row.get("cover") or book.get("cover") or "").replace("/s_", "/t7_")),
            categories=tuple(categories),
            url=weread_url(book_id),
            status="已读" if row.get("finishReading") else "",
            last_read_at=timestamp_to_string(row.get("readUpdateTime") or 0),
            shelf_category=shelf_category,
            source_type="book",
            is_top=bool(row.get("isTop")),
            is_secret=bool(row.get("secret")),
            note_count=note_row.get("noteCount", 0),
            review_count=note_row.get("reviewCount", 0),
            bookmark_count=note_row.get("bookmarkCount", 0),
            raw={"shelf": row, "notebook": note_row},
        )

    def enrich_book(self, book: Book) -> Book:
        if book.source_type != "book":
            return book
        info = self.client.book_info(book.book_id)
        progress = self.client.read_info(book.book_id).get("book") or {}
        rating = normalize_rating(info.get("newRating"))
        rating_detail = info.get("newRatingDetail") or {}
        reading_progress = normalize_progress(progress.get("progress"))
        reading_time = progress.get("recordReadingTime") or progress.get("readingTime") or 0
        status = "已读" if progress.get("finishTime") or (progress.get("progress") or 0) >= 100 else "在读"
        if not progress.get("isStartReading") and not reading_time:
            status = book.status or "想读"
        return Book(
            **{
                **book.__dict__,
                "isbn": info.get("isbn") or "",
                "rating": rating,
                "progress": reading_progress,
                "reading_time": reading_time,
                "reading_days": progress.get("totalReadDay") or progress.get("readDay") or progress.get("readingDays"),
                "status": status,
                "finished_at": timestamp_to_string(progress.get("finishTime") or 0),
                "started_at": timestamp_to_string(
                    progress.get("beginReadingTime")
                    or progress.get("startReadingTime")
                    or progress.get("readingBookTime")
                    or 0
                ),
                "last_read_at": timestamp_to_string(progress.get("updateTime") or 0) or book.last_read_at,
                "intro": info.get("intro") or book.intro,
                "my_rating": normalize_my_rating(rating_detail.get("myRating")),
            }
        )

    def list_sync_items(self, book_id: str) -> list[SyncItem]:
        if book_id.startswith(("album:", "mp:")):
            return []
        chapters = self.client.chapter_info(book_id)
        bookmarks = self.client.bookmark_list(book_id)
        _, reviews = self.client.review_list(book_id)
        items: list[SyncItem] = []
        for chapter in sorted(chapters.values(), key=lambda row: row.get("chapterIdx", 0)):
            uid = str(chapter.get("chapterUid"))
            payload = {"title": chapter.get("title") or "", "level": chapter.get("level", 1)}
            items.append(
                SyncItem(
                    item_key=item_key(book_id, "chapter", uid),
                    book_id=book_id,
                    item_type="chapter",
                    weread_id=uid,
                    content=payload["title"],
                    sort_key=f"{int(chapter.get('chapterIdx', 0)):010d}:0000000000:chapter",
                    hash_payload=payload,
                    metadata=chapter,
                )
            )
        for row in bookmarks:
            weread_id = str(row.get("bookmarkId") or stable_fallback_id(row))
            content = row.get("markText") or ""
            if not content:
                continue
            payload = {"content": content, "abstract": row.get("abstract"), "style": row.get("style"), "colorStyle": row.get("colorStyle")}
            items.append(
                SyncItem(
                    item_key=item_key(book_id, "bookmark", weread_id),
                    book_id=book_id,
                    item_type="bookmark",
                    weread_id=weread_id,
                    content=content,
                    sort_key=note_sort_key(row, chapters),
                    hash_payload=payload,
                    metadata=row,
                )
            )
        for row in reviews:
            weread_id = str(row.get("reviewId") or stable_fallback_id(row))
            content = row.get("content") or ""
            if not content:
                continue
            payload = {"content": content, "abstract": row.get("abstract"), "star": row.get("star")}
            items.append(
                SyncItem(
                    item_key=item_key(book_id, "review", weread_id),
                    book_id=book_id,
                    item_type="review",
                    weread_id=weread_id,
                    content=content,
                    sort_key=note_sort_key(row, chapters),
                    hash_payload=payload,
                    metadata=row,
                )
            )
        return sorted(items, key=lambda item: item.sort_key)

    def list_daily_read_times(self, year: int | None = None) -> list[ReadTimeBucket]:
        year = year or datetime.now(timezone.utc).year
        base_time = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        data = self.client.readdata_detail(mode="annually", base_time=base_time)
        raw = data.get("dailyReadTimes") or data.get("readTimes") or {}
        buckets: list[ReadTimeBucket] = []
        for timestamp, duration in raw.items():
            try:
                buckets.append(ReadTimeBucket(timestamp=int(timestamp), duration=int(duration or 0)))
            except (TypeError, ValueError):
                continue
        return sorted(buckets, key=lambda item: item.timestamp)


def stable_fallback_id(row: dict[str, Any]) -> str:
    return content_hash(row)[:20]


def normalize_rating(value: Any) -> int | float | None:
    if value is None:
        return None
    if value > 100:
        return value / 1000
    if value > 10:
        return value / 10
    return value


def normalize_my_rating(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        normalized = value.strip().lower()
        mapping = {
            "poor": "1 星",
            "fair": "3 星",
            "good": "5 星",
            "1": "1 星",
            "2": "2 星",
            "3": "3 星",
            "4": "4 星",
            "5": "5 星",
        }
        if normalized in mapping:
            return mapping[normalized]
        star_count = value.count("⭐")
        if 1 <= star_count <= 5:
            return f"{star_count} 星"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 10:
        number = number / 20
    number = int(round(number))
    if 1 <= number <= 5:
        return f"{number} 星"
    return ""


def normalize_progress(value: Any) -> float:
    value = value or 0
    if value > 1:
        value = value / 100
    return round(min(max(value, 0), 1), 4)


def format_reading_time(seconds: int | float) -> str:
    seconds = int(seconds or 0)
    hours = seconds // 3600
    minutes = seconds % 3600 // 60
    parts = []
    if hours:
        parts.append(f"{hours}时")
    if minutes:
        parts.append(f"{minutes}分")
    return "".join(parts)


def timestamp_to_string(value: int | float) -> str:
    if not value:
        return ""
    from datetime import datetime

    return datetime.utcfromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def weread_url(book_id: str) -> str:
    return f"https://weread.qq.com/web/reader/{calculate_book_str_id(book_id)}"


def transform_id(book_id: str) -> tuple[str, list[str]]:
    if re.match(r"^\d*$", book_id):
        return "3", [format(int(book_id[i : min(i + 9, len(book_id))]), "x") for i in range(0, len(book_id), 9)]
    return "4", ["".join(format(ord(char), "x") for char in book_id)]


def calculate_book_str_id(book_id: str) -> str:
    digest = hashlib.md5(book_id.encode("utf-8")).hexdigest()
    code, transformed_ids = transform_id(book_id)
    result = digest[0:3] + code + "2" + digest[-2:]
    for index, transformed_id in enumerate(transformed_ids):
        result += f"{len(transformed_id):02x}" + transformed_id
        if index < len(transformed_ids) - 1:
            result += "g"
    if len(result) < 20:
        result += digest[0 : 20 - len(result)]
    return result + hashlib.md5(result.encode("utf-8")).hexdigest()[0:3]
