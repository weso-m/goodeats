# 🥦 Meal Plan Generator — Modular Mains & Sides

Welcome to the modular meal planning project!  
This system builds out realistic weekly meal plans using **separate “main” and “side” recipe cards**, generating balanced days around your calorie and protein targets — and a grocery list that makes sense in the real world.

---

## 🍽️ What It Does

Each recipe lives in a YAML card inside `./cards`.  
The planner combines them automatically to hit your weekly nutrition goals.

It can run in:
- **Auto Mode** — picks meals based on your targets (no manual selection needed)  
- **Manual Mode** — you choose the recipes and counts yourself

You’ll get:
- `week_plan.csv` – every lunch and dinner slot  
- `day_summary.csv` – macros and notes per day  
- `grocery_list.csv` – one shopping list for the week  
- `weekly_plan.md` – markdown summary of the week  
- optional PNG charts for calories/macros (if you have matplotlib installed)

---

## 📁 Project Structure

```
cards/          # Recipe YAMLs (mains + sides)
out/            # Generated outputs (csv, markdown, png)
targets.yaml    # Daily macro & meal variety targets
weekly_plan.py  # Main script
```

---

## 🆕 Recent Updates (Nov 2025)

- **Fixed auto-mode** so meals now rotate between multiple mains correctly (no more “Souvlaki every day”).  
- **Expanded recipe library:** added **13 new grocery-realistic modular cards** with practical store units (1 lb meat packs, 1 onion, etc.).  
- **Adjusted meal_freq_cap_per_week** to reflect roughly one full recipe’s worth per week.  
- **Improved calorie + protein realism** for all new recipes.  
- **Separated mains and sides** properly in the YAML structure.  
- **Enhanced grocery aggregation** to unify ounces and grams.  

### 🥘 New Recipes Added

**Mains**
- Beefy Fajita Taco Rice  
- Chicken & Vegetable Noodle Soup  
- Singapore Noodles  
- Beef & Bean Chili  
- Egg & Cheese on English Muffin  
- Sambal Marinated Chicken  
- Mujadara (Lentils and Rice)  
- Chicken, Broccoli & Ziti  

**Sides**
- Baked Potato w/ Sour Cream & Chives  
- Steamed Spinach  
- Ginger-Soy Carrots  
- Steamed Rice  
- Split Lentil Daal  

---

## ⚙️ How to Run

You can generate a plan from scratch:

```bash
python weekly_plan.py --cards_dir ./cards --out_dir ./out
```

Optional flags:
```bash
--seed 42                  # use fixed RNG seed for reproducible plans
--targets ./targets.yaml   # specify calorie & protein goals
--selection ./my_selection.yaml   # manual recipe selection
```

Outputs land in the `out/` folder.

---

## 📊 Outputs

| File | Description |
|------|--------------|
| `week_plan.csv` | Daily plan with mains + sides |
| `day_summary.csv` | Calories, protein, carbs, fats, and notes |
| `grocery_list.csv` | Combined grocery list (sorted by section) |
| `weekly_plan.md` | Markdown view of the whole week |
| `*.png` | Optional charts (if matplotlib is installed) |

---

## 💡 Tips

- Each card defines portion sizes and macros per serving.
- To add new recipes, just drop new YAMLs into `cards/` — the script picks them up automatically.  
- Batch-cook friendly recipes are prioritized.  
- Use `targets.yaml` to adjust calorie or protein ranges.

---

## 🌱 Next Steps

- Add more vegetarian and seafood mains  
- Add international side variety (e.g., roasted cauliflower, green beans almondine)  
- Optional breakfast and snack support  
- Smarter grocery merging for mixed units

---

Happy cooking, planning, and automating 🍳  
— *Your Modular Meal Plan Project*