"""Preset and manual-override tests.

The regression these guard: CLI flags (``--protein`` / ``--kcal`` …) use short
names while :class:`Target` uses explicit ``*_min`` / ``*_max`` fields, so the
translation has to be explicit. Without it every manual filter silently does
nothing and the user sees "no filter applied" while believing otherwise.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcd_order_copilot.profiles import (  # noqa: E402
    PRESET_ORDER,
    PRESETS,
    Target,
    build_target,
)


class TestPresets(unittest.TestCase):
    def test_every_preset_has_a_plain_language_reason(self):
        for key, preset in PRESETS.items():
            self.assertTrue(preset.reason.strip(), f"{key} 缺少解释")
            self.assertGreater(len(preset.reason), 10, f"{key} 的解释太短")

    def test_every_preset_constrains_something(self):
        for key, preset in PRESETS.items():
            self.assertTrue(preset.target.active_keys(), f"{key} 没有设任何条件")

    def test_thresholds_are_integers_not_fake_precision(self):
        for key, preset in PRESETS.items():
            for field_name in preset.target.active_keys():
                value = getattr(preset.target, field_name)
                self.assertEqual(value, int(value), f"{key}.{field_name} 用了伪精度小数")

    def test_order_only_references_existing_presets(self):
        self.assertEqual(sorted(PRESET_ORDER), sorted(PRESETS))


class TestManualOverrides(unittest.TestCase):
    def test_short_cli_names_map_onto_target_fields(self):
        target, notes = build_target(
            None,
            None,
            {"kcal": 600, "protein": 30, "carb": 45, "fat": 20, "sodium": 800},
        )
        self.assertEqual(target.kcal_max, 600)
        self.assertEqual(target.protein_min, 30)
        self.assertEqual(target.carb_max, 45)
        self.assertEqual(target.fat_max, 20)
        self.assertEqual(target.sodium_max, 800)
        self.assertTrue(any("自定义条件" in n for n in notes))
        self.assertTrue(any("蛋白下限" in n for n in notes), "提示应说人话，不该暴露字段名")

    def test_overrides_win_over_the_preset(self):
        target, _ = build_target("bulk", None, {"protein": 45})
        self.assertEqual(target.protein_min, 45)
        self.assertEqual(target.kcal_max, 900, "未覆盖的项应保留预设值")

    def test_unknown_key_is_reported_not_swallowed(self):
        target, notes = build_target(None, None, {"not_a_field": 1})
        self.assertEqual(target.active_keys(), [])
        self.assertTrue(any("无法识别" in n for n in notes))

    def test_zero_is_a_real_value_not_a_missing_one(self):
        target, _ = build_target(None, None, {"fat": 0})
        self.assertEqual(target.fat_max, 0)


class TestWeightScaling(unittest.TestCase):
    def test_weight_scales_the_preset(self):
        target, _ = build_target("cut", 70)
        self.assertEqual(target.kcal_max, 560)   # 8.0 kcal/kg
        self.assertEqual(target.protein_min, 25)  # 0.35 g/kg -> 24.5 -> 25

    def test_no_weight_uses_the_generic_range(self):
        target, _ = build_target("cut", None)
        self.assertEqual(target.kcal_max, 600)
        self.assertEqual(target.protein_min, 25)


class TestTarget(unittest.TestCase):
    def test_default_target_constrains_nothing(self):
        self.assertEqual(Target().active_keys(), [])
        self.assertEqual(Target().as_dict(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
