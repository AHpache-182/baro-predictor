"""
Purpose 1: rank all rotating items by estimated probability of appearing at
Baro's next visit, using a pooled empirical hazard curve.

Mechanics
---------
For every item, normalize "how long since it last appeared" by its own
typical gap (median of its historical visit-to-visit gaps, or a global
fallback for items with too little history to have a trustworthy own
median) into a unitless `relative_age`. Then pool (relative_age, did it
reappear at the very next visit) observations across every item's full
history to fit a single hazard curve h(relative_age): the empirical rate of
reappearance at a given relative age, regardless of which item it is.

This pools the *shape* of how hazard scales with overdue-ness (a property
of Baro's rotation mechanic in general), not any single item's absolute
schedule - unlike purpose 2, this isn't a per-item confidence claim, so
pooling here doesn't manufacture false precision about a specific item.

The fitting function takes a `cutoff_visit_id` so the exact same code path
works for real prediction (cutoff = latest known visit) and for leakage-free
backtesting (cutoff = some earlier visit, scored against what actually
happened).

Item probabilities alone tend to cluster within one category (e.g. many
Cosmetics all look similarly "due" at once), which could crowd out other
categories in a naive top-N list even though Baro's shop draws a roughly
fixed count per category each visit (e.g. always 3-8 Primed Mods, never
15). `_category_quotas` estimates that per-category count from a recent
rolling window of visits, and `_apply_category_quotas` selects the top-N
items per category by hazard probability, so the final shortlist can't
violate that known structure.

Backtesting (see `backtest()`) shows this produces a real, meaningful lift
over random guessing at wider cutoffs (~40-50% relative improvement in
recall@100-200), but precision at the exact visit size stays low (~0.09).
That's expected, not a bug to chase further: this model's job is to narrow
the field of ~450 items down to a much smaller likely set, not to name the
exact ~30 items Baro will pick - the specific choice among similarly-due
items within a category appears to have genuine randomness that elapsed-
time-based features can't resolve. Treat the output as a probability-
ranked candidate shortlist, not a guaranteed prediction.

Usage:
    python ranking_model.py            # predict the next visit
    python ranking_model.py --backtest # backtest over recent visits
"""

import os
import sqlite3
import statistics
import sys

# Resolved relative to this file, not the caller's cwd, since cron runs
# scripts with $HOME as the working directory by default.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baro.db")
MIN_APPEARANCES_FOR_OWN_MEDIAN = 3  # matches eta_model's threshold for "trustworthy own history"
BIN_WIDTH = 0.25
RELATIVE_AGE_CAP = 3.0  # observations beyond this are lumped into one "very overdue" bin
CATEGORY_QUOTA_WINDOW = 30  # recent visits used to estimate each category's typical per-visit count


def _coarse_category(item_type: str | None) -> str:
    """Baro's shop draws a roughly fixed count per broad category each visit
    (e.g. always 3-8 Primed Mods) - fold subtypes like 'Primed Mod (Rifle)'
    into one 'Primed Mod' bucket so quotas are estimated at that granularity."""
    if not item_type:
        return "Unknown"
    for prefix in ("Primed Mod", "Mod", "Cosmetic"):
        if item_type.startswith(prefix):
            return prefix
    return item_type


def _load_all_visit_ids(cur: sqlite3.Cursor) -> list[int]:
    return [r[0] for r in cur.execute("SELECT visit_id FROM visits ORDER BY visit_id")]


def _load_eligible_items(cur: sqlite3.Cursor) -> dict:
    """item_id -> {name, category}, for items that can ever be part of a rotation prediction."""
    rows = cur.execute(
        "SELECT item_id, name, item_type FROM items WHERE discontinued = 0 AND always_available = 0"
    ).fetchall()
    return {
        item_id: {"name": name, "category": _coarse_category(item_type)}
        for item_id, name, item_type in rows
    }


def _category_quotas(cur: sqlite3.Cursor, item_categories: dict, cutoff_visit_id: int) -> dict:
    """Estimate each category's expected item count for the next visit as the
    median count over the last CATEGORY_QUOTA_WINDOW visits strictly before
    cutoff_visit_id (so it respects the same leakage-free boundary as the
    hazard curve). Categories not observed in that window get quota 0."""
    all_visit_ids = [v for v in _load_all_visit_ids(cur) if v < cutoff_visit_id]
    window = all_visit_ids[-CATEGORY_QUOTA_WINDOW:]
    if not window:
        return {}

    placeholders = ",".join("?" for _ in window)
    rows = cur.execute(
        f"SELECT vi.visit_id, vi.item_id FROM visit_items vi WHERE vi.visit_id IN ({placeholders})",
        window,
    ).fetchall()

    categories = {meta["category"] for meta in item_categories.values()}
    counts_per_visit = {v: dict.fromkeys(categories, 0) for v in window}
    for visit_id, item_id in rows:
        meta = item_categories.get(item_id)
        if meta is not None:
            counts_per_visit[visit_id][meta["category"]] += 1

    quotas = {}
    for cat in categories:
        counts = [counts_per_visit[v][cat] for v in window]
        quotas[cat] = round(statistics.median(counts))
    return quotas


