from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from .heatmap_export import build_heatmap_payload, write_heatmap_payload
from .notion_schema import NotionConfigError, NotionWorkspace
from .state_store import StateStore
from .sync_engine import SyncEngine
from .weread_gateway import WeReadGatewayClient, WeReadGatewayError, WeReadService


class CliError(RuntimeError):
    pass


def build_engine() -> SyncEngine:
    load_dotenv()
    api_key = os.getenv("WEREAD_API_KEY")
    if not api_key:
        raise CliError("Missing WEREAD_API_KEY")
    workspace = NotionWorkspace.from_env()
    weread = WeReadService(WeReadGatewayClient(api_key))
    return SyncEngine(weread=weread, workspace=workspace, state_store=StateStore(workspace))


def cmd_init(args: argparse.Namespace) -> int:
    engine = build_engine()
    ids = engine.init()
    print("Template ready:")
    for name, data_source_id in ids.items():
        print(f"- {name}: {data_source_id}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    load_dotenv()
    missing = [name for name in ("WEREAD_API_KEY", "NOTION_TOKEN", "NOTION_PAGE") if not os.getenv(name)]
    if missing:
        raise CliError(f"Missing environment variables: {', '.join(missing)}")
    engine = build_engine()
    for line in engine.doctor():
        print(line)
    print("WeRead API key: configured")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    engine = build_engine()
    stats = engine.sync(
        dry_run=args.dry_run,
        force=args.force,
        limit=args.limit,
        reading_time=args.reading_time,
        books_only=args.books_only,
    )
    prefix = "Dry run complete" if args.dry_run else "Sync complete"
    print(f"{prefix}: {stats.summary()}")
    return 0


def cmd_export_heatmap(args: argparse.Namespace) -> int:
    load_dotenv()
    workspace = NotionWorkspace.from_env()
    workspace.require_template()
    payload = build_heatmap_payload(workspace)
    path = write_heatmap_payload(payload, args.out)
    years = ", ".join(payload["years"].keys()) or "none"
    print(f"Heatmap data exported: {path}; years={years}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weread2notion-next")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or repair the Notion template.")
    init_parser.set_defaults(func=cmd_init)

    doctor_parser = subparsers.add_parser("doctor", help="Check environment and Notion access.")
    doctor_parser.set_defaults(func=cmd_doctor)

    sync_parser = subparsers.add_parser("sync", help="Sync WeRead data to Notion.")
    sync_parser.add_argument("--dry-run", action="store_true", help="Plan changes without writing to Notion.")
    sync_parser.add_argument("--force", action="store_true", help="Sync all books even if Sort indicates no changes.")
    sync_parser.add_argument("--limit", type=int, help="Only sync the latest N notebook books.")
    sync_parser.add_argument("--reading-time", action="store_true", help="Also sync daily reading-time statistics.")
    sync_parser.add_argument("--books-only", action="store_true", help="Only update book metadata; skip chapters, highlights, and notes.")
    sync_parser.set_defaults(func=cmd_sync)

    heatmap_parser = subparsers.add_parser("export-heatmap", help="Export public heatmap data from the Notion day database.")
    heatmap_parser.add_argument("--out", default="public/heatmap-data.json", help="Output JSON path.")
    heatmap_parser.set_defaults(func=cmd_export_heatmap)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CliError, NotionConfigError, WeReadGatewayError) as exc:
        if os.getenv("GITHUB_ACTIONS") == "true":
            safe = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error::{safe}", file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        message = str(exc)
        if "EOF occurred in violation of protocol" in message or "ConnectError" in type(exc).__name__:
            if args.command == "sync":
                message = (
                    "Network connection to Notion or WeRead was interrupted. "
                    "Please rerun the same command; incremental state prevents duplicate synced items."
                )
            else:
                message = (
                    "Network connection to Notion or WeRead was interrupted. "
                    "Please rerun the same command."
                )
        if os.getenv("GITHUB_ACTIONS") == "true":
            safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error::{safe}", file=sys.stderr)
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
