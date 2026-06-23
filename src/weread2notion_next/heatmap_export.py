from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .notion_schema import DAY_DB, NotionWorkspace, property_value, read_minutes_from_properties


def build_heatmap_payload(workspace: NotionWorkspace) -> dict[str, Any]:
    rows = workspace.query_all(workspace.db(DAY_DB))
    return heatmap_payload_from_rows(rows)


def heatmap_payload_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    years: dict[str, dict[str, Any]] = {}
    for row in rows:
        properties = row.get("properties") or {}
        date_value = property_value(properties.get("日期"))
        minutes = read_minutes_from_properties(properties)
        if not date_value:
            timestamp = property_value(properties.get("时间戳"))
            if timestamp:
                date_value = datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%d")
        if not date_value:
            continue
        day = str(date_value)[:10]
        year = day[:4]
        bucket = years.setdefault(year, {"days": {}, "total_minutes": 0})
        minutes = round(float(minutes), 2)
        bucket["days"][day] = minutes
        bucket["total_minutes"] = round(float(bucket["total_minutes"]) + minutes, 2)

    for year, bucket in years.items():
        bucket["days"] = dict(sorted(bucket["days"].items()))
        bucket["active_days"] = sum(1 for value in bucket["days"].values() if value > 0)
        bucket["year"] = int(year)
    return {
        "schema": "weread2notion-next/heatmap/v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "years": dict(sorted(years.items())),
    }


def write_heatmap_payload(payload: dict[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
