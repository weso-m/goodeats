#!/usr/bin/env python3
"""
Meal Plan Generator (Modular Mains & Sides)

- Reads recipe cards from YAML files (./cards/*.yaml by default)
- Uses either:
    - manual weekly selection (YAML or --select), OR
    - automatic weekly plan generation (no selection given)
- Cards:
    - role: main | side | both
    - mains: 300–800 kcal, lunch/dinner
    - sides: 50–300 kcal, side
- Auto mode:
    - Chooses N unique recipes (mains + sides) based on targets.yaml
    - Ensures at least 1 main
    - Builds each meal as main + 0–2 sides, aiming for 450–800 kcal
    - Repeats those recipes across 14 slots (leftovers & batching)
- Outputs:
    - out/week_plan.csv
    - out/day_summary.csv
    - out/grocery_list.csv
    - out/weekly_plan.md
    - out/*.png plots (if matplotlib is installed)
"""

from __future__ import annotations
import argparse
import collections
import csv
import dataclasses as dc
import glob
import os
import random
import sys
from typing import Dict, List, Tuple, Optional

import yaml

OZ_TO_G = 28.349523125
G_TO_OZ = 1.0 / OZ_TO_G


# ---------- Data Models ----------

@dc.dataclass
class Ingredient:
    item: str
    qty: float
    unit: str  # g | oz | tbsp | tsp | cup | whole | etc.
    grocery_section: str = "other"


@dc.dataclass
class Macros:
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float

@dc.dataclass
class RecipeCard:
    id: str
    name: str
    role: str              # "main", "side", or "both"
    servings_default: int
    portion_size_note: str
    macros_per_serving: Macros
    primary_carb: List[str]
    protein_source: List[str]
    veg: List[str]
    allergens: List[str]
    meal_types: List[str]
    meal_freq_cap_per_week: int
    prep_time_min: int
    cook_time_min: int
    batch_friendly: bool
    reheat_method: List[str]
    ingredients: List[Ingredient]
    steps: List[str]
    notes: List[str]
    meal_slots: List[str]  # NEW: ["breakfast","lunch","dinner","snack"] options

    @staticmethod
    def from_dict(d: Dict) -> "RecipeCard":
        m = d.get("macros_per_serving", {})
        macros = Macros(
            calories=float(m.get("calories", 0)),
            protein_g=float(m.get("protein_g", 0)),
            carbs_g=float(m.get("carbs_g", 0)),
            fat_g=float(m.get("fat_g", 0)),
        )
        ings: List[Ingredient] = []
        for r in d.get("ingredients", []):
            if isinstance(r, dict):
                ings.append(
                    Ingredient(
                        item=str(r.get("item")),
                        qty=float(r.get("qty", 0)),
                        unit=str(r.get("unit", "")),
                        grocery_section=str(r.get("grocery_section", "other")),
                    )
                )

        return RecipeCard(
            id=str(d["id"]),
            name=str(d["name"]),
            role=str(d.get("role", "main")),
            servings_default=int(d.get("servings_default", 2)),
            portion_size_note=str(d.get("portion_size_note", "")),
            macros_per_serving=macros,
            primary_carb=list(d.get("primary_carb", [])),
            protein_source=list(d.get("protein_source", [])),
            veg=list(d.get("veg", [])),
            allergens=list(d.get("allergens", [])),
            meal_types=list(d.get("meal_types", [])),
            meal_freq_cap_per_week=int(d.get("meal_freq_cap_per_week", 3)),
            prep_time_min=int(d.get("prep_time_min", 0)),
            cook_time_min=int(d.get("cook_time_min", 0)),
            batch_friendly=bool(d.get("batch_friendly", True)),
            reheat_method=list(d.get("reheat_method", [])),
            ingredients=ings,
            steps=[str(s) for s in d.get("steps", [])],
            notes=[str(n) for n in d.get("notes", [])],
            meal_slots=list(d.get("meal_slots", [])),  # NEW
        )

@dc.dataclass
class Targets:
    calories_min: float = 1400
    calories_max: float = 1600
    protein_min_g: float = 110
    carbs_max_g: Optional[float] = None
    fat_max_g: Optional[float] = None
    min_unique_main_meals: Optional[int] = None
    max_unique_main_meals: Optional[int] = None

    # NEW: daily structure
    meal_slots: List[str] = dc.field(default_factory=lambda: ["Lunch", "Dinner"])
    meals_per_day: int = 2
    include_snacks: bool = False
    max_snacks_per_day: int = 0



