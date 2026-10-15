#!/usr/bin/env python3
"""Live store mode -- a tiny backend proxy that sits in front of the demo page.

Why a backend is unavoidable
----------------------------
The page cannot call ``mcp.mcd.cn`` itself, for two independent reasons:

1. **No CORS.** An ``OPTIONS`` preflight against the endpoint answers 404 with
   no ``Access-Control-*`` headers at all, so a browser refuses the request
   before the token even enters the picture. Measured, not assumed -- see
   ``docs/evidence-real.md``.
2. **The token must never ship inside the HTML.** Anything baked into the page
   is readable by anyone who picks "view source".

So the browser talks to this process, and this process talks to McDonald's.

Token handling
--------------
The token arrives on every request in the ``X-MCD-Token`` header. It is held in
memory for the lifetime of that request, is never written to disk, and is never
logged. The page keeps the user's own token in its own ``localStorage``.

The server deliberately does **not** read ``MCD_MCP_TOKEN`` from the
environment unless you pass ``--allow-env-token``. A public deployment must not
carry a shared credential that anonymous visitors could spend.

Only read-only tools are called: ``query-nearby-stores``, ``query-meals``,
``query-meal-detail`` and ``list-nutrition-foods``. No order, coupon, points or
lottery tool is ever touched.

Usage:
    python server.py                 # http://127.0.0.1:8777
    python server.py --port 8080
    PORT=8080 python server.py       # picked up from the environment
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcd_order_copilot.mcp_client import (  # noqa: E402
    MCPClient,
    MCPError,
    MCPTransportError,
)
from mcd_order_copilot.payload import build_payload  # noqa: E402

CST = timezone(timedelta(hours=8))

#: 16 concurrent detail calls measured at 2.55s for a full 115-item menu.
#: 24 would shave another 0.7s but starts to look like hammering.
DETAIL_WORKERS = int(os.environ.get("MCD_DETAIL_WORKERS", "16"))
#: Hard ceiling so one pathological menu cannot stall a worker forever.
MAX_DETAILS = int(os.environ.get("MCD_MAX_DETAILS", "160"))
#: A store menu barely changes minute to minute; 5 minutes saves the network
#: round trip when someone flips between two stores or reloads.
CACHE_TTL = int(os.environ.get("MCD_CACHE_TTL", "300"))
#: Store-menu builds are expensive; cap how many run at once.
MAX_PARALLEL_BUILDS = int(os.environ.get("MCD_MAX_PARALLEL", "4"))

#: Never serve these, whatever the path says. The project source itself is MIT
#: licensed and public, so it stays reachable -- what must not leak is a local
#: config that could carry a real token.
DENY_PREFIXES = (".git", ".workbuddy", "node_modules", "__pycache__", ".venv")
DENY_NAMES = {"my-mcp-config.local.json"}

#: Text assets worth compressing. The demo page is a single 300 KB+ HTML file,
#: so this is the difference between a pleasant and a painful first load.
GZIP_EXTS = {".html", ".htm", ".css", ".js", ".json", ".svg", ".txt", ".md"}

#: Empty unless ``--allow-env-token`` was passed. See the module docstring.
ENV_TOKEN_FALLBACK = ""


# --------------------------------------------------------------------- helpers


def _fingerprint(token: str) -> str:
    """A short, non-reversible handle for a token -- safe to use as a cache key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


_thread_state = threading.local()


def _thread_client(token: str) -> MCPClient:
    """One MCP client per worker thread, reused across calls.

    Each client carries its own session id, so sharing one across threads would
    interleave two JSON-RPC conversations on a single session.
    """
    client = getattr(_thread_state, "client", None)
    if client is None or getattr(_thread_state, "token", None) != token:
        client = MCPClient(token=token)
        client.connect()
        _thread_state.client = client
        _thread_state.token = token
    return client


