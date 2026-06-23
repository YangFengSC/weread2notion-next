from weread2notion_next.heatmap_export import heatmap_payload_from_rows


def test_heatmap_payload_from_rows_groups_days_by_year():
    payload = heatmap_payload_from_rows(
        [
            {
                "properties": {
                    "日期": {"type": "date", "date": {"start": "2026-01-01"}},
                    "时长": {"type": "number", "number": 12.5},
                }
            },
            {
                "properties": {
                    "日期": {"type": "date", "date": {"start": "2026-01-02"}},
                    "时长": {"type": "number", "number": 0},
                }
            },
        ]
    )

    year = payload["years"]["2026"]
    assert year["days"] == {"2026-01-01": 12.5, "2026-01-02": 0.0}
    assert year["active_days"] == 1
    assert year["total_minutes"] == 12.5


def test_heatmap_payload_falls_back_to_legacy_minute_and_hour_fields():
    payload = heatmap_payload_from_rows(
        [
            {
                "properties": {
                    "日期": {"type": "date", "date": {"start": "2026-01-01"}},
                    "分钟": {"type": "number", "number": 30},
                }
            },
            {
                "properties": {
                    "日期": {"type": "date", "date": {"start": "2026-01-02"}},
                    "小时": {"type": "number", "number": 1.5},
                }
            },
        ]
    )

    year = payload["years"]["2026"]
    assert year["days"] == {"2026-01-01": 30.0, "2026-01-02": 90.0}
    assert year["total_minutes"] == 120.0
