#!/usr/bin/env python3
"""Busca gangas de vuelos desde Valencia (Ryanair) y avisa por Telegram.

Lo ejecuta GitHub Actions dos veces al día; también funciona en el Mac:
    python3 escanear.py            # busca, guarda y avisa
    python3 escanear.py --sin-avisos

Solo usa la biblioteca estándar. Escribe en data/:
    gangas.json      todas las idas y vueltas más baratas por destino (lo que pinta la web)
    favoritos.json   el más barato de cada mes para los destinos favoritos
    historial.json   precio mínimo de cada día por destino (para saber qué es "normal")
    alertados.json   lo que ya se avisó, para no repetir
"""
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent
DATA = RAIZ / "data"
MADRID = ZoneInfo("Europe/Madrid")
API = "https://services-api.ryanair.com/farfnd/v4"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

# Zonas por país (código de país de Ryanair, en minúsculas). Lo que no esté aquí es "lejos".
CERCA = {"es", "pt", "fr", "it", "ad"}
MEDIA = {"gb", "ie", "be", "nl", "de", "ch", "at", "pl", "cz", "hu", "sk", "si", "hr", "mt", "dk", "lu"}
# Aeropuertos que no siguen la regla de su país.
LEJOS_FORZADO = {"EDI", "GLA", "PIK", "ABZ", "INV",            # Escocia
                 "LPA", "TFN", "TFS", "ACE", "FUE", "SPC"}      # Canarias


def zona(iata, pais):
    if iata in LEJOS_FORZADO:
        return "lejos"
    if pais in CERCA:
        return "cerca"
    if pais in MEDIA:
        return "media"
    return "lejos"