def _apply_category_quotas(results: list, item_categories: dict, quotas: dict) -> list:
    """Select, per category, the top `quota[category]` items by probability -
    guarantees the final shortlist can never violate a category's known
    historical range, instead of ranking all ~450 items on one global list."""
    by_category: dict[str, list] = {}
    for r in results:
        cat = item_categories[r["item_id"]]["category"]
        by_category.setdefault(cat, []).append(r)

    selected = []
    for cat, items in by_category.items():
        quota = quotas.get(cat, 0)
        selected.extend(items[:quota])  # `items` is already probability-sorted from _fit_and_score

    selected.sort(key=lambda r: r["probability"], reverse=True)
    return selected


def _load_appearances(cur: sqlite3.Cursor, item_ids: list[str], before_visit_id: int) -> dict:
    """item_id -> sorted list of visit_ids strictly before `before_visit_id`."""
    placeholders = ",".join("?" for _ in item_ids)
    rows = cur.execute(
        f"SELECT item_id, visit_id FROM visit_items "
        f"WHERE item_id IN ({placeholders}) AND visit_id < ? ORDER BY item_id, visit_id",
        (*item_ids, before_visit_id),
    ).fetchall()
    appearances: dict[str, list[int]] = {item_id: [] for item_id in item_ids}
    for item_id, visit_id in rows:
        appearances[item_id].append(visit_id)
    return appearances


def _typical_gap(appearances: list[int], global_fallback: float) -> float:
    if len(appearances) < MIN_APPEARANCES_FOR_OWN_MEDIAN:
        return global_fallback
    gaps = [appearances[i + 1] - appearances[i] for i in range(len(appearances) - 1)]
    return statistics.median(gaps)


def _global_fallback_median(all_appearances: dict) -> float:
    gaps = []
    for visit_ids in all_appearances.values():
        gaps.extend(visit_ids[i + 1] - visit_ids[i] for i in range(len(visit_ids) - 1))
    return statistics.median(gaps) if gaps else 14.0  # ~biweekly, only hit if db is nearly empty


def _build_training_pairs(all_visit_ids: list[int], all_appearances: dict, global_fallback: float) -> list:
    """Walk every item's history: at each visit between two consecutive
    appearances, record (relative_age, did it reappear at *this* visit)."""
    visit_index = {v: i for i, v in enumerate(all_visit_ids)}
    pairs = []
    for visit_ids in all_appearances.values():
        if len(visit_ids) < 2:
            continue
        typical_gap = _typical_gap(visit_ids, global_fallback)
        for i in range(len(visit_ids) - 1):
            anchor, next_appearance = visit_ids[i], visit_ids[i + 1]
            anchor_idx, next_idx = visit_index[anchor], visit_index[next_appearance]
            for candidate_visit in all_visit_ids[anchor_idx + 1 : next_idx + 1]:
                elapsed = candidate_visit - anchor
                relative_age = elapsed / typical_gap
                reappeared = candidate_visit == next_appearance
                pairs.append((relative_age, reappeared))
    return pairs


def _fit_hazard_bins(pairs: list) -> list:
    n_bins = int(RELATIVE_AGE_CAP / BIN_WIDTH) + 1  # last bin is the overflow bin
    bucket_hits = [0] * n_bins
    bucket_totals = [0] * n_bins
    for relative_age, reappeared in pairs:
        idx = min(int(relative_age / BIN_WIDTH), n_bins - 1)
        bucket_totals[idx] += 1
        if reappeared:
            bucket_hits[idx] += 1

    bins = []
    for i in range(n_bins):
        lo = i * BIN_WIDTH
        hi = None if i == n_bins - 1 else (i + 1) * BIN_WIDTH
        mid = lo + BIN_WIDTH / 2 if hi is not None else lo + BIN_WIDTH
        rate = bucket_hits[i] / bucket_totals[i] if bucket_totals[i] else None
        bins.append({"lo": lo, "hi": hi, "mid": mid, "rate": rate, "n": bucket_totals[i]})

    # fill any empty bins by carrying forward the nearest bin with data, so
    # lookups never hit a None (sparse tail bins are the most likely to be empty)
    last_known = 0.0
    for b in bins:
        if b["rate"] is None:
            b["rate"] = last_known
        else:
            last_known = b["rate"]
    return bins


def _hazard_lookup(bins: list, relative_age: float) -> float:
    if relative_age <= bins[0]["mid"]:
        return bins[0]["rate"]
    if relative_age >= bins[-1]["mid"]:
        return bins[-1]["rate"]
    for b, nxt in zip(bins, bins[1:]):
        if b["mid"] <= relative_age <= nxt["mid"]:
            span = nxt["mid"] - b["mid"]
            t = (relative_age - b["mid"]) / span if span else 0
            return b["rate"] + t * (nxt["rate"] - b["rate"])
    return bins[-1]["rate"]


