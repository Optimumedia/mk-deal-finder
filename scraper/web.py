"""Your private dashboard: http://localhost:8800 (runs inside the bot task).

Same page as the public GitHub dashboard, but it reads data/private.json
(your votes, seller facts, personalised scores) and adds a small JSON API so
you can rate deals right on the page:

    GET  /api/ping                 → {ok, private: true}
    GET  /api/reasons              → {researcher: [{code, label, group}]}
    POST /api/vote   {id, researcher, vote: up|ok|down|clear, reason?, note?}
    POST /api/facts  {id, researcher, facts: {electricity|water|road: yes|no|unknown,
                                              area_m2, price_eur, land_type, note}}
    POST /api/pipeline {id, researcher, stage?, next_step?, due?, notes?}
    POST /api/rescore              → re-score now (no scraping); GET it for progress

Only listens on 127.0.0.1 — nothing is reachable from your network or the internet.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import feedback, reasons

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PRIVATE_JSON = ROOT / "data" / "private.json"
log = logging.getLogger("web")

_rescore = {"running": False, "started_at": None, "finished_at": None, "error": None}
_lock = threading.Lock()
FACT_FIELDS = {"electricity", "water", "road", "area_m2", "price_eur", "land_type", "note"}


def _listing(listing_id: str) -> dict | None:
    from .bot import listing_info          # read-only lookup in deals.db
    return listing_info(listing_id)


def vote(body: dict) -> dict:
    lid, researcher, v = body.get("id"), body.get("researcher"), body.get("vote")
    if not lid or researcher not in reasons.ALL or v not in ("up", "ok", "down", "clear"):
        return {"ok": False, "error": "id, researcher and vote (up|ok|down|clear) are required"}
    with feedback.closing(feedback.connect()) as conn:
        if v == "clear":
            feedback.undo(conn, lid, researcher)
            feedback.pipeline_remove(conn, lid)
            return {"ok": True}
        info = _listing(lid)
        feedback.undo(conn, lid, researcher)                 # one current opinion per deal
        if v == "down":
            code = body.get("reason") or "oth"
            if code not in reasons.BY_CODE:
                return {"ok": False, "error": f"unknown reason {code}"}
            feedback.record(conn, lid, researcher, -1, info)
            feedback.reject(conn, lid, researcher, code, info)
            note = (body.get("note") or "").strip()
            if note:
                feedback.add_rejection_note(conn, lid, note[:500])
            feedback.pipeline_remove(conn, lid)
        else:
            feedback.record(conn, lid, researcher, 1, info)
            if v == "ok":
                feedback.confirm(conn, lid)
            feedback.pipeline_add(conn, lid, researcher)       # 👍 / ✅ → My pipeline
    log.info("vote %s %s %s %s", v, researcher, lid, body.get("reason") or "")
    return {"ok": True}


def facts(body: dict) -> dict:
    lid, f = body.get("id"), body.get("facts") or {}
    if not lid or not isinstance(f, dict):
        return {"ok": False, "error": "id and facts are required"}
    with feedback.closing(feedback.connect()) as conn:
        for k, v in f.items():
            if k not in FACT_FIELDS:
                continue
            if k in ("electricity", "water", "road"):
                if v in ("yes", "no"):
                    feedback.set_override(conn, lid, k, v)
                elif v in ("unknown", None, ""):
                    conn.execute("DELETE FROM overrides WHERE listing_id=? AND field=?", (lid, k))
                    conn.commit()
            elif k in ("area_m2", "price_eur"):
                if v in (None, ""):
                    conn.execute("DELETE FROM overrides WHERE listing_id=? AND field=?", (lid, k))
                    conn.commit()
                else:
                    try:
                        n = float(v)
                    except (TypeError, ValueError):
                        return {"ok": False, "error": f"{k} must be a number"}
                    if n <= 0:
                        return {"ok": False, "error": f"{k} must be positive"}
                    feedback.set_override(conn, lid, k, n)
            elif k == "land_type":
                if v in ("building", "agricultural"):
                    feedback.set_override(conn, lid, k, v)
                else:
                    conn.execute("DELETE FROM overrides WHERE listing_id=? AND field='land_type'", (lid,))
                    conn.commit()
            elif k == "note":
                if v in (None, ""):
                    conn.execute("DELETE FROM overrides WHERE listing_id=? AND field='note'", (lid,))
                    conn.commit()
                else:
                    feedback.set_override(conn, lid, "note", str(v)[:500])
    log.info("facts %s %s", lid, f)
    return {"ok": True}


def pipeline(body: dict) -> dict:
    lid = body.get("id")
    if not lid:
        return {"ok": False, "error": "id is required"}
    fields = {k: body[k] for k in ("stage", "next_step", "due", "notes") if k in body}
    if "stage" in fields and fields["stage"] not in feedback.STAGES:
        return {"ok": False, "error": f"stage must be one of {', '.join(feedback.STAGES)}"}
    if fields.get("due"):
        try:
            from datetime import date
            date.fromisoformat(fields["due"])
        except ValueError:
            return {"ok": False, "error": "due must be a date (YYYY-MM-DD)"}
    with feedback.closing(feedback.connect()) as conn:
        if not feedback.pipeline_update(conn, lid, **fields):
            # not there yet (e.g. moved straight from a list) — add, then update
            feedback.pipeline_add(conn, lid, body.get("researcher") or "flip")
            feedback.pipeline_update(conn, lid, **fields)
    log.info("pipeline %s %s", lid, fields)
    return {"ok": True}


def start_rescore() -> dict:
    with _lock:
        if _rescore["running"]:
            return {"ok": True, **_rescore}
        _rescore.update(running=True, started_at=time.time(), error=None)

    def work():
        try:
            from .run import rescore
            rescore()
            _rescore["finished_at"] = time.time()
        except Exception as e:                      # surfaced to the page, never kills the server
            log.exception("rescore failed")
            _rescore["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            _rescore["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, **_rescore}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DOCS), **kw)

    def log_message(self, fmt, *args):            # quiet: errors only
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _local_only(self) -> bool:
        # The page and the API must come from this PC (blocks other sites posting here).
        origin = self.headers.get("Origin")
        port = self.server.server_port
        return origin in (None, f"http://localhost:{port}", f"http://127.0.0.1:{port}", f"http://[::1]:{port}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/ping":
            return self._json({"ok": True, "private": True})
        if path == "/api/reasons":
            return self._json({r: [{"code": x.code, "label": x.label, "group": x.group} for x in reasons.for_researcher(r)]
                               for r in reasons.ALL})
        if path == "/api/rescore":
            return self._json({"ok": True, **_rescore})
        if path == "/private.json":
            if not PRIVATE_JSON.exists():
                return self._json({"ok": False, "error": "no private data yet — click Re-score now"}, 404)
            data = PRIVATE_JSON.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        return super().do_GET()

    def do_POST(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "forbidden"}, 403)
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"ok": False, "error": "bad JSON"}, 400)
        handler = {"/api/vote": vote, "/api/facts": facts, "/api/pipeline": pipeline,
                   "/api/rescore": lambda _b: start_rescore()}.get(path)
        if not handler:
            return self._json({"ok": False, "error": "not found"}, 404)
        try:
            res = handler(body)
        except Exception as e:
            log.exception("api %s failed", path)
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        return self._json(res, 200 if res.get("ok") else 400)


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve(port: int = 8800, block: bool = False) -> ThreadingHTTPServer | None:
    """Listen on the IPv4 and IPv6 loopback addresses only.

    Windows resolves "localhost" to ::1 first; with an IPv4-only server every
    request to http://localhost waited ~2 s for the fallback (the desktop
    shortcut timed out on exactly that).
    """
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        log.error("private dashboard not started (port %d busy?): %s", port, e)
        return None
    servers = [httpd]
    try:
        servers.append(_V6Server(("::1", port), Handler))
    except OSError as e:                      # no IPv6 loopback: IPv4 still works
        log.warning("IPv6 loopback not available: %s", e)
    log.info("private dashboard on http://localhost:%d", port)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    if block:
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.extra_servers = servers[1:]         # so callers (tests) can close them too
    return httpd


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    serve(block=True)
