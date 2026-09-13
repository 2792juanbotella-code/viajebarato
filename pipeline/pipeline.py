"""ViajeBarato pipeline — detección de chollos de vuelos.

Layers:
  1. Trending:   TP v1/city-directions (destinos de moda por origen)
  2. Matriz:     TP aviasales/v3/prices_for_dates (precios por mes / cheapest)
  3. Anomalías:  z-score del precio actual vs histórico de la ruta
                 (running mean/std por ruta, guardado en SQLite)
  4. (manual)    Verificación con VIAJA / Google Flights antes de enviar

Usage:
  python pipeline.py --origin VLC          # run completo, imprime chollos
  python pipeline.py --origin VLC --json   # salida JSON
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).parent
DB = HERE / "data" / "prices.db"

# --- env ---
def _load_env():
    env = {}
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

ENV = _load_env()
TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", ENV.get("TRAVELPAYOUTS_TOKEN", ""))
MARKER = os.environ.get("TP_MARKER", ENV.get("MARKER", ""))


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


# ---------- Layer 1: trending ----------
def trending(origin: str) -> list[dict]:
    """Destinos de moda desde origin con precio cheapest actual."""
    q = urlencode({"origin": origin, "currency": "eur", "token": TOKEN})
    d = _get(f"https://api.travelpayouts.com/v1/city-directions?{q}")
    out = []
    for code, r in (d.get("data") or {}).items():
        out.append({
            "origin": origin, "destination": code,
            "price": r.get("price"),
            "airline": r.get("airline"),
            "departure_at": r.get("departure_at"),
            "return_at": r.get("return_at"),
            "transfers": r.get("transfers"),
        })
    return out


# ---------- Layer 2: matriz de precios ----------
def month_prices(origin: str, destination: str, month: str) -> list[dict]:
    """Precios por fecha para un mes YYYY-MM (one-way, cheapest)."""
    q = urlencode({
        "origin": origin, "destination": destination,
        "month": month, "currency": "eur",
        "sorting": "price", "limit": 30, "one_way": "true", "token": TOKEN,
    })
    d = _get(f"https://api.travelpayouts.com/aviasales/v3/prices_for_dates?{q}")
    return (d.get("data") or [])


def cheapest_current(origin: str, destination: str) -> dict | None:
    """Precio más barato ahora mismo para la ruta (cualquier fecha)."""
    q = urlencode({
        "origin": origin, "destination": destination,
        "currency": "eur", "sorting": "price", "limit": 1,
        "one_way": "true", "token": TOKEN,
    })
    d = _get(f"https://api.travelpayouts.com/aviasales/v3/prices_for_dates?{q}")
    data = d.get("data") or []
    return data[0] if data else None


# ---------- Layer 3: anomalías ----------
def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS price_history (
        origin TEXT, destination TEXT, price REAL, seen_at TEXT,
        PRIMARY KEY (origin, destination, seen_at))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_route ON price_history(origin, destination)")
    return con


def record_and_score(origin: str, destination: str, price: float) -> dict:
    """Guarda el precio y devuelve z-score vs histórico de la ruta."""
    con = _db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?,?)",
                (origin, destination, price, now))
    con.commit()
    rows = con.execute(
        "SELECT price FROM price_history WHERE origin=? AND destination=?",
        (origin, destination)).fetchall()
    con.close()
    prices = [r[0] for r in rows]
    n = len(prices)
    if n < 5:
        return {"n": n, "zscore": None, "mean": None, "std": None}
    mean = sum(prices) / n
    var = sum((p - mean) ** 2 for p in prices) / n
    std = var ** 0.5
    z = (price - mean) / std if std > 0 else None
    return {"n": n, "zscore": z, "mean": mean, "std": std}


Z_THRESHOLD = -1.8      # precio muy por debajo de lo típico
MIN_DISCOUNT_PCT = 0.25  # y al menos 25% más barato que la media


def find_deals(origins: list[str], max_dest: int = 12) -> list[dict]:
    deals = []
    for origin in origins:
        trend = trending(origin)[:max_dest]
        for t in trend:
            dest = t["destination"]
            cur = cheapest_current(origin, dest)
            if not cur:
                continue
            price = cur["price"]
            s = record_and_score(origin, dest, price)
            is_deal = (
                s["zscore"] is not None
                and s["zscore"] <= Z_THRESHOLD
                and s["mean"] and price <= s["mean"] * (1 - MIN_DISCOUNT_PCT)
            )
            deals.append({
                "route": f"{origin}→{dest}",
                "origin": origin, "destination": dest,
                "price": price,
                "price_mean": round(s["mean"], 1) if s["mean"] else None,
                "zscore": round(s["zscore"], 2) if s["zscore"] is not None else None,
                "n_obs": s["n"],
                "deal": is_deal,
                "departure_at": cur.get("departure_at"),
                "link": "https://www.aviasales.com" + cur.get("link", ""),
                "affiliate_link": (f"https://tp.media/r?marker={MARKER}&p=4114&u={cur.get('link', '')}"
                                   if MARKER and cur.get("link") else None),
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            time.sleep(0.4)  # rate limit friendly
    return deals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default="VLC")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    origins = [o.strip().upper() for o in args.origin.split(",")]
    deals = find_deals(origins)

    hot = [d for d in deals if d["deal"]]
    if args.json:
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                          "deals": deals, "hot": hot}, ensure_ascii=False, indent=2))
    else:
        print(f"ViajeBarato scan {datetime.now():%Y-%m-%d %H:%M} — origins={origins}")
        print(f"rutas escaneadas: {len(deals)} | chollos (z≤{Z_THRESHOLD}): {len(hot)}")
        for d in deals:
            flag = "🔥" if d["deal"] else "  "
            zs = d["zscore"] if d["zscore"] is not None else "—"
            print(f"{flag} {d['route']:9s} {d['price']:>5.0f}€  media={d['price_mean'] or '—'}  z={zs}  n={d['n_obs']}")
    # exit code: 1 si hay chollos (útil para alertas cron)
    sys.exit(1 if hot else 0)


if __name__ == "__main__":
    main()