def leer(nombre, defecto):
    p = DATA / nombre
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def guardar(nombre, valor):
    DATA.mkdir(exist_ok=True)
    (DATA / nombre).write_text(json.dumps(valor, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def pedir(ruta, params, intentos=3):
    url = f"{API}/{ruta}?{urllib.parse.urlencode(params)}"
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # red, 403, 429…
            if i == intentos - 1:
                print(f"  ✗ {ruta} {params.get('arrivalAirportIataCode', '')}: {e}", file=sys.stderr)
                return {}
            time.sleep(3 * (i + 1))


def destinos(cfg, desde, hasta):
    """Todos los destinos de Ryanair desde el origen (la búsqueda de solo ida los da todos)."""
    d = pedir("oneWayFares", {
        "departureAirportIataCode": cfg["origen"],
        "outboundDepartureDateFrom": desde.isoformat(),
        "outboundDepartureDateTo": hasta.isoformat(),
        "market": "es-es", "currency": "EUR",
    })
    out = {}
    for f in d.get("fares", []):
        a = f["outbound"]["arrivalAirport"]
        out[a["iataCode"]] = {"ciudad": a["city"]["name"], "pais": a["city"]["countryCode"],
                              "nombre_pais": a.get("countryName", "")}
    return out


def ida_y_vuelta(cfg, iata, desde, hasta):
    d = pedir("roundTripFares", {
        "departureAirportIataCode": cfg["origen"],
        "arrivalAirportIataCode": iata,
        "outboundDepartureDateFrom": desde.isoformat(),
        "outboundDepartureDateTo": hasta.isoformat(),
        "inboundDepartureDateFrom": desde.isoformat(),
        "inboundDepartureDateTo": (hasta + timedelta(days=cfg["noches_max"])).isoformat(),
        "durationFrom": cfg["noches_min"], "durationTo": cfg["noches_max"],
        "market": "es-es", "currency": "EUR",
    })
    fares = d.get("fares", [])
    if not fares:
        return None
    f = min(fares, key=lambda f: f["summary"]["price"]["value"])
    ida, vuelta = f["outbound"]["departureDate"], f["inbound"]["departureDate"]
    return {"precio": round(f["summary"]["price"]["value"], 2),
            "ida": ida[:16], "vuelta": vuelta[:16],
            "noches": (date.fromisoformat(vuelta[:10]) - date.fromisoformat(ida[:10])).days}


def enlace(cfg, iata, ida, vuelta):
    q = urllib.parse.urlencode({
        "adults": 1, "teens": 0, "children": 0, "infants": 0,
        "dateOut": ida[:10], "dateIn": vuelta[:10], "isConnectedFlight": "false",
        "discount": 0, "isReturn": "true", "promoCode": "",
        "originIata": cfg["origen"], "destinationIata": iata})
    return f"https://www.ryanair.com/es/es/trip/flights/select?{q}"


def precio_normal(hist_destino, hoy):
    """Mediana de los últimos 30 días (sin contar hoy). None si hay menos de 5 días."""
    lim = (hoy - timedelta(days=30)).isoformat()
    valores = [p for d, p in hist_destino.items() if lim <= d < hoy.isoformat()]
    return statistics.median(valores) if len(valores) >= 5 else None


def es_ganga(cfg, g):
    """Devuelve el motivo si es ganga, o None."""
    if g["precio"] > cfg["tope"]:
        return None
    fav = cfg["favoritos"].get(g["iata"])
    if fav is not None and g["precio"] <= fav:
        return "favorito"
    if g["precio"] <= cfg["umbrales"][g["zona"]]:
        return "umbral"
    if g.get("normal") and g["precio"] <= g["normal"] * (1 - cfg["descuento_pct"] / 100):
        return "rebaja"
    return None


def telegram(texto):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("  (sin TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: no se envía)")
        return False
    datos = urllib.parse.urlencode({"chat_id": chat, "text": texto, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", datos, timeout=20) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print(f"  ✗ Telegram: {e}", file=sys.stderr)
        return False


def fecha_corta(iso):
    d = date.fromisoformat(iso[:10])
    meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    return f"{d.day} {meses[d.month - 1]}"


def cargar_env_local():
    """En el Mac, lee ~/.config/agente-vuelos/.env si las variables no están puestas."""
    p = Path.home() / ".config/agente-vuelos/.env"
    if p.exists():
        for linea in p.read_text().splitlines():
            if "=" in linea and not linea.startswith("#"):
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    avisar = "--sin-avisos" not in sys.argv
    cargar_env_local()
    cfg = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
    ahora = datetime.now(MADRID)
    hoy = ahora.date()
    desde = hoy + timedelta(days=1)
    hasta = hoy + timedelta(days=30 * cfg["meses"])

    print(f"Buscando destinos desde {cfg['origen']}…")
    dest = destinos(cfg, desde, hasta)
    for iata in cfg["favoritos"]:
        dest.setdefault(iata, {"ciudad": iata, "pais": "", "nombre_pais": ""})
    if not dest:
        print("Ryanair no ha devuelto destinos; no se toca nada.", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(dest)} destinos")

    historial = leer("historial.json", {})
    gangas = []
    for iata, info in sorted(dest.items()):
        r = ida_y_vuelta(cfg, iata, desde, hasta)
        time.sleep(0.4)
        if not r:
            continue
        h = historial.setdefault(iata, {})
        normal = precio_normal(h, hoy)
        h[hoy.isoformat()] = min(r["precio"], h.get(hoy.isoformat(), r["precio"]))
        g = {"iata": iata, **info, "zona": zona(iata, info["pais"]), **r,
             "normal": round(normal, 2) if normal else None,
             "enlace": enlace(cfg, iata, r["ida"], r["vuelta"])}
        g["motivo"] = es_ganga(cfg, g)
        gangas.append(g)
    gangas.sort(key=lambda g: g["precio"])

    # Favoritos: el más barato de cada mes.
    favoritos = {}
    for iata in cfg["favoritos"]:
        meses = []
        inicio = desde
        for _ in range(cfg["meses"]):
            fin_mes = (inicio.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            r = ida_y_vuelta(cfg, iata, inicio, fin_mes)
            time.sleep(0.4)
            meses.append({"mes": inicio.strftime("%Y-%m"), **(r or {}),
                          "enlace": enlace(cfg, iata, r["ida"], r["vuelta"]) if r else None})
            inicio = fin_mes + timedelta(days=1)
        favoritos[iata] = {"ciudad": dest[iata]["ciudad"], "limite": cfg["favoritos"][iata], "meses": meses,
                           "historial": dict(sorted(historial.get(iata, {}).items())[-60:])}

    # Avisos: solo lo nuevo, o si bajó al menos un 5 % desde el último aviso.
    alertados = leer("alertados.json", {})
    alertados = {k: v for k, v in alertados.items() if v["ida"][:10] > hoy.isoformat()}
    nuevas = []
    for g in gangas:
        if not g["motivo"]:
            continue
        clave = f"{g['iata']}|{g['ida'][:10]}|{g['vuelta'][:10]}"
        previo = alertados.get(clave)
        if previo and g["precio"] > previo["precio"] * 0.95:
            continue
        nuevas.append(g)
        alertados[clave] = {"precio": g["precio"], "ida": g["ida"], "avisado": ahora.isoformat(timespec="minutes")}

    enviado = False
    if nuevas and avisar:
        iconos = {"favorito": "⭐", "rebaja": "📉", "umbral": "✈️"}
        lineas = [f"<b>{len(nuevas)} ganga{'s' if len(nuevas) > 1 else ''} desde Valencia</b>"]
        for g in nuevas[:15]:
            extra = f" (normal {g['normal']:.0f} €)" if g["motivo"] == "rebaja" and g["normal"] else ""
            lineas.append(f"{iconos[g['motivo']]} <b>{g['ciudad']}</b> {g['precio']:.0f} € ida y vuelta{extra}\n"
                          f"   {fecha_corta(g['ida'])} → {fecha_corta(g['vuelta'])} · {g['noches']} noches · "
                          f"<a href=\"{g['enlace']}\">ver en Ryanair</a>")
        if len(nuevas) > 15:
            lineas.append(f"…y {len(nuevas) - 15} más en la web.")
        lineas.append("<i>Precio por persona, sin maleta facturada.</i>")
        enviado = telegram("\n\n".join(lineas))
    if not avisar or enviado or not nuevas:
        guardar("alertados.json", alertados)  # si falló el envío, se reintenta en la siguiente vuelta

    guardar("historial.json", historial)
    guardar("gangas.json", {"actualizado": ahora.isoformat(timespec="minutes"), "origen": cfg["origen"],
                            "total_destinos": len(gangas), "vuelos": gangas})
    guardar("favoritos.json", {"actualizado": ahora.isoformat(timespec="minutes"), "destinos": favoritos})
    print(f"  {sum(1 for g in gangas if g['motivo'])} gangas, {len(nuevas)} nuevas, aviso enviado: {enviado}")


if __name__ == "__main__":
    main()
