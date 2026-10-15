#!/usr/bin/env python3
"""Refresh ``fixtures/`` from the live McDonald's MCP endpoint.

This is the reproducible build step behind the offline demo. It runs the same
read-only tools the product uses, so the fixtures are a genuine capture rather
than something typed by hand. ``docs/evidence-real.md`` is generated from its
call log.

Usage:
    export MCD_MCP_TOKEN=...           # Windows: set MCD_MCP_TOKEN=...
    python scripts/sync_fixtures.py --city 广州
    python scripts/sync_fixtures.py --store-code 1400335 --details all

Only read-only tools are called. No order, coupon redemption, points or lottery
tool is ever touched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcd_order_copilot.mcp_client import MCPClient, MCPError, MCPTransportError  # noqa: E402
from mcd_order_copilot.normalize import classify  # noqa: E402

CST = timezone(timedelta(hours=8))


def write_json(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    size = os.path.getsize(path)
    print(f"  wrote {os.path.relpath(path, ROOT):40s} {size:>9,} B")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="广州")
    parser.add_argument("--store-code", default="")
    parser.add_argument("--keyword", default="麦当劳")
    parser.add_argument(
        "--details",
        choices=["combo", "all", "none"],
        default="combo",
        help="combo=只为套餐拉详情（默认）；all=全部餐品；none=跳过",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    fixture_dir = os.path.join(ROOT, "fixtures")
    os.makedirs(fixture_dir, exist_ok=True)

    try:
        client = MCPClient(verbose=args.verbose)
        info = client.connect()
    except MCPTransportError as exc:
        print(f"connect failed: {exc}", file=sys.stderr)
        return 1

    print(f"connected: {info.get('name')} v{info.get('version')}")
    tools = client.list_tools()
    print(f"tools/list: {len(tools)} tools\n")

    store_code = args.store_code
    store = {}
    if not store_code:
        store = client.find_store(keyword=args.keyword, city=args.city) or {}
        store_code = store.get("storeCode", "")
    if not store_code:
        print("no store found; pass --store-code", file=sys.stderr)
        return 1
    print(f"store: {store.get('storeName', store_code)} ({store_code})\n")

    print("[1/4] list-nutrition-foods")
    nutrition = client.list_nutrition_foods()
    print(f"  {len(nutrition)} rows")
    write_json(os.path.join(fixture_dir, "nutrition.json"), nutrition)

    print("\n[2/4] query-meals")
    meals_payload = client.query_meals(store_code)
    categories = meals_payload.get("categories", [])
    meals = meals_payload.get("meals", {})
    print(f"  {len(categories)} categories / {len(meals)} items")
    write_json(os.path.join(fixture_dir, "menu.json"), meals)

    print("\n[3/4] query-meal-detail")
    details: dict[str, dict] = {}
    targets = [
        (code, entry)
        for code, entry in meals.items()
        if isinstance(entry, dict)
        and (
            args.details == "all"
            or (args.details == "combo" and classify(str(entry.get("name", ""))) == "combo")
        )
    ]
    print(f"  {len(targets)} items to fetch")
    failures = 0
    for index, (code, entry) in enumerate(targets, 1):
        name = entry.get("name", code)
        try:
            detail = client.query_meal_detail(store_code, str(code))
            if detail:
                details[str(code)] = detail
                rounds = len(detail.get("rounds") or [])
                print(f"    [{index:>3}/{len(targets)}] {name}  rounds={rounds}")
            else:
                print(f"    [{index:>3}/{len(targets)}] {name}  (empty)")
        except (MCPError, MCPTransportError) as exc:
            failures += 1
            print(f"    [{index:>3}/{len(targets)}] {name}  FAILED: {exc}")
    if details:
        write_json(os.path.join(fixture_dir, "meal_details.json"), details)

    print("\n[4/4] metadata")
    now = datetime.now(CST)
    if store:
        write_json(os.path.join(fixture_dir, "store.json"), store)
    write_json(
        os.path.join(fixture_dir, "categories.json"),
        categories,
    )
    write_json(
        os.path.join(fixture_dir, "menu_snapshot.json"),
        {
            "snapshot_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "storeCode": store_code,
            "categories": categories,
            "meals": meals,
        },
    )
    write_json(
        os.path.join(fixture_dir, "_meta.json"),
        {
            "source": f"{client.endpoint} ({info.get('name', 'mcd-mcp')} v{info.get('version', '?')})",
            "captured_at": now.isoformat(timespec="seconds"),
            "captured_by": "scripts/sync_fixtures.py",
            "store": {"storeCode": store_code, "storeName": store.get("storeName", "")},
            "counts": {
                "tools": len(tools),
                "nutrition": len(nutrition),
                "menu": len(meals),
                "categories": len(categories),
                "meal_details": len(details),
            },
            "read_only": True,
            "tools_called": sorted({c["tool"] for c in client.calls}),
            "note": (
                "Real responses captured from the official MCP endpoint so the demo runs "
                "without a token. No nutrition value is hard-coded or estimated."
            ),
        },
    )

    print("\n" + "=" * 56)
    print(f"done. store={store_code} menu={len(meals)} details={len(details)} failures={failures}")
    print(f"tools called: {sorted({c['tool'] for c in client.calls})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
