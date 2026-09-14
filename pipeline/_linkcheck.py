import os
import re
import ssl
from urllib.request import urlopen

html = open(r"C:\Users\User\viajebarato\index.html", encoding="utf-8").read()
links = re.findall(r'href="(https://tp\.media/r\?[^"]+)"', html)
good = [l for l in links if "marker=777254.web&u=https%3A%2F%2F" in l]
print("tp.media total:", len(links), "| formato nuevo:", len(good),
      "| formato viejo:", len(links) - len(good))

rows = re.findall(r"<tr><td>([^<]*\u2192[^<]*)</td><td>([^<]*)</td>"
                  r'<td class="price">([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td>',
                  html)
print("--- tabla (ruta | salida | precio | aerolinea | escalas) ---")
for r in rows[:10]:
    print(" | ".join(r))

cafile = os.environ.get("SSL_CERT_FILE", "")
ctx = None
if cafile and os.path.exists(cafile):
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
if good:
    resp = urlopen(good[0], timeout=30, context=ctx)
    print("link[0] ->", resp.status, resp.geturl()[:100])