@dc.dataclass
class MealSlot:
    day: int                 # 0..6
    slot: str                # "Lunch" | "Dinner"
    card_ids: List[str]      # one main + optional sides
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float


# ---------- Config Loading ----------

def load_targets(path: Optional[str]) -> Targets:
    """
    Load targets from YAML, or fall back to defaults.

    Priority:
    1. --targets <file> if provided and exists
    2. targets.yaml next to this script
    3. targets.yaml in current working directory
    4. Built-in defaults
    """
    cfg_path = None

    if path:
        if os.path.exists(path):
            cfg_path = path
        else:
            print(f"[warn] Targets file '{path}' not found. Using defaults.")
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cand1 = os.path.join(script_dir, "targets.yaml")
        cand2 = os.path.join(os.getcwd(), "targets.yaml")
        if os.path.exists(cand1):
            cfg_path = cand1
        elif os.path.exists(cand2):
            cfg_path = cand2

    if not cfg_path:
        t = Targets()
        print("[info] No targets.yaml found. Using built-in defaults.")
        # set default unique range only if not specified
        if t.min_unique_main_meals is None and t.max_unique_main_meals is None:
            t.min_unique_main_meals, t.max_unique_main_meals = 2, 3
        return t

    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    t = Targets(
        calories_min=float(data.get("calories_min", 1400)),
        calories_max=float(data.get("calories_max", 1600)),
        protein_min_g=float(data.get("protein_min_g", 110)),
        carbs_max_g=float(data["carbs_max_g"]) if "carbs_max_g" in data else None,
        fat_max_g=float(data["fat_max_g"]) if "fat_max_g" in data else None,
        min_unique_main_meals=(
            int(data["min_unique_main_meals"]) if "min_unique_main_meals" in data else None
        ),
        max_unique_main_meals=(
            int(data["max_unique_main_meals"]) if "max_unique_main_meals" in data else None
        ),
    )

    # NEW: meal structure config
    meal_slots = data.get("meal_slots")
    if isinstance(meal_slots, list) and meal_slots:
        t.meal_slots = [str(s) for s in meal_slots]
    # meals_per_day: default to len(meal_slots) if provided, else keep default
    if "meals_per_day" in data:
        t.meals_per_day = max(1, int(data["meals_per_day"]))
    else:
        t.meals_per_day = max(1, len(t.meal_slots))

    t.include_snacks = bool(data.get("include_snacks", False))
    t.max_snacks_per_day = int(data.get(
        "max_snacks_per_day",
        0 if not t.include_snacks else 1
    ))


    # Normalize unique recipe counts
    min_u = t.min_unique_main_meals
    max_u = t.max_unique_main_meals

    if min_u is None and max_u is None:
        min_u, max_u = 2, 3  # default only if user gave nothing
    elif min_u is None:
        min_u = max(1, max_u)
    elif max_u is None:
        max_u = max(min_u, 1)

    min_u = max(1, int(min_u))
    max_u = max(min_u, int(max_u))

    t.min_unique_main_meals = min_u
    t.max_unique_main_meals = max_u

    print(
        f"[info] Targets loaded from {cfg_path}: "
        f"kcal {t.calories_min}-{t.calories_max}, "
        f"protein ≥{t.protein_min_g} g, "
        f"unique recipes {t.min_unique_main_meals}-{t.max_unique_main_meals}, "
        f"meals/day {t.meals_per_day}, "
        f"slots {t.meal_slots}, "
        f"snacks: {t.include_snacks} (max {t.max_snacks_per_day}/day)"
    )
    return t


def load_cards(cards_dir: str) -> Dict[str, RecipeCard]:
    cards: Dict[str, RecipeCard] = {}
    for path in glob.glob(os.path.join(cards_dir, "*.y*ml")):
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if not doc:
            continue
        if isinstance(doc, list):
            for d in doc:
                card = RecipeCard.from_dict(d)
                cards[card.id] = card
        else:
            card = RecipeCard.from_dict(doc)
            cards[card.id] = card
    return cards


