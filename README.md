# WeRead2Notion Next

Sync the full WeRead bookshelf, shelf archives, books, highlights, notes, chapters, authors, categories, and reading-time data to Notion.

This project is a new implementation that uses the WeRead Gateway API key flow. It does not read `WEREAD_COOKIE` and does not write `NOTION_TOKEN` or `WEREAD_API_KEY` into Notion.

## Features

- Local CLI and GitHub Actions use the same sync engine.
- `init` creates or repairs the Notion data sources automatically under `NOTION_PAGE`.
- The project is now data-sync only: it does not create or maintain homepage layouts, linked views, dashboards, menus, or visual sections. Use Notion/Notion AI to arrange the synced data.
- Created databases use the `微信读书数据库` prefix. New database names do not include version numbers.
- The bookshelf is read from `/shelf/sync`, not only `/user/notebooks`, so books without notes are included.
- WeRead shelf archives are synced into the book property `书架分类`.
- The book database follows the richer WeRead2Notion Pro style: reading status, reading time, reading days, intro, shelf category, source type, top/private flags, and note counters.
- Book pages contain a managed section named `微信读书同步`; the sync only changes blocks in that section.
- Incremental state is stored in local SQLite at `.weread2notion-cache/state.sqlite3` by default. If the SQLite file is missing, the sync rebuilds state from the Notion managed section before applying changes.
- Reading-time data sources (`日`, `周`, `月`, `年`, `阅读记录`) are available. Use `sync --reading-time` when you want to update daily reading-time stats.

## Local Usage

```bash
python -m pip install -e ".[dev]"
```

Create a `.env` file or export these variables:

```bash
WEREAD_API_KEY=...
NOTION_TOKEN=...
NOTION_PAGE=https://www.notion.so/...
# Optional:
# WEREAD_STATE_DB=.weread2notion-cache/state.sqlite3
```

Then run:

```bash
weread2notion-next doctor
weread2notion-next init
weread2notion-next sync --dry-run --limit 1
weread2notion-next sync
# Optional, faster metadata backfill:
weread2notion-next sync --force --books-only
# Optional, slower:
weread2notion-next sync --reading-time
```

Use `--limit N` while testing. A full first sync may touch hundreds of bookshelf entries.

## GitHub Actions

Add repository secrets:

- `WEREAD_API_KEY`
- `NOTION_TOKEN`
- `NOTION_PAGE`

The included workflow runs daily and can also be started manually.

## Heatmap Worker

This repo includes an optional Cloudflare Worker heatmap endpoint:

```text
https://YOUR_WORKER.workers.dev/weread/heatmap?year=2026
```

Workflow:

1. `publish heatmap data` exports Notion `日` database rows to `heatmap-data.json`.
2. GitHub Pages hosts that JSON.
3. Cloudflare Worker fetches the JSON and renders an embeddable heatmap page.

Setup:

1. Enable GitHub Pages: repository `Settings` -> `Pages` -> source `GitHub Actions`.
2. Run the `publish heatmap data` workflow once.
3. Copy `worker/wrangler.toml.example` to `worker/wrangler.toml`.
4. Replace `HEATMAP_DATA_URL` with your Pages URL:

```text
https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPO/heatmap-data.json
```

5. Deploy the Worker:

```bash
cd worker
npm install -g wrangler
wrangler login
wrangler deploy
```

Optional: set `PUBLIC_CODE` in `wrangler.toml`, then embed with:

```text
https://YOUR_WORKER.workers.dev/weread/heatmap?activationCode=YOUR_CODE&year=2026
```

## Acceptance Checklist

- First run creates the Notion template automatically.
- Full bookshelf sync includes books without notes and preserves WeRead shelf archive names in `书架分类`.
- Repeated syncs do not duplicate existing highlights or notes.
- Missing local SQLite state can be recovered from the Notion managed section.
- Changed highlights or notes update their existing Notion blocks.
- Deleted highlights or notes are removed from the managed area.
- Blocks outside `微信读书同步` are never touched by the sync engine.
- Page layout is not managed by this tool; synced databases are the contract for Notion AI or manual templates.
