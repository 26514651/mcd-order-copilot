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
Two ways in, and the difference between them matters.

**The visitor's own token** arrives on every request in the ``X-MCD-Token``
header. It is held in memory for the lifetime of that request, is never written
to disk, and is never logged. The page keeps it in its own ``localStorage``.

**A shared demo token** may be supplied once at start-up -- via ``--shared-token``,
via the ``MCD_MCP_TOKEN`` environment variable, or via a ``.mcd-token`` file
sitting next to this script -- so a visitor can try live mode without owning a
token. This matters because the public API issues tokens only to registered
developers, which is to say almost none of the people you would send a demo link
to can get one. When configured, the shared token is spent on general endpoints
only:

======================  ==================================================
``GET  /api/ping``      reports whether a shared token is available
``POST /api/stores``    city + landmark -> nearby restaurants
``POST /api/store-menu`` one store's live menu
``POST /api/geo-stores`` coordinates -> place words -> nearby restaurants
``POST /api/stores-by-place``  a place -> nearby restaurants
======================  ==================================================

``POST /api/addresses`` is **excluded on purpose**. It reads the *caller's own*
saved delivery addresses, so accepting a shared token there would hand every
anonymous visitor the owner's home address. It always requires a token supplied
by the caller.

Anonymous traffic on the shared token is rate limited (see the ``RATE_*``
constants): the documented ceiling is 600 requests per minute for a whole token,
and building one store menu costs about 115 of them. Visitors who bring their own
token are never limited by this server.

