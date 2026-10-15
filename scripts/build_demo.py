#!/usr/bin/env python3
"""Build ``index.html`` -- the zero-setup demo page.

Takes the real captured fixtures, runs them through the same matching and
combo-summing code the CLI uses, and bakes the result into a single
self-contained HTML file.

Two reasons the data is inlined rather than fetched:

1. Opening the file straight from disk (``file://``) must work, and ``fetch``
   is blocked there by CORS. A reviewer should not need a web server.
2. The page has to survive being saved and forwarded -- one file, no siblings.

Live store mode (``server.py``) reuses ``payload.build_payload``, so the
snapshot and the live view are rendered by exactly the same code.

Usage:
    python scripts/build_demo.py
    python scripts/build_demo.py --out index.html
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcd_order_copilot.demo_data import DemoData  # noqa: E402
from mcd_order_copilot.payload import build_payload, image_map  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default=os.path.join(ROOT, "demo", "template.html"))
    parser.add_argument("--out", default=os.path.join(ROOT, "index.html"))
    parser.add_argument("--fixtures", default="")
    args = parser.parse_args()

    data = DemoData.load(args.fixtures or None)
    payload = build_payload(
        menu=data.menu,
        details=data.details,
        nutrition=data.nutrition,
        store=data.store,
        meta=data.meta,
        images=image_map(os.path.join(ROOT, "docs", "images")),
    )

    with open(args.template, encoding="utf-8") as fh:
        template = fh.read()

    # Inline the JSON. ``</`` is escaped so the payload can never close the
    # surrounding <script> tag early.
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    if "__DEMO_DATA__" not in template:
        print("template is missing the __DEMO_DATA__ placeholder", file=sys.stderr)
        return 1
    html = template.replace("__DEMO_DATA__", blob)

    # The home-screen icon ships as base64 so the page stays a single file that
    # can be saved and forwarded. Missing icon is cosmetic, never fatal.
    if "__ICON_B64__" in html:
        icon_path = os.path.join(ROOT, "demo", "icon180.b64")
        try:
            with open(icon_path, encoding="ascii") as fh:
                icon_b64 = fh.read().strip()
        except OSError:
            icon_b64 = ""
            print(f"warning: {icon_path} missing -- home-screen icon left blank", file=sys.stderr)
        html = html.replace("__ICON_B64__", icon_b64)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    size = os.path.getsize(args.out)
    print(f"wrote {os.path.relpath(args.out, ROOT)}  {size:,} B")
    print(f"  items        : {payload['meta']['item_count']}")
    print(f"  with data    : {payload['meta']['with_nutrition']}")
    print(f"  combos       : {payload['meta']['combos']}")
    print(f"  presets      : {len(payload['presets'])}")
    print(f"  store        : {payload['store'].get('storeName')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
