"""Regenera la tabla de chollos en index.html desde scan_latest.json y hace push.

Ejecutado por el cron diario tras el scan. Inserta una selección multirregión
equilibrada (chollos + hasta 6 mejores precios por región de España) entre
los marcadores DEALS_START/END.
"""
import json
import os
import pathlib
import re
import subprocess
from datetime import datetime

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
HTML = ROOT / "index.html"
MARKER = "777254"

AIRLINES = {
    "FR": "Ryanair", "U2": "easyJet", "EC": "easyJet", "VY": "Vueling", "IB": "Iberia",
    "I2": "Iberia Express", "UX": "Air Europa", "EW": "Eurowings", "PC": "Pegasus",
    "W4": "Wizz Air", "W6": "Wizz Air", "TP": "TAP", "AF": "Air France",
    "KL": "KLM", "LH": "Lufthansa", "BA": "British Airways",
    "AZ": "ITA Airways", "A3": "Aegean", "SN": "Brussels Airlines",
    "OS": "Austrian", "SK": "SAS", "DY": "Norwegian", "D8": "Norwegian",
    "LS": "Jet2", "HV": "Transavia", "TO": "Transavia FR", "NT": "Binter",
    "YW": "Air Nostrum", "LX": "SWISS", "AY": "Finnair", "EI": "Aer Lingus",
    "TK": "Turkish", "QR": "Qatar", "EK": "Emirates", "0B": "Blue Air",
    "V7": "Volotea", "DE": "Condor", "X3": "TUI fly",
}

REGIONS = {
    "madrid": {"MAD"},
    "cataluna": {"BCN", "GRO", "REU"},
    "levante": {"VLC", "ALC", "CDT", "RMU"},
    "andalucia": {"AGP", "SVQ", "GRX", "XRY", "LEI"},
    "norte": {"BIO", "SDR", "OVD", "SCQ", "LCG", "VGO", "VIT", "PNA", "ZAZ", "LEN", "BJZ", "MLN"},
    "islas": {"PMI", "IBZ", "MAH", "LPA", "TFN", "TFS", "FUE", "ACE", "SPC", "VDE", "GMZ"},
}
AIRPORT_TO_REGION = {ap: reg for reg, aps in REGIONS.items() for ap in aps}

def fmt_airline(code):
    if not code:
        return "—"
    return AIRLINES.get(code, code)

def fmt_transfers(t):
    if t is None:
        return "—"
    t = int(t)
    return "Directo" if t == 0 else ("1 escala" if t == 1 else f"{t} escalas")

scan = json.loads((HERE / "scan_latest.json").read_text(encoding="utf-8"))
deals = scan.get("deals", [])
hot = scan.get("hot", [])

if not deals and not hot:
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

# Selección multirregión equilibrada:
# 1. Todos los chollos 'hot'
# 2. Hasta 6 mejores precios por cada región de España
selected = []
seen = set()

for h in hot:
    key = (h["origin"], h["destination"], h.get("departure_at"))
    if key not in seen:
        seen.add(key)
        selected.append(h)

by_reg = {r: [] for r in REGIONS}
for d in sorted(deals, key=lambda x: x.get("price") or 9e9):
    reg = AIRPORT_TO_REGION.get(d["origin"])
    if reg and len(by_reg[reg]) < 6:
        key = (d["origin"], d["destination"], d.get("departure_at"))
        if key not in seen:
            seen.add(key)
            by_reg[reg].append(d)
            selected.append(d)

selected.sort(key=lambda x: (not x.get("deal", False), x.get("price") or 9e9))

out = []
for d in selected:
    fire = "🔥 " if d.get("deal") else ""
    disc = ""
    if d.get("price_mean"):
        pct = round((1 - d["price"] / d["price_mean"]) * 100)
        if pct > 0:
            disc = f' <span class="badge-disc">−{pct}%</span>'
    reg = AIRPORT_TO_REGION.get(d["origin"], "otros")
    out.append(
        f'      <tr data-origin="{d["origin"]}" data-region="{reg}" data-price="{d["price"]:.0f}">'
        f'<td class="td-route">{fire}{d["origin"]} → {d["destination"]}</td>'
        f'<td class="td-date">{fmt_date(d.get("departure_at"))}</td>'
        f'<td class="td-price price">{d["price"]:.0f} €{disc}</td>'
        f'<td class="td-airline">{fmt_airline(d.get("airline"))}</td>'
        f'<td class="td-transfers">{fmt_transfers(d.get("transfers"))}</td>'
        f'<td class="td-action"><a class="btn" target="_blank" rel="nofollow noopener" href="{aff_link(d)}">Ver</a></td></tr>'
    )

out.append(
    '      <tr id="noDealsRow" style="display:none"><td colspan="6" style="text-align:center;padding:24px 10px;color:var(--mut)">No hay chollos activos para esta región en la foto de hoy. Prueba otra zona o usa el buscador superior.</td></tr>'
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
    # push portable: si hay proxy de egress (SSL_CERT_FILE), git-schannel no confía
    # en sus leaf certs -> backend openssl + CA del proxy solo en ese caso
    push = ["git", "push", "-q", "origin", "main"]
    cafile = os.environ.get("SSL_CERT_FILE", "")
    if cafile and os.path.exists(cafile):
        push = ["git", "-c", "http.sslBackend=openssl",
                "-c", f"http.sslCAInfo={cafile}", "push", "-q", "origin", "main"]
    subprocess.run(push, cwd=ROOT, check=True)
    print(f"web actualizada: {len(selected)} rutas, push OK")
else:
    print("nada que commitear:", r.stdout, r.stderr)
