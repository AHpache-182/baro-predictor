"""
Scrape Baro Ki'Teer's historical trade data from wiki.warframe.com's data
module (Module:Baro/data) - a structured Lua table that is itself the source
the wiki's human-readable trade tables are generated from. This is more
reliable than parsing the rendered HTML table.

Usage:
    pip install requests beautifulsoup4 --break-system-packages
    python scrape_baro_data_module.py

Requires network access - run this on your own machine.
"""

import os
import re
import sqlite3
import subprocess

from bs4 import BeautifulSoup

from lua_table_parser import parse_lua_return_table

PAGE_URL = "https://wiki.warframe.com/w/Module:Baro/data"
# Resolved relative to this file, not the caller's cwd, since cron runs
# scripts with $HOME as the working directory by default.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baro.db")

# wiki.warframe.com sits behind Cloudflare/WeirdGloop bot management that
# serves an interstitial "Redirecting..." page to Python's requests library
# based on TLS fingerprint, even with a browser User-Agent set. curl isn't
# flagged, so we shell out to it instead of using requests for this fetch.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def fetch_lua_source(url: str) -> str:
    """
    Fetch the normal wiki page (not action=raw, which is disallowed) and
    pull the Lua source out of the rendered <pre> code block.
    """
    result = subprocess.run(
        ["curl", "-sS", "-A", USER_AGENT, "--fail", url],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr.strip()}")
    soup = BeautifulSoup(result.stdout, "html.parser")

    # Module pages render the /doc subpage's example table in a plain <pre>
    # above the actual source, which is the one carrying class "mw-script".
    pre = soup.find("pre", class_="mw-script")
    if pre is None:
        raise RuntimeError(
            "Could not find the module source <pre class=\"mw-script\"> block - "
            "wiki layout may have changed."
        )
    return pre.get_text()


def build_database(data: dict, db_path: str) -> None:
    items_raw = data.get("Items", {})
    always_available = set(data.get("AlwaysAvailable", {}).keys())

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript(
        """
        DROP TABLE IF EXISTS items;
        DROP TABLE IF EXISTS visits;
        DROP TABLE IF EXISTS visit_items;

        CREATE TABLE items (
            item_id           TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            item_type         TEXT,
            release_date      TEXT,           -- filled in later from item catalog, not this source
            tradable          INTEGER DEFAULT 1,
            discontinued      INTEGER DEFAULT 0,  -- from IsDiscont: will never be offered again
            always_available  INTEGER DEFAULT 0   -- from AlwaysAvailable: not part of the rotation
        );

        CREATE TABLE visits (
            visit_id       INTEGER PRIMARY KEY,
            date           TEXT NOT NULL UNIQUE,
            relay_location TEXT
        );

        CREATE TABLE visit_items (
            visit_id  INTEGER NOT NULL,
            item_id   TEXT NOT NULL,
            ducats    INTEGER,
            credits   INTEGER,
            PRIMARY KEY (visit_id, item_id),
            FOREIGN KEY (visit_id) REFERENCES visits(visit_id),
            FOREIGN KEY (item_id) REFERENCES items(item_id)
        );
        """
    )

    # --- build a per-item merged, deduped, sorted date list ---
    # Canonical PC timeline = PcOfferingDates (pre cross-platform sync) +
    # OfferingDates (post 2022-07-29 sync, all platforms). ConsoleOfferingDates
    # is explicitly deprecated per the module's own docs, so it's ignored.
    per_item_dates = {}
    seen_item_ids = set()
    item_id_map = {}

    for name, rec in items_raw.items():
        item_id = slugify(name)
        base_id = item_id
        suffix = 2
        while item_id in seen_item_ids:
            item_id = f"{base_id}_{suffix}"
            suffix += 1
        seen_item_ids.add(item_id)
        item_id_map[name] = item_id

        dates = set(rec.get("PcOfferingDates", [])) | set(rec.get("OfferingDates", []))
        per_item_dates[name] = sorted(dates)

        cur.execute(
            "INSERT INTO items "
            "(item_id, name, item_type, release_date, tradable, discontinued, always_available) "
            "VALUES (?, ?, ?, NULL, 1, ?, ?)",
            (
                item_id,
                rec.get("Name", name),
                rec.get("Type"),
                1 if rec.get("IsDiscont") else 0,
                1 if name in always_available else 0,
            ),
        )

    # --- visits table: union of every date across every item ---
    all_dates = sorted({d for dates in per_item_dates.values() for d in dates})
    date_to_visit_id = {}
    for i, date in enumerate(all_dates, start=1):
        cur.execute(
            "INSERT INTO visits (visit_id, date, relay_location) VALUES (?, ?, NULL)",
            (i, date),
        )
        date_to_visit_id[date] = i

    # --- visit_items table ---
    n_pairs = 0
    for name, rec in items_raw.items():
        item_id = item_id_map[name]
        credits = rec.get("CreditCost")
        ducats = rec.get("DucatCost")
        for date in per_item_dates[name]:
            visit_id = date_to_visit_id[date]
            cur.execute(
                "INSERT OR IGNORE INTO visit_items (visit_id, item_id, ducats, credits) "
                "VALUES (?, ?, ?, ?)",
                (visit_id, item_id, ducats, credits),
            )
            n_pairs += 1

    conn.commit()

    n_items = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    n_visits = cur.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    n_discontinued = cur.execute("SELECT COUNT(*) FROM items WHERE discontinued = 1").fetchone()[0]
    n_always = cur.execute("SELECT COUNT(*) FROM items WHERE always_available = 1").fetchone()[0]
    n_never_offered = cur.execute(
        "SELECT COUNT(*) FROM items WHERE item_id NOT IN (SELECT DISTINCT item_id FROM visit_items)"
    ).fetchone()[0]

    print(f"Loaded {n_items} items, {n_visits} visits, {n_pairs} item-visit pairs.")
    if all_dates:
        print(f"Earliest visit: {all_dates[0]}  |  Latest visit: {all_dates[-1]}")
    print(f"Discontinued items: {n_discontinued}")
    print(f"Always-available items (excluded from rotation logic): {n_always}")
    print(f"Items with zero recorded offerings so far: {n_never_offered}")

    conn.close()


def main():
    print(f"Fetching {PAGE_URL} ...")
    lua_source = fetch_lua_source(PAGE_URL)

    print("Parsing Lua data module ...")
    data = parse_lua_return_table(lua_source)

    print(f"Building database at {DB_PATH} ...")
    build_database(data, DB_PATH)

    print("Done.")


if __name__ == "__main__":
    main()
