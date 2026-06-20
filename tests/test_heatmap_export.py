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
