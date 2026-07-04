"""
Local API + static frontend for the Baro Ki'Teer predictor.

Wraps the two standalone model components (ranking_model.py, eta_model.py)
in HTTP endpoints so the static frontend (served from the same process) can
call them via fetch(). Both model calls are fast (<50ms against the current
dataset size) so responses are computed fresh on every request - no caching.

Usage:
    python -m uvicorn app:app --reload
    open http://127.0.0.1:8000
"""

import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

import eta_model
import ranking_model

app = FastAPI(title="Baro Ki'Teer Predictor")


@app.get("/api/predict/next-visit")
def predict_next_visit():
    results = ranking_model.predict_next_visit()
    return {"count": len(results), "items": results}


@app.get("/api/predict/item/{name}")
def predict_item(name: str):
    result = eta_model.get_item_prediction(name)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@app.get("/api/items/search")
def search_items(q: str = ""):
    q = q.strip()
    if not q:
        return {"items": []}
    conn = sqlite3.connect(eta_model.DB_PATH)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT name FROM items WHERE lower(name) LIKE '%' || lower(?) || '%' "
        "ORDER BY (lower(name) LIKE lower(?) || '%') DESC, name ASC LIMIT 10",
        (q, q),
    ).fetchall()
    conn.close()
    return {"items": [r[0] for r in rows]}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