def load_selection(path: Optional[str], inline_select: List[str]) -> Dict[str, int]:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, list):
            return {d["id"]: int(d.get("count", 1)) for d in data}
        if isinstance(data, dict):
            return {k: int(v) for k, v in data.items()}
        raise ValueError("Unsupported selection YAML format")
    sel: Dict[str, int] = {}
    for token in inline_select:
        if ":" not in token:
            raise ValueError(f"Selection token must be ID:COUNT, got {token}")
        cid, cnt = token.split(":", 1)
        sel[cid] = int(cnt)
    return sel


# ---------- Planning Helpers ----------

def expand_pool(selection: Dict[str, int], cards: Dict[str, RecipeCard]) -> List[str]:
    pool: List[str] = []
    for cid, n in selection.items():
        if cid not in cards:
            raise KeyError(f"Card not found: {cid}")
        cap = cards[cid].meal_freq_cap_per_week
        if n > cap:
            print(f"[warn] {cid} requested {n}× but capped at {cap}; truncating.")
            n = cap
        pool.extend([cid] * n)
    return pool


def enforce_variety(pool: List[str], cards: Dict[str, RecipeCard]) -> None:
    """
    Light variety: ensure at least one seafood if available, limit beef-heavy cards.
    Only used in manual/selection mode.
    """
    if not pool:
        return

    protein_map = {cid: set(cards[cid].protein_source) for cid in set(pool)}

    has_seafood = any(
        ("fish" in protein_map[cid] or "shrimp" in protein_map[cid] or "salmon" in protein_map[cid])
        for cid in pool
    )
    if not has_seafood:
        seafood_candidates = [
            c.id for c in cards.values()
            if any(p in ("fish", "shrimp", "salmon") for p in c.protein_source)
        ]
        if seafood_candidates:
            pool.append(seafood_candidates[0])

    red_meat_indices = [
        i for i, cid in enumerate(pool)
        if "beef" in protein_map.get(cid, set())
    ]
    if len(red_meat_indices) > 1:
        non_beef = [cid for cid in pool if "beef" not in protein_map.get(cid, set())]
        if non_beef:
            replacement = collections.Counter(non_beef).most_common(1)[0][0]
            for idx in red_meat_indices[1:]:
                pool[idx] = replacement
def choose_main_for_slot(
    rnd: random.Random,
    mains: List[RecipeCard],
    slot_name: str,
    day_cal_so_far: float,
    targets: Targets,
) -> RecipeCard:
    """
    Pick a main for a given slot, preferring:
    - cards that support this slot,
    - and keep the day <= calories_max when possible.
    Falls back to the lightest compatible option if all overshoot.
    """
    slot_l = slot_name.lower()

    compatible = [m for m in mains if card_supports_slot(m, slot_name, is_snack=False)]
    if not compatible:
        compatible = mains  # fallback: any main

    # Try to find mains that don't blow past the max
    within: List[RecipeCard] = []
    if targets.calories_max:
        for m in compatible:
            if day_cal_so_far + m.macros_per_serving.calories <= targets.calories_max:
                within.append(m)

    if within:
        return rnd.choice(within)

    # If impossible to stay under max, pick the lightest compatible
    return min(compatible, key=lambda m: m.macros_per_serving.calories)


