#!/usr/bin/env python3
"""
Renfe GTFS AV/LD/MD Archiver
Descarga https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip
y extrae un CSV con todos los trayectos del día:
  data/YYYY/MM/gtfs-YYYY-MM-DD.csv

Solo escribe si el contenido ha cambiado respecto al día anterior.
"""

import csv
import datetime
import hashlib
import io
import os
import sys
import time
import urllib.request
import zipfile

GTFS_URL = "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"

COLUMNAS = [
    "fecha",
    "tren",
    "categoria",
    "tipo_servicio",
    "detalle",
    "servicio_comercial",
    "sentido",
    "cod_origen",
    "estacion_origen",
    "cod_destino",
    "estacion_destino",
    "num_paradas",
    "paradas",
]


# ── Tipo de servicio por rango ADIF ───────────────────────────────────────────
def get_tipo(tren: str) -> tuple:
    try:
        n = int(tren)
    except (ValueError, TypeError):
        return ("Desconocido", "?", "?")
    if n <= 1999:
        return ("Larga Distancia", "LD", "LD")
    elif n <= 5999:
        lav = {2: "LAV Sur", 3: "LAV Nordeste", 4: "LAV Norte/NW", 5: "LAV Levante"}.get(n // 1000, "AV")
        return ("AVE / Alta Velocidad LD", "AV", lav)
    elif n <= 7999:
        return ("AV otros operadores", "AV", "AV")
    elif n <= 9999:
        return ("Avant (AV Media Distancia)", "AV", "Avant")
    elif n <= 11999:
        return ("Ocasional LD/AV", "LD", "Ocas.")
    elif n <= 18999:
        areas = {12: "Norte", 13: "Sur", 14: "Este", 15: "Nordeste",
                 16: "Norte", 17: "Centro", 18: "Varias"}
        return ("Media Distancia", "MD", f"Área {areas.get(n // 1000, '')}")
    elif n <= 19499:
        return ("AV MD otros operadores", "AV", "AV MD")
    elif n <= 29999:
        nucleos = {19: "Mixto", 20: "Madrid", 21: "Madrid", 22: "Asturias",
                   23: "Andalucía", 24: "Valencia/Murcia", 25: "País Vasco",
                   26: "Barcelona", 27: "Madrid", 28: "Galicia", 29: "Otros"}
        return ("Cercanías", "Ce", f"Núcleo {nucleos.get(n // 1000, '')}")
    elif n <= 32999:
        return ("LD/MD/Ce fascículo", "LD/MD", "Fascículo")
    elif n <= 36499:
        return ("Ocasional MD/Ce", "MD", "Ocas.")
    elif n <= 39999:
        return ("Servicio interno", "Int", "Interno")
    elif n <= 69999:
        return ("Mercancías", "Merc", "Merc.")
    elif n <= 74999:
        return ("Ancho métrico (FEVE)", "FEVE", "FEVE")
    elif n <= 79999:
        return ("Cercanías (rebase)", "Ce", "Ce rebase")
    else:
        return ("Reservado", "?", "?")


# ── Descarga ──────────────────────────────────────────────────────────────────
def fetch() -> bytes:
    req = urllib.request.Request(
        GTFS_URL,
        headers={"User-Agent": "renfe-gtfs-archiver/1.0"},
        method="GET",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return resp.read()
        except Exception as e:
            if attempt == 2:
                print(f"ERROR tras 3 intentos: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"Intento {attempt + 1} fallido: {e}. Reintentando...", file=sys.stderr)
            time.sleep(10)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def last_hash_path(out_dir: str) -> str:
    return os.path.join(out_dir, ".last_hash_gtfs")


def load_last_hash(out_dir: str):
    p = last_hash_path(out_dir)
    return open(p).read().strip() if os.path.exists(p) else None


def save_hash(out_dir: str, h: str):
    with open(last_hash_path(out_dir), "w") as f:
        f.write(h)


# ── Procesado del ZIP ─────────────────────────────────────────────────────────
def parse_gtfs(data: bytes) -> list:
    """Devuelve lista de dicts con un registro por tren+trayecto único."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        def read(name):
            with zf.open(name) as f:
                lines = f.read().decode("utf-8-sig").splitlines()
            reader = csv.DictReader(lines)
            # limpiar espacios en nombres de columna
            reader.fieldnames = [c.strip() for c in reader.fieldnames]
            return [{k.strip(): v.strip() for k, v in row.items()} for row in reader]

        trips      = read("trips.txt")
        stops      = read("stops.txt")
        stop_times = read("stop_times.txt")
        routes     = read("routes.txt")

    # Índices rápidos
    stop_name  = {s["stop_id"]: s["stop_name"] for s in stops}
    route_name = {r["route_id"]: r.get("route_short_name", "") for r in routes}

    # trip_short_name = número de tren (5 dígitos)
    trip_info = {
        t["trip_id"]: {
            "tren": t.get("trip_short_name", "")[:5].strip(),
            "route_id": t.get("route_id", ""),
        }
        for t in trips
    }

    # Agrupar stop_times por trip_id, ordenados por stop_sequence
    from collections import defaultdict
    trip_stops = defaultdict(list)
    for st in stop_times:
        try:
            seq = int(st["stop_sequence"])
        except ValueError:
            seq = 0
        trip_stops[st["trip_id"]].append((seq, st["stop_id"]))

    # Para cada trip: primera parada, última parada, lista completa
    rows = {}  # clave: (tren, cod_origen, cod_destino)
    for trip_id, stops_list in trip_stops.items():
        stops_list.sort(key=lambda x: x[0])
        info = trip_info.get(trip_id, {})
        tren = info.get("tren", "")
        if not tren:
            continue

        cod_origen  = stops_list[0][1]
        cod_destino = stops_list[-1][1]
        key = (tren, cod_origen, cod_destino)

        if key not in rows:
            tipo, cat, det = get_tipo(tren)
            sentido = "Par (↓ Sur/Destino)" if int(tren) % 2 == 0 else "Impar (↑ Norte/Origen)" if tren.isdigit() else ""
            rows[key] = {
                "tren":             tren,
                "categoria":        cat,
                "tipo_servicio":    tipo,
                "detalle":          det,
                "servicio_comercial": route_name.get(info.get("route_id", ""), ""),
                "sentido":          sentido,
                "cod_origen":       cod_origen,
                "estacion_origen":  stop_name.get(cod_origen, cod_origen),
                "cod_destino":      cod_destino,
                "estacion_destino": stop_name.get(cod_destino, cod_destino),
                "num_paradas":      len(stops_list),
                "paradas":          " > ".join(stop_name.get(s[1], s[1]) for s in stops_list),
            }

    return sorted(rows.values(), key=lambda r: r["tren"])


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    fecha   = now_utc.strftime("%Y-%m-%d")
    out_dir = os.path.join("data", now_utc.strftime("%Y"), now_utc.strftime("%m"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"Descargando GTFS desde {GTFS_URL} ...")
    data = fetch()
    h    = sha256(data)

    if load_last_hash(out_dir) == h:
        print("Sin cambios respecto a la descarga anterior. No se escribe CSV.")
        sys.exit(0)

    print("Procesando GTFS...")
    trayectos = parse_gtfs(data)

    csv_path = os.path.join(out_dir, f"gtfs-{fecha}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNAS)
        writer.writeheader()
        for row in trayectos:
            writer.writerow({"fecha": fecha, **row})

    save_hash(out_dir, h)

    print(f"Guardado  : {csv_path}")
    print(f"Fecha     : {fecha}")
    print(f"Trayectos : {len(trayectos)}")


if __name__ == "__main__":
    main()
