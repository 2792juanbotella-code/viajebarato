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
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
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


def _ssl_ctx():
    """Contexto SSL para el proxy de egress del gateway (si está activo).

    Con SSL_CERT_FILE presente usamos la CA del proxy, quitando
    VERIFY_X509_STRICT (Py>=3.13 lo activa por defecto y los leaf certs
    del proxy no llevan Authority Key Identifier). Sin proxy (cron del
    usuario) devuelve None → urlopen usa el contexto por defecto.
    """
    cafile = os.environ.get("SSL_CERT_FILE") or ""
    if not (cafile and os.path.exists(cafile)):
        return None
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=30, context=_ssl_ctx()) as r:
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
    con = sqlite3.connect(DB, timeout=30)  # espera locks (backfill/cron concurrentes)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""CREATE TABLE IF NOT EXISTS price_history (
        origin TEXT, destination TEXT, price REAL, seen_at TEXT,
        PRIMARY KEY (origin, destination, seen_at))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_route ON price_history(origin, destination)")
    return con


def record_and_score(origin: str, destination: str, price: float) -> dict:
    """Guarda el precio del día y devuelve z-score vs histórico PREVIO de la ruta.

    - Media/std calculadas ANTES de insertar el precio actual: el precio nuevo
      ya no arrastra su propia línea base (antes sesgaba la media hacia abajo
      e hinchaba la std, haciendo z menos negativo de lo real).
    - Un registro por ruta y día (seen_at = fecha UTC): re-escanear el mismo
      día hace REPLACE en vez de duplicar filas (curva diaria limpia).
    """
    con = _db()
    today = datetime.now(timezone.utc).date().isoformat()
    prior = [r[0] for r in con.execute(
        "SELECT price FROM price_history WHERE origin=? AND destination=? AND seen_at<?",
        (origin, destination, today)).fetchall()]
    n = len(prior)
    if n >= 5:
        mean = sum(prior) / n
        var = sum((p - mean) ** 2 for p in prior) / n
        std = var ** 0.5
        z = (price - mean) / std if std > 0 else None
    else:
        mean = std = z = None
    con.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?,?)",
                (origin, destination, price, today))
    con.commit()
    con.close()
    return {"n": n, "zscore": z, "mean": mean, "std": std}


Z_THRESHOLD = -1.8      # precio muy por debajo de lo típico
MIN_DISCOUNT_PCT = 0.25  # y al menos 25% más barato que la media

# Destinos excluidos: espacio aéreo UE cerrado (Rusia/Ucrania) — ofertas no
# operativas para viajeros españoles. backfill.py lo importa; purgar la BD si
# ya existen filas (ver historial git 14-sep-2026).
BLOCKLIST_DEST = {"MOW", "VKO", "SVO", "DME", "LED", "KBP", "IEV"}


def find_deals(origins: list[str], max_dest: int = 12) -> list[dict]:
    deals = []
    for origin in origins:
        try:
            trend = trending(origin)[:max_dest]
        except Exception as e:  # noqa: BLE001 — un origen caído no tumba el scan
            print(f"warn: trending {origin} falló: {e}", file=sys.stderr)
            continue
        for t in trend:
            dest = t["destination"]
            if dest in BLOCKLIST_DEST:
                continue
            try:
                cur = cheapest_current(origin, dest)
            except Exception as e:  # noqa: BLE001
                print(f"warn: {origin}→{dest} falló: {e}", file=sys.stderr)
                continue
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
                "airline": cur.get("airline"),
                "transfers": cur.get("transfers"),
                "duration_min": cur.get("duration_to"),
                "link": "https://www.aviasales.com" + cur.get("link", ""),
                # verificado en vivo 14-sep: u=<url completa de aviasales enc> → 200 aviasales (path solo → 400)
                "affiliate_link": (f"https://tp.media/r?marker={MARKER}.web&u={quote('https://www.aviasales.com' + cur.get('link', ''), safe='')}&p=4114&campaign_id=100"
                                   if MARKER and cur.get("link") else None),
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            time.sleep(0.4)  # rate limit friendly
    return deals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default="VLC")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--output", help="fichero donde escribir el JSON (UTF-8 sin BOM; evita redirects de PowerShell)")
    args = ap.parse_args()

    origins = [o.strip().upper() for o in args.origin.split(",")]
    deals = find_deals(origins)

    hot = [d for d in deals if d["deal"]]
    if args.json:
        payload = json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                              "deals": deals, "hot": hot}, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(payload + "\n", encoding="utf-8")
        print(payload)
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
