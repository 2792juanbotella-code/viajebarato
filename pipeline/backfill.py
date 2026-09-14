"""Backfill del histórico de precios por ruta (para z-scores).

Usa `aviasales/v3/prices_for_dates` con month= para cada uno de los últimos
6 meses y guarda UN precio por (ruta, mes) en price_history. Con 6-7 puntos
por ruta los z-scores ya computan (n>=5) y en 2-3 semanas el running baseline
se vuelve más fiable que el propio backfill (el propio TP cachea ~meses).

Usage:
  python backfill.py --origins VLC,MAD,BCN
  python backfill.py --pairs-from ../.openclaw/workspace-viajes/deal-hunter/sweep_routes.json
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from pipeline import _db, TOKEN, BLOCKLIST_DEST  # reutiliza env + conexión + blocklist
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).parent


def _get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url[:80]}: {last}")


def month_prices(origin: str, destination: str, month: str) -> list[dict]:
    q = urlencode({
        "origin": origin, "destination": destination,
        "month": month, "currency": "eur",
        "sorting": "price", "limit": 1, "one_way": "true", "token": TOKEN,
    })
    d = _get(f"https://api.travelpayouts.com/aviasales/v3/prices_for_dates?{q}")
    return (d.get("data") or [])


def backfill_route(con, origin, dest, months):
    added = 0
    for m in months:
        rows = month_prices(origin, dest, m)
        if not rows:
            continue
        price = rows[0].get("price")
        if not price:
            continue
        # seen_at sintético: día 15 del mes de referencia (precio cacheado TP)
        seen_at = f"{m}-15T12:00:00+00:00"
        cur = con.execute(
            "SELECT COUNT(*) FROM price_history WHERE origin=? AND destination=? AND seen_at=?",
            (origin, dest, seen_at)).fetchone()[0]
        if cur == 0:
            con.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?,?)",
                        (origin, dest, price, seen_at))
            added += 1
        time.sleep(0.35)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origins", default=None, help="CSV de orígenes")
    ap.add_argument("--pairs-from", default=None, help="JSON con .origins y .pairs")
    ap.add_argument("--months", type=int, default=6)
    args = ap.parse_args()

    origins, pairs = [], []
    if args.pairs_from:
        cfg = json.loads(Path(args.pairs_from).read_text(encoding="utf-8"))
        origins = [o.strip().upper() for o in cfg.get("origins", [])]
        pairs = [(p["from"], p["to"]) for p in cfg.get("pairs", [])]
    elif args.origins:
        origins = [o.strip().upper() for o in args.origins.split(",")]

    now = datetime.now(timezone.utc)
    months = [(now.year - (1 if now.month <= i else 0), (now.month - i - 1) % 12 + 1)
              for i in range(args.months + 1)]  # incluye mes actual
    months = [f"{y}-{m:02d}" for (y, m) in months]

    con = _db()
    total = 0
    # 1) pares curados primero
    for o, d in pairs:
        if d in BLOCKLIST_DEST:
            continue
        total += backfill_route(con, o, d, months)
        con.commit()
    # 2) orígenes: trending -> top destinos
    for o in origins:
        try:
            q = urlencode({"origin": o, "currency": "eur", "token": TOKEN})
            d = _get(f"https://api.travelpayouts.com/v1/city-directions?{q}")
            dests = list((d.get("data") or {}).keys())[:12]
        except Exception as e:  # noqa: BLE001
            print(f"[{o}] trending falló: {e}")
            continue
        for dest in dests:
            if dest in BLOCKLIST_DEST:
                continue
            total += backfill_route(con, o, dest, months)
        con.commit()
        print(f"[{o}] +{total} filas acumuladas")
    con.close()
    print(f"BACKFILL OK: {total} filas nuevas, meses={months}")


if __name__ == "__main__":
    main()
