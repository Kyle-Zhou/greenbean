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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import markdown as markdown_lib

_MD_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists"]
_POLL_MS = 1500

_PAGE_CSS = """
body { max-width: 48rem; margin: 2rem auto; padding: 0 1rem;
       font: 16px/1.6 -apple-system, system-ui, sans-serif; color: #1a1a1a; }
a { color: #2563eb; } pre { background: #f5f5f5; padding: .75rem;
   overflow-x: auto; border-radius: 6px; } code { font-size: .9em; }
table { border-collapse: collapse; } td, th { border: 1px solid #ddd;
   padding: .3rem .6rem; } nav { font-size: .9em; margin-bottom: 1.5rem; }
"""


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


def _index_body(root: Path) -> str:
    if not root.exists():
        return "<p>No generated docs yet.</p>"
    docs = sorted(p for p in root.rglob("*.md") if p.is_file())
    if not docs:
        return "<p>No generated docs yet.</p>"
    items = []
    for doc in docs:
        rel = doc.relative_to(root).as_posix()
        items.append(f'<li><a href="/{quote(rel)}">{html_lib.escape(rel)}</a></li>')
    return f"<h1>greenbean docs</h1><ul>{''.join(items)}</ul>"


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


def _make_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/__mtime":
                self._mtime(parse_qs(parsed.query).get("path", [""])[0])
                return

            target = _resolve_under(root, parsed.path)
            if target is None:
                self._html(404, _page("not found", "<p>404</p>", poll_path=None))
            elif target == root.resolve() or target.is_dir():
                self._html(200, _page("greenbean docs", _index_body(root), poll_path=None))
            elif target.is_file() and target.suffix == ".md":
                rel = target.relative_to(root.resolve()).as_posix()
                self._html(200, _render_markdown(target, rel))
            else:
                self._html(404, _page("not found", "<p>404</p>", poll_path=None))

        def _mtime(self, raw_path: str) -> None:
            target = _resolve_under(root, raw_path)
            mtime = target.stat().st_mtime if target and target.is_file() else 0.0
            body = f"{mtime}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep the watch loop's stdout clean

    return Handler


def start_viewer(root: Path, port: int) -> ThreadingHTTPServer:
    """Serve ``root`` on ``127.0.0.1:port`` from a daemon thread; return the server.

    Localhost-only by construction. The caller (``watch``) keeps the handle and
    calls ``.shutdown()`` on exit. Binding happens synchronously so a port
    clash surfaces immediately rather than from inside the thread.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
