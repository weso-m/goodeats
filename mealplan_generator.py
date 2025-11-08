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
            role=str(d.get("role", "main")),  # default to main for legacy cards
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
        )


@dc.dataclass
class Targets:
    calories_min: float = 1400
    calories_max: float = 1600
    protein_min_g: float = 110
    carbs_max_g: Optional[float] = None
    fat_max_g: Optional[float] = None
    # desired range of unique recipes per week (mains + sides)
    min_unique_main_meals: Optional[int] = None
    max_unique_main_meals: Optional[int] = None


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
        f"unique recipes {t.min_unique_main_meals}-{t.max_unique_main_meals}"
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
def build_auto_week_plan(cards: Dict[str, RecipeCard], targets: Targets, seed: int) -> List[MealSlot]:
    """
    Auto mode:

    - Interprets min_unique_main_meals / max_unique_main_meals as the desired
      range of UNIQUE MAINS (not mains+sides).
    - Chooses that many mains from eligible mains.
    - Chooses a small pool of sides from eligible sides.
    - For each of 14 meals:
        * picks a main from chosen mains
        * adds 0–2 sides (if helpful) to land in ~450–800 kcal band.
    - Reuses this small set across the week for batching.
    """
    rnd = random.Random(seed)

    # Eligible mains
    mains = [
        c for c in cards.values()
        if c.role in ("main", "both")
        and c.batch_friendly
        and 300 <= c.macros_per_serving.calories <= 800
        and any(mt in ("lunch", "dinner") for mt in c.meal_types)
    ]

    # Eligible sides
    sides = [
        c for c in cards.values()
        if c.role in ("side", "both")
        and c.batch_friendly
        and 50 <= c.macros_per_serving.calories <= 300
        and "side" in c.meal_types
    ]

    if not mains:
        raise ValueError(
            "Auto mode: no eligible mains found. "
            "Check role=='main'/'both', batch_friendly=True, "
            "300–800 kcal, and meal_types includes 'lunch' or 'dinner'."
        )

    # --- Determine how many unique mains to use ---
    # Treat targets.min/max_unique_main_meals as constraints on mains only.
    min_m = targets.min_unique_main_meals or 2
    max_m = targets.max_unique_main_meals or min_m

    min_m = max(1, int(min_m))
    max_m = max(min_m, int(max_m))
    max_m = min(max_m, len(mains))        # cannot exceed available mains
    min_m = min(min_m, max_m)

    if max_m < 1:
        # Failsafe: at least 1 main
        max_m = min(1, len(mains))
        min_m = max_m

    n_mains = rnd.randint(min_m, max_m) if max_m > 0 else 1
    n_mains = max(1, min(n_mains, len(mains)))

    chosen_mains = rnd.sample(mains, n_mains)

    # --- Choose supporting sides ---
    chosen_sides: List[RecipeCard] = []
    if sides:
        # Keep variety but not chaos: cap sides to a small pool
        # e.g. up to 2 * n_mains, but at least 1 if any exist.
        max_sides = min(len(sides), max(1, 2 * n_mains))
        n_sides = rnd.randint(1, max_sides)
        chosen_sides = rnd.sample(sides, n_sides)

    print(
        f"[info] Auto mode using {len(chosen_mains)} mains and "
        f"{len(chosen_sides)} sides for batching."
    )
    if len(chosen_mains) == 1:
        print(
            "[warn] Only one eligible main selected; all meals will reuse this main. "
            "Add more eligible mains or adjust filters for more variety."
        )

    # --- Build 14 meal slots ---
    total_slots = 14  # 7 days * 2 meals
    slots: List[MealSlot] = []
    day = 0

    for i in range(total_slots):
        slot_name = "Lunch" if i % 2 == 0 else "Dinner"

        # Always pick a main from the chosen mains
        main = rnd.choice(chosen_mains)
        comp_ids = [main.id]
        cal = main.macros_per_serving.calories
        p = main.macros_per_serving.protein_g
        c = main.macros_per_serving.carbs_g
        f = main.macros_per_serving.fat_g

        # Add up to 2 sides to land roughly in 450–800 kcal
        if chosen_sides:
            side_candidates = rnd.sample(chosen_sides, len(chosen_sides))
            for side in side_candidates:
                if len(comp_ids) >= 3:  # main + up to 2 sides
                    break

                new_cal = cal + side.macros_per_serving.calories

                # If we're below 450, adding is good as long as we don't blow past 800.
                # If we're already between 450–800, only add if we stay ≤800.
                if cal < 450:
                    if new_cal <= 800:
                        comp_ids.append(side.id)
                        cal = new_cal
                        p += side.macros_per_serving.protein_g
                        c += side.macros_per_serving.carbs_g
                        f += side.macros_per_serving.fat_g
                else:
                    if new_cal <= 800:
                        comp_ids.append(side.id)
                        cal = new_cal
                        p += side.macros_per_serving.protein_g
                        c += side.macros_per_serving.carbs_g
                        f += side.macros_per_serving.fat_g

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

        if slot_name == "Dinner":
            day += 1

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
        # Manual mode
        pool = expand_pool(selection, cards)
        enforce_variety(pool, cards)
        slots = build_week_plan(pool, cards, seed=seed)
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
