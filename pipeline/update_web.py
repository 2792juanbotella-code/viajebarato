"""Regenera la tabla de chollos en index.html desde scan_latest.json y hace push.

Ejecutado por el cron diario tras el scan. Inserta el top-10 de rutas más
baratas (o chollos si los hay) entre los marcadores DEALS_START/END.
"""
import json
import pathlib
import re
import subprocess
from datetime import datetime

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
HTML = ROOT / "index.html"
MARKER = "573638"

scan = json.loads((HERE / "scan_latest.json").read_text(encoding="utf-8"))
deals = scan.get("deals", [])
hot = scan.get("hot", [])

rows = hot if hot else sorted(deals, key=lambda d: d["price"] or 9e9)[:10]
if not rows:
    raise SystemExit("sin datos en scan_latest.json — no toco la web")

def aff_link(d):
    if d.get("affiliate_link"):
        return d["affiliate_link"]
    return d.get("link") or "#"

def fmt_date(s):
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%d %b")
    except ValueError:
        return s[:10]

out = []
for d in rows[:10]:
    fire = "🔥 " if d["deal"] else ""
    disc = ""
    if d.get("price_mean"):
        pct = round((1 - d["price"] / d["price_mean"]) * 100)
        if pct > 0:
            disc = f' <span style="color:var(--ok)">−{pct}%</span>'
    out.append(
        f'      <tr><td>{fire}{d["origin"]} → {d["destination"]}</td>'
        f'<td>{fmt_date(d.get("departure_at"))}</td>'
        f'<td class="price">{d["price"]:.0f} €{disc}</td>'
        f'<td>—</td><td>—</td>'
        f'<td><a class="btn" target="_blank" rel="nofollow" href="{aff_link(d)}">Ver</a></td></tr>'
    )

html = HTML.read_text(encoding="utf-8")
table = "\n".join(out) + f'\n      <tr><td colspan="6" class="note">Actualizado {datetime.now():%d/%m/%Y %H:%M} · precios de un solo día, sin equipaje</td></tr>'
new = re.sub(r"(<!-- DEALS_START -->).*?(<!-- DEALS_END -->)",
             r"\1\n" + table.replace("\\", "\\\\") + r"\n    \2", html, flags=re.S)
if new == html:
    raise SystemExit("no se pudo insertar la tabla (marcadores no encontrados)")
HTML.write_text(new, encoding="utf-8")

r = subprocess.run(["git", "add", "-A"],
                   cwd=ROOT, capture_output=True, text=True)
r = subprocess.run(["git", "commit", "-m", f"chollos {datetime.now():%Y-%m-%d}"],
                   cwd=ROOT, capture_output=True, text=True)
if r.returncode == 0:
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ROOT, check=True)
    print(f"web actualizada: {len(rows)} rutas, push OK")
else:
    print("nada que commitear:", r.stdout, r.stderr)