def build_week_plan_manual(selection: Dict[str, int],
                           cards: Dict[str, RecipeCard],
                           seed: int) -> List[MealSlot]:
    """
    Manual mode (selection.yaml):

    - selection: {card_id: count} where counts mean "how many times I'd like this used"
    - Uses meal_freq_cap_per_week as an upper bound per card.
    - Ensures every slot has a MAIN (role: main/both).
    - Uses sides (role: side/both) only as add-ons; never alone.
    - If there are not enough mains to cover 14 slots within caps,
      we repeat mains (with a warning) rather than output side-only meals.
    """
    rnd = random.Random(seed)

    main_pool: List[str] = []
    side_pool: List[str] = []

    for cid, requested in selection.items():
        if cid not in cards:
            print(f"[warn] Selection references unknown card: {cid}; skipping.")
            continue

        card = cards[cid]
        cap = card.meal_freq_cap_per_week
        use = min(int(requested), int(cap)) if cap is not None else int(requested)
        if use <= 0:
            continue

        if card.role in ("main", "both"):
            main_pool.extend([cid] * use)
        if card.role in ("side", "both"):
            side_pool.extend([cid] * use)

    if not main_pool:
        raise ValueError(
            "Manual mode: selection must include at least one main (role: main/both). "
            "Currently only sides are selected."
        )

    rnd.shuffle(main_pool)
    rnd.shuffle(side_pool)

    total_slots = 14  # 7 days * 2 meals

    # If we don't have enough mains within caps, repeat them (but warn).
    if len(main_pool) < total_slots:
        print(
            f"[warn] Manual selection only provides {len(main_pool)} main 'uses' "
            f"for {total_slots} slots. Reusing mains to avoid side-only meals."
        )

    slots: List[MealSlot] = []
    day = 0
    side_idx = 0
    side_len = len(side_pool)

    for i in range(total_slots):
        slot_name = "Lunch" if i % 2 == 0 else "Dinner"

        # Always choose a main
        main_cid = main_pool[i % len(main_pool)]
        main = cards[main_cid]

        comp_ids = [main_cid]
        cal = main.macros_per_serving.calories
        p = main.macros_per_serving.protein_g
        carbs = main.macros_per_serving.carbs_g
        fat = main.macros_per_serving.fat_g

        # Try to attach up to 2 sides, if we have any.
        # We walk through side_pool once; leftover sides are ignored (better than side-only meals).
        if side_len:
            attempts = 0
            while attempts < side_len and len(comp_ids) < 3:
                sid = side_pool[side_idx % side_len]
                side_idx += 1
                attempts += 1

                side = cards[sid]
                new_cal = cal + side.macros_per_serving.calories

                # Use similar band as auto-mode: prefer 450–800 kcal when possible
                if cal < 450:
                    if new_cal <= 800:
                        comp_ids.append(sid)
                        cal = new_cal
                        p += side.macros_per_serving.protein_g
                        carbs += side.macros_per_serving.carbs_g
                        fat += side.macros_per_serving.fat_g
                else:
                    if new_cal <= 800:
                        comp_ids.append(sid)
                        cal = new_cal
                        p += side.macros_per_serving.protein_g
                        carbs += side.macros_per_serving.carbs_g
                        fat += side.macros_per_serving.fat_g

        slots.append(
            MealSlot(
                day=day,
                slot=slot_name,
                card_ids=comp_ids,
                calories=cal,
                protein_g=p,
                carbs_g=carbs,
                fat_g=fat,
            )
        )

        if slot_name == "Dinner":
            day += 1

    return slots

def generate_week_slots(targets: Targets) -> List[Tuple[int, str, bool]]:
    """
    Returns list of (day, slot_name, is_snack) for the whole week
    based on targets.
    """
    slots: List[Tuple[int, str, bool]] = []
    base_slots = targets.meal_slots or ["Lunch", "Dinner"]
    meals_per_day = max(1, targets.meals_per_day)

    for day in range(7):
        # Main meals
        for i in range(meals_per_day):
            slot_name = base_slots[i % len(base_slots)]
            slots.append((day, slot_name, False))

        # Snacks
        if targets.include_snacks and targets.max_snacks_per_day > 0:
            for s in range(targets.max_snacks_per_day):
                name = "Snack" if targets.max_snacks_per_day == 1 else f"Snack{s+1}"
                slots.append((day, name, True))

    return slots

def build_week_plan(pool: List[str], cards: Dict[str, RecipeCard], seed: int = 42) -> List[MealSlot]:
    """
    Manual mode: each selected card ID = a full meal component.
    One card per slot (user controls combos via selection if desired).
    """
    rnd = random.Random(seed)
    rnd.shuffle(pool)
    target = 14
    if len(pool) < target:
        pool = pool + pool[: (target - len(pool))]
    elif len(pool) > target:
        pool = pool[:target]

    slots: List[MealSlot] = []
    day = 0
    for i, cid in enumerate(pool):
        slot_name = "Lunch" if i % 2 == 0 else "Dinner"
        card = cards[cid]
        slots.append(
            MealSlot(
                day=day,
                slot=slot_name,
                card_ids=[cid],
                calories=card.macros_per_serving.calories,
                protein_g=card.macros_per_serving.protein_g,
                carbs_g=card.macros_per_serving.carbs_g,
                fat_g=card.macros_per_serving.fat_g,
            )
        )
        if slot_name == "Dinner":
            day += 1

    return slots


