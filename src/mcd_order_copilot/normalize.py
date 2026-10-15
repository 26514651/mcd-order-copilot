"""Menu <-> nutrition table name matching.

The two data sources name the same product differently:

    menu table        ``可乐``          ``薯条``       ``纯牛奶(盒装)``    ``那么大鸡排（椒盐风味）``
    nutrition table   ``可乐中杯``      ``中薯条``     ``纯牛奶（盒装）``   ``那么大鸡排``

Calling this a "fuzzy match" understates it. A raw exact-name join covers only
~18% of a real store menu, so this module is what makes the product usable.

Five cascade levels, tried in order:

    L1  exact name
    L2  normalised: NFKC width, punctuation, whitespace, case, brackets
    L3  size-normalised: the menu says ``可乐``, the table says ``可乐中杯``
    L4  family: bracketed qualifiers and flavour/packaging suffixes removed
    L5  unique containment

L3 is an **inference** and is reported as such -- the UI marks those numbers so
a reader can tell a certain match from a reasonable one. This module never
guesses silently: when several candidates fit equally well it returns ``None``,
because a wrong number is worse than a missing one.

Two categories are deliberately *not* treated as failures, and are reported
separately by :meth:`NutritionIndex.coverage`:

* **Combos** (套餐 / 三件套 / 小食盘 / 乐园餐) -- their totals come from summing
  components via ``query-meal-detail``, not from a table lookup.
* **Merchandise and dips** (毛绒周边 / 棒球帽 / 蘸酱) -- these legitimately have
  no nutrition row, and inventing one would be worse than showing nothing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Iterator

# ---------------------------------------------------------------- normalisation

#: Packaging / serving qualifiers stripped before the family comparison.
_BRACKET_RE = re.compile(r"[\(（\[【][^)）\]】]*[\)）\]】]")

_SUFFIXES = (
    "风味", "口味", "款", "装",
    "盒装", "瓶装", "罐装", "杯装", "桶装", "分享装", "单份装",
)

_PUNCT = "·•　 \t\r\n-_/\\|,，.。、:：;；!！?？'\"“”‘’~～+*#&"

#: Size words, ordered by how likely they are to be the intended default.
#: "medium" first because that is what a combo ships with.
_SIZE_WORDS = ("中杯", "中号", "中份", "中", "小杯", "小号", "小份", "小", "大杯", "大号", "大份", "大")

#: Substrings that mark a menu entry as a combo rather than a single product.
COMBO_MARKERS = (
    "套餐", "件套", "两件套", "四件套", "小食盘", "乐园餐", "全家桶", "拼盘",
    "分享餐", "随心选", "双人餐", "多人餐", "亲子", "精选", "双拼",
)

#: Substrings that mark an entry as merchandise rather than food.
NON_FOOD_MARKERS = ("周边", "棒球帽", "水手帽", "百宝袋", "毛绒", "玩具", "礼品卡")

_CONTAINMENT_MIN_LEN = 4  # never containment-match on very short strings

#: Distinguishes "this entry has no ``nutrition`` attribute" (a bare name was
#: passed to :meth:`NutritionIndex.coverage`) from "it has one, and it is empty".
_ABSENT = object()


def normalize(name: str) -> str:
    """Aggressive normalisation: width, case, punctuation, whitespace, brackets."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    text = text.lower()
    for ch in _PUNCT:
        text = text.replace(ch, "")
    text = _BRACKET_RE.sub("", text)
    return text.strip()


def _strip_suffixes(text: str) -> str:
    """Remove trailing flavour / packaging words, repeatedly."""
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            norm_suffix = normalize(suffix)
            if norm_suffix and text.endswith(norm_suffix) and len(text) > len(norm_suffix) + 1:
                text = text[: -len(norm_suffix)]
                changed = True
    return text


