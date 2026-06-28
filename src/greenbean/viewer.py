"""Localhost markdown viewer bundled into ``greenbean watch`` (Architecture §8.2).

Not a separate daemon and not a publishing surface — a flag on the poll loop
that's already running. It serves the output dir (every repo greenbean has
generated docs for, so you can browse across them), renders ``.md`` on the fly,
and auto-refreshes the open page when the underlying file changes via a small
mtime poll. Read-only, binds to localhost only, never touches the output dir.

Deliberately stdlib-only on the serving side (``http.server`` in a daemon
thread) — no web framework. The one dependency is a markdown renderer.
"""

from __future__ import annotations

import html as html_lib
import json
import threading
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import markdown as markdown_lib

_MD_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists"]
_POLL_MS = 1500
_STATUS_MS = 1000

_PAGE_CSS = """
body { max-width: 48rem; margin: 2rem auto; padding: 0 1rem;
       font: 16px/1.6 -apple-system, system-ui, sans-serif; color: #1a1a1a; }
a { color: #2563eb; } pre { background: #f5f5f5; padding: .75rem;
   overflow-x: auto; border-radius: 6px; } code { font-size: .9em; }
table { border-collapse: collapse; } td, th { border: 1px solid #ddd;
   padding: .3rem .6rem; } nav { font-size: .9em; margin-bottom: 1.5rem; }
.gb-status { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
   padding: .5rem 1rem 1rem; margin-bottom: 2rem; }
.gb-status h2 { font-size: 1rem; margin: .75rem 0; }
.gb-status table { width: 100%; } .gb-status td { border: none; padding: .2rem .4rem; }
.gb-status td:first-child { color: #64748b; width: 9rem; font-size: .9em; }
.gb-dot { display: inline-block; width: .6rem; height: .6rem; border-radius: 50%;
   background: #22c55e; margin-right: .4rem; }
"""


@dataclass(frozen=True, slots=True)
class ViewerStatus:
    """A snapshot of what ``watch`` is doing, rendered in the status panel.

    All fields are plain JSON-serializable types so the snapshot doubles as the
    ``/__status`` payload the page polls. Times are pre-formatted for display;
    the ``*_epoch`` fields drive the client-side countdown.
    """

    repo_path: str
    repo_url: str | None
    repo_web_url: str | None
    branch: str | None
    head_short: str | None
    head_subject: str | None
    head_author: str | None
    head_time: str | None
    commit_web_url: str | None
    last_sync: str | None
    next_sync_epoch: float | None
    interval: str
    last_outcome: str | None
    output_root: str


class StatusHolder:
    """Hands the latest ``ViewerStatus`` from the watch loop to the viewer.

    The watch loop (asyncio, main thread) calls ``set`` each tick; the viewer
    (daemon thread) calls ``get``. A single reference swap is atomic under the
    GIL, so no lock is needed for this one-writer/one-reader handoff.
    """

    def __init__(self) -> None:
        self._status: ViewerStatus | None = None

    def set(self, status: ViewerStatus) -> None:
        self._status = status

    def get(self) -> ViewerStatus | None:
        return self._status


def _poll_script(rel_path: str) -> str:
    """Client-side mtime poll: reload when the served file changes on disk."""
    target = quote(rel_path)
    return (
        "<script>let last=null;async function c(){try{"
        f'const r=await fetch("/__mtime?path={target}");const t=await r.text();'
        "if(last!==null&&t!==last)location.reload();last=t;}catch(e){}}"
        f"setInterval(c,{_POLL_MS});c();</script>"
    )


def _page(title: str, body: str, *, poll_path: str | None) -> str:
    poll = _poll_script(poll_path) if poll_path else ""
    nav = '<nav><a href="/">&larr; all docs</a></nav>'
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html_lib.escape(title)}</title><style>{_PAGE_CSS}</style></head>"
        f"<body>{nav}{body}{poll}</body></html>"
    )


def _link_html(text: str | None, href: str | None) -> str:
    if not text:
        return "—"
    safe = html_lib.escape(text)
    return f'<a href="{html_lib.escape(href)}">{safe}</a>' if href else safe


def _txt(value: str | None) -> str:
    return html_lib.escape(value) if value else "—"


def _status_panel(status: ViewerStatus | None) -> str:
    """Render the watch-status panel.

    Server-renders the latest snapshot (so it works without JS); the same cell
    ids are refreshed live by ``_status_script`` polling ``/__status``.
    """
    s = status
    repo = _link_html(
        (s.repo_url or s.repo_path) if s else None, s.repo_web_url if s else None
    )
    commit = _link_html(s.head_short if s else None, s.commit_web_url if s else None)
    rows = [
        ("repo", "gb-repo", repo),
        ("branch", "gb-branch", _txt(s.branch if s else None)),
        ("commit", "gb-commit", commit),
        ("message", "gb-subject", _txt(s.head_subject if s else None)),
        ("author", "gb-author", _txt(s.head_author if s else None)),
        ("committed", "gb-time", _txt(s.head_time if s else None)),
        ("last sync", "gb-last", _txt(s.last_sync if s else None)),
        ("next sync", "gb-next", "—"),
        ("interval", "gb-interval", _txt(s.interval if s else None)),
        ("last result", "gb-outcome", _txt(s.last_outcome if s else None)),
        ("output dir", "gb-output", _txt(s.output_root if s else None)),
    ]
    cells = "".join(
        f'<tr><td>{label}</td><td id="{cid}">{val}</td></tr>'
        for label, cid, val in rows
    )
    return (
        '<section class="gb-status"><h2><span class="gb-dot"></span>watch status</h2>'
        f"<table>{cells}</table></section>"
    )


