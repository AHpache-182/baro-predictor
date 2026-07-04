"""
Purpose 2: per-item ETA / gap-based return estimate.

For an item with >= 3 recorded appearances, estimate when it's likely to
return based on the empirical distribution of gaps (in visits) between its
own past appearances - no pooling across items, no category fallback. For
items with < 3 appearances, there's no prediction: just the raw facts.

Discontinued items never get a future estimate, regardless of history size,
since they're flagged as never returning. Always-available items aren't
part of the rotation at all, so they're out of scope for this model.

Usage:
    python eta_model.py "Prisma Gorgon"
"""

import math
import os
import sqlite3
import sys
from datetime import date, timedelta

# Resolved relative to this file, not the caller's cwd, since cron runs
# scripts with $HOME as the working directory by default.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baro.db")
MIN_APPEARANCES_FOR_PREDICTION = 3
CADENCE_SAMPLE_SIZE = 20  # recent visit-to-visit gaps used to estimate current cadence


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolation percentile (numpy's default 'linear' method), 0 <= p <= 1."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _recent_cadence_days(cur: sqlite3.Cursor) -> float:
    """Median gap (in days) between the most recent visits, used to translate a
    visit-count estimate into an approximate calendar-date range."""
    rows = cur.execute(
        "SELECT date FROM visits ORDER BY visit_id DESC LIMIT ?",
        (CADENCE_SAMPLE_SIZE + 1,),
    ).fetchall()
    dates = [date.fromisoformat(r[0]) for r in rows]
    gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
    gaps.sort()
    return _percentile(gaps, 0.5)


def get_item_prediction(item_identifier: str, db_path: str = DB_PATH) -> dict:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    item = cur.execute(
        "SELECT item_id, name, discontinued, always_available FROM items "
        "WHERE item_id = ? OR lower(name) = lower(?)",
        (item_identifier, item_identifier),
    ).fetchone()
    if item is None:
        conn.close()
        return {"status": "not_found", "message": f"No item matching {item_identifier!r}."}

    item_id, name, discontinued, always_available = item
    discontinued, always_available = bool(discontinued), bool(always_available)

    if always_available:
        conn.close()
        return {
            "item_id": item_id,
            "name": name,
            "status": "always_available",
            "message": f"{name} is offered at every visit - it isn't part of the rotation.",
        }

    appearances = cur.execute(
        "SELECT vi.visit_id, v.date FROM visit_items vi JOIN visits v ON v.visit_id = vi.visit_id "
        "WHERE vi.item_id = ? ORDER BY vi.visit_id",
        (item_id,),
    ).fetchall()
    latest_visit_id, latest_date = cur.execute(
        "SELECT visit_id, date FROM visits ORDER BY visit_id DESC LIMIT 1"
    ).fetchone()

    base = {
        "item_id": item_id,
        "name": name,
        "discontinued": discontinued,
        "appearances_count": len(appearances),
        "appearances": [{"visit_id": vid, "date": d} for vid, d in appearances],
    }

    if discontinued:
        conn.close()
        base["status"] = "discontinued"
        base["message"] = (
            f"{name} is discontinued and will not be offered again. "
            f"It appeared {len(appearances)} time(s) historically."
        )
        return base

    if len(appearances) < MIN_APPEARANCES_FOR_PREDICTION:
        conn.close()
        base["status"] = "insufficient_data"
        if appearances:
            when = "; ".join(f"visit #{vid} ({d})" for vid, d in appearances)
            base["message"] = (
                f"{name} has appeared {len(appearances)} time(s): {when}. "
                "Not enough history to estimate when it will return."
            )
        else:
            base["message"] = f"{name} has never been offered. Not enough history to estimate when it will return."
        return base

    visit_ids = [vid for vid, _ in appearances]
    gaps = sorted(visit_ids[i + 1] - visit_ids[i] for i in range(len(visit_ids) - 1))
    min_gap, median_gap, max_gap = gaps[0], _percentile(gaps, 0.5), gaps[-1]

    last_appearance_visit_id, last_appearance_date = appearances[-1]
    visits_since_last = latest_visit_id - last_appearance_visit_id
    overdue = visits_since_last >= max_gap

    visits_remaining_low = max(0, min_gap - visits_since_last)
    visits_remaining_high = max(0, max_gap - visits_since_last)

    cadence_days = _recent_cadence_days(cur)
    conn.close()

    latest_date_obj = date.fromisoformat(latest_date)
    date_low = latest_date_obj + timedelta(days=round(visits_remaining_low * cadence_days))
    date_high = latest_date_obj + timedelta(days=round(visits_remaining_high * cadence_days))

    base.update(
        {
            "status": "predicted",
            "gaps_visits": gaps,
            "min_gap_visits": min_gap,
            "median_gap_visits": median_gap,
            "max_gap_visits": max_gap,
            "last_appearance_visit_id": last_appearance_visit_id,
            "last_appearance_date": last_appearance_date,
            "visits_since_last": visits_since_last,
            "overdue": overdue,
            "visits_remaining_range": (visits_remaining_low, visits_remaining_high),
            "estimated_date_range": (date_low.isoformat(), date_high.isoformat()),
        }
    )

    if overdue:
        base["message"] = (
            f"{name} last appeared {visits_since_last} visits ago (visit #{last_appearance_visit_id}, "
            f"{last_appearance_date}), which already meets or exceeds its longest historical gap "
            f"({max_gap} visits). It's overdue - could return any time now."
        )
    else:
        base["message"] = (
            f"{name} last appeared {visits_since_last} visits ago (visit #{last_appearance_visit_id}, "
            f"{last_appearance_date}). Historically it returns after {min_gap}-{max_gap} visits "
            f"(median {median_gap:.1f}), so expect it in roughly {visits_remaining_low}-{visits_remaining_high} "
            f"more visits - around {date_low.isoformat()} to {date_high.isoformat()}."
        )

    return base


def main():
    if len(sys.argv) < 2:
        print('Usage: python eta_model.py "<item name>"')
        sys.exit(1)
    result = get_item_prediction(" ".join(sys.argv[1:]))
    print(result["message"])


if __name__ == "__main__":
    main()
