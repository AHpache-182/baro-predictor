"""
Poll api.warframestat.us for Baro Ki'Teer's current inventory and record any
new visit into baro.db. Meant to be run periodically (e.g. via cron) to keep
the database current between full backfills.

Idempotent: a visit is identified by its activation date, so re-running
after a visit has already been recorded is a no-op.

Usage:
    python poll_baro_visits.py
"""

import os
import sqlite3
from datetime import datetime, timezone

import requests

from scrape_baro_data_module import slugify

API_URL = "https://api.warframestat.us/pc/voidTrader"
# Resolved relative to this file, not the caller's cwd, since cron runs
# scripts with $HOME as the working directory by default.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baro.db")


def fetch_void_trader() -> dict:
    resp = requests.get(API_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_timestamp(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def record_visit(data: dict, db_path: str) -> None:
    inventory = data.get("inventory", [])
    if not inventory:
        activation = parse_timestamp(data["activation"])
        now = datetime.now(timezone.utc)
        if now < activation:
            print(f"No active visit. Baro next arrives {activation.date()}.")
        else:
            print("No active visit and no inventory data (between visits).")
        return

    visit_date = parse_timestamp(data["activation"]).date().isoformat()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    existing = cur.execute(
        "SELECT visit_id FROM visits WHERE date = ?", (visit_date,)
    ).fetchone()
    if existing:
        print(f"Visit on {visit_date} already recorded (visit_id={existing[0]}). Nothing to do.")
        conn.close()
        return

    next_visit_id = (cur.execute("SELECT MAX(visit_id) FROM visits").fetchone()[0] or 0) + 1
    cur.execute(
        "INSERT INTO visits (visit_id, date, relay_location) VALUES (?, ?, NULL)",
        (next_visit_id, visit_date),
    )

    new_items = 0
    recorded_items = 0
    for entry in inventory:
        name = entry["item"]
        item_id = slugify(name)

        row = cur.execute(
            "SELECT item_id, always_available FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            row = cur.execute(
                "SELECT item_id, always_available FROM items WHERE lower(name) = lower(?)",
                (name,),
            ).fetchone()

        if row is None:
            cur.execute(
                "INSERT INTO items (item_id, name, item_type, release_date, tradable, "
                "discontinued, always_available) VALUES (?, ?, NULL, NULL, 1, 0, 0)",
                (item_id, name),
            )
            print(f"  New item not seen in backfill, added to catalog: {name}")
            new_items += 1
            always_available = False
        else:
            item_id, always_available_flag = row
            always_available = bool(always_available_flag)

        if always_available:
            continue

        cur.execute(
            "INSERT OR IGNORE INTO visit_items (visit_id, item_id, ducats, credits) "
            "VALUES (?, ?, ?, ?)",
            (next_visit_id, item_id, entry.get("ducats"), entry.get("credits")),
        )
        recorded_items += 1

    conn.commit()
    conn.close()

    print(
        f"Recorded new visit on {visit_date} (visit_id={next_visit_id}) "
        f"with {recorded_items} items ({new_items} new to catalog)."
    )


def main():
    data = fetch_void_trader()
    record_visit(data, DB_PATH)


if __name__ == "__main__":
    main()