def variant_levels(name: str) -> Iterator[tuple[int, str]]:
    """Yield ``(level, normalised_form)`` pairs, most-certain first."""
    raw = unicodedata.normalize("NFKC", str(name or "")).strip()
    base = normalize(raw)
    if not base:
        return

    seen: set[str] = set()

    def emit(level: int, form: str) -> Iterator[tuple[int, str]]:
        if form and form not in seen:
            seen.add(form)
            yield level, form

    yield from emit(2, base)

    # L3: attach a size word on either side.
    for size in _SIZE_WORDS:
        norm_size = normalize(size)
        yield from emit(3, base + norm_size)
        yield from emit(3, norm_size + base)

    # L4: same forms, but on the family name.
    family = _strip_suffixes(base)
    if family != base:
        yield from emit(4, family)
        for size in _SIZE_WORDS:
            norm_size = normalize(size)
            yield from emit(4, family + norm_size)
            yield from emit(4, norm_size + family)


def variants(name: str) -> list[str]:
    """Flat list of every normalised form tried, in order."""
    return [form for _level, form in variant_levels(name)]


def classify(name: str) -> str:
    """Return ``'combo'`` / ``'non-food'`` / ``'product'`` for a menu entry.

    Name-based, and therefore a heuristic: prefer the built item's ``is_combo``
    and ``source`` when they are available, since those come from the actual
    ``rounds`` structure rather than from the wording of a title.
    """
    text = str(name or "")
    if any(marker in text for marker in COMBO_MARKERS):
        return "combo"
    if any(marker in text for marker in NON_FOOD_MARKERS):
        return "non-food"
    # "蘸酱" is ambiguous: ``蘸酱炸鸡`` is fried chicken *served with* a dip and
    # is real food, while ``酸甜蘸酱`` is the dip itself. Only a name that ends
    # on the condiment counts as non-food.
    if normalize(text).endswith(normalize("蘸酱")):
        return "non-food"
    return "product"


# ---------------------------------------------------------------------- lookup


@dataclass
class Match:
    """Result of a lookup. ``level`` is 1-5, or ``None`` when nothing matched."""

    row: dict | None
    level: int | None = None
    query: str = ""
    matched_on: str = ""

    @property
    def ok(self) -> bool:
        return self.row is not None

    @property
    def confidence(self) -> str:
        return {
            1: "exact",
            2: "normalised",
            3: "size-inferred",
            4: "family",
            5: "containment",
        }.get(self.level or 0, "none")

    @property
    def inferred(self) -> bool:
        """True when the number came from a reasonable guess rather than a namesake."""
        return self.level in (3, 5)


