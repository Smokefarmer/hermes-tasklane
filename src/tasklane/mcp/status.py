"""TaskLane MCP browser status page (data + HTML)."""

from __future__ import annotations

from pathlib import Path

from tasklane.mcp.core import _run, _store, _summary, _WORKER_SERVICE


# --------------------------------------------------------------------------- #
# browser status page
# --------------------------------------------------------------------------- #
def _status_data() -> dict[str, Any]:
    from tasklane.metrics import spend_last_24h

    store = _store()
    rows = store.list()
    counts: dict[str, int] = {}
    total_cost = 0.0
    for r in rows:
        counts[r.get("state", "?")] = counts.get(r.get("state", "?"), 0) + 1
        m = r.get("metrics")
        if isinstance(m, dict):
            try:
                total_cost += float(m.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
    recent = sorted(rows, key=lambda r: r.get("updated_at", ""), reverse=True)[:15]
    active = _run(["systemctl", "is-active", _WORKER_SERVICE], cwd=Path("/"), timeout=10)["stdout"].strip()
    return {"worker_active": active, "counts": counts, "recent": [_summary(r) for r in recent],
            "spend_24h_usd": spend_last_24h(store), "spend_total_usd": round(total_cost, 4)}


def _status_html(d: dict[str, Any]) -> str:
    badge = {"completed": "#2e7d32", "running": "#1565c0", "blocked": "#ef6c00",
             "failed": "#c62828", "ready": "#6a1b9a", "needs-human": "#ad1457"}
    rows = "".join(
        f"<tr><td><code>{j['id']}</code></td>"
        f"<td><span style='color:{badge.get(j['state'],'#555')};font-weight:600'>{j['state']}</span></td>"
        f"<td>{j.get('attempt',0)}</td><td>{(j.get('title') or '')[:60]}</td>"
        f"<td>{('$%.2f' % j['cost_usd']) if j.get('cost_usd') else ''}</td>"
        f"<td style='color:#a00'>{(j.get('last_error') or '')[:60]}</td>"
        f"<td style='color:#888'>{(j.get('updated_at') or '')[:19]}</td></tr>"
        for j in d["recent"]
    ) or "<tr><td colspan=7 style='color:#888'>no jobs</td></tr>"
    counts = " · ".join(f"{k}: <b>{v}</b>" for k, v in sorted(d["counts"].items())) or "no jobs"
    spend = f"24h: <b>${d.get('spend_24h_usd', 0):.2f}</b> / total: <b>${d.get('spend_total_usd', 0):.2f}</b>"
    wa = d["worker_active"]
    wc = "#2e7d32" if wa == "active" else "#c62828"
    return f"""<!doctype html><html><head><meta charset=utf-8><title>TaskLane status</title>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=15>
<style>body{{font-family:system-ui,Arial;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%;margin-top:12px}}
td,th{{border-bottom:1px solid #eee;padding:6px 10px;text-align:left;font-size:14px}}th{{color:#666}}code{{font-size:13px}}</style></head>
<body><h2>TaskLane</h2>
<p>worker: <b style="color:{wc}">{wa}</b> &nbsp;|&nbsp; {counts} &nbsp;|&nbsp; {spend} &nbsp;|&nbsp; <span style=color:#888>auto-refresh 15s</span></p>
<table><tr><th>job</th><th>state</th><th>try</th><th>title</th><th>cost</th><th>last error</th><th>updated (UTC)</th></tr>{rows}</table>
</body></html>"""

