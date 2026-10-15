"""End-to-end checks on the offline CLI.

The point is that ``python -m mcd_order_copilot`` works with no token, no
network and no arguments -- that is the whole reason the fixtures exist.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcd_order_copilot.cli import main  # noqa: E402


def run(argv):
    """Run the CLI and return (exit_code, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


class TestOfflineCli(unittest.TestCase):
    def test_default_mode_needs_no_token_and_no_flags(self):
        code, out = run([])
        self.assertEqual(code, 0)
        self.assertIn("离线演示模式", out)

    def test_demo_table_lists_items(self):
        code, out = run(["--demo", "--limit", "3"])
        self.assertEqual(code, 0)
        for column in ("餐品", "价格", "热量", "蛋白", "碳水", "脂肪", "钠"):
            self.assertIn(column, out)

    def test_demo_honours_a_goal_preset(self):
        code, out = run(["--demo", "--goal", "cut", "--weight", "70"])
        self.assertEqual(code, 0)
        self.assertIn("减脂期", out)
        self.assertIn("按体重", out)

    def test_impossible_filter_explains_itself(self):
        code, out = run(["--demo", "--protein", "999"])
        self.assertEqual(code, 0)
        self.assertIn("没有餐品同时满足", out)

    def test_json_output_is_parseable(self):
        code, out = run(["--demo", "--json", "--goal", "bulk"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "demo")
        self.assertIn("items", payload)
        self.assertIn("notes", payload)
        self.assertTrue(payload["stats"]["total"] > 0)

    def test_coverage_report_is_internally_consistent(self):
        code, out = run(["--demo", "--coverage"])
        self.assertEqual(code, 0)
        self.assertIn("覆盖报告", out)
        self.assertIn("单品", out)
        self.assertIn("真无官方营养数据", out)
        # The old bug: a set meal resolved by summing components was listed as
        # having no official data. It must not appear in the genuine-miss list.
        miss_section = out.split("真无官方营养数据")[-1]
        self.assertNotIn("堡堡随心拼（7件任选）", miss_section)
        self.assertNotIn("经典麦旋风", miss_section)

    def test_list_presets(self):
        code, out = run(["--list-presets"])
        self.assertEqual(code, 0)
        for key in ("bulk", "cut", "recomp", "high_protein"):
            self.assertIn(key, out)
        self.assertIn("非医疗", out)

    def test_every_output_carries_the_disclaimer(self):
        for argv in (["--demo"], ["--demo", "--goal", "bulk"], ["--demo", "--limit", "1"]):
            _code, out = run(argv)
            self.assertIn("非麦当劳官方产品", out, f"{argv} 缺少免责声明")

    def test_unknown_preset_is_rejected(self):
        with self.assertRaises(SystemExit):
            main(["--demo", "--goal", "not-a-preset"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
