"""Menu assembly, combo arithmetic and filtering.

Two things this module does that a plain nutrition lookup does not:

* **Combo arithmetic.** ``query-meal-detail`` returns ``rounds`` -> ``choices``,
  so a set meal is an equation, not a number. Swapping the medium fries for a
  large one changes both the price (+2) and the calories (+90). Almost every
  comparable tool stops at single items; this is the part that makes the
  product worth using.
* **Honest gaps.** When a component cannot be matched, or an official nutrition
  row simply does not exist yet, the result carries ``unknown_components`` and
  the UI prints "partial" rather than passing off a lower bound as a total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .normalize import Match, NutritionIndex, classify, nutrition_of

#: Nutrition fields that add up linearly.
SUM_FIELDS = ("kcal", "protein", "fat", "carb", "sodium", "calcium", "kj")


@dataclass
class Item:
    """A single sellable thing: either a standalone product or a set meal."""

    code: str
    name: str
    price: float | None = None
    image: str = ""
    nutrition: dict[str, float] | None = None
    match: Match | None = None
    is_combo: bool = False
    components: list[dict] = field(default_factory=list)
    #: component names that could not be resolved to an official nutrition row
    unknown_components: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    #: where the numbers came from: ``table`` / ``combo-sum`` / ``none``
    source: str = ""

    @property
    def complete(self) -> bool:
        """True when every component of this item is backed by official data."""
        return self.nutrition is not None and not self.unknown_components

    def value_of(self, key: str) -> float | None:
        if not self.nutrition:
            return None
        return self.nutrition.get(key)


def sum_nutrition(parts: Iterable[dict[str, float] | None]) -> tuple[dict[str, float] | None, int]:
    """Add up component nutrition. Returns (totals, number_of_known_parts)."""
    totals = {k: 0.0 for k in SUM_FIELDS}
    known = 0
    for part in parts:
        if not part:
            continue
        known += 1
        for key in SUM_FIELDS:
            value = part.get(key)
            if value is not None:
                totals[key] += float(value)
    if known == 0:
        return None, 0
    return totals, known


def flatten_combo_components(rounds: list[dict] | None) -> list[str]:
    """Names of the default selection in each round of a set meal."""
    names: list[str] = []
    for rnd in rounds or []:
        for choice in rnd.get("choices") or rnd.get("items") or []:
            if choice.get("isDefault"):
                names.append(str(choice.get("name", "")))
                break
        else:
            # No default flagged -- take the first option so the combo has a shape.
            choices = rnd.get("choices") or rnd.get("items") or []
            if choices:
                names.append(str(choices[0].get("name", "")))
    return [n for n in names if n]


def build_item(
    code: str,
    name: str,
    price: float | None,
    image: str,
    index: NutritionIndex,
    detail: dict | None = None,
) -> Item:
    """Build an ``Item``, resolving direct nutrition or summing combo components.

    Combos are **never** name-matched against the nutrition table. ``巨无霸三件套``
    contains a 巨无霸, but its calories are not 巨无霸's calories -- matching the
    two would be silently wrong, and a wrong number is the one thing this project
    refuses to ship. A combo either resolves through its ``rounds`` or reports
    itself as unavailable.
    """
    detail = detail or {}
    rounds = detail.get("rounds") or []
    kind = classify(name)
    item = Item(code=code, name=name, price=price, image=image, raw=detail or {})

    if rounds:
        # A single round is just "this product, with its option group" -- it is
        # not a combo, and labelling it as one would mislead the reader.
        item.is_combo = len(rounds) >= 2 or kind == "combo"
        component_names = flatten_combo_components(rounds)
        item.components = [{"name": n} for n in component_names]
        parts: list[dict[str, float] | None] = []
        for component in component_names:
            match = index.lookup(component)
            rows = nutrition_of(match.row)
            if rows is None:
                item.unknown_components.append(component)
            parts.append(rows)
        totals, known = sum_nutrition(parts)
        item.nutrition = totals
        item.source = "combo-sum"
        item.match = Match(None, None, name, "combo-sum")
        return item

    if kind == "combo":
        # A set meal with no resolvable structure: report the gap, do not guess.
        item.is_combo = True
        item.unknown_components.append(name)
        item.source = "none"
        item.match = Match(None, None, name, "")
        return item

    if kind == "non-food":
        # Merchandise and condiment packets have no nutrition row by design.
        item.unknown_components.append(name)
        item.source = "none"
        item.match = Match(None, None, name, "")
        return item

    match = index.lookup(name)
    item.match = match
    item.nutrition = nutrition_of(match.row)
    item.source = "table" if item.nutrition else "none"
    if item.nutrition is None:
        item.unknown_components.append(name)
    return item


def build_menu(raw_menu: dict, index: NutritionIndex, details: dict[str, dict] | None = None) -> list[Item]:
    """Turn a ``query-meals`` payload into a list of items."""
    details = details or {}
    items: list[Item] = []
    for code, entry in (raw_menu or {}).items():
        if not isinstance(entry, dict):
            continue
        price_raw = entry.get("currentPrice") or entry.get("price")
        try:
            price = float(price_raw) if price_raw not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        items.append(
            build_item(
                code=str(code),
                name=str(entry.get("name", "")),
                price=price,
                image=str(entry.get("image", "")),
                index=index,
                detail=details.get(str(code)),
            )
        )
    return items


# --------------------------------------------------------------------- filtering


def meets(item: Item, target) -> bool:
    """Soft filter. Items missing the relevant number are excluded, not assumed."""
    checks = (
        ("kcal_max", "kcal", lambda v, t: v <= t),
        ("protein_min", "protein", lambda v, t: v >= t),
        ("carb_max", "carb", lambda v, t: v <= t),
        ("fat_max", "fat", lambda v, t: v <= t),
        ("sodium_max", "sodium", lambda v, t: v <= t),
    )
    for attr, field_name, compare in checks:
        threshold = getattr(target, attr, None)
        if threshold is None:
            continue
        value = item.value_of(field_name)
        if value is None:
            return False
        if not compare(value, threshold):
            return False
    return True


def rank(items: list[Item], target, sort_by: str = "protein") -> list[Item]:
    """Sort items so the most relevant ones surface first."""
    key_map = {
        "protein": lambda i: -(i.value_of("protein") or -1),
        "kcal": lambda i: (i.value_of("kcal") if i.value_of("kcal") is not None else 9e9),
        "carb": lambda i: (i.value_of("carb") if i.value_of("carb") is not None else 9e9),
        "fat": lambda i: (i.value_of("fat") if i.value_of("fat") is not None else 9e9),
        "sodium": lambda i: (i.value_of("sodium") if i.value_of("sodium") is not None else 9e9),
        "price": lambda i: (i.price if i.price is not None else 9e9),
    }
    key = key_map.get(sort_by, key_map["protein"])
    return sorted(items, key=lambda i: (not i.complete, key(i)))


def annotate_tags(item: Item) -> list[str]:
    """Neutral, data-derived labels. Never phrased as advice."""
    tags: list[str] = []
    protein = item.value_of("protein")
    kcal = item.value_of("kcal")
    fat = item.value_of("fat")
    sodium = item.value_of("sodium")
    carb = item.value_of("carb")

    if protein is not None and protein >= 25:
        tags.append("高蛋白")
    if kcal is not None and kcal <= 500:
        tags.append("低热量")
    if fat is not None and fat <= 15:
        tags.append("低脂")
    if sodium is not None and sodium <= 700:
        tags.append("低钠")
    if carb is not None and carb <= 40:
        tags.append("低碳水")
    if item.is_combo:
        tags.append("套餐")
    return tags


def summarise(items: list[Item], target) -> dict:
    """Small stats block for the UI header and for the CLI.

    ``missing_data`` lumps two very different gaps together, so it is split out:
    an item with *no* official row at all, versus a set meal where one component
    could not be resolved and the total is therefore a known-parts sum. Reporting
    the second as "no official data" would understate what the user is seeing.
    """
    matched = [i for i in items if meets(item=i, target=target)] if target.active_keys() else list(items)
    complete = [i for i in items if i.complete]
    no_data = [i for i in items if i.nutrition is None]
    partial = [i for i in items if i.nutrition is not None and i.unknown_components]
    return {
        "total": len(items),
        "matched": len(matched),
        "complete_data": len(complete),
        "partial_data": len(partial),
        "no_data": len(no_data),
        "missing_data": len(items) - len(complete),
        "target": target.as_dict(),
    }


def dump(item: Item) -> dict[str, Any]:
    """Serialise an item for JSON output."""
    return {
        "code": item.code,
        "name": item.name,
        "price": item.price,
        "image": item.image,
        "is_combo": item.is_combo,
        "nutrition": item.nutrition,
        "match_level": item.match.level if item.match else None,
        "match_confidence": item.match.confidence if item.match else "none",
        "components": item.components,
        "unknown_components": item.unknown_components,
        "tags": item.tags,
    }
