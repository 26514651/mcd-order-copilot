"""Name-matching and classification tests.

These cover the parts most likely to break silently: the cascade levels, the
"never guess" rule, and the combo/merchandise split that the coverage report
depends on.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcd_order_copilot.normalize import (  # noqa: E402
    Match,
    NutritionIndex,
    classify,
    normalize,
    variants,
)


def idx(*names: str) -> NutritionIndex:
    """A minimal nutrition index, one row per name."""
    return NutritionIndex([{"name": n, "kcal": "100"} for n in names])


class TestNormalize(unittest.TestCase):
    def test_width_case_and_space_fold_together(self):
        self.assertEqual(normalize("可口可乐 中杯"), normalize("可口可乐中杯"))
        self.assertEqual(normalize("ABC"), normalize("abc"))

    def test_brackets_are_dropped_with_their_contents(self):
        self.assertEqual(normalize("纯牛奶(盒装)"), normalize("纯牛奶（盒装）"))
        self.assertEqual(normalize("纯牛奶(盒装)"), normalize("纯牛奶"))

    def test_empty_and_none_are_safe(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")

    def test_variants_include_a_size_form(self):
        self.assertIn("可乐中杯", variants("可乐"))


class TestClassify(unittest.TestCase):
    def test_combo_markers(self):
        self.assertEqual(classify("巨无霸三件套"), "combo")
        self.assertEqual(classify("全明星双人分享餐八件套"), "combo")

    def test_plain_products_stay_products(self):
        self.assertEqual(classify("巨无霸"), "product")
        self.assertEqual(classify("板烧鸡腿堡"), "product")

    def test_dipped_chicken_is_food_not_merchandise(self):
        # "蘸酱" here means "served with a dip", not "this is a dip".
        self.assertEqual(classify("蘸酱炸鸡"), "product")
        self.assertEqual(classify("蘸酱韩式甜辣酱鸡块"), "product")

    def test_a_dip_itself_is_merchandise(self):
        self.assertEqual(classify("酸甜蘸酱"), "non-food")

    def test_merchandise_markers(self):
        self.assertEqual(classify("薯条脆卜卜毛绒周边"), "non-food")


class TestLookup(unittest.TestCase):
    def test_l1_exact(self):
        match = idx("巨无霸").lookup("巨无霸")
        self.assertEqual(match.level, 1)
        self.assertFalse(match.inferred)

    def test_l2_normalised(self):
        match = idx("纯牛奶（盒装）").lookup("纯牛奶(盒装)")
        self.assertEqual(match.level, 2)
        self.assertFalse(match.inferred)

    def test_l3_size_inference_and_it_is_flagged(self):
        match = idx("可乐中杯").lookup("可乐")
        self.assertEqual(match.level, 3)
        self.assertTrue(match.inferred, "规格推断必须被标记为推断值")

    def test_l5_containment_when_only_one_row_fits(self):
        match = idx("板烧鸡腿堡").lookup("秘制板烧鸡腿堡")
        self.assertEqual(match.level, 5)
        self.assertTrue(match.inferred)

    def test_containment_refuses_to_guess_between_two_rows(self):
        # Two rows fit equally well -> a wrong number is worse than none.
        match = idx("板烧鸡腿堡", "板烧鸡腿堡套餐").lookup("板烧鸡腿")
        self.assertIsNone(match.level)

    def test_unknown_name_matches_nothing(self):
        match = idx("巨无霸").lookup("完全不存在的东西")
        self.assertIsNone(match.level)
        self.assertFalse(match.ok)


class _Stub:
    """Stands in for a built ``Item`` -- only the attributes coverage() reads."""

    def __init__(
        self,
        name: str,
        source: str,
        level: int | None = 1,
        is_combo: bool = False,
        has_number: bool | None = None,
    ):
        self.name = name
        self.source = source
        self.is_combo = is_combo
        self.match = Match(None, level, name, "") if level else None
        if has_number is None:
            has_number = source != "none"
        self.nutrition = {"kcal": 1.0} if has_number else None


class TestCoverage(unittest.TestCase):
    def test_bare_names_still_work(self):
        report = idx("巨无霸").coverage(["巨无霸", "不存在"])
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["product_matched"], 1)
        self.assertEqual(report["misses"], ["不存在"])

    def test_combo_sum_items_are_not_reported_as_misses(self):
        # Regression: passing names lost the fact that a set meal was resolved
        # by summing components, so the report claimed "no data" for a meal the
        # table was already showing a number for.
        report = idx().coverage([_Stub("巨无霸三件套", "combo-sum", level=None, is_combo=True)])
        self.assertEqual(report["misses"], [])
        self.assertEqual(report["combo_resolved"], 1)
        self.assertEqual(report["via"]["combo-sum"], 1)

    def test_unresolved_combo_is_reported_separately(self):
        report = idx().coverage([_Stub("某某套餐", "none", level=None, is_combo=True)])
        self.assertEqual(report["combo_unresolved"], ["某某套餐"])
        self.assertEqual(report["misses"], [])

    def test_combo_that_resolved_nothing_is_not_called_resolved(self):
        # Reaching ``rounds`` is not the same as producing a total: if every
        # component misses, the meal ends up with no number and must not be
        # counted as "summed successfully".
        report = idx().coverage([
            _Stub("心形薯饼5块", "combo-sum", level=None, is_combo=True, has_number=False),
        ])
        self.assertEqual(report["combo_resolved"], 0)
        self.assertEqual(report["combo_unresolved"], ["心形薯饼5块"])
        self.assertEqual(report["via"]["combo-sum"], 0)

    def test_is_combo_beats_the_name_heuristic(self):
        # The name has no combo marker, but the data says it is one.
        report = idx().coverage([_Stub("堡堡随心拼", "combo-sum", level=None, is_combo=True)])
        self.assertEqual(report["by_kind"]["combo"], 1)
        self.assertEqual(report["by_kind"]["product"], 0)

    def test_product_rate_counts_combo_sum_as_available(self):
        report = idx().coverage([_Stub("经典麦旋风", "combo-sum", level=None)])
        self.assertEqual(report["product_total"], 1)
        self.assertEqual(report["product_matched"], 1)
        self.assertEqual(report["rate"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