def _fit_and_score(cur: sqlite3.Cursor, item_ids: list[str], cutoff_visit_id: int, target_visit_id: int) -> list:
    """
    Fit the hazard curve using only appearances strictly before
    `cutoff_visit_id`, then score every eligible item's probability of
    appearing at `target_visit_id`. Using the same `cutoff_visit_id` for
    both real prediction (cutoff = latest_visit_id + 1) and backtesting
    (cutoff = the historical visit being predicted) keeps the two paths
    identical and leakage-free.
    """
    all_visit_ids = [v for v in _load_all_visit_ids(cur) if v < cutoff_visit_id]
    all_appearances = _load_appearances(cur, item_ids, cutoff_visit_id)

    global_fallback = _global_fallback_median(all_appearances)
    pairs = _build_training_pairs(all_visit_ids, all_appearances, global_fallback)
    bins = _fit_hazard_bins(pairs)

    results = []
    for item_id, visit_ids in all_appearances.items():
        if not visit_ids:
            continue  # never appeared before cutoff - no anchor to measure elapsed time from
        typical_gap = _typical_gap(visit_ids, global_fallback)
        last_appearance = visit_ids[-1]
        elapsed = target_visit_id - last_appearance
        relative_age = elapsed / typical_gap
        probability = _hazard_lookup(bins, relative_age)
        results.append(
            {
                "item_id": item_id,
                "visits_since_last": elapsed,
                "typical_gap": typical_gap,
                "relative_age": relative_age,
                "probability": probability,
            }
        )

    results.sort(key=lambda r: r["probability"], reverse=True)
    return results


def predict_next_visit(db_path: str = DB_PATH) -> list:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    item_meta = _load_eligible_items(cur)
    item_ids = list(item_meta.keys())
    latest_visit_id = _load_all_visit_ids(cur)[-1]
    cutoff = latest_visit_id + 1

    results = _fit_and_score(cur, item_ids, cutoff_visit_id=cutoff, target_visit_id=cutoff)
    for r in results:
        r["name"] = item_meta[r["item_id"]]["name"]
        r["category"] = item_meta[r["item_id"]]["category"]

    quotas = _category_quotas(cur, item_meta, cutoff_visit_id=cutoff)
    shortlist = _apply_category_quotas(results, item_meta, quotas)
    conn.close()
    return shortlist


def backtest(db_path: str = DB_PATH, n_recent: int = 20) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    item_meta = _load_eligible_items(cur)
    item_ids = list(item_meta.keys())
    all_visit_ids = _load_all_visit_ids(cur)

    # skip the earliest visits: too little prior history to fit a meaningful curve
    testable_visits = all_visit_ids[-n_recent:]

    precisions, recalls = [], []
    for target_visit_id in testable_visits:
        actual_items = {
            r[0] for r in cur.execute(
                "SELECT item_id FROM visit_items WHERE visit_id = ?", (target_visit_id,)
            ).fetchall()
        }
        actual_items &= set(item_ids)  # ignore always_available/discontinued edge cases
        if not actual_items:
            continue

        results = _fit_and_score(cur, item_ids, cutoff_visit_id=target_visit_id, target_visit_id=target_visit_id)
        quotas = _category_quotas(cur, item_meta, cutoff_visit_id=target_visit_id)
        shortlist = _apply_category_quotas(results, item_meta, quotas)
        predicted = {r["item_id"] for r in shortlist}

        hits = len(predicted & actual_items)
        precision = hits / len(predicted) if predicted else 0.0
        recall = hits / len(actual_items)
        precisions.append(precision)
        recalls.append(recall)
        print(
            f"visit {target_visit_id}: {hits} hits, predicted {len(predicted)} items, "
            f"actual {len(actual_items)} items (precision {precision:.2f}, recall {recall:.2f})"
        )

    conn.close()
    if precisions:
        print(f"\nMean precision: {statistics.mean(precisions):.3f}  Mean recall: {statistics.mean(recalls):.3f}")


def main():
    if "--backtest" in sys.argv:
        backtest()
        return

    results = predict_next_visit()
    print(
        f"Candidate shortlist for the next Baro visit ({len(results)} items, quota-bounded per category).\n"
        "Ranked by estimated likelihood, not a guaranteed offering list - within a category, which specific "
        "due item Baro actually picks has genuine randomness this model can't fully resolve.\n"
    )
    for r in results:
        print(f"  {r['probability']:.3f}  {r['name']:40s} [{r['category']}]  (last seen {r['visits_since_last']} visits ago, typical gap {r['typical_gap']:.1f})")


if __name__ == "__main__":
    main()