# ---------- Auto-Planning (Mains + Sides) ----------
def card_supports_slot(card: RecipeCard, slot_name: str, is_snack: bool) -> bool:
    slots = [s.lower() for s in (card.meal_slots or [])]
    slot_name_l = slot_name.lower()

    if is_snack:
        return "snack" in slots

    # For main meals: allow if declared, else fallback to legacy behavior
    if slots:
        # match breakfast/lunch/dinner by name
        if slot_name_l == "breakfast":
            return "breakfast" in slots
        if slot_name_l == "lunch":
            return "lunch" in slots or "dinner" in slots
        if slot_name_l == "dinner":
            return "dinner" in slots or "lunch" in slots
    else:
        # legacy cards: treat mains/sides as lunch+dinner
        return slot_name_l in ("lunch", "dinner")

    return False

def build_auto_week_plan(cards: Dict[str, RecipeCard], targets: Targets, seed: int) -> List[MealSlot]:
    rnd = random.Random(seed)

    # Build weekly slot schedule (configured via targets.yaml)
    week_slots = generate_week_slots(targets)
    if not week_slots:
        raise ValueError("No meal slots configured in targets.yaml (meal_slots/meals_per_day).")

    # Eligible pools
    mains_all = [
        c for c in cards.values()
        if c.role in ("main", "both")
        and c.batch_friendly
        and 250 <= c.macros_per_serving.calories <= 800
    ]
    sides_all = [
        c for c in cards.values()
        if c.role in ("side", "both")
        and c.batch_friendly
        and 40 <= c.macros_per_serving.calories <= 300
    ]
    snacks_all = [
        c for c in cards.values()
        if c.batch_friendly
        and "snack" in [s.lower() for s in (c.meal_slots or [])]
        and 50 <= c.macros_per_serving.calories <= 300
    ]

    if not mains_all:
        raise ValueError("Auto mode: no eligible mains available.")

    # --- Unique mains selection (similar to before) ---
    total_available = len(mains_all) + len(sides_all)
    min_u = targets.min_unique_main_meals or 2
    max_u = targets.max_unique_main_meals or max(min_u, 3)

    min_u = max(1, min_u)
    max_u = max(min_u, max_u)
    max_u = min(max_u, total_available) if total_available > 0 else max_u

    n_unique = rnd.randint(min_u, max_u)
    n_mains = max(1, min(len(mains_all), n_unique))
    chosen_mains = rnd.sample(mains_all, n_mains)

    chosen_sides: List[RecipeCard] = []
    if sides_all:
        max_sides = min(len(sides_all), max(1, 2 * n_mains))
        n_sides = rnd.randint(1, max_sides)
        chosen_sides = rnd.sample(sides_all, n_sides)

    if targets.include_snacks and week_slots and not snacks_all:
        print("[warn] Snack slots configured but no snack-eligible cards (meal_slots includes 'snack').")

    print(
        f"[info] Auto mode using {len(chosen_mains)} mains and "
        f"{len(chosen_sides)} sides. Snacks pool: {len(snacks_all)}."
    )

    # Track per-day totals while building
    day_totals = {d: 0.0 for d in range(7)}
    slots: List[MealSlot] = []

    for (day, slot_name, is_snack) in week_slots:
        # --- Snack slots ---
        if is_snack:
            if not snacks_all:
                continue  # nothing snack-safe, skip quietly

            # Prefer snacks that don't push us over calories_max
            allowed_snacks = snacks_all
            snack = None
            if targets.calories_max:
                under = [
                    s for s in allowed_snacks
                    if day_totals[day] + s.macros_per_serving.calories <= targets.calories_max
                ]
                if under:
                    snack = rnd.choice(under)

            if snack is None:
                # If we're already under min, we can allow one anyway, but avoid absurd overshoot
                if targets.calories_min and day_totals[day] < targets.calories_min:
                    snack = min(
                        allowed_snacks,
                        key=lambda s: s.macros_per_serving.calories
                    )

            if snack is None:
                continue  # no reasonable snack to add

            cal = snack.macros_per_serving.calories
            p = snack.macros_per_serving.protein_g
            c = snack.macros_per_serving.carbs_g
            f = snack.macros_per_serving.fat_g

            day_totals[day] += cal

            slots.append(
                MealSlot(
                    day=day,
                    slot=slot_name,
                    card_ids=[snack.id],
                    calories=cal,
                    protein_g=p,
                    carbs_g=c,
                    fat_g=f,
                )
            )
            continue

        # --- Main meals (Breakfast/Lunch/Dinner style) ---
        # Choose a main that fits this slot and (if possible) stays under daily max
        main = choose_main_for_slot(rnd, chosen_mains, slot_name, day_totals[day], targets)

        comp_ids = [main.id]
        cal = main.macros_per_serving.calories
        p = main.macros_per_serving.protein_g
        c = main.macros_per_serving.carbs_g
        f = main.macros_per_serving.fat_g

        # Attach sides carefully: try not to blow past daily max
        if chosen_sides:
            side_candidates = chosen_sides[:]
            rnd.shuffle(side_candidates)

            for side in side_candidates:
                if len(comp_ids) >= 3:
                    break
                if not card_supports_slot(side, slot_name, is_snack=False):
                    continue

                side_cal = side.macros_per_serving.calories
                new_meal_cal = cal + side_cal
                new_day_cal = day_totals[day] + side_cal

                # Aim for 450-800 kcal per main meal, but respect daily max where possible
                within_meal_band = (
                    (cal < 450 and new_meal_cal <= 800) or
                    (450 <= cal <= 800 and new_meal_cal <= 800)
                )

                if not within_meal_band:
                    continue

                # If calories_max set, don't exceed it unless we're way under min and need the bump
                if targets.calories_max:
                    if new_day_cal > targets.calories_max:
                        # Allow slight overshoot only if we're well under min and this helps
                        if not (targets.calories_min and day_totals[day] < targets.calories_min and new_day_cal <= targets.calories_max * 1.05):
                            continue

                # Attach side
                comp_ids.append(side.id)
                cal = new_meal_cal
                p += side.macros_per_serving.protein_g
                c += side.macros_per_serving.carbs_g
                f += side.macros_per_serving.fat_g

        day_totals[day] += cal

        slots.append(
            MealSlot(
                day=day,
                slot=slot_name,
                card_ids=comp_ids,
                calories=cal,
                protein_g=p,
                carbs_g=c,
                fat_g=f,
            )
        )

    # Soft validation / debug
    for d in range(7):
        dt = day_totals[d]
        if targets.calories_max and dt > targets.calories_max * 1.05:
            print(f"[warn] Day {d} total {dt:.0f} kcal exceeds calories_max; "
                  "check meal sizes/targets — may be mathematically impossible to stay under.")

    return slots



# ---------- Summaries & Grocery ----------

def summarize_days(slots: List[MealSlot], targets: Targets) -> List[Dict]:
    summaries: List[Dict] = []

    for day in range(7):
        day_slots = [s for s in slots if s.day == day]

        cals = sum(s.calories for s in day_slots)
        protein = sum(s.protein_g for s in day_slots)
        carbs = sum(s.carbs_g for s in day_slots)
        fat = sum(s.fat_g for s in day_slots)

        notes: List[str] = []

        # Calories
        if cals < targets.calories_min:
            gap = targets.calories_min - cals
            if gap <= 200:
                notes.append(
                    "Slightly increase meal portions (e.g. +2 oz protein or +100 g potato/veg with olive oil)."
                )
            else:
                notes.append(
                    "Day is under target; choose higher-calorie cards or larger portions so main meals do the work."
                )
        elif cals > targets.calories_max:
            notes.append(
                "Slightly reduce potato/rice or added fats/dressings to bring calories into range."
            )

        # Protein
        if protein < targets.protein_min_g:
            notes.append(
                "Protein a bit low; add ~2–4 oz lean protein to one meal or bump protein portions."
            )

        # Optional carb/fat caps
        if targets.carbs_max_g is not None and carbs > targets.carbs_max_g:
            notes.append("Carbs above target; trim carb portions slightly on this day.")
        if targets.fat_max_g is not None and fat > targets.fat_max_g:
            notes.append("Fat above target; ease up on oils, sauces, or fatty cuts.")

        summaries.append(
            {
                "day": day,
                "calories": round(cals),
                "protein_g": round(protein),
                "carbs_g": round(carbs),
                "fat_g": round(fat),
                "notes": " ".join(notes),
            }
        )

    return summaries


def normalize_key_unit(item: str, unit: str) -> Tuple[str, str]:
    item_norm = item.strip().lower()
    unit_norm = unit.strip().lower()
    if unit_norm == "cups":
        unit_norm = "cup"
    return item_norm, unit_norm


def can_convert(u1: str, u2: str) -> bool:
    return {u1, u2} == {"g", "oz"}


