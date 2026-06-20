export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/" && url.pathname !== "/weread/heatmap") {
      return new Response("Not found", { status: 404 });
    }

    if (env.PUBLIC_CODE) {
      const code = url.searchParams.get("activationCode") || "";
      if (code !== env.PUBLIC_CODE) {
        return new Response("Forbidden", { status: 403 });
      }
    }

    const dataUrl = env.HEATMAP_DATA_URL || url.searchParams.get("dataUrl");
    if (!dataUrl) {
      return new Response("Missing HEATMAP_DATA_URL", { status: 500 });
    }

    const year = url.searchParams.get("year") || String(new Date().getUTCFullYear());
    const response = await fetch(dataUrl, {
      cf: { cacheTtl: 300, cacheEverything: true },
      headers: { "accept": "application/json" },
    });
    if (!response.ok) {
      return new Response(`Could not fetch heatmap data: ${response.status}`, { status: 502 });
    }
    const data = await response.json();
    const html = renderHeatmap(data, year);
    return new Response(html, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "public, max-age=300",
      },
    });
  },
};

function renderHeatmap(data, year) {
  const current = data.years?.[year] || { days: {}, total_minutes: 0, active_days: 0 };
  const days = daysOfYear(Number(year));
  const firstDay = new Date(`${year}-01-01T00:00:00Z`).getUTCDay();
  const leading = (firstDay + 6) % 7;
  const cells = [];
  for (let i = 0; i < leading; i += 1) cells.push(`<span class="cell empty"></span>`);
  for (const day of days) {
    const minutes = Number(current.days?.[day] || 0);
    cells.push(`<span class="cell level-${level(minutes)}" title="${day}: ${minutes} 分钟"></span>`);
  }
  const totalHours = Math.floor(Number(current.total_minutes || 0) / 60);
  const totalMinutes = Math.round(Number(current.total_minutes || 0) % 60);
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: dark light; --bg:#111827; --panel:#182235; --text:#e5e7eb; --muted:#9ca3af; --empty:#253149; --l1:#1f6f43; --l2:#2f9e55; --l3:#63c174; --l4:#a7e8a5; }
body { margin:0; font:14px/1.4 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:transparent; color:var(--text); }
.wrap { box-sizing:border-box; width:100%; min-height:190px; padding:18px; border:1px solid rgba(148,163,184,.22); border-radius:10px; background:var(--bg); }
.top { display:flex; align-items:baseline; justify-content:space-between; gap:16px; margin-bottom:16px; }
.title { font-size:18px; font-weight:700; }
.meta { color:var(--muted); white-space:nowrap; }
.grid { display:grid; grid-auto-flow:column; grid-template-rows:repeat(7, 12px); grid-auto-columns:12px; gap:4px; overflow-x:auto; padding-bottom:4px; }
.cell { width:12px; height:12px; border-radius:3px; background:var(--empty); flex:0 0 auto; }
.empty { opacity:0; }
.level-0 { background:var(--empty); }
.level-1 { background:var(--l1); }
.level-2 { background:var(--l2); }
.level-3 { background:var(--l3); }
.level-4 { background:var(--l4); }
.legend { display:flex; justify-content:flex-end; align-items:center; gap:6px; margin-top:12px; color:var(--muted); font-size:12px; }
.legend .cell { display:inline-block; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="title">微信读书记录 ${escapeHtml(year)}</div>
    <div class="meta">${current.active_days || 0} 天 · ${totalHours} 时 ${totalMinutes} 分</div>
  </div>
  <div class="grid">${cells.join("")}</div>
  <div class="legend"><span>少</span><span class="cell level-0"></span><span class="cell level-1"></span><span class="cell level-2"></span><span class="cell level-3"></span><span class="cell level-4"></span><span>多</span></div>
</div>
</body>
</html>`;
}

function daysOfYear(year) {
  const result = [];
  const date = new Date(Date.UTC(year, 0, 1));
  while (date.getUTCFullYear() === year) {
    result.push(date.toISOString().slice(0, 10));
    date.setUTCDate(date.getUTCDate() + 1);
  }
  return result;
}

function level(minutes) {
  if (minutes <= 0) return 0;
  if (minutes < 10) return 1;
  if (minutes < 30) return 2;
  if (minutes < 60) return 3;
  return 4;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}