class _Cache:
    """Tiny TTL cache. Keys are token fingerprints, never raw tokens."""

    def __init__(self, ttl: int) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[tuple, tuple[float, object]] = {}

    def get(self, key: tuple):
        with self._lock:
            hit = self._data.get(key)
        if not hit:
            return None
        expires, value = hit
        if expires < time.time():
            with self._lock:
                self._data.pop(key, None)
            return None
        return value

    def put(self, key: tuple, value) -> None:
        with self._lock:
            if len(self._data) > 64:  # keep it bounded
                now = time.time()
                for stale in [k for k, (exp, _) in self._data.items() if exp < now]:
                    self._data.pop(stale, None)
            self._data[key] = (time.time() + self.ttl, value)


CACHE = _Cache(CACHE_TTL)
_BUILD_SLOTS = threading.Semaphore(MAX_PARALLEL_BUILDS)


# ------------------------------------------------------------------ live calls


def _safe_detail(token: str, store_code: str, code: str) -> dict | None:
    """Fetch one meal detail. A single failure must not sink the whole menu."""
    try:
        return _thread_client(token).query_meal_detail(store_code, code) or None
    except (MCPError, MCPTransportError):
        return None
    except Exception:  # noqa: BLE001 - a malformed item must not kill the request
        return None


def fetch_stores(token: str, city: str, keyword: str) -> list[dict]:
    """Resolve ``city`` + a location keyword to nearby restaurants.

    Note the API takes a *place keyword*, not a latitude/longitude pair --
    ``query-nearby-stores`` has no coordinate parameters at all, so "the store
    closest to me" has to be approximated by naming a landmark or district.
    """
    client = MCPClient(token=token)
    client.connect()
    args: dict = {"beType": 1, "searchType": 2, "keyword": keyword or "麦当劳"}
    if city:
        args["city"] = city
    result = client.call_tool("query-nearby-stores", args)
    data = result.data if isinstance(result.data, dict) else {}
    stores = data.get("data")
    if not isinstance(stores, list):
        return []
    out: list[dict] = []
    for store in stores:
        if not isinstance(store, dict):
            continue
        try:
            distance = int(float(store.get("distance") or 0))
        except (TypeError, ValueError):
            distance = 0
        out.append(
            {
                "storeCode": str(store.get("storeCode", "")),
                "storeName": store.get("storeName", ""),
                # The field really is ``address`` -- ``storeAddress`` is the name
                # used by a different endpoint and reads as empty here.
                "address": store.get("address", ""),
                "distance": distance,
                "open": bool(store.get("businessStatus")),
                "openFrom": store.get("businessStartTime", ""),
                "openTo": store.get("businessEndTime", ""),
            }
        )
    out.sort(key=lambda s: s["distance"] or 10**9)
    return [s for s in out if s["storeCode"]]


def fetch_store_menu(token: str, store_code: str) -> dict:
    """Build a complete demo payload for one restaurant, right now.

    Every item gets a detail call so combo rounds resolve and the totals in the
    list are real. A full 115-item menu takes about 2.5s at 16 workers, which is
    why the cache in front of this is worth having.
    """
    cached = CACHE.get((_fingerprint(token), store_code))
    if cached is not None:
        return cached

    if not _BUILD_SLOTS.acquire(blocking=True, timeout=90):
        raise RuntimeError("服务器正忙，请稍后重试")
    try:
        # Another thread may have finished the same build while we waited.
        cached = CACHE.get((_fingerprint(token), store_code))
        if cached is not None:
            return cached

        client = _thread_client(token)
        nutrition = client.list_nutrition_foods()

        meals_payload = client.query_meals(store_code)
        meals = meals_payload.get("meals") or {}
        categories = meals_payload.get("categories") or []
        if not meals:
            raise RuntimeError("这家门店暂时取不到菜单（可能已打烊或不在营业范围）")

        codes = [str(code) for code in meals][:MAX_DETAILS]
        details: dict[str, dict] = {}
        if codes:
            with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
                for code, detail in zip(
                    codes, pool.map(lambda c: _safe_detail(token, store_code, c), codes)
                ):
                    if detail:
                        details[code] = detail

        store = next(
            (s for s in _recent_stores() if s.get("storeCode") == store_code),
            {},
        )

        now = datetime.now(CST)
        payload = build_payload(
            menu=meals,
            details=details,
            nutrition=nutrition,
            store={"storeCode": store_code, "storeName": store.get("storeName", "")},
            meta={
                "source": "live",
                "live": True,
                "captured_at": now.isoformat(timespec="seconds"),
                "store": {"storeCode": store_code, "storeName": store.get("storeName", "")},
                "counts": {
                    "nutrition": len(nutrition),
                    "menu": len(meals),
                    "categories": len(categories),
                    "meal_details": len(details),
                },
                "read_only": True,
                "note": "实时取自麦当劳 MCP 官方接口，未做任何估算。",
            },
        )
        CACHE.put((_fingerprint(token), store_code), payload)
        return payload
    finally:
        _BUILD_SLOTS.release()


