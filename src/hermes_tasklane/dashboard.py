from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import cli


REFRESH_SECONDS = 10


def dashboard_state(cfg: cli.Config) -> dict[str, Any]:
    watch = cli.build_watch_report(cfg)
    ignored_blocked = cli.watch_ignored_blocked_jobs(cfg)
    jobs = cli.iter_job_records(cfg)
    jobs_by_state: dict[str, list[dict[str, Any]]] = {state: [] for state in sorted(cli.JOB_STATES)}
    for job in jobs:
        state = str(job.get("state") or "unknown")
        if state == "blocked" and str(job.get("id") or "") in ignored_blocked:
            continue
        jobs_by_state.setdefault(state, []).append(cli.compact_job(job))
    for records in jobs_by_state.values():
        records.sort(key=lambda item: str(item.get("id") or ""))
    return {
        "timestamp": cli.now_iso(),
        "refresh_seconds": REFRESH_SECONDS,
        "watch": watch,
        "jobs": jobs_by_state,
        "totals": watch.get("counts") or {},
        "tasklane": {
            "inbox": watch.get("inbox", 0),
            "task_root": str(cfg.task_root),
            "hermes_home": str(cfg.hermes_home),
        },
    }


def job_detail(cfg: cli.Config, job_id: str) -> dict[str, Any]:
    payload = cli.find_job_record(cfg, job_id)
    if not isinstance(payload, dict):
        raise KeyError(job_id)
    events: list[dict[str, Any]] = []
    event_path = cli.job_event_log_path(cfg, job_id)
    if event_path.exists():
        for line in event_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"raw": line})
    return {"job": payload, "events": events}


def format_age(timestamp: str | None) -> str:
    parsed = cli.parse_timestamp(timestamp)
    if not parsed:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h {minutes % 60}m"


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tasklane Pipeline</title>
  <link rel="stylesheet" href="/assets/dashboard.css">
</head>
<body>
  <main class="app-shell">
    <header class="topbar">
      <div class="brand-row">
        <img class="brand-mark" src="/assets/status-mark.png" alt="">
        <div>
          <h1>Tasklane Pipeline</h1>
          <p id="subtitle">Loading queue state</p>
        </div>
      </div>
      <div class="top-actions">
        <span id="health-pill" class="pill">Loading</span>
        <button id="refresh-button" type="button">Refresh</button>
      </div>
    </header>

    <section class="summary-grid" aria-label="Queue summary">
      <div class="metric"><span>Running</span><strong id="count-running">0</strong></div>
      <div class="metric"><span>Ready</span><strong id="count-ready">0</strong></div>
      <div class="metric"><span>Failed</span><strong id="count-failed">0</strong></div>
      <div class="metric"><span>Blocked</span><strong id="count-blocked">0</strong></div>
      <div class="metric"><span>Completed</span><strong id="count-completed">0</strong></div>
      <div class="metric"><span>Needs Human</span><strong id="count-needs-human">0</strong></div>
    </section>

    <section class="content-grid">
      <section class="panel primary-panel" aria-labelledby="active-heading">
        <div class="panel-heading">
          <h2 id="active-heading">Active Lane</h2>
          <span id="gateway-state" class="subtle">Gateway</span>
        </div>
        <div id="active-list" class="job-list"></div>
      </section>

      <section class="panel" aria-labelledby="warnings-heading">
        <div class="panel-heading">
          <h2 id="warnings-heading">Findings</h2>
          <span id="updated-at" class="subtle">Updated</span>
        </div>
        <div id="problem-list" class="problem-list"></div>
      </section>
    </section>

    <section class="panel" aria-labelledby="queue-heading">
      <div class="panel-heading">
        <h2 id="queue-heading">Queue</h2>
        <div class="filters" role="group" aria-label="State filters">
          <button class="filter active" data-filter="all" type="button">All</button>
          <button class="filter" data-filter="ready" type="button">Ready</button>
          <button class="filter" data-filter="running" type="button">Running</button>
          <button class="filter" data-filter="failed" type="button">Failed</button>
          <button class="filter" data-filter="blocked" type="button">Blocked</button>
          <button class="filter" data-filter="completed" type="button">Completed</button>
        </div>
      </div>
      <div id="queue-list" class="queue-list"></div>
    </section>

    <section class="panel detail-panel" aria-labelledby="detail-heading">
      <div class="panel-heading">
        <h2 id="detail-heading">Job Detail</h2>
        <span id="detail-state" class="subtle">Select a job</span>
      </div>
      <pre id="job-detail" class="detail-output">Select a job to inspect events and delivery metadata.</pre>
    </section>
  </main>
  <script src="/assets/dashboard.js"></script>
