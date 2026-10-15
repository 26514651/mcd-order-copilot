"""Command line entry point.

    python -m mcd_order_copilot --demo                     # offline, no token needed
    python -m mcd_order_copilot --demo --goal cut          # apply a goal preset
    python -m mcd_order_copilot --demo --protein 30 --kcal 600
    python -m mcd_order_copilot --live --city 广州          # real MCP calls

``--demo`` is the default when no token is present, so ``python -m
mcd_order_copilot`` always does something useful.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import __version__
from .demo_data import DemoData
from .normalize import NutritionIndex
from .nutrition import annotate_tags, build_menu, dump, meets, rank, summarise
from .profiles import PRESET_ORDER, PRESETS, Target, build_target

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"


def _supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if _supports_colour() else text


def _fmt(value: float | None, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-order-copilot",
        description="麦当劳点单营养速查：先看清这顿的蛋白、碳水、脂肪和热量。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m mcd_order_copilot --demo\n"
            "  python -m mcd_order_copilot --demo --goal cut --weight 70\n"
            "  python -m mcd_order_copilot --demo --protein 30 --kcal 600 --limit 10\n"
            "  python -m mcd_order_copilot --live --city 广州 --goal bulk\n"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="离线模式：用仓库内 fixtures 数据，不需要 Token")
    mode.add_argument("--live", action="store_true", help="在线模式：实时调用麦当劳 MCP 接口")

    parser.add_argument("--goal", choices=PRESET_ORDER, help="目标预设（增肌/减脂/增肌减脂…）")
    parser.add_argument("--weight", type=float, help="可选：体重 kg，用于把参考值折算成个人区间")
    parser.add_argument("--kcal", type=float, help="热量上限 kcal")
    parser.add_argument("--protein", type=float, help="蛋白质下限 g")
    parser.add_argument("--carb", type=float, help="碳水上限 g")
    parser.add_argument("--fat", type=float, help="脂肪上限 g")
    parser.add_argument("--sodium", type=float, help="钠上限 mg")
    parser.add_argument("--sort", default="protein", choices=["protein", "kcal", "carb", "fat", "sodium", "price"])
    parser.add_argument("--limit", type=int, default=12, help="输出条数，默认 12")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于管道处理")
    parser.add_argument("--coverage", action="store_true", help="打印菜单与营养表的匹配率报告")
    parser.add_argument("--list-presets", action="store_true", help="列出身高…列出目标预设")
    parser.add_argument("--city", default="", help="在线模式：城市名")
    parser.add_argument("--store-code", default="", help="在线模式：直接指定门店编码")
    parser.add_argument("--fixtures", default="", help="fixtures 目录（默认自动查找）")
    parser.add_argument("--verbose", action="store_true", help="打印每个请求的状态")
    parser.add_argument("--version", action="version", version=f"mcd-order-copilot {__version__}")
    return parser


def cmd_list_presets() -> int:
    print(_c("目标预设（参考值 · 非医疗建议）", BOLD))
    print()
    for key in PRESET_ORDER:
        preset = PRESETS[key]
        fields = []
        if preset.target.kcal_max is not None:
            fields.append(f"热量 ≤ {preset.target.kcal_max:g} kcal")
        if preset.target.protein_min is not None:
            fields.append(f"蛋白 ≥ {preset.target.protein_min:g} g")
        if preset.target.carb_max is not None:
            fields.append(f"碳水 ≤ {preset.target.carb_max:g} g")
        if preset.target.fat_max is not None:
            fields.append(f"脂肪 ≤ {preset.target.fat_max:g} g")
        if preset.target.sodium_max is not None:
            fields.append(f"钠 ≤ {preset.target.sodium_max:g} mg")
        print(f"  {preset.emoji} {_c(preset.label, BOLD):<18} --goal {key}")
        print(f"     {DIM if _supports_colour() else ''}{' · '.join(fields)}{RESET}")
        print(f"     {preset.reason}")
        print()
    print(_c("以上为公开共识区间下限/上限，不构成营养处方。", DIM))
    return 0


def _render_table(items, target, limit: int) -> None:
    header = f"{'餐品':<24}{'价格':>7}{'热量':>8}{'蛋白':>7}{'碳水':>7}{'脂肪':>7}{'钠':>8}"
    print(_c(header, BOLD))
    print("─" * len(header))
    for item in items[:limit]:
        name = item.name
        if len(name) > 22:
            name = name[:21] + "…"
        kcal = item.value_of("kcal")
        protein = item.value_of("protein")
        carb = item.value_of("carb")
        fat = item.value_of("fat")
        sodium = item.value_of("sodium")
        flag = "" if item.complete else _c(" *", YELLOW)

        kcal_s = _fmt(kcal)
        if target.kcal_max is not None and kcal is not None:
            kcal_s = _c(kcal_s, GREEN if kcal <= target.kcal_max else RED)
        protein_s = _fmt(protein, "", 0)
        if target.protein_min is not None and protein is not None:
            protein_s = _c(protein_s, GREEN if protein >= target.protein_min else RED)

        print(
            f"{name:<24}{_fmt(item.price, '', 1):>7}{kcal_s:>8}"
            f"{protein_s:>7}{_fmt(carb):>7}{_fmt(fat):>7}{_fmt(sodium):>8}{flag}"
        )
    print()
    print(f"{_c('*', YELLOW)} = 部分成分无法对应官方营养数据，合计值仅为已知部分之和")


def run_demo(args) -> int:
    data = DemoData.load(args.fixtures or None)
    index = NutritionIndex(data.nutrition)
    items = build_menu(data.menu, index, data.details)
    for item in items:
        item.tags = annotate_tags(item)

    if args.coverage:
        report = index.coverage(items)
        print(_c("覆盖报告（数据可得性）", BOLD))
        print(f"  门店在售          : {report['total']}")
        print(
            f"  ├ 单品            : {report['product_total']}  "
            f"→ 有官方数据 {report['product_matched']}（{report['rate'] * 100:.1f}%）"
        )
        print(
            f"  │                       名称表匹配 {report['product_via']['table']}"
            f" · 套餐明细解析 {report['product_via']['combo-sum']}"
        )
        combo_total = report["by_kind"]["combo"]
        print(
            f"  ├ 套餐 / 组合      : {combo_total}  "
            f"→ 成分加总成功 {report['combo_resolved']}"
            f" · 结构不可解 {len(report['combo_unresolved'])}"
        )
        print(f"  └ 周边 / 蘸酱      : {report['by_kind']['non-food']}  → 确无营养数据（合理）")
        print()
        print(_c("  名称匹配层级分布", BOLD))
        level_labels = {
            1: "L1 精确名称",
            2: "L2 归一化（全半角/括号/空格）",
            3: "L3 规格推断（可乐 → 可乐中杯）",
            4: "L4 风味后缀剥离",
            5: "L5 唯一包含",
        }
        for level, count in report["by_level"].items():
            mark = " ← 推断值，界面会标注" if level in (3, 5) and count else ""
            print(f"    {level_labels[level]:<28}: {count}{_c(mark, YELLOW)}")
        print()
        if report["misses"]:
            print(_c(f"  真无官方营养数据：{len(report['misses'])} 项（显示「暂无」，不估算）", BOLD))
            for name in report["misses"][:24]:
                print(f"      - {name}")
            if len(report["misses"]) > 24:
                print(f"      … 其余 {len(report['misses']) - 24} 项")
            print()
        print(_c("结论：单品可得率才是真实可用指标；套餐与周边不应计入分母。", DIM))
        return 0

    overrides = {
        k: getattr(args, k)
        for k in ("kcal", "protein", "carb", "fat", "sodium")
        if getattr(args, k) is not None
    }
    # ``applied_only`` because a preset is a five-field goal profile but only
    # its own core keys filter the list -- see rule 3 in profiles.py.
    target, notes = build_target(args.goal or None, args.weight, overrides, applied_only=True)

    if args.json:
        payload = {
            "mode": "demo",
            "source": data.source_label,
            "target": target.as_dict(),
            "notes": notes,
            "stats": summarise(items, target),
            "items": [dump(i) for i in rank([i for i in items if meets(i, target)], target, args.sort)],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print()
    print(_c("点单参谋 · 麦当劳营养速查", BOLD) + _c("   [离线演示模式]", DIM))
    print(_c(f"数据来源：{data.source_label}（真实接口快照，见 fixtures/_meta.json）", DIM))
    print()

    preset = PRESETS.get(args.goal) if args.goal else None
    if preset:
        print(f"{preset.emoji} {_c(preset.label, BOLD)}  {preset.reason}")
    for note in notes[1:]:
        print(_c(f"   {note}", DIM))
    print()

    stats = summarise(items, target)
    if target.active_keys():
        print(f"门店在售 {stats['total']} 项 → 符合条件 {_c(str(stats['matched']), BOLD)} 项")
    else:
        print(f"门店在售 {stats['total']} 项（未设置筛选条件，展示全部）")
    if stats["no_data"] or stats["partial_data"]:
        gaps = []
        if stats["no_data"]:
            gaps.append(f"{stats['no_data']} 项官方暂无营养数据")
        if stats["partial_data"]:
            gaps.append(f"{stats['partial_data']} 项套餐有组件未收录（合计仅为已知部分之和）")
        print(_c("其中 " + "、".join(gaps) + "，均已标注", DIM))
    print()

    selected = rank([i for i in items if meets(i, target)], target, args.sort)
    if not selected:
        print(_c("没有餐品同时满足所有条件。建议放宽其中一个上限，或改用 --goal 预设。", YELLOW))
        print()
        print(_c("数据来自麦当劳 MCP 官方接口 · 本项目非麦当劳官方产品 · 营养信息仅供参考", DIM))
        return 0

    _render_table(selected, target, args.limit)
    print()
    print(_c("数据来自麦当劳 MCP 官方接口 · 本项目非麦当劳官方产品 · 营养信息仅供参考", DIM))
    return 0


def run_live(args) -> int:
    try:
        from .mcp_client import MCPClient, MCPError, MCPTransportError
    except ImportError as exc:  # pragma: no cover
        print(f"import failed: {exc}", file=sys.stderr)
        return 2

    try:
        client = MCPClient(verbose=args.verbose)
        info = client.connect()
    except MCPTransportError as exc:
        print(_c(f"连接失败：{exc}", RED), file=sys.stderr)
        print("提示：在线模式需要有效的 MCD_MCP_TOKEN；如需先看效果，请用 --demo", file=sys.stderr)
        return 1

    print(_c(f"已连接：{info.get('name', 'mcd-mcp')} v{info.get('version', '?')}", GREEN))

    try:
        store_code = args.store_code
        if not store_code:
            store = client.find_store(keyword="麦当劳", city=args.city)
            if not store:
                print(_c("未找到门店。可用 --store-code 直接指定。", RED), file=sys.stderr)
                return 1
            store_code = store.get("storeCode", "")
            print(f"门店：{store.get('storeName')} ({store_code})")

        nutrition_rows = client.list_nutrition_foods()
        print(f"营养表：{len(nutrition_rows)} 条")
        raw_menu = client.query_meals(store_code).get("meals", {})
        print(f"门店菜单：{len(raw_menu)} 项")

        # Resolve combo structure for set meals so their totals can be summed.
        details: dict[str, dict] = {}
        for code, entry in raw_menu.items():
            if "套" in str(entry.get("name", "")) or "餐" in str(entry.get("name", "")):
                try:
                    details[str(code)] = client.query_meal_detail(store_code, str(code))
                except (MCPError, MCPTransportError) as exc:
                    print(_c(f"  [warn] {entry.get('name')} 详情获取失败：{exc}", YELLOW))

        index = NutritionIndex(nutrition_rows)
        items = build_menu(raw_menu, index, details)
        for item in items:
            item.tags = annotate_tags(item)
    except (MCPError, MCPTransportError) as exc:
        print(_c(f"调用失败：{exc}", RED), file=sys.stderr)
        return 1

    overrides = {
        k: getattr(args, k)
        for k in ("kcal", "protein", "carb", "fat", "sodium")
        if getattr(args, k) is not None
    }
    # ``applied_only`` because a preset is a five-field goal profile but only
    # its own core keys filter the list -- see rule 3 in profiles.py.
    target, notes = build_target(args.goal or None, args.weight, overrides, applied_only=True)

    if args.json:
        print(
            json.dumps(
                {
                    "mode": "live",
                    "store_code": store_code,
                    "target": target.as_dict(),
                    "stats": summarise(items, target),
                    "items": [dump(i) for i in rank([i for i in items if meets(i, target)], target, args.sort)],
                    "calls": client.calls,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print()
    stats = summarise(items, target)
    print(f"门店在售 {stats['total']} 项 → 符合条件 {stats['matched']} 项")
    print()
    _render_table(rank([i for i in items if meets(i, target)], target, args.sort), target, args.limit)
    print()
    print(_c("数据实时来自麦当劳 MCP 官方接口 · 本项目非麦当劳官方产品", DIM))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_presets:
        return cmd_list_presets()

    if not args.live:
        # --demo is the default whenever we can work offline.
        try:
            return run_demo(args)
        except FileNotFoundError as exc:
            print(_c(str(exc), RED), file=sys.stderr)
            return 2

    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