_RECENT_STORES: list[dict] = []
_RECENT_LOCK = threading.Lock()


def _remember_stores(stores: list[dict]) -> None:
    global _RECENT_STORES
    with _RECENT_LOCK:
        merged = {s["storeCode"]: s for s in stores}
        for old in _RECENT_STORES:
            merged.setdefault(old["storeCode"], old)
        _RECENT_STORES = list(merged.values())[:200]


def _recent_stores() -> list[dict]:
    with _RECENT_LOCK:
        return list(_RECENT_STORES)


# -------------------------------------------------------------------- handlers


class Handler(SimpleHTTPRequestHandler):
    server_version = "mcd-order-copilot"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # ------------------------------------------------------------- utilities

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        # Default logging writes the request line, which is fine -- we never put
        # the token in a URL. Keep it terse.
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if accepts_gzip and len(body) > 1024:
            body = gzip.compress(body, 6)
            encoding = "gzip"
        else:
            encoding = "identity"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _token(self) -> str:
        return (self.headers.get("X-MCD-Token") or "").strip()

    # ---------------------------------------------------------- static guard

    def translate_path(self, path):
        resolved = super().translate_path(path)
        rel = os.path.relpath(resolved, ROOT).replace("\\", "/")
        head = rel.split("/", 1)[0]
        if head in DENY_PREFIXES or os.path.basename(rel) in DENY_NAMES:
            return os.path.join(ROOT, "__denied__")
        return resolved

    # ------------------------------------------------------------ compression

    def _gzip_static(self, path: str) -> bool:
        """Serve a text asset pre-compressed. Returns True when it handled it.

        ``SimpleHTTPRequestHandler`` streams files raw, and the demo page is a
        single 300 KB+ HTML document -- on a phone that is a noticeable wait.
        gzip takes it to roughly a quarter of that. Returns False for anything
        that is not a compressible file, so the normal path still runs.
        """
        if "gzip" not in (self.headers.get("Accept-Encoding") or "").lower():
            return False
        resolved = self.translate_path(path)
        if os.path.isdir(resolved):
            # ``translate_path`` returns the *directory* for "/" -- the index
            # lookup normally happens inside ``send_head``. A directory asked
            # for without a trailing slash must still get its 301 redirect, so
            # only take over when the request already ends in "/".
            if not path.split("?")[0].endswith("/"):
                return False
            for index in ("index.html", "index.htm"):
                candidate = os.path.join(resolved, index)
                if os.path.isfile(candidate):
                    resolved = candidate
                    break
        if not os.path.isfile(resolved):
            return False
        if os.path.splitext(resolved)[1].lower() not in GZIP_EXTS:
            return False
        try:
            with open(resolved, "rb") as fh:
                raw = fh.read()
        except OSError:
            return False

        body = gzip.compress(raw, 6)
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(resolved))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", self.date_time_string(os.path.getmtime(resolved)))
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return True

    # ----------------------------------------------------------------- verbs

    def do_OPTIONS(self):  # noqa: N802 - stdlib signature
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802 - stdlib signature
        if self.path.split("?")[0] == "/api/ping":
            self._json(
                200,
                {
                    "ok": True,
                    "service": "mcd-order-copilot",
                    "live": True,
                    "detail_workers": DETAIL_WORKERS,
                    "cache_ttl": CACHE_TTL,
                },
            )
            return
        if self._gzip_static(self.path):
            return
        super().do_GET()

    def do_HEAD(self):  # noqa: N802 - stdlib signature
        if self._gzip_static(self.path):
            return
        super().do_HEAD()

    def do_POST(self):  # noqa: N802 - stdlib signature
        path = self.path.split("?")[0]
        if path == "/api/stores":
            self._handle_stores()
        elif path == "/api/store-menu":
            self._handle_store_menu()
        else:
            self._json(404, {"ok": False, "error": "unknown endpoint"})

    # ------------------------------------------------------------- endpoints

    def _handle_stores(self) -> None:
        body = self._read_json()
        token = self._token() or ENV_TOKEN_FALLBACK
        if not token:
            self._json(401, {"ok": False, "error": "缺少 Token。请在页面里填入你的 MCP Token。"})
            return
        city = str(body.get("city") or "").strip()
        keyword = str(body.get("keyword") or "").strip()
        if not city and not keyword:
            self._json(400, {"ok": False, "error": "至少填一个城市或位置关键词。"})
            return
        try:
            stores = fetch_stores(token, city, keyword)
        except MCPTransportError as exc:
            self._json(502, {"ok": False, "error": f"连接麦当劳接口失败：{exc}"})
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._json(502, {"ok": False, "error": f"查询门店失败：{exc}"})
            return
        _remember_stores(stores)
        self._json(200, {"ok": True, "stores": stores, "count": len(stores)})

    def _handle_store_menu(self) -> None:
        body = self._read_json()
        token = self._token() or ENV_TOKEN_FALLBACK
        if not token:
            self._json(401, {"ok": False, "error": "缺少 Token。"})
            return
        store_code = str(body.get("storeCode") or "").strip()
        if not store_code:
            self._json(400, {"ok": False, "error": "缺少 storeCode。"})
            return
        started = time.time()
        try:
            payload = fetch_store_menu(token, store_code)
        except MCPTransportError as exc:
            self._json(502, {"ok": False, "error": f"连接麦当劳接口失败：{exc}"})
            return
        except RuntimeError as exc:
            self._json(503, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._json(502, {"ok": False, "error": f"读取门店菜单失败：{exc}"})
            return
        payload = dict(payload)
        payload["ok"] = True
        payload["elapsed_ms"] = int((time.time() - started) * 1000)
        self._json(200, payload)


def _lan_ip() -> str:
    """Best-effort LAN address, for the printed phone URL. Never fatal."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("223.5.5.5", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return "127.0.0.1"


def main() -> int:
    global ENV_TOKEN_FALLBACK
    parser = argparse.ArgumentParser(description="点单参谋 · 实时门店后端")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8777")))
    parser.add_argument(
        "--allow-env-token",
        action="store_true",
        help="本地调试用：允许回落到 MCD_MCP_TOKEN 环境变量。公开部署不要开。",
    )
    args = parser.parse_args()

    if args.allow_env_token:
        ENV_TOKEN_FALLBACK = os.environ.get("MCD_MCP_TOKEN", "").strip()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("点单参谋 · 实时门店后端")
    print(f"  本机    http://127.0.0.1:{args.port}/")
    print(f"  局域网  http://{_lan_ip()}:{args.port}/")
    print(f"  详情并发 {DETAIL_WORKERS} · 缓存 {CACHE_TTL}s · 并行构建上限 {MAX_PARALLEL_BUILDS}")
    print("  Token 只随请求转发，不落盘、不写日志。Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