</body>
</html>
"""


DASHBOARD_CSS = """
:root {
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --background: #090b0a;
  --foreground: #f4f7f4;
  --card: #111411;
  --card-foreground: #f4f7f4;
  --popover: #151915;
  --popover-foreground: #f4f7f4;
  --primary: #d8fbe6;
  --primary-foreground: #102018;
  --secondary: #1c221d;
  --secondary-foreground: #e6eee8;
  --muted: #171c18;
  --muted-foreground: #98a69d;
  --accent: #1f2a22;
  --accent-foreground: #d8fbe6;
  --destructive: #ff6b7a;
  --destructive-foreground: #2a080d;
  --border: #2b342e;
  --input: #2b342e;
  --ring: #8de6b4;
  --warning: #ffd166;
  --success: #74e0a3;
  --info: #7dd7c8;
  background: var(--background);
  color: var(--foreground);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  min-width: 320px;
  background: linear-gradient(180deg, #090b0a 0%, #0e120f 52%, #0b0e0c 100%);
  color: var(--foreground);
}

button {
  border: 1px solid var(--input);
  border-radius: 8px;
  background: var(--secondary);
  color: var(--secondary-foreground);
  min-height: 40px;
  padding: 0 14px;
  font: inherit;
  cursor: pointer;
  box-shadow: 0 1px 1px rgba(0, 0, 0, 0.18);
  transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
}

button:hover {
  background: var(--accent);
  border-color: #3f4c43;
}

button:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.app-shell {
  width: min(1440px, calc(100% - 32px));
  margin: 0 auto;
  padding: 24px 0 40px;
}

.topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 14px 0 20px;
}

.brand-row {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}

.brand-mark {
  width: 48px;
  height: 48px;
  border-radius: 8px;
  flex: 0 0 auto;
  image-rendering: auto;
  border: 1px solid var(--border);
  background: var(--card);
}

h1, h2, p { margin: 0; }
h1 { font-size: 30px; line-height: 1.1; letter-spacing: 0; }
h2 { font-size: 18px; line-height: 1.2; letter-spacing: 0; }

#subtitle {
  margin-top: 5px;
  color: var(--muted-foreground);
  font-size: 14px;
}

.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.pill {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  border-radius: 8px;
  padding: 0 12px;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--card-foreground);
  font-weight: 700;
}

.pill.ok { color: var(--success); border-color: rgba(116, 224, 163, 0.46); background: rgba(28, 87, 55, 0.24); }
.pill.warning { color: var(--warning); border-color: rgba(255, 209, 102, 0.42); background: rgba(112, 81, 20, 0.25); }
.pill.critical { color: var(--destructive); border-color: rgba(255, 107, 122, 0.44); background: rgba(117, 31, 43, 0.25); }

.summary-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.metric, .panel {
  background: color-mix(in srgb, var(--card) 92%, transparent);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 18px 45px rgba(0, 0, 0, 0.28);
}

.metric {
  min-height: 92px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.metric span {
  color: var(--muted-foreground);
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
}

.metric strong {
  font-size: 34px;
  line-height: 1;
  letter-spacing: 0;
  color: var(--foreground);
}

.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
  gap: 16px;
  margin-bottom: 16px;
}

.panel {
  padding: 16px;
  min-width: 0;
}

.panel-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}

.subtle {
  color: var(--muted-foreground);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.job-list, .queue-list, .problem-list {
  display: grid;
  gap: 10px;
}

.job-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  min-height: 72px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--popover);
}

.job-item button {
  min-height: 34px;
  padding: 0 10px;
}

.job-title {
  font-weight: 800;
  overflow-wrap: anywhere;
  color: var(--popover-foreground);
}

