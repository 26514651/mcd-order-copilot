#!/usr/bin/env python3
"""Fetch the official product images referenced by fixtures/menu.json.

Why download instead of hot-linking?
    The demo page must work on a phone with zero setup. If the official CDN
    ever rejects a hot-linked request (referer policy, rate limit), the page
    would show broken images -- which is worse than not showing the page at all.
    The demo page therefore loads local copies first and falls back to the CDN.

Usage:
    python scripts/fetch_images.py [--out docs/images] [--jobs 8]

Note:
    Images are property of McDonald's China and are used here for information
    display only, sourced from the official data interface.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (compatible; mcd-order-copilot/0.1; demo image cache)"
REFERER = "https://www.mcd.cn/"


def fetch_one(code: str, url: str, out_dir: str) -> tuple[str, str]:
    """Return (code, status). Status is 'ok' / 'skip' / error text."""
    ext = os.path.splitext(url.split("?")[0])[1].lower() or ".png"
    path = os.path.join(out_dir, f"{code}{ext}")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return code, "skip"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    try:
        data = urllib.request.urlopen(req, timeout=30).read()
        if len(data) < 100:
            return code, "empty"
        with open(path, "wb") as fh:
            fh.write(data)
        return code, "ok"
    except Exception as exc:  # network is allowed to fail; caller reports it
        return code, f"FAIL {type(exc).__name__}: {exc}"


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    ap = argparse.ArgumentParser()
    ap.add_argument("--menu", default=os.path.join(root, "fixtures", "menu.json"))
    ap.add_argument("--out", default=os.path.join(root, "docs", "images"))
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()

    with open(args.menu, encoding="utf-8") as fh:
        menu = json.load(fh)

    os.makedirs(args.out, exist_ok=True)
    tasks = [(code, item["image"]) for code, item in menu.items() if item.get("image")]
    print(f"menu items   : {len(menu)}")
    print(f"with image   : {len(tasks)}")
    print(f"output dir   : {args.out}")
    print("-" * 56)

    ok = skip = 0
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for code, status in pool.map(lambda t: fetch_one(t[0], t[1], args.out), tasks):
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                failures.append((code, status))
                print(f"  [fail] {code}: {status}")

    total = sum(
        os.path.getsize(os.path.join(args.out, f))
        for f in os.listdir(args.out)
        if os.path.isfile(os.path.join(args.out, f))
    )
    print("-" * 56)
    print(f"downloaded   : {ok}")
    print(f"cached       : {skip}")
    print(f"failed       : {len(failures)}")
    print(f"total size   : {total / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
