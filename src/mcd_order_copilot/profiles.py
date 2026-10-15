"""Goal presets for the "I don't know what my numbers should be" case.

Design rules, in order of importance:

1. **Public consensus ranges, never pseudo-precision.**
   Write "protein >= 25 g" (a floor), never "protein 25.4 g". A fake decimal
   implies a clinical basis this project does not have.
2. **A preset is a full goal profile, not a lone filter.**
   Every preset states a position on all five targets, and the UI shows all five
   with the basis behind each number. A panel that renders only two rows reads as
   an oversight, and leaves the reader unable to tell whether carbohydrate, fat
   and sodium were considered and rejected or simply forgotten.
3. **...but only a couple of them actually filter the list.**
   Profile and filter are deliberately different things. Measured against the
   real menu, five simultaneous ceilings return an empty page every time: the
   median item already carries ~894 mg sodium and 22 g fat, and a "fat <= 20 AND
   sodium <= 800 AND carb <= 60" stack leaves zero items. An empty list teaches
   the reader nothing, so ``filter_keys`` names the one or two constraints that
   are applied by default; the rest are shown as reference targets the user can
   switch on by hand.
4. **Every number carries its basis.**
   Sources are public consensus documents only (Chinese dietary guidelines, WHO
   sodium guidance, ISSN position stands), none of which is specific to fast
   food, and none of which is a clinical prescription.
5. **Optional body-weight input.** With a weight, targets scale by common
   per-kg coefficients and are labelled as estimates; without one, the generic
   range is used as-is.
6. **Not medical advice.** These presets cover training-nutrition goals only.
   They intentionally exclude clinical populations (see ``docs/scope.md``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def round_half_up(value: float) -> int:
    """Round to nearest, halves away from zero.

    ``round()`` uses banker's rounding, so 24.5 becomes 24 while 25.5 becomes 26.
    A reader comparing two body weights would read that as a bug rather than as a
    rule, and there is no clinical reason to prefer the even neighbour.
    """
    return int(math.floor(value + 0.5))


#: Where each threshold comes from, shown in the UI next to the number.
#:
#: These are the *generic* bases. A preset overrides a key when its own
#: reasoning differs -- a cutting phase, for instance, argues about protein
#: differently from a bulking phase.
PARAM_BASIS: dict[str, str] = {
    "kcal_max": "一顿正餐约占全天能量的 1/3；全天总量按个人 TDEE 估算",
    "protein_min": "每餐约 0.4 g/kg 体重可最大化肌肉蛋白合成（Moore 等 2009）；ISSN 建议全天 1.6~2.2 g/kg",
    "carb_max": "训练人群全天碳水建议 3~5 g/kg（ISSN 立场声明 2018），单餐按全天约 1/3 折算",
    "fat_max": "脂肪供能比建议 20%~30%（《中国居民膳食指南 2022》）；全天不宜低于 0.5 g/kg",
    "sodium_max": "WHO 建议成人全天钠低于 2000 mg（约 5 g 食盐），单餐取其一半以内",
}

#: Field name -> short label, used in the "derived from your weight" note and
#: anywhere else a two-character tag is needed.
PARAM_SHORT: dict[str, str] = {
    "kcal_max": "热量",
    "protein_min": "蛋白",
    "carb_max": "碳水",
    "fat_max": "脂肪",
    "sodium_max": "钠",
}


@dataclass
class Target:
    """A soft filter target. ``None`` means the user chose not to constrain it."""

    kcal_max: float | None = None
    protein_min: float | None = None
    carb_max: float | None = None
    fat_max: float | None = None
    sodium_max: float | None = None

    def active_keys(self) -> list[str]:
        return [k for k in ("kcal_max", "protein_min", "carb_max", "fat_max", "sodium_max") if getattr(self, k) is not None]

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in self.active_keys()}


@dataclass
class Preset:
    key: str
    label: str
    emoji: str
    reason: str
    #: The full goal profile -- all five targets, always. Shown in the UI.
    target: Target
    #: The subset of :attr:`target` applied to the product list by default.
    #: See rule 3 in the module docstring for why this is not simply "all five".
    filter_keys: tuple[str, ...] = ()
    #: per-kg coefficients applied when an optional weight is supplied.
    #: Sodium is deliberately absent: it is a daily total, not a per-bodyweight
    #: quantity, so scaling it by weight would be fabrication.
    per_kg: dict[str, float] = field(default_factory=dict)
    #: field name -> why *this* preset picked *this* number. Falls back to
    #: :data:`PARAM_BASIS` when the generic reasoning is already the right one.
    basis: dict[str, str] = field(default_factory=dict)

    def basis_for(self, field_name: str) -> str:
        return self.basis.get(field_name) or PARAM_BASIS.get(field_name, "")

    def target_for(self, weight_kg: float | None = None) -> tuple[Target, str]:
        """Return the target plus a note describing how it was derived."""
        if not weight_kg or not self.per_kg:
            return self.target, "通用区间（未提供体重）"
        scaled = Target(**{k: getattr(self.target, k) for k in vars(self.target)})
        parts: list[str] = []
        for k in ("protein_min", "kcal_max", "carb_max", "fat_max"):
            if k in self.per_kg:
                setattr(scaled, k, round_half_up(self.per_kg[k] * weight_kg))
                parts.append(f"{PARAM_SHORT[k]} {self.per_kg[k]:g}/kg")
        return scaled, "按体重 " + f"{weight_kg:g}kg 估算（" + "、".join(parts) + "）"

    def filtered(self, weight_kg: float | None = None) -> dict[str, float]:
        """The constraint set actually passed to the filter."""
        target, _ = self.target_for(weight_kg)
        return {k: getattr(target, k) for k in self.filter_keys if getattr(target, k) is not None}


PRESETS: dict[str, Preset] = {
    "bulk": Preset(
        key="bulk",
        label="增肌期",
        emoji="💪",
        reason="训练后要接得住：蛋白给足、热量留有余地。默认按这两项筛，碳水、脂肪、钠给出对照目标。",
        target=Target(kcal_max=900, protein_min=25, carb_max=90, fat_max=30, sodium_max=1000),
        filter_keys=("protein_min", "kcal_max"),
        per_kg={"kcal_max": 12.0, "protein_min": 0.35, "carb_max": 1.20, "fat_max": 0.40},
        basis={
            "kcal_max": "增肌需要热量盈余（较维持量高约 10%~15%），一顿按全天 1/3 折算",
            "protein_min": "增肌期蛋白取推荐区间中上段，每餐约 0.35~0.4 g/kg 体重",
            "carb_max": "训练后补充碳水有助于恢复，全天 4~6 g/kg 属常见区间",
        },
    ),
    "cut": Preset(
        key="cut",
        label="减脂期",
        emoji="🔥",
        reason="热量缺口是核心，但蛋白质不能掉——掉蛋白会连肌肉一起掉。所以热量设上限、蛋白同时设下限。",
        target=Target(kcal_max=600, protein_min=25, carb_max=60, fat_max=20, sodium_max=800),
        filter_keys=("kcal_max", "protein_min"),
        per_kg={"kcal_max": 8.0, "protein_min": 0.35, "carb_max": 0.80, "fat_max": 0.25},
        basis={
            "kcal_max": "减脂需热量缺口（较维持量低约 15%~25%），一顿按全天 1/3 折算",
            "protein_min": "减脂期应提高蛋白以保住肌肉，ISSN 建议全天 1.6~2.4 g/kg",
            "carb_max": "减脂期碳水通常下调至全天 2~3 g/kg，单餐相应收紧",
            "fat_max": "脂肪供给下调但不宜过低，全天不应低于 0.5 g/kg 以维持激素水平",
        },
    ),
    "recomp": Preset(
        key="recomp",
        label="增肌减脂期",
        emoji="⚖️",
        reason="同时要够的蛋白、又要控住的热量。这是综合目标里最紧的一档，通过筛选的餐品会明显变少。",
        target=Target(kcal_max=900, protein_min=30, carb_max=70, fat_max=25, sodium_max=900),
        filter_keys=("protein_min", "kcal_max"),
        per_kg={"kcal_max": 10.0, "protein_min": 0.42, "carb_max": 0.90, "fat_max": 0.30},
        basis={
            "kcal_max": "身体重组以接近维持量的热量为前提，一顿按全天 1/3 折算",
            "protein_min": "重组期需要较高的蛋白摄入，取 ISSN 推荐区间中上段",
            "fat_max": "脂肪限量但保有下限，全天不低于 0.5 g/kg",
        },
    ),
    "high_protein": Preset(
        key="high_protein",
        label="高蛋白",
        emoji="🥩",
        reason="重心全在蛋白质够不够，其余四项放到宽松档，避免把高蛋白餐品误筛掉。",
        target=Target(kcal_max=1200, protein_min=35, carb_max=110, fat_max=45, sodium_max=1400),
        filter_keys=("protein_min",),
        per_kg={"kcal_max": 16.0, "protein_min": 0.45, "carb_max": 1.50, "fat_max": 0.60},
        basis={
            "kcal_max": "其余四项刻意放宽，本预设只对蛋白设硬门槛",
            "protein_min": "取 ISSN 推荐区间上沿，每餐约 0.45 g/kg 体重",
        },
    ),
    "low_fat": Preset(
        key="low_fat",
        label="控脂",
        emoji="🥗",
        reason="脂肪设硬上限。油炸类和酱料多的品项会集中被筛掉，这正是这个筛选的意义。",
        target=Target(kcal_max=800, protein_min=25, carb_max=90, fat_max=18, sodium_max=1000),
        filter_keys=("fat_max",),
        per_kg={"kcal_max": 11.0, "protein_min": 0.35, "carb_max": 1.20, "fat_max": 0.25},
        basis={
            "fat_max": "取脂肪供能比 20%~30% 的下沿，单餐收紧到全天 1/3 以内",
            "kcal_max": "脂肪下调后热量顺带放宽，避免把非油炸主食一并筛掉",
        },
    ),
    "low_sodium": Preset(
        key="low_sodium",
        label="控钠",
        emoji="🧂",
        reason="钠设硬上限。快餐的钠普遍偏高——这份菜单里餐品钠含量的中位数就有将近 900 mg。",
        target=Target(kcal_max=800, protein_min=25, carb_max=90, fat_max=30, sodium_max=800),
        filter_keys=("sodium_max",),
        per_kg={"kcal_max": 11.0, "protein_min": 0.35, "carb_max": 1.20, "fat_max": 0.40},
        basis={
            "sodium_max": "单餐不超过 WHO 每日建议 2000 mg 的四成，留出另外两餐的额度",
            "kcal_max": "热量与蛋白按常规单餐区间设定，本预设的硬门槛是钠",
        },
    ),
    "carb_control": Preset(
        key="carb_control",
        label="控碳水",
        emoji="🍞",
        reason="碳水设上限。官方数据里没有升糖指数，所以这里控的是碳水总量，不是升糖速度。",
        target=Target(kcal_max=800, protein_min=25, carb_max=45, fat_max=35, sodium_max=1000),
        filter_keys=("carb_max",),
        per_kg={"kcal_max": 11.0, "protein_min": 0.35, "carb_max": 0.60, "fat_max": 0.45},
        basis={
            "carb_max": "低于训练人群全天 3 g/kg 的常规下限，单餐按 1/3 折算",
            "fat_max": "碳水压低后脂肪可适度放宽，仍守 30% 供能比上限",
        },
    ),
    "pre_workout": Preset(
        key="pre_workout",
        label="训练前补给",
        emoji="⚡",
        reason="训练前要的是能快速用的碳水，同时别太撑。碳水留出空间，热量和脂肪都收着。",
        target=Target(kcal_max=500, protein_min=15, carb_max=60, fat_max=15, sodium_max=800),
        filter_keys=("kcal_max", "carb_max"),
        per_kg={"kcal_max": 7.0, "protein_min": 0.20, "carb_max": 0.80, "fat_max": 0.20},
        basis={
            "kcal_max": "训练前总热量过高会引起胃肠不适，单餐控制在 500 kcal 以内",
            "carb_max": "训练前 1~4 小时建议补充 1~4 g/kg 碳水（ISSN 2018），取单餐可执行区间",
            "fat_max": "训练前脂肪宜少，以缩短胃排空时间",
        },
    ),
}

#: Order shown in the UI -- deliberately starts with the three "I don't know" options.
PRESET_ORDER = ["bulk", "cut", "recomp", "high_protein", "carb_control", "low_fat", "low_sodium", "pre_workout"]

#: CLI flag name -> :class:`Target` field name.
#:
#: ``--protein`` reads better on a command line than ``--protein-min``, but the
#: translation has to be explicit: a field is either a ceiling or a floor, and
#: guessing that from the flag name is how ``--protein`` silently becomes a
#: no-op instead of a filter.
OVERRIDE_ALIASES = {
    "kcal": "kcal_max",
    "protein": "protein_min",
    "carb": "carb_max",
    "fat": "fat_max",
    "sodium": "sodium_max",
}

#: Target field -> the wording shown back to the user.
OVERRIDE_LABELS = {
    "kcal_max": "热量上限",
    "protein_min": "蛋白下限",
    "carb_max": "碳水上限",
    "fat_max": "脂肪上限",
    "sodium_max": "钠上限",
}


def build_target(
    preset_key: str | None = None,
    weight_kg: float | None = None,
    overrides: dict[str, float] | None = None,
    applied_only: bool = False,
) -> tuple[Target, list[str]]:
    """Compose a target from a preset, an optional body weight, and manual overrides.

    Returns the target plus human-readable notes explaining where it came from.

    ``applied_only`` narrows a preset down to the keys that actually filter the
    list (:attr:`Preset.filter_keys`). A caller that feeds the result to
    ``meets()`` wants this; a caller that renders the goal profile does not,
    because the profile is deliberately wider than the filter -- applying all
    five thresholds at once returns an empty page (rule 3 above).
    """
    notes: list[str] = []
    if preset_key:
        preset = PRESETS.get(preset_key)
        if preset is None:
            notes.append(f"未知预设 {preset_key!r}，已忽略")
            target = Target()
        else:
            target, how = preset.target_for(weight_kg)
            notes.append(f"{preset.label}：{preset.reason}")
            notes.append(f"数值来源：{how}")
            if applied_only:
                keep = preset.filtered(weight_kg)
                target = Target(**{k: keep.get(k) for k in vars(Target())})
                labels = [OVERRIDE_LABELS[k] for k in preset.filter_keys if k in keep]
                notes.append("实际筛选条件：" + "、".join(labels) + "（其余为对照目标，可用命令行参数手动加入）")
    else:
        target = Target()
        notes.append("未选择预设，全部参数由用户自定义")

    if overrides:
        applied: list[str] = []
        for key, value in overrides.items():
            if value is None:
                continue
            field_name = OVERRIDE_ALIASES.get(key, key)
            if field_name not in vars(Target()):
                notes.append(f"无法识别的参数 {key!r}，已忽略")
                continue
            setattr(target, field_name, float(value))
            applied.append(OVERRIDE_LABELS.get(field_name, field_name))
        if applied:
            notes.append("已应用自定义条件：" + "、".join(applied))

    notes.append("以上为目标区间下限/上限参考值，非医疗或营养处方")
    return target, notes
