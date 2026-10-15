"""Combo arithmetic, filtering and the honesty rules.

The three invariants worth protecting, in order:

1. A combo is never name-matched against the nutrition table.
2. A missing number is never treated as zero.
3. A missing number is never estimated.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcd_order_copilot.normalize import NutritionIndex  # noqa: E402
from mcd_order_copilot.nutrition import (  # noqa: E402
    build_item,
    build_menu,
    dump,
    flatten_combo_components,
    meets,
    rank,
    sum_nutrition,
)
from mcd_order_copilot.profiles import Target  # noqa: E402


def row(name: str, kcal: float, protein: float = 0.0):
    return {
        "name": name,
        "kcal": str(kcal),
        "protein": str(protein),
        "fat": "1",
        "carb": "1",
        "sodium": "1",
        "calcium": "1",
    }


def rounds(*groups):
    """Build a ``rounds`` payload: each group is the list of choice names."""
    payload = []
    for group in groups:
        choices = []
        for i, name in enumerate(group):
            choices.append({"name": name, "isDefault": i == 0, "diffPrice": "+ ¥0"})
        payload.append({"choices": choices})
    return payload


class TestSumNutrition(unittest.TestCase):
    def test_missing_parts_are_skipped_not_zeroed(self):
        totals, known = sum_nutrition([{"kcal": 100}, None, {"kcal": 50}])
        self.assertEqual(totals["kcal"], 150)
        self.assertEqual(known, 2)

    def test_all_missing_returns_none(self):
        totals, known = sum_nutrition([None, None])
        self.assertIsNone(totals)
        self.assertEqual(known, 0)


class TestFlattenComboComponents(unittest.TestCase):
    def test_takes_the_default_of_each_round(self):
        names = flatten_combo_components(rounds(["巨无霸"], ["中薯条", "大薯条"]))
        self.assertEqual(names, ["巨无霸", "中薯条"])

    def test_falls_back_to_first_choice_without_a_default(self):
        detail = [{"choices": [{"name": "可乐中杯"}, {"name": "雪碧"}]}]
        self.assertEqual(flatten_combo_components(detail), ["可乐中杯"])


class TestComboNeverNameMatched(unittest.TestCase):
    def test_combo_without_structure_reports_a_gap_rather_than_the_component_number(self):
        index = NutritionIndex([row("巨无霸", 513, 27)])
        item = build_item("1", "巨无霸三件套", 34.0, "", index, detail=None)
        self.assertTrue(item.is_combo)
        self.assertIsNone(item.nutrition, "套餐不得套用同名单品的热量")
        self.assertEqual(item.source, "none")
        self.assertIn("巨无霸三件套", item.unknown_components)

    def test_combo_totals_come_from_its_components(self):
        index = NutritionIndex([row("巨无霸", 513, 27), row("中薯条", 289, 4)])
        detail = {"rounds": rounds(["巨无霸"], ["中薯条", "大薯条"])}
        item = build_item("1", "巨无霸三件套", 34.0, "", index, detail=detail)
        self.assertTrue(item.is_combo)
        self.assertEqual(item.value_of("kcal"), 802)
        self.assertEqual(item.value_of("protein"), 31)
        self.assertEqual(item.source, "combo-sum")
        self.assertTrue(item.complete)

    def test_partial_combo_keeps_the_gap_visible(self):
        index = NutritionIndex([row("巨无霸", 513, 27)])
        detail = {"rounds": rounds(["巨无霸"], ["中薯条"])}
        item = build_item("1", "巨无霸三件套", 34.0, "", index, detail=detail)
        self.assertFalse(item.complete)
        self.assertIn("中薯条", item.unknown_components)
        self.assertEqual(item.value_of("kcal"), 513, "合计值只能是已知部分之和")

    def test_single_round_is_not_labelled_a_combo(self):
        # "this product, with its option group" is not a set meal.
        index = NutritionIndex([row("巨无霸", 513, 27)])
        detail = {"rounds": rounds(["巨无霸"])}
        item = build_item("1", "巨无霸", 25.0, "", index, detail=detail)
        self.assertFalse(item.is_combo)

    def test_named_combo_with_a_single_round_is_still_a_combo(self):
        index = NutritionIndex([row("巨无霸", 513, 27)])
        detail = {"rounds": rounds(["巨无霸"])}
        item = build_item("1", "巨无霸套餐", 30.0, "", index, detail=detail)
        self.assertTrue(item.is_combo)


class TestHonestGaps(unittest.TestCase):
    def test_product_without_a_row_gets_no_number(self):
        index = NutritionIndex([])
        item = build_item("1", "某个新品", 20.0, "", index)
        self.assertIsNone(item.nutrition)
        self.assertEqual(item.source, "none")
        self.assertFalse(item.complete)

    def test_merchandise_gets_no_number(self):
        index = NutritionIndex([row("毛绒周边", 999)])
        item = build_item("1", "薯条脆卜卜毛绒周边", 49.0, "", index)
        self.assertIsNone(item.nutrition)
        self.assertEqual(item.source, "none")


class TestFiltering(unittest.TestCase):
    def setUp(self):
        self.index = NutritionIndex([row("巨无霸", 513, 27), row("沙拉", 120, 5)])

    def test_missing_field_excludes_instead_of_assuming_zero(self):
        item = build_item("1", "没有数据的餐品", 10.0, "", self.index)
        # protein is None; target wants protein >= 30. Must be False, not True.
        self.assertFalse(meets(item, Target(protein_min=30)))

    def test_calorie_ceiling_is_inclusive(self):
        item = build_item("1", "沙拉", 10.0, "", self.index)
        self.assertTrue(meets(item, Target(kcal_max=120)))
        self.assertFalse(meets(item, Target(kcal_max=119)))

    def test_protein_floor_is_inclusive(self):
        item = build_item("1", "巨无霸", 25.0, "", self.index)
        self.assertTrue(meets(item, Target(protein_min=27)))
        self.assertFalse(meets(item, Target(protein_min=28)))

    def test_rank_puts_incomplete_items_last(self):
        good = build_item("1", "巨无霸", 25.0, "", self.index)
        gap = build_item("2", "没有数据的餐品", 10.0, "", self.index)
        ordered = rank([gap, good], Target(protein_min=1), "protein")
        self.assertEqual([i.name for i in ordered], ["巨无霸", "没有数据的餐品"])


class TestBuildMenuAndDump(unittest.TestCase):
    def test_build_menu_reads_price_and_name(self):
        raw = {"1001": {"name": "巨无霸", "currentPrice": "25.5", "image": "http://x/a.png"}}
        items = build_menu(raw, NutritionIndex([row("巨无霸", 513, 27)]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, 25.5)
        self.assertEqual(items[0].value_of("kcal"), 513)

    def test_dump_is_json_friendly(self):
        raw = {"1001": {"name": "巨无霸", "currentPrice": "25.5"}}
        item = build_menu(raw, NutritionIndex([row("巨无霸", 513, 27)]))[0]
        payload = dump(item)
        self.assertEqual(payload["name"], "巨无霸")
        self.assertIn("match_level", payload)
        self.assertIsInstance(payload["nutrition"], dict)


class TestRealFixtures(unittest.TestCase):
    """Guards against a regression that only shows up on the captured snapshot."""

    @classmethod
    def setUpClass(cls):
        from mcd_order_copilot.demo_data import DemoData

        cls.data = DemoData.load()
        cls.index = NutritionIndex(cls.data.nutrition)
        cls.items = build_menu(cls.data.menu, cls.index, cls.data.details)

    def test_snapshot_is_present_and_non_trivial(self):
        self.assertGreater(len(self.data.nutrition), 100)
        self.assertGreater(len(self.items), 50)

    def test_combos_resolve_through_their_structure(self):
        combos = [i for i in self.items if i.is_combo]
        self.assertTrue(combos)
        unresolved = [i for i in combos if i.source not in ("combo-sum", "none")]
        self.assertEqual(unresolved, [], "套餐只允许两种来源：成分加总，或如实报告不可用")

    def test_combos_are_not_matched_by_name(self):
        for item in self.items:
            if item.is_combo and item.match is not None:
                self.assertNotIn(
                    item.match.level,
                    (1, 2, 3, 4, 5),
                    f"套餐 {item.name} 走了名称匹配，这是不允许的",
                )

    def test_known_no_data_product_is_reported_not_estimated(self):
        by_name = {i.name: i for i in self.items}
        item = by_name.get("麦乐鸡")
        if item is None:
            self.skipTest("本期快照没有这个餐品")
        self.assertIsNone(item.nutrition)
        self.assertEqual(item.source, "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