def _status_script() -> str:
    """Client poll of ``/__status`` plus a 1s countdown to the next sync."""
    return (
        "<script>"
        "function gbDur(s){s=Math.max(0,Math.round(s));"
        "var m=Math.floor(s/60),x=s%60;return m>0?m+'m '+x+'s':x+'s';}"
        "function gbSet(id,v){var e=document.getElementById(id);"
        "if(e)e.textContent=(v===null||v===undefined||v==='')?'\\u2014':v;}"
        "function gbLink(id,v,href){var e=document.getElementById(id);if(!e)return;"
        "if(v&&href){e.innerHTML='';var a=document.createElement('a');a.href=href;"
        "a.textContent=v;e.appendChild(a);}else{gbSet(id,v);}}"
        "var gbNext=null;"
        "async function gbPoll(){try{var r=await fetch('/__status');var d=await r.json();"
        "gbLink('gb-repo',d.repo_url||d.repo_path,d.repo_web_url);gbSet('gb-branch',d.branch);"
        "gbLink('gb-commit',d.head_short,d.commit_web_url);gbSet('gb-subject',d.head_subject);"
        "gbSet('gb-author',d.head_author);gbSet('gb-time',d.head_time);"
        "gbSet('gb-last',d.last_sync);gbSet('gb-interval',d.interval);"
        "gbSet('gb-outcome',d.last_outcome);gbSet('gb-output',d.output_root);"
        "gbNext=d.next_sync_epoch;}catch(e){}}"
        "function gbTick(){var e=document.getElementById('gb-next');"
        "if(e&&gbNext)e.textContent='in '+gbDur(gbNext-Date.now()/1000);}"
        f"setInterval(gbPoll,{_STATUS_MS});setInterval(gbTick,1000);gbPoll();"
        "</script>"
    )


def _index_body(root: Path, status: ViewerStatus | None) -> str:
    panel = _status_panel(status)
    docs = sorted(p for p in root.rglob("*.md") if p.is_file()) if root.exists() else []
    if docs:
        items = "".join(
            f'<li><a href="/{quote(rel)}">{html_lib.escape(rel)}</a></li>'
            for rel in (d.relative_to(root).as_posix() for d in docs)
        )
        docs_html = f"<h1>greenbean docs</h1><ul>{items}</ul>"
    else:
        docs_html = "<h1>greenbean docs</h1><p>No generated docs yet.</p>"
    return panel + docs_html + _status_script()


def _render_markdown(path: Path, rel: str) -> str:
    text = path.read_text(encoding="utf-8")
    rendered = markdown_lib.markdown(text, extensions=_MD_EXTENSIONS)
    return _page(rel, rendered, poll_path=rel)


def _resolve_under(root: Path, route: str) -> Path | None:
    """Resolve a URL path under ``root``, rejecting any traversal escape."""
    rel = unquote(route).lstrip("/")
    root_r = root.resolve()
    candidate = (root_r / rel).resolve()
    if candidate != root_r and root_r not in candidate.parents:
        return None
    return candidate


def _make_handler(
    root: Path, status: StatusHolder | None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/__status":
                self._status_json()
                return
            if parsed.path == "/__mtime":
                self._mtime(parse_qs(parsed.query).get("path", [""])[0])
                return

            target = _resolve_under(root, parsed.path)
            current = status.get() if status else None
            if target is None:
                self._html(404, _page("not found", "<p>404</p>", poll_path=None))
            elif target == root.resolve() or target.is_dir():
                self._html(
                    200, _page("greenbean docs", _index_body(root, current), poll_path=None)
                )
            elif target.is_file() and target.suffix == ".md":
                rel = target.relative_to(root.resolve()).as_posix()
                self._html(200, _render_markdown(target, rel))
            else:
                self._html(404, _page("not found", "<p>404</p>", poll_path=None))

        def _status_json(self) -> None:
            current = status.get() if status else None
            payload = json.dumps(asdict(current) if current else {})
            self._send(200, "application/json", payload.encode("utf-8"))

        def _mtime(self, raw_path: str) -> None:
            target = _resolve_under(root, raw_path)
            mtime = target.stat().st_mtime if target and target.is_file() else 0.0
            self._send(200, "text/plain", f"{mtime}".encode())

        def _html(self, status_code: int, html: str) -> None:
            self._send(status_code, "text/html; charset=utf-8", html.encode("utf-8"))

        def _send(self, status_code: int, content_type: str, body: bytes) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep the watch loop's stdout clean

    return Handler


def start_viewer(
    root: Path, port: int, status: StatusHolder | None = None
) -> ThreadingHTTPServer:
    """Serve ``root`` on ``127.0.0.1:port`` from a daemon thread; return the server.

    Localhost-only by construction. ``status`` (optional) feeds the live status
    panel — the watch loop pushes snapshots into the same holder. The caller
    keeps the handle and calls ``.shutdown()`` on exit. Binding happens
    synchronously so a port clash surfaces immediately rather than in the thread.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(root, status))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