def convert_qty(qty: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return qty
    if from_unit == "oz" and to_unit == "g":
        return qty * OZ_TO_G
    if from_unit == "g" and to_unit == "oz":
        return qty * G_TO_OZ
    raise ValueError(f"Unsupported conversion {from_unit}->{to_unit}")


def aggregate_grocery(slots: List[MealSlot], cards: Dict[str, RecipeCard]) -> List[Dict]:
    counts = collections.Counter(
        cid
        for s in slots
        for cid in s.card_ids
    )
    by_item: Dict[Tuple[str, str], Dict] = {}

    for cid, used_servings in counts.items():
        card = cards[cid]
        scale = used_servings / float(card.servings_default)
        for ing in card.ingredients:
            k_item, k_unit = normalize_key_unit(ing.item, ing.unit)
            key = (k_item, k_unit)
            if key not in by_item:
                by_item[key] = {
                    "item": k_item,
                    "qty": 0.0,
                    "unit": k_unit,
                    "grocery_section": ing.grocery_section,
                }
            by_item[key]["qty"] += ing.qty * scale

    merged: Dict[str, Dict[str, float]] = {}
    meta: Dict[str, str] = {}
    for (item, unit), rec in by_item.items():
        merged.setdefault(item, {})
        merged[item][unit] = merged[item].get(unit, 0.0) + rec["qty"]
        meta[item] = rec.get("grocery_section", "other")

    final_rows: List[Dict] = []
    for item, units in merged.items():
        if "g" in units and "oz" in units:
            total_g = units.get("g", 0.0) + convert_qty(units.get("oz", 0.0), "oz", "g")
            final_rows.append(
                {"item": item, "qty": round(total_g, 1), "unit": "g", "grocery_section": meta[item]}
            )
        else:
            unit = next(iter(units.keys()))
            qty = units[unit]
            if unit == "g":
                qty = round(qty)
            elif unit in ("oz", "tbsp", "tsp", "cup"):
                qty = round(qty, 1)
            final_rows.append(
                {"item": item, "qty": qty, "unit": unit, "grocery_section": meta[item]}
            )

    final_rows.sort(key=lambda r: (r["grocery_section"], r["item"]))
    return final_rows


# ---------- Output Writers ----------

def ensure_dir(p: str) -> None:
    if p:
        os.makedirs(p, exist_ok=True)


def write_csv(path: str, rows: List[Dict]) -> None:
    if not rows:
        return
    ensure_dir(os.path.dirname(path))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_week_plan_csv(path: str, slots: List[MealSlot], cards: Dict[str, RecipeCard]) -> None:
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    rows: List[Dict] = []
    for s in slots:
        names = " + ".join(cards[cid].name for cid in s.card_ids)
        rows.append(
            {
                "day": day_names[s.day],
                "slot": s.slot,
                "card_ids": " | ".join(s.card_ids),
                "name": names,
                "calories": round(s.calories),
                "protein_g": round(s.protein_g),
            }
        )
    write_csv(path, rows)


def write_day_summary_csv(path: str, day_summary: List[Dict]) -> None:
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    rows = []
    for d in day_summary:
        rows.append(
            {
                "day": day_names[d["day"]],
                "calories": d["calories"],
                "protein_g": d["protein_g"],
                "carbs_g": d["carbs_g"],
                "fat_g": d["fat_g"],
                "notes": d["notes"],
            }
        )
    write_csv(path, rows)


def write_markdown(path: str, slots: List[MealSlot], cards: Dict[str, RecipeCard],
                   day_summary: List[Dict], grocery_rows: List[Dict]) -> None:
    ensure_dir(os.path.dirname(path))
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Weekly Plan (Lunch • Dinner)\n\n")
        for d in range(7):
            lunch = next((s for s in slots if s.day == d and s.slot == "Lunch"), None)
            dinner = next((s for s in slots if s.day == d and s.slot == "Dinner"), None)
            if lunch:
                l_name = " + ".join(cards[cid].name for cid in lunch.card_ids)
            else:
                l_name = "—"
            if dinner:
                d_name = " + ".join(cards[cid].name for cid in dinner.card_ids)
            else:
                d_name = "—"
            f.write(f"**{day_names[d]}** — {l_name} • {d_name}\n\n")

        f.write("\n## Daily Summary\n\n")
        for d in day_summary:
            f.write(
                f"- {day_names[d['day']]}: ~{d['calories']} kcal "
                f"(P {d['protein_g']} g / C {d['carbs_g']} g / F {d['fat_g']} g). {d['notes']}\n"
            )

        f.write("\n## Grocery List (by section)\n\n")
        current_section = None
        for row in grocery_rows:
            sec = row["grocery_section"] or "other"
            if sec != current_section:
                f.write(f"### {sec.capitalize()}\n")
                current_section = sec
            f.write(f"- {row['item']} — {row['qty']} {row['unit']}\n")


def plot_summaries(day_summary: List[Dict], targets: Targets, out_dir: str) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("[warn] matplotlib not installed; skipping plots. Run `pip install matplotlib` to enable.")
        return

    ensure_dir(out_dir)
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    sorted_days = sorted(day_summary, key=lambda d: d["day"])
    cals = [d["calories"] for d in sorted_days]
    prots = [d["protein_g"] for d in sorted_days]
    carbs = [d["carbs_g"] for d in sorted_days]
    fats = [d["fat_g"] for d in sorted_days]

    # Calories
    plt.figure()
    plt.bar(labels, cals)
    plt.axhline(targets.calories_min, linestyle="--")
    plt.axhline(targets.calories_max, linestyle="--")
    plt.ylabel("Calories")
    plt.title("Daily Calories vs Target")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "calorie_summary.png"))
    plt.close()

    # Protein
    plt.figure()
    plt.bar(labels, prots)
    plt.axhline(targets.protein_min_g, linestyle="--")
    plt.ylabel("Protein (g)")
    plt.title("Daily Protein vs Target")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "protein_summary.png"))
    plt.close()

    # Macro composition
    x = range(len(labels))
    width = 0.25
    plt.figure()
    plt.bar([i - width for i in x], prots, width, label="Protein")
    plt.bar(x, carbs, width, label="Carbs")
    plt.bar([i + width for i in x], fats, width, label="Fat")
    plt.xticks(list(x), labels)
    plt.ylabel("Grams")
    plt.title("Daily Macros (P/C/F)")
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "macros_summary.png"))
    plt.close()