Only read-only tools are called: ``query-nearby-stores``, ``query-meals``,
``query-meal-detail``, ``list-nutrition-foods`` and ``delivery-query-addresses``
(the account's own saved delivery addresses). No order, coupon, points or
lottery tool is ever touched.

Getting from "where I am" to "what's around me" needs one extra hop, because
``query-nearby-stores`` classifies by *place name* and ignores coordinates
entirely. Photon (OpenStreetMap, no API key required) turns a GPS fix into
Chinese place words; the coordinate itself is used for that single call and is
never stored.

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
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

#: Extensions never served over HTTP. The source is public on GitHub anyway, but
#: a demo site should serve a page, not hand out its own backend -- and a
#: deployment directory always ends up next to whatever else was lying around.
DENY_EXTS = {".py", ".pyc", ".pyo", ".pyd", ".md", ".toml", ".cfg", ".ini", ".log", ".bak"}

DENY_NAMES = {
    "my-mcp-config.local.json",
    ".mcd-token",
    "mcd-token",
    "mcd-token.txt",
    "shared-token.txt",
    "token.txt",
}

#: Text assets worth compressing. The demo page is a single 300 KB+ HTML file,
#: so this is the difference between a pleasant and a painful first load.
GZIP_EXTS = {".html", ".htm", ".css", ".js", ".json", ".svg", ".txt", ".md"}

#: Where a deployment may drop the shared demo token. Read at start-up, held in
#: memory, never echoed to a client and never written to a log. Several names are
#: tried because some upload paths quietly drop dot-files.
SHARED_TOKEN_FILES = (".mcd-token", "mcd-token.txt", "shared-token.txt")

#: Empty means "every visitor brings their own token". See the module docstring.
SHARED_TOKEN = ""

# ---- shared-token budget ----------------------------------------------------
#: The documented ceiling is 600 requests/minute for a whole token, and a single
#: store menu costs ~115 calls, so an unbounded shared token would be drained by
#: a handful of concurrent visitors. These caps keep the demo usable for
#: everyone instead of fast for the first five. Same-store menus are cached for
#: CACHE_TTL seconds and shared across visitors, which is what makes a small
#: per-visitor budget go a long way.
RATE_PER_MIN = int(os.environ.get("MCD_RATE_PER_MIN", "10"))
RATE_PER_DAY = int(os.environ.get("MCD_RATE_PER_DAY", "60"))
RATE_GLOBAL_PER_MIN = int(os.environ.get("MCD_RATE_GLOBAL_PER_MIN", "200"))


# --------------------------------------------------------------------- helpers


def _client_ip(handler: "Handler") -> str:
    """Best-effort caller identity, for metering the shared demo budget.

    The deployment sits behind a reverse proxy, so ``client_address`` is the
    proxy rather than the visitor; ``X-Forwarded-For`` carries the real caller.
    Both are attacker-controlled in the general case, which is acceptable here --
    this only has to stop casual over-use of a demo quota, not a determined
    attacker, and nothing of value is unlocked by faking it.
    """
    forwarded = handler.headers.get("X-Forwarded-For") or ""
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    if handler.client_address:
        return str(handler.client_address[0])[:64]
    return "unknown"


class _RateLimiter:
    """Sliding-window counters for the shared token, in memory only.

    Two windows per caller plus one global window. A plain dict behind a lock is
    the whole implementation -- this is a demo proxy running as a single
    process, not a load balancer. Set a cap to 0 to disable that window.
    """

    def __init__(self, per_minute: int, per_day: int, global_per_minute: int) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self.global_per_minute = global_per_minute
        self._lock = threading.Lock()
        self._minute: dict[str, list[float]] = {}
        self._day: dict[str, list[float]] = {}
        self._global: list[float] = []

    @staticmethod
    def _trim(stamps: list[float], window: float, now: float) -> None:
        """Drop stamps older than the window. Appends are ordered, so the
        expired ones are always a prefix -- no need to walk the whole list."""
        cut = now - window
        drop = 0
        for stamp in stamps:
            if stamp >= cut:
                break
            drop += 1
        if drop:
            del stamps[:drop]

    def check(self, key: str) -> str:
        """Record one use. Return "" when allowed, else a message for the visitor."""
        now = time.time()
        with self._lock:
            self._trim(self._global, 60.0, now)
            if self.global_per_minute and len(self._global) >= self.global_per_minute:
                return ("这会儿体验的人有点多，演示额度每分钟有上限。"
                        "等一分钟再点，或者填上你自己的 MCP Token。")

            minute = self._minute.setdefault(key, [])
            self._trim(minute, 60.0, now)
            if self.per_minute and len(minute) >= self.per_minute:
                return f"点得有点快（每分钟上限 {self.per_minute} 次），歇一分钟再来。"

            day = self._day.setdefault(key, [])
            self._trim(day, 86400.0, now)
            if self.per_day and len(day) >= self.per_day:
                return (f"今天的演示额度用完了（每天上限 {self.per_day} 次），明天恢复。"
                        "现在想接着用，可以填上你自己的 MCP Token。")

            minute.append(now)
            day.append(now)
            self._global.append(now)
            self._sweep(now)
        return ""

    def _sweep(self, now: float) -> None:
        """Forget callers who have gone quiet, so the dicts cannot grow forever.
        Called with the lock held."""
        if len(self._minute) < 500:
            return
        for store, window in ((self._minute, 60.0), (self._day, 86400.0)):
            stale = [key for key, stamps in store.items() if not stamps or now - stamps[-1] > window]
            for key in stale:
                del store[key]


RATE_LIMITER = _RateLimiter(RATE_PER_MIN, RATE_PER_DAY, RATE_GLOBAL_PER_MIN)


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


def _query_stores(client: MCPClient, city: str, keyword: str) -> list[dict]:
    """One ``query-nearby-stores`` call, normalised into plain dicts.

    Note the API takes a *place keyword*, not a latitude/longitude pair --
    ``query-nearby-stores`` has no coordinate parameters at all, and an injected
    ``latitude`` field is silently ignored (measured). "The store closest to me"
    can therefore only be approximated by naming the place we are standing in.
    """
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


def fetch_stores(token: str, city: str, keyword: str) -> list[dict]:
    """Resolve ``city`` + a location keyword to nearby restaurants."""
    client = MCPClient(token=token)
    client.connect()
    return _query_stores(client, city, keyword)


# ---------------------------------------------------------- place resolution
#
# ``query-nearby-stores`` speaks *place names*, never coordinates. Getting from
# a GPS fix to "what is around me" therefore needs one extra hop: coordinate ->
# Chinese place words -> the API. Photon (OpenStreetMap) does the first hop
# because it needs no API key, which matters for a project a reviewer should be
# able to run without signing up for anything. It only ever supplies *words*;
# every number the user sees still comes from McDonald's.

GEOCODE_ENDPOINT = os.environ.get("MCD_GEOCODE_ENDPOINT", "https://photon.komoot.io/reverse")

_DISTRICT_RE = re.compile(r"([\u4e00-\u9fa5]{2,5}?(?:区|县))")
_STREET_RE = re.compile(r"([\u4e00-\u9fa5]{2,9}?(?:街道|大道|路|街|巷))")


def place_from_address(full_address: str) -> dict:
    """Pull city / district / street words out of a Chinese address string.

    Used for addresses the user already saved with McDonald's, e.g.
    ``广东省广州市荔湾区逢源街道何家祠道14号`` -> 广州 / 荔湾区 / 逢源街道.
    """
    text = (full_address or "").strip()
    if not text:
        return {}
    city = ""
    # ``[^省市区]`` keeps "广东省广州市" from capturing as "广东省广州", and the
    # ``(?:^|[省区])`` anchor stops it from starting mid-word.
    match = re.search(r"(?:^|[省区])([^省市区]{2,5}?)市", text)
    tail = text
    if match:
        city = match.group(1)
        tail = text[match.end():]
    district = ""
    match = _DISTRICT_RE.match(tail)
    if match:
        district = match.group(1)
        tail = tail[match.end():]
    street = ""
    match = _STREET_RE.match(tail)
    if match:
        street = match.group(1)
    return {"city": city, "district": district, "street": street, "name": "", "full": text}


def _place_keys(props: dict) -> tuple[str, str, str, str]:
    city = str(props.get("city") or props.get("county") or "").replace("市", "")
    district = str(props.get("district") or props.get("suburb") or "")
    street = str(props.get("street") or "")
    name = str(props.get("name") or "")
    return city, district, street, name


def reverse_geocode(lat: float, lng: float) -> dict:
    """Coordinate -> Chinese place words via Photon (OpenStreetMap).

    Several nearby features are requested, not just the closest one: the
    nearest OSM object to a GPS fix is often a block of flats nobody has heard
    of, while the street or the neighbouring mall is what really pins the
    location down. Each of them becomes a candidate keyword.
    """
    query = urllib.parse.urlencode({"lat": f"{lat:.6f}", "lon": f"{lng:.6f}", "limit": 6})
    request = urllib.request.Request(
        f"{GEOCODE_ENDPOINT}?{query}",
        headers={
            "User-Agent": "mcd-order-copilot/0.1 (demo; contact via GitHub repo)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    features = [f.get("properties") or {} for f in (payload.get("features") or [])]
    if not features:
        return {}
    city, district, street, name = _place_keys(features[0])
    if not (city or district or street or name):
        return {}

    ordered: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in ordered:
            ordered.append(value)

    # Most specific shape first, then the individual pieces. Measured: even a
    # district+street pair can land a kilometre off ("琶洲街道阅江西路"
    # resolved into 琶洲西, a kilometre from the fix), so the alternatives
    # genuinely are not redundant.
    for props in features:
        _, dist, st, _ = _place_keys(props)
        if dist and st:
            add(dist + st)
    for props in features:
        add(_place_keys(props)[2])
    for props in features:
        add(_place_keys(props)[3])
    for props in features:
        add(_place_keys(props)[1])

    return {
        "city": city,
        "district": district,
        "street": street,
        "name": name,
        "full": " ".join(p for p in (city, district, street, name) if p),
        "candidates": ordered[:6],
    }


def keyword_candidates(place: dict) -> list[str]:
    """Place strings worth trying, most specific first.

    Measured behaviour: the API geocodes whatever string it is handed, so a
    district+street pair usually lands closer than either half alone -- but no
    single shape is right every time. Several are tried and the answers merged.
    """
    listed = place.get("candidates")
    if isinstance(listed, list) and listed:
        return [str(c).strip() for c in listed if str(c).strip()][:6]
    district = (place.get("district") or "").strip()
    street = (place.get("street") or "").strip()
    name = (place.get("name") or "").strip()
    candidates: list[str] = []
    for value in (district + street, street, name, district):
        if value and value not in candidates:
            candidates.append(value)
    return candidates[:6]


def _safe_query(token: str, city: str, keyword: str) -> list[dict]:
    """One candidate keyword. A failure here must not sink the whole lookup."""
    try:
        return _query_stores(_thread_client(token), city, keyword)
    except (MCPError, MCPTransportError):
        return []
    except Exception:  # noqa: BLE001
        return []


def fetch_stores_by_place(token: str, place: dict) -> dict:
    """Query every candidate place name at once, merge by storeCode.

    Sequentially that is six round trips -- several seconds of spinner on a
    phone. In parallel it costs about one.
    """
    candidates = keyword_candidates(place)
    if not candidates:
        raise RuntimeError("没能从定位结果里认出地名，请手动填写城市和地标。")
    city = place.get("city") or ""
    merged: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as pool:
        for keyword, stores in zip(
            candidates, pool.map(lambda kw: _safe_query(token, city, kw), candidates)
        ):
            for store in stores:
                code = store["storeCode"]
                previous = merged.get(code)
                if previous is None or (store["distance"] or 10**9) < (previous["distance"] or 10**9):
                    merged[code] = store
    stores = sorted(merged.values(), key=lambda s: s["distance"] or 10**9)
    return {"place": place, "keywords": candidates, "stores": stores}


def fetch_saved_addresses(token: str) -> list[dict]:
    """Addresses this account already saved with McDonald's (delivery book).

    Read-only, and the single best substitute for GPS: no third party involved,
    no typing for the user, and it resolves to a real street-level address.
    """
    client = MCPClient(token=token)
    client.connect()
    result = client.call_tool("delivery-query-addresses", {})
    data = result.data if isinstance(result.data, dict) else {}
    addresses = ((data.get("data") or {}).get("addresses")) or []
    out: list[dict] = []
    for row in addresses:
        if not isinstance(row, dict):
            continue
        full = str(row.get("fullAddress") or "").strip()
        if not full:
            continue
        place = place_from_address(full)
        out.append(
            {
                "addressId": str(row.get("addressId") or ""),
                "contactName": str(row.get("contactName") or ""),
                "fullAddress": full,
                "city": place.get("city") or "",
                "district": place.get("district") or "",
                "street": place.get("street") or "",
            }
        )
    return out


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

    def _resolve_token(self) -> tuple[str, str, int]:
        """Pick the token for a *general* endpoint.

        Returns ``(token, "", 200)`` on success or ``("", message, status)`` when
        the caller has neither their own token nor a shared one to fall back on
        (401), or has used up their share of the shared demo budget (429). A
        visitor who supplies their own token is never metered -- we are only
        rationing the owner's quota.
        """
        own = self._token()
        if own:
            return own, "", 200
        if not SHARED_TOKEN:
            return "", "缺少 Token。请在页面里填入你自己的 MCP Token。", 401
        reason = RATE_LIMITER.check(_client_ip(self))
        if reason:
            return "", reason, 429
        return SHARED_TOKEN, "", 200

    def _require_own_token(self) -> str:
        """A token supplied by the caller, for endpoints that must not borrow
        the shared one -- anything that reads *their* account rather than
        public menu data."""
        return self._token()

    # ---------------------------------------------------------- static guard

    def translate_path(self, path):
        resolved = super().translate_path(path)
        rel = os.path.relpath(resolved, ROOT).replace("\\", "/")
        head = rel.split("/", 1)[0]
        if (
            head in DENY_PREFIXES
            or os.path.basename(rel) in DENY_NAMES
            or os.path.splitext(rel)[1].lower() in DENY_EXTS
        ):
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
                    # The page uses this to decide whether to ask for a token at
                    # all: with a shared one configured, a visitor can just click.
                    "shared_token": bool(SHARED_TOKEN),
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
        elif path == "/api/geo-stores":
            self._handle_geo_stores()
        elif path == "/api/stores-by-place":
            self._handle_stores_by_place()
        elif path == "/api/addresses":
            self._handle_addresses()
        else:
            self._json(404, {"ok": False, "error": "unknown endpoint"})

    # ------------------------------------------------------------- endpoints

    def _handle_stores(self) -> None:
        body = self._read_json()
        token, why, status = self._resolve_token()
        if not token:
            self._json(status, {"ok": False, "error": why})
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
        token, why, status = self._resolve_token()
        if not token:
            self._json(status, {"ok": False, "error": why})
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

    def _handle_geo_stores(self) -> None:
        """Coordinate -> place words -> nearby stores. Used by "用我的位置".

        The coordinate never leaves this process: it is converted to a place
        name here and discarded, so no location is stored on either side.
        """
        body = self._read_json()
        token, why, status = self._resolve_token()
        if not token:
            self._json(status, {"ok": False, "error": why})
            return
        try:
            lat = float(body.get("lat"))
            lng = float(body.get("lng"))
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "缺少有效的经纬度。"})
            return
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            self._json(400, {"ok": False, "error": "经纬度超出范围。"})
            return

        try:
            place = reverse_geocode(lat, lng)
        except Exception as exc:  # noqa: BLE001 - degrade to manual entry
            self._json(
                503,
                {
                    "ok": False,
                    "error": f"拿到定位了，但地名反查服务暂时不可用（{type(exc).__name__}）。"
                             "请手动填写城市和地标。",
                },
            )
            return
        if not place or not (place.get("city") or place.get("street") or place.get("district")):
            self._json(503, {"ok": False, "error": "没能从坐标认出地名，请手动填写城市和地标。"})
            return

        try:
            result = fetch_stores_by_place(token, place)
        except MCPTransportError as exc:
            self._json(502, {"ok": False, "error": f"连接麦当劳接口失败：{exc}"})
            return
        except RuntimeError as exc:
            self._json(503, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._json(502, {"ok": False, "error": f"查询门店失败：{exc}"})
            return

        stores = result["stores"]
        _remember_stores(stores)
        self._json(
            200,
            {
                "ok": True,
                "place": result["place"],
                "keywords": result["keywords"],
                "stores": stores,
                "count": len(stores),
            },
        )

    def _handle_stores_by_place(self) -> None:
        """Same as above, but the place words come from a saved address."""
        body = self._read_json()
        token, why, status = self._resolve_token()
        if not token:
            self._json(status, {"ok": False, "error": why})
            return
        place = {
            "city": str(body.get("city") or "").strip(),
            "district": str(body.get("district") or "").strip(),
            "street": str(body.get("street") or "").strip(),
            "name": str(body.get("name") or "").strip(),
            "full": str(body.get("fullAddress") or "").strip(),
        }
        if not any(place.get(key) for key in ("city", "district", "street", "name")):
            self._json(400, {"ok": False, "error": "这条地址里没有可用的地名。"})
            return
        try:
            result = fetch_stores_by_place(token, place)
        except MCPTransportError as exc:
            self._json(502, {"ok": False, "error": f"连接麦当劳接口失败：{exc}"})
            return
        except RuntimeError as exc:
            self._json(503, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._json(502, {"ok": False, "error": f"查询门店失败：{exc}"})
            return
        stores = result["stores"]
        _remember_stores(stores)
        self._json(
            200,
            {
                "ok": True,
                "place": result["place"],
                "keywords": result["keywords"],
                "stores": stores,
                "count": len(stores),
            },
        )

    def _handle_addresses(self) -> None:
        """The *caller's own* saved delivery addresses (read-only).

        Deliberately does **not** fall back to the shared demo token. This reads
        a private address book, so borrowing the shared credential here would
        show the deployment owner's home address to every anonymous visitor.
        """
        token = self._require_own_token()
        if not token:
            self._json(
                403,
                {
                    "ok": False,
                    "error": "「我存的地址」读的是你自己麦当劳账号里的地址，"
                             "所以需要填你自己的 MCP Token —— 演示额度不能借到这里。",
                    "needOwnToken": True,
                },
            )
            return
        try:
            addresses = fetch_saved_addresses(token)
        except MCPTransportError as exc:
            self._json(502, {"ok": False, "error": f"连接麦当劳接口失败：{exc}"})
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._json(502, {"ok": False, "error": f"读取地址簿失败：{exc}"})
            return
        self._json(200, {"ok": True, "addresses": addresses, "count": len(addresses)})


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


def _load_shared_token(explicit: str, explicit_file: str) -> tuple[str, str]:
    """Resolve the shared demo token. Returns ``(token, where_it_came_from)``.

    Three sources, most explicit first. The file is last for a reason: the
    publishing sandbox injects only ``PORT``, so a deployment that cannot set
    environment variables still needs a way in -- dropping ``.mcd-token`` next to
    this script is it.
    """
    if explicit.strip():
        return explicit.strip(), "--shared-token"
    env = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if env:
        return env, "MCD_MCP_TOKEN 环境变量"
    candidates: list[Path] = []
    if explicit_file.strip():
        candidates.append(Path(explicit_file.strip()))
    here = Path(__file__).resolve().parent
    candidates.extend(here / name for name in SHARED_TOKEN_FILES)
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value, str(path)
    return "", ""


def main() -> int:
    global SHARED_TOKEN
    parser = argparse.ArgumentParser(description="点单参谋 · 实时门店后端")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8777")))
    parser.add_argument(
        "--shared-token",
        default="",
        help="开放给访客使用的演示 Token。不给则访客必须自带。",
    )
    parser.add_argument(
        "--shared-token-file",
        default="",
        help="从文件读取演示 Token（默认还会依次找脚本同目录的 "
             + " / ".join(SHARED_TOKEN_FILES) + "）。",
    )
    parser.add_argument(
        "--no-shared-token",
        action="store_true",
        help="只用访客自带的 Token，禁用演示额度。",
    )
    args = parser.parse_args()

    source = ""
    if not args.no_shared_token:
        SHARED_TOKEN, source = _load_shared_token(args.shared_token, args.shared_token_file)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("点单参谋 · 实时门店后端")
    print(f"  本机    http://127.0.0.1:{args.port}/")
    print(f"  局域网  http://{_lan_ip()}:{args.port}/")
    print(f"  详情并发 {DETAIL_WORKERS} · 缓存 {CACHE_TTL}s · 并行构建上限 {MAX_PARALLEL_BUILDS}")
    if SHARED_TOKEN:
        print(f"  演示额度 已启用（来源：{source}）")
        print(f"          访客无需自带 Token · 每人 {RATE_PER_MIN}/分钟、{RATE_PER_DAY}/天"
              f" · 全场 {RATE_GLOBAL_PER_MIN}/分钟")
        print("          「我存的地址」不借演示额度 —— 它读的是调用者自己的账号。")
    else:
        print("  演示额度 未配置：访客必须自带 MCP Token")
    print("  Token 只随请求转发，不落盘、不写日志。Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