class NutritionIndex:
    """Index of ``list-nutrition-foods`` rows, searchable by product name."""

    def __init__(self, rows: Iterable[dict]) -> None:
        self.rows: list[dict] = [r for r in rows if isinstance(r, dict)]
        self._exact: dict[str, dict] = {}
        self._normalised: dict[str, dict] = {}
        for row in self.rows:
            name = str(row.get("name") or row.get("productName") or "")
            if not name:
                continue
            self._exact.setdefault(name.strip(), row)
            norm = normalize(name)
            if norm:
                self._normalised.setdefault(norm, row)

    # ------------------------------------------------------------------ lookup

    def lookup(self, menu_name: str) -> Match:
        if not menu_name:
            return Match(None, None, menu_name, "")

        key = unicodedata.normalize("NFKC", str(menu_name)).strip()
        row = self._exact.get(key)
        if row is not None:
            return Match(row, 1, menu_name, key)

        for level, form in variant_levels(menu_name):
            row = self._normalised.get(form)
            if row is not None:
                return Match(row, level, menu_name, form)

        # L5: containment, accepted only when exactly one row fits.
        hits: list[tuple[str, dict]] = []
        for form in variants(menu_name):
            if len(form) < _CONTAINMENT_MIN_LEN:
                continue
            for norm_key, candidate in self._normalised.items():
                if form in norm_key or norm_key in form:
                    hits.append((norm_key, candidate))
        unique = {id(r): r for _k, r in hits}
        if len(unique) == 1:
            only = next(iter(unique.values()))
            matched_on = next(k for k, v in hits if v is only)
            return Match(only, 5, menu_name, matched_on)

        return Match(None, None, menu_name, "")

    # ------------------------------------------------------------------- stats

    def coverage(self, entries: Iterable) -> dict:
        """Match statistics for a whole menu, split by what kind of failure it is.

        Accepts either plain menu names (``str``) or already-built items carrying
        ``name`` / ``source`` / ``is_combo`` / ``match`` attributes. Passing the
        built items is strongly preferred: a bare name loses the fact that a set
        meal was resolved by summing its components, which would make the report
        claim "no official data" for an item the table is already showing a
        number for. Re-classifying a name in isolation cannot recover that.

        Combos and merchandise are counted separately: including them in the
        headline rate would understate the matcher, and hiding them would
        overstate the product.
        """
        total = 0
        by_level: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        by_kind: dict[str, int] = {"product": 0, "combo": 0, "non-food": 0}
        via: dict[str, int] = {"table": 0, "combo-sum": 0}
        product_via: dict[str, int] = {"table": 0, "combo-sum": 0}
        misses: list[str] = []
        combo_unresolved: list[str] = []
        non_food: list[str] = []
        inferred: list[str] = []
        product_total = 0
        product_matched = 0

        for entry in entries:
            total += 1
            name = getattr(entry, "name", None) or str(entry)
            is_combo = getattr(entry, "is_combo", None)

            # ``is_combo`` is authoritative when present: a set meal whose name
            # carries no marker (``堡堡随心拼``) is still a set meal.
            kind = "combo" if is_combo else classify(name)
            by_kind[kind] += 1

            nutrition = getattr(entry, "nutrition", _ABSENT)
            if nutrition is _ABSENT:
                # A bare name: a table lookup is the only route available.
                route = getattr(entry, "source", None) or "table"
                found = self.lookup(name)
                level = found.level
                resolved = level is not None
            else:
                # What matters is whether a number actually came out, not which
                # route was attempted: a set meal can reach ``rounds`` and still
                # resolve nothing, leaving it with no total at all.
                route = getattr(entry, "source", None) or "table"
                level = getattr(getattr(entry, "match", None), "level", None)
                resolved = nutrition is not None

            if resolved:
                bucket = "combo-sum" if route == "combo-sum" else "table"
                via[bucket] += 1
                if level:
                    by_level[level] += 1
                if kind == "product":
                    product_total += 1
                    product_matched += 1
                    product_via[bucket] += 1
                    if level in (3, 5):
                        inferred.append(name)
                continue

            if kind == "product":
                product_total += 1
                misses.append(name)
            elif kind == "combo":
                combo_unresolved.append(name)
            else:
                non_food.append(name)

        return {
            "total": total,
            "matched": total - len(misses) - len(combo_unresolved) - len(non_food),
            "rate": round(product_matched / product_total, 4) if product_total else 0.0,
            "product_total": product_total,
            "product_matched": product_matched,
            "by_level": by_level,
            "by_kind": by_kind,
            "via": via,
            "product_via": product_via,
            "combo_resolved": by_kind["combo"] - len(combo_unresolved),
            "inferred": inferred,
            "misses": misses,
            "combo_unresolved": combo_unresolved,
            "non_food": non_food,
        }


def nutrition_of(row: dict | None) -> dict[str, float] | None:
    """Convert a raw nutrition row into typed numbers.

    Returns ``None`` when the row is missing, so callers must handle the gap
    explicitly instead of rendering zeros.
    """
    if not row:
        return None

    def num(*keys: str) -> float | None:
        for key in keys:
            value = row.get(key)
            if value in (None, "", "null", "None"):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    kcal = num("kcal", "energyKcal")
    if kcal is None:
        return None
    return {
        "kcal": kcal,
        "protein": num("protein"),
        "fat": num("fat"),
        "carb": num("carb", "carbohydrate"),
        "sodium": num("sodium"),
        "calcium": num("calcium"),
        "kj": num("kj", "energyKj"),
    }
