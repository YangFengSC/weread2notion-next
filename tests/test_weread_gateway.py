import pytest

from weread2notion_next.models import Book
from weread2notion_next.weread_gateway import (
    WeReadGatewayClient,
    WeReadGatewayError,
    WeReadService,
    normalize_my_rating,
)


class FakeResponse:
    def __init__(self, data, ok=True):
        self.data = data
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.payloads = []

    def post(self, url, json, timeout):
        self.payloads.append(json)
        return FakeResponse(self.responses.pop(0))


def test_gateway_request_adds_skill_version_and_authorization():
    session = FakeSession([{"errcode": 0, "ok": True}])
    client = WeReadGatewayClient("abc1234567890", session=session)
    assert client.request("/ping")["ok"] is True
    assert session.headers["Authorization"] == "Bearer abc1234567890"
    assert session.payloads[0]["api_name"] == "/ping"
    assert session.payloads[0]["skill_version"]


def test_gateway_request_raises_on_errcode():
    session = FakeSession([{"errcode": 1, "message": "bad"}] * 3)
    client = WeReadGatewayClient("abc1234567890", session=session)
    with pytest.raises(WeReadGatewayError):
        client.request("/bad")


def test_service_lists_books_from_notebook_payload():
    session = FakeSession(
        [
            {
                "errcode": 0,
                "books": [],
                "albums": [],
                "archive": [],
            },
            {
                "errcode": 0,
                "hasMore": 0,
                "books": [
                    {
                        "sort": 10,
                        "book": {
                            "bookId": "b1",
                            "title": "Title",
                            "author": "Alice",
                            "categories": [{"title": "文学"}],
                        },
                    }
                ],
            }
        ]
    )
    books = WeReadService(WeReadGatewayClient("abc1234567890", session=session)).list_books()
    assert books[0].book_id == "b1"
    assert books[0].categories == ("文学",)


def test_service_lists_complete_shelf_and_archive_category():
    session = FakeSession(
        [
            {
                "errcode": 0,
                "books": [
                    {
                        "bookId": "b1",
                        "title": "Shelf Title",
                        "author": "Alice",
                        "category": "小说",
                        "readUpdateTime": 20,
                        "isTop": 1,
                        "secret": 0,
                    }
                ],
                "albums": [
                    {
                        "albumInfo": {"albumId": "a1", "name": "Audio", "authorName": "Bob"},
                        "albumInfoExtra": {"secret": 1},
                    }
                ],
                "archive": [{"name": "待读", "bookIds": ["b1"]}],
            },
            {
                "errcode": 0,
                "hasMore": 0,
                "books": [
                    {
                        "noteCount": 3,
                        "reviewCount": 1,
                        "bookmarkCount": 2,
                        "book": {"bookId": "b1", "categories": [{"title": "文学"}]},
                    }
                ],
            },
        ]
    )

    books = WeReadService(WeReadGatewayClient("abc1234567890", session=session)).list_books()

    assert [book.book_id for book in books] == ["album:a1", "b1"]
    assert books[1].shelf_category == "待读"
    assert books[1].categories == ("小说", "文学")
    assert books[1].note_count == 3


def test_service_lists_daily_read_times_from_readdata_detail():
    session = FakeSession(
        [
            {
                "errcode": 0,
                "dailyReadTimes": {
                    "1767225600": 120,
                    "1767312000": 0,
                },
            }
        ]
    )

    buckets = WeReadService(WeReadGatewayClient("abc1234567890", session=session)).list_daily_read_times(2026)

    assert [(bucket.timestamp, bucket.duration) for bucket in buckets] == [
        (1767225600, 120),
        (1767312000, 0),
    ]
    assert session.payloads[0]["api_name"] == "/readdata/detail"
    assert session.payloads[0]["mode"] == "annually"


def test_service_lists_reading_years_from_overall_readdata():
    session = FakeSession(
        [
            {
                "errcode": 0,
                "readTimes": {
                    "1704067200": 3600,
                    "1735689600": 7200,
                },
            }
        ]
    )

    years = WeReadService(WeReadGatewayClient("abc1234567890", session=session)).list_reading_years()

    assert 2024 in years
    assert 2025 in years
    assert session.payloads[0]["api_name"] == "/readdata/detail"
    assert session.payloads[0]["mode"] == "overall"


def test_normalize_my_rating_to_one_to_five_star_labels():
    assert normalize_my_rating("poor") == "1 星"
    assert normalize_my_rating("fair") == "3 星"
    assert normalize_my_rating("good") == "5 星"
    assert normalize_my_rating(80) == "4 星"
    assert normalize_my_rating("⭐⭐") == "2 星"


def test_enrich_book_uses_reading_time_fallback():
    session = FakeSession(
        [
            {"errcode": 0, "newRating": 85, "newRatingDetail": {"myRating": "good"}},
            {
                "errcode": 0,
                "book": {
                    "progress": 7,
                    "recordReadingTime": 0,
                    "readingTime": 756,
                    "isStartReading": 1,
                    "startReadingTime": 1776389718,
                    "updateTime": 1776659532,
                },
            },
        ]
    )
    service = WeReadService(WeReadGatewayClient("abc1234567890", session=session))

    book = service.enrich_book(Book(book_id="b1", title="Book", sort=1))

    assert book.reading_time == 756
    assert book.status == "在读"
    assert book.my_rating == "5 星"
