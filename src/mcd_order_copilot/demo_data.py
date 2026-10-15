"""Offline fixtures loader -- this is what makes ``--demo`` possible.

The demo exists for one reason: a visitor must be able to see the product
**without a token, without an account and without installing anything**.
Whatever is shown is therefore real captured data (see ``fixtures/_meta.json``
for the capture timestamp and store), never invented values.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

FIXTURE_DIRNAME = "fixtures"


def _repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    # src/mcd_order_copilot/demo_data.py -> repo root is two levels up
    return os.path.dirname(os.path.dirname(here))


def _find_fixtures(explicit: str | None = None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(_repo_root(), FIXTURE_DIRNAME))
    candidates.append(os.path.join(os.getcwd(), FIXTURE_DIRNAME))
    for path in candidates:
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "nutrition.json")):
            return path
    raise FileNotFoundError(
        "fixtures/ not found. Run from the repository root, or pass --fixtures <dir>."
    )


@dataclass
class DemoData:
    nutrition: list[dict] = field(default_factory=list)
    menu: dict = field(default_factory=dict)
    store: dict = field(default_factory=dict)
    categories: list = field(default_factory=list)
    menu_snapshot: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    dir: str = ""

    @property
    def source_label(self) -> str:
        store_name = (self.store or {}).get("storeName", "示例门店")
        captured = (self.meta or {}).get("captured_at", "")
        return f"{store_name} · {captured} 快照"

    @classmethod
    def load(cls, fixtures_dir: str | None = None) -> "DemoData":
        base = _find_fixtures(fixtures_dir)

        def read(name: str, default: Any) -> Any:
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                return default
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)

        snapshot = read("menu_snapshot.json", {}) or {}
        menu = snapshot.get("meals") or read("menu.json", {})

        # Optional per-item detail fixtures, keyed by product code.
        details: dict[str, dict] = {}
        sample = read("meal_detail_sample.json", None)
        if isinstance(sample, dict) and sample.get("code"):
            details[str(sample["code"])] = sample
        extra = read("meal_details.json", None)
        if isinstance(extra, dict):
            for code, payload in extra.items():
                details[str(code)] = payload

        return cls(
            nutrition=read("nutrition.json", []) or [],
            menu=menu or {},
            store=read("store.json", {}) or {},
            categories=snapshot.get("categories") or read("categories.json", []) or [],
            menu_snapshot=snapshot,
            details=details,
            meta=read("_meta.json", {}) or {},
            dir=base,
        )