.job-meta {
  margin-top: 6px;
  color: var(--muted-foreground);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.state-badge {
  display: inline-flex;
  min-height: 28px;
  align-items: center;
  border-radius: 8px;
  padding: 0 9px;
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
  background: var(--muted);
  color: var(--muted-foreground);
  border: 1px solid var(--border);
}

.state-running { background: rgba(125, 215, 200, 0.16); color: var(--info); border-color: rgba(125, 215, 200, 0.42); }
.state-ready { background: rgba(116, 224, 163, 0.15); color: var(--success); border-color: rgba(116, 224, 163, 0.42); }
.state-failed { background: rgba(255, 107, 122, 0.15); color: var(--destructive); border-color: rgba(255, 107, 122, 0.42); }
.state-blocked, .state-needs-human { background: rgba(255, 209, 102, 0.16); color: var(--warning); border-color: rgba(255, 209, 102, 0.42); }
.state-completed { background: rgba(152, 166, 157, 0.12); color: #bac7bf; border-color: rgba(152, 166, 157, 0.24); }

.filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.filter.active {
  background: var(--primary);
  color: var(--primary-foreground);
  border-color: var(--primary);
}

.problem {
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--popover);
  overflow-wrap: anywhere;
  color: var(--popover-foreground);
}

.problem.warning { border-color: rgba(255, 209, 102, 0.42); background: rgba(112, 81, 20, 0.18); }
.problem.critical { border-color: rgba(255, 107, 122, 0.42); background: rgba(117, 31, 43, 0.18); }

.detail-panel { margin-top: 16px; }

.detail-output {
  min-height: 220px;
  max-height: 540px;
  overflow: auto;
  margin: 0;
  padding: 14px;
  border-radius: 8px;
  background: #050706;
  color: #dfe9e3;
  border: 1px solid var(--border);
  font-size: 13px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.empty {
  color: var(--muted-foreground);
  padding: 16px;
  border: 1px dashed #3b473f;
  border-radius: 8px;
  background: var(--popover);
}

@media (max-width: 980px) {
  .summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .content-grid { grid-template-columns: 1fr; }
}

@media (max-width: 640px) {
  .app-shell {
    width: min(100% - 20px, 1440px);
    padding-top: 12px;
  }
  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .top-actions {
    width: 100%;
    justify-content: space-between;
  }
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .job-item { grid-template-columns: 1fr; }
  h1 { font-size: 25px; }
}
"""


DASHBOARD_JS = """
let currentFilter = 'all';

const stateIds = {
  running: 'count-running',
  ready: 'count-ready',
  failed: 'count-failed',
  blocked: 'count-blocked',
  completed: 'count-completed',
  'needs-human': 'count-needs-human'
};

function el(id) {
  return document.getElementById(id);
}

function text(value) {
  return value === null || value === undefined || value === '' ? '-' : String(value);
}

function stateClass(state) {
  return 'state-' + String(state || 'unknown').replace(/[^a-z0-9-]/g, '-');
}

function jobLabel(job) {
  return `${text(job.project)} · ${text(job.id)}`;
}

function renderJob(job) {
  const item = document.createElement('div');
  item.className = 'job-item';
  item.dataset.state = job.state || 'unknown';
  const runtime = job.runtime_minutes !== null && job.runtime_minutes !== undefined ? ` · ${job.runtime_minutes} min` : '';
  item.innerHTML = `
    <div>
      <div class="job-title">${escapeHtml(job.title || job.id)}</div>
      <div class="job-meta">${escapeHtml(jobLabel(job))} · ${escapeHtml(job.base_branch || 'no base')} · ${escapeHtml(job.delivery_mode || '')}${runtime}</div>
    </div>
    <div class="top-actions">
      <span class="state-badge ${stateClass(job.state)}">${escapeHtml(job.state || 'unknown')}</span>
      <button type="button" data-job-id="${escapeHtml(job.id || '')}">Inspect</button>
    </div>
  `;
  const button = item.querySelector('button');
  button.addEventListener('click', () => loadJob(job.id));
  return item;
}

function renderJobs(container, jobs, emptyText) {
  container.innerHTML = '';
  if (!jobs.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  for (const job of jobs) {
    container.appendChild(renderJob(job));
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function allQueueJobs(data) {
  const jobs = data.jobs || {};
  const order = ['running', 'ready', 'failed', 'needs-human', 'blocked', 'completed'];
  return order.flatMap((state) => jobs[state] || []);
}

function renderProblems(data) {
  const list = el('problem-list');
  list.innerHTML = '';
  const problems = data.watch.problems || [];
  const notices = data.watch.notices || [];
  const rows = [...problems, ...notices.map((notice) => ({...notice, severity: 'notice', message: notice.code}))];
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No findings right now.';
    list.appendChild(empty);
    return;
  }
  for (const row of rows) {
    const item = document.createElement('div');
    item.className = `problem ${row.severity || ''}`;
    const job = row.job ? ` · ${row.job.id}` : '';
    item.textContent = `${row.severity || 'notice'}: ${row.code || ''} ${row.message || ''}${job}`;
    list.appendChild(item);
  }
}

function render(data) {
  const counts = data.totals || {};
  for (const [state, id] of Object.entries(stateIds)) {
    el(id).textContent = counts[state] || 0;
  }
  const health = data.watch.health || 'unknown';
  const pill = el('health-pill');
  pill.textContent = health;
  pill.className = `pill ${health}`;
  el('gateway-state').textContent = `Gateway: ${data.watch.gateway?.state || 'unknown'}`;
  el('updated-at').textContent = new Date(data.timestamp).toLocaleString();
  el('subtitle').textContent = `${data.tasklane.task_root} · refreshes every ${data.refresh_seconds}s`;

  renderJobs(el('active-list'), data.jobs.running || [], 'No active job.');
  renderProblems(data);

  const queue = allQueueJobs(data).filter((job) => currentFilter === 'all' || job.state === currentFilter);
  renderJobs(el('queue-list'), queue, 'No jobs match this filter.');
}

async function loadStatus() {
  const response = await fetch('/api/status', {cache: 'no-store'});
  if (!response.ok) throw new Error(`status ${response.status}`);
  const data = await response.json();
  render(data);
}

async function loadJob(jobId) {
  if (!jobId) return;
  const output = el('job-detail');
  const state = el('detail-state');
  state.textContent = jobId;
  output.textContent = 'Loading job detail...';
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {cache: 'no-store'});
  if (!response.ok) {
    output.textContent = `Unable to load ${jobId}: HTTP ${response.status}`;
    return;
  }
  const data = await response.json();
  output.textContent = JSON.stringify(data, null, 2);
}

for (const button of document.querySelectorAll('.filter')) {
  button.addEventListener('click', () => {
    currentFilter = button.dataset.filter;
    for (const other of document.querySelectorAll('.filter')) other.classList.remove('active');
    button.classList.add('active');
    loadStatus().catch(showError);
  });
}

function showError(error) {
  el('health-pill').textContent = 'error';
  el('health-pill').className = 'pill critical';
  el('subtitle').textContent = error.message;
}

el('refresh-button').addEventListener('click', () => loadStatus().catch(showError));
loadStatus().catch(showError);
setInterval(() => loadStatus().catch(showError), 10000);
"""


STATUS_MARK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000300000003008060000005702f987000000017352474200aece1ce9000000097048597300000b1300000b1301009a9c18000000b6494441546843ed96c10dc2300c04cb48b001da49a4b8b540134994008d24126c209964392b2402fef5011448ed59fde9d9d19f619e2f47c4f0b01206c220d801e803df049dd8c26459a120176c1df089c0050e620446102f5f809c270013ff73d1012006c248180f2ce123704a0152f2f8029bd047e1900100b610dc1f625e246e081c52bcbea4d58401b0d0ea50ef0d383806104015828940d8184788240123af2c246e023ea90c868070a6f41db47e0c58002017868ad4d551b649e00fca6f1ff4370088f081ea337a8b47000808dc2441b003d007be09fba3e4cd596370ba0000000049454e44ae426082"
)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "hermes-tasklane-dashboard/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self.respond_text(INDEX_HTML, "text/html; charset=utf-8")
                return
            if path == "/assets/dashboard.css":
                self.respond_text(DASHBOARD_CSS, "text/css; charset=utf-8")
                return
            if path == "/assets/dashboard.js":
                self.respond_text(DASHBOARD_JS, "application/javascript; charset=utf-8")
                return
            if path == "/assets/status-mark.png":
                self.respond_bytes(STATUS_MARK_PNG, "image/png")
                return
            if path == "/api/status":
                self.respond_json(dashboard_state(self.server.cfg))  # type: ignore[attr-defined]
                return
            if path.startswith("/api/jobs/"):
                job_id = unquote(path.removeprefix("/api/jobs/"))
                self.respond_json(job_detail(self.server.cfg, job_id))  # type: ignore[attr-defined]
                return
            self.respond_error(HTTPStatus.NOT_FOUND, "not found")
        except KeyError:
            self.respond_error(HTTPStatus.NOT_FOUND, "job not found")
        except Exception as exc:
            self.respond_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def respond_json(self, payload: dict[str, Any]) -> None:
        self.respond_bytes(json.dumps(payload, indent=2).encode("utf-8"), "application/json; charset=utf-8")

    def respond_text(self, payload: str, content_type: str) -> None:
        self.respond_bytes(payload.encode("utf-8"), content_type)

    def respond_bytes(self, payload: bytes, content_type: str | None = None) -> None:
        guessed = content_type or mimetypes.guess_type(self.path)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def respond_error(self, status: HTTPStatus, message: str) -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], cfg: cli.Config):
        self.cfg = cfg
        super().__init__(server_address, DashboardHandler)


def serve_dashboard(cfg: cli.Config, *, host: str, port: int) -> None:
    server = DashboardServer((host, port), cfg)
    print(f"Tasklane dashboard listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
