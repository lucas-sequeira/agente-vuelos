#!/usr/bin/env python3
"""Web local del agente de vuelos: http://127.0.0.1:8792 (solo biblioteca estándar).

GET  /              la página
GET  /data          gangas + favoritos + config (hace git pull como mucho cada 3 min)
GET  /api/cuota     búsquedas de SerpApi que quedan este mes
POST /api/config    guarda los límites y los sube a GitHub (solo config.json)
POST /api/buscar    busca vuelos (y hotel, si se pide) en Google con SerpApi
"""
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PUERTO = 8792
_ultimo_pull = 0.0


def env():
    p = Path.home() / ".config/agente-vuelos/.env"
    if p.exists():
        for linea in p.read_text().splitlines():
            if "=" in linea and not linea.startswith("#"):
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def git(*args):
    return subprocess.run(["git", "-C", str(RAIZ), *args], capture_output=True, text=True, timeout=60)


def pull():
    global _ultimo_pull
    if time.time() - _ultimo_pull > 180:
        _ultimo_pull = time.time()
        git("pull", "--rebase", "-q")


def leer(p, defecto=None):
    try:
        return json.loads((RAIZ / p).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def serpapi(params):
    params = {**params, "api_key": os.environ.get("SERPAPI_KEY", "")}
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=90) as r:
        return json.load(r)


def validar_config(c):
    actual = leer("config.json")
    nuevo = dict(actual)
    for z in ("cerca", "media", "lejos"):
        v = float(c["umbrales"][z])
        assert 0 < v <= 2000
        nuevo["umbrales"][z] = round(v)
    nuevo["tope"] = round(float(c["tope"]))
    assert 0 < nuevo["tope"] <= 5000
    nuevo["descuento_pct"] = round(float(c["descuento_pct"]))
    assert 5 <= nuevo["descuento_pct"] <= 90
    fav = {}
    for k, v in c["favoritos"].items():
        k = k.strip().upper()
        assert re.fullmatch(r"[A-Z]{3}", k)
        fav[k] = round(float(v))
        assert 0 < fav[k] <= 5000
    nuevo["favoritos"] = fav
    nuevo["noches_min"] = int(c["noches_min"])
    nuevo["noches_max"] = int(c["noches_max"])
    assert 1 <= nuevo["noches_min"] <= nuevo["noches_max"] <= 21
    return nuevo


def buscar(q):
    destino = q["destino"].strip().upper()
    assert re.fullmatch(r"[A-Z]{3}", destino)
    ida, vuelta = q["ida"], q.get("vuelta") or ""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ida) and (not vuelta or re.fullmatch(r"\d{4}-\d{2}-\d{2}", vuelta))
    p = {"engine": "google_flights", "departure_id": leer("config.json")["origen"], "arrival_id": destino,
         "outbound_date": ida, "currency": "EUR", "hl": "es", "gl": "es", "type": 1 if vuelta else 2}
    if vuelta:
        p["return_date"] = vuelta
    d = serpapi(p)
    if "error" in d:
        return {"error": d["error"]}
    vuelos = []
    for f in d.get("best_flights", []) + d.get("other_flights", []):
        tramos = f["flights"]
        vuelos.append({
            "precio": f.get("price"),
            "aerolineas": sorted({t["airline"] for t in tramos}),
            "salida": tramos[0]["departure_airport"]["time"],
            "llegada": tramos[-1]["arrival_airport"]["time"],
            "escalas": len(tramos) - 1,
            "duracion": f.get("total_duration"),
        })
    vuelos.sort(key=lambda v: v["precio"] or 99999)
    pi = d.get("price_insights", {})
    res = {"vuelos": vuelos[:12], "nivel": pi.get("price_level"), "rango": pi.get("typical_price_range"),
           "google": d.get("search_metadata", {}).get("google_flights_url")}
    if q.get("hotel") and vuelta:
        ciudad = q.get("ciudad") or destino
        h = serpapi({"engine": "google_hotels", "q": f"hoteles en {ciudad}", "check_in_date": ida,
                     "check_out_date": vuelta, "adults": int(q.get("personas") or 1), "currency": "EUR",
                     "hl": "es", "gl": "es", "sort_by": 3})
        hoteles = []
        for x in h.get("properties", [])[:10]:
            total = (x.get("total_rate") or {}).get("extracted_lowest")
            if total:
                hoteles.append({"nombre": x.get("name"), "total": total,
                                "noche": (x.get("rate_per_night") or {}).get("extracted_lowest"),
                                "nota": x.get("overall_rating"), "estrellas": x.get("extracted_hotel_class"),
                                "enlace": x.get("link")})
        res["hoteles"] = sorted(hoteles, key=lambda x: x["total"])
    return res


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def enviar(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
        datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(datos)

    def cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def origen_local(self):
        o = self.headers.get("Origin")
        return o is None or o in (f"http://127.0.0.1:{PUERTO}", f"http://localhost:{PUERTO}")

    def do_GET(self):
        ruta = self.path.split("?")[0]
        if ruta == "/":
            return self.enviar(200, (RAIZ / "index.html").read_bytes(), "text/html; charset=utf-8")
        if ruta == "/data":
            pull()
            return self.enviar(200, {"gangas": leer("data/gangas.json", {}), "favoritos": leer("data/favoritos.json", {}),
                                     "config": leer("config.json", {})})
        if ruta == "/api/cuota":
            try:
                url = "https://serpapi.com/account.json?api_key=" + os.environ.get("SERPAPI_KEY", "")
                with urllib.request.urlopen(url, timeout=20) as r:
                    a = json.load(r)
                return self.enviar(200, {"quedan": a.get("total_searches_left"), "plan": a.get("plan_name")})
            except Exception as e:
                return self.enviar(200, {"quedan": None, "error": str(e)})
        self.enviar(404, {"error": "no existe"})

    def do_POST(self):
        if not self.origen_local():
            return self.enviar(403, {"error": "solo desde este Mac"})
        ruta = self.path.split("?")[0]
        try:
            if ruta == "/api/config":
                nuevo = validar_config(self.cuerpo())
                (RAIZ / "config.json").write_text(json.dumps(nuevo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                git("pull", "--rebase", "-q")
                git("add", "config.json")
                if git("diff", "--cached", "--quiet").returncode:
                    git("commit", "-q", "-m", "config: límites cambiados desde la web")
                    r = git("push", "-q")
                    if r.returncode:
                        return self.enviar(200, {"ok": True, "aviso": "Guardado en el Mac, pero no se pudo subir a GitHub."})
                return self.enviar(200, {"ok": True, "config": nuevo})
            if ruta == "/api/buscar":
                return self.enviar(200, buscar(self.cuerpo()))
        except (AssertionError, KeyError, ValueError, TypeError):
            return self.enviar(400, {"error": "Datos no válidos"})
        except Exception as e:
            return self.enviar(500, {"error": str(e)})
        self.enviar(404, {"error": "no existe"})


if __name__ == "__main__":
    env()
    print(f"Agente de vuelos en http://127.0.0.1:{PUERTO}")
    ThreadingHTTPServer(("127.0.0.1", PUERTO), H).serve_forever()
