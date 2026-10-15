"""Raw MCP responses -> the JSON payload the demo page consumes.

One implementation, two callers:

* ``scripts/build_demo.py`` bakes fixture data into a standalone HTML file that
  works straight off ``file://`` with no token.
* ``server.py`` answers with a *live* store menu for a restaurant the user
  picked on the page.

Sharing the code is the whole point. Whatever the offline snapshot shows for an
item, live mode shows for the same item -- the two can never drift apart,
because there is only one of them.

The output shape is the contract with ``demo/template.html``. Read it together
with the ``cardNode`` / ``openSheet`` functions there before changing a field
name.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from .normalize import NutritionIndex, nutrition_of
from .nutrition import annotate_tags, build_item
from .profiles import PRESET_ORDER, PRESETS

CST = timezone(timedelta(hours=8))
PRICE_RE = re.compile(r"-?\d+(?:\.\d+)?")

#: The five numbers a preset carries. Order matters: it is the display order.
TARGET_FIELDS = ("kcal_max", "protein_min", "carb_max", "fat_max", "sodium_max")


def parse_diff_price(raw) -> float:
    """``"+ ¥2"`` / ``"-¥1.5"`` / ``0`` -> signed float."""
    if raw in (None, "", "-"):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    match = PRICE_RE.search(str(raw))
    if not match:
        return 0.0
    value = float(match.group(0))
    return -value if "-" in str(raw) else value


def image_map(image_dir: str) -> dict[str, str]:
    """``code -> filename`` for locally cached product images.

    Live mode passes no directory: product images then come straight from the
    official CDN URL that arrived with the menu, which is what the page falls
    back to anyway.
    """
    mapping: dict[str, str] = {}
    if not image_dir or not os.path.isdir(image_dir):
        return mapping
    for filename in os.listdir(image_dir):
        stem, ext = os.path.splitext(filename)
        if ext.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            mapping[stem] = filename
    return mapping


def slims_rounds(detail: dict, index: NutritionIndex) -> list[dict]:
    """Turn ``rounds`` into something a browser can render and re-total.

    Every choice carries its own nutrition so the page can recompute a combo
    total locally when a user swaps the fries or the drink -- no server round
    trip, and the arithmetic stays visible.
    """
    rounds_out: list[dict] = []
    for round_index, raw_round in enumerate(detail.get("rounds") or [], start=1):
        choices_out: list[dict] = []
        default_name = ""
        for choice in raw_round.get("choices") or []:
            name = str(choice.get("name", ""))
            match = index.lookup(name)
            nutrition = nutrition_of(match.row)
            removable = [
                str(value.get("name", ""))
                for group in (choice.get("modification") or {}).get("items", []) or []
                for value in group.get("values", []) or []
                if value.get("name")
            ]
            is_default = str(choice.get("isDefault")) in ("1", "true", "True")
            if is_default and not default_name:
                default_name = name
            choices_out.append(
                {
                    "name": name,
                    "kcal": nutrition["kcal"] if nutrition else None,
                    "protein": nutrition["protein"] if nutrition else None,
                    "price": parse_diff_price(choice.get("diffPrice")),
                    "default": is_default,
                    "removable": removable,
                }
            )
        if not choices_out:
            continue
        if not default_name:
            default_name = choices_out[0]["name"]
        rounds_out.append(
            {
                "index": round_index,
                "label": str(raw_round.get("name") or f"第 {round_index} 项"),
                "default": default_name,
                "choices": choices_out,
            }
        )
    return rounds_out


def build_items(
    menu: dict,
    details: dict,
    nutrition: list[dict],
    images: dict[str, str] | None = None,
) -> list[dict]:
    """Match, sum and annotate every item on a menu."""
    index = NutritionIndex(nutrition or [])
    images = images or {}
    items: list[dict] = []

    for code, entry in (menu or {}).items():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        price_raw = entry.get("currentPrice") or entry.get("price")
        try:
            price = float(price_raw) if price_raw not in (None, "") else None
        except (TypeError, ValueError):
            price = None

        detail = (details or {}).get(str(code)) or {}
        item = build_item(
            code=str(code),
            name=name,
            price=price,
            image=str(entry.get("image", "")),
            index=index,
            detail=detail,
        )
        item.tags = annotate_tags(item)

        rounds = slims_rounds(detail, index) if detail.get("rounds") else []
        removable: list[str] = []
        if rounds:
            for rnd in rounds:
                for choice in rnd["choices"]:
                    if choice["name"] == rnd["default"]:
                        removable.extend(choice["removable"])
        else:
            for group in (detail.get("modification") or {}).get("items", []) or []:
                for value in group.get("values", []) or []:
                    if value.get("name"):
                        removable.append(str(value["name"]))

        items.append(
            {
                "code": item.code,
                "name": item.name,
                "price": item.price,
                "image": item.image,
                "image_local": images.get(item.code, ""),
                "nutrition": item.nutrition,
                "tags": item.tags,
                "is_combo": item.is_combo,
                "source": item.source,
                "match_level": item.match.level if item.match else None,
                "match_confidence": item.match.confidence if item.match else "none",
                "matched_on": item.match.matched_on if item.match else "",
                "rounds": rounds,
                "removable": list(dict.fromkeys(removable))[:12],
            }
        )
    return items


def build_presets() -> list[dict]:
    """The eight goal presets, with their five numbers and the basis behind each."""
    return [
        {
            "key": key,
            "label": PRESETS[key].label,
            "emoji": PRESETS[key].emoji,
            "reason": PRESETS[key].reason,
            "target": {k: getattr(PRESETS[key].target, k) for k in TARGET_FIELDS},
            "per_kg": PRESETS[key].per_kg,
            # Which of the five the preset actually filters on by default.
            "filter": PRESETS[key].filtered(),
            "basis": {k: PRESETS[key].basis_for(k) for k in TARGET_FIELDS},
        }
        for key in PRESET_ORDER
    ]


def build_payload(
    menu: dict,
    details: dict,
    nutrition: list[dict],
    store: dict,
    meta: dict | None = None,
    images: dict[str, str] | None = None,
) -> dict:
    """Assemble everything the demo page needs, in one blob."""
    items = build_items(menu, details, nutrition, images)
    return {
        "meta": {
            **(meta or {}),
            "built_at": datetime.now(CST).isoformat(timespec="seconds"),
            "item_count": len(items),
            "with_nutrition": sum(1 for i in items if i["nutrition"]),
            "combos": sum(1 for i in items if i["is_combo"]),
        },
        "store": store or {},
        "presets": build_presets(),
        "items": items,
    }