# ---------- Main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly meal plan generator from modular mains & sides")
    ap.add_argument("--cards_dir", default="./cards", help="Directory with *.yaml recipe cards")
    ap.add_argument("--selection", default=None, help="YAML file mapping card_id->count")
    ap.add_argument("--select", nargs="*", default=[], help="Inline selection: REC_ID:COUNT ...")
    ap.add_argument("--out_dir", default="./out", help="Output directory")
    ap.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed (omit for different plan each run)"
    )
    ap.add_argument(
        "--targets",
        default=None,
        help="YAML file with daily macro & planning targets (optional). If omitted, tries targets.yaml."
    )
    args = ap.parse_args()

    cards = load_cards(args.cards_dir)
    if not cards:
        print("No recipe cards loaded from", args.cards_dir)
        return 1

    # Seed
    if args.seed is None:
        seed = random.randrange(0, 10**9)
        print(f"[info] No seed provided. Using random seed: {seed}")
    else:
        seed = args.seed
        print(f"[info] Using fixed seed: {seed}")

    targets = load_targets(args.targets)
    selection = load_selection(args.selection, args.select)

    if selection:
        # Manual mode: ensure every slot has a main, sides as add-ons only
        slots = build_week_plan_manual(selection, cards, seed=seed)
    else:
        # Auto mode
        print(
            "No weekly selection provided; generating automatic weekly plan "
            f"with {targets.min_unique_main_meals}-{targets.max_unique_main_meals} unique recipes."
        )
        slots = build_auto_week_plan(cards, targets, seed=seed)

    day_summary = summarize_days(slots, targets)
    grocery_rows = aggregate_grocery(slots, cards)

    ensure_dir(args.out_dir)
    write_week_plan_csv(os.path.join(args.out_dir, "week_plan.csv"), slots, cards)
    write_day_summary_csv(os.path.join(args.out_dir, "day_summary.csv"), day_summary)
    write_csv(os.path.join(args.out_dir, "grocery_list.csv"), grocery_rows)
    write_markdown(os.path.join(args.out_dir, "weekly_plan.md"), slots, cards, day_summary, grocery_rows)
    plot_summaries(day_summary, targets, args.out_dir)

    print("Outputs written in", args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
