"""Fuentes de resultados. Todas devuelven un DataFrame con:

fecha (datetime64), local, visitante, gl, gv (NaN si no se ha jugado)
y, cuando la fuente las trae, cuotas de cierre: c1, cx, c2.

Cada descarga se guarda en datos/cache. Si una descarga falla se usa la copia
guardada, así una caída de la fuente no rompe la actualización.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
CACHE = RAIZ / "datos" / "cache"
COLUMNAS = ["fecha", "local", "visitante", "gl", "gv"]


class FuenteNoDisponible(Exception):
    pass


def _descargar(url: str, nombre_cache: str, headers: dict | None = None, max_edad_h: float = 6) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    ruta = CACHE / nombre_cache
    if ruta.exists() and (time.time() - ruta.stat().st_mtime) < max_edad_h * 3600:
        return ruta.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "pizarra-poisson/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            datos = r.read()
        ruta.write_bytes(datos)
        return datos
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if ruta.exists():
            print(f"  · aviso: no se pudo descargar {url} ({e}); uso la copia guardada")
            return ruta.read_bytes()
        raise FuenteNoDisponible(f"{url}: {e}") from e


def _limpiar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.normalize()
    for c in ("gl", "gv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["local"] = df["local"].astype(str).str.strip()
    df["visitante"] = df["visitante"].astype(str).str.strip()
    df = df[(df["local"] != "") & (df["visitante"] != "")]
    return df.sort_values("fecha").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# openfootball (GitHub, sin clave). Lo mantienen voluntarios: puede ir
# algunas jornadas atrasado.
# --------------------------------------------------------------------------- #
def openfootball(codigo: str, temporadas: list[int]) -> pd.DataFrame:
    filas = []
    hoy = pd.Timestamp(date.today())
    for t in temporadas:
        s = f"{t}-{str(t + 1)[-2:]}"
        url = f"https://raw.githubusercontent.com/openfootball/football.json/master/{s}/{codigo}.json"
        try:
            data = json.loads(_descargar(url, f"openfootball_{s}_{codigo}.json"))
        except FuenteNoDisponible:
            continue
        for m in data.get("matches", []):
            sc = m.get("score")
            ft = sc.get("ft") if isinstance(sc, dict) else sc if isinstance(sc, list) else None
            f = pd.Timestamp(m["date"])
            jugado = ft is not None and len(ft) == 2 and f <= hoy
            filas.append({"fecha": f, "local": m["team1"], "visitante": m["team2"],
                          "gl": ft[0] if jugado else np.nan, "gv": ft[1] if jugado else np.nan,
                          "temporada": t})
    if not filas:
        raise FuenteNoDisponible(f"openfootball {codigo}: sin datos")
    return _limpiar(pd.DataFrame(filas))


# --------------------------------------------------------------------------- #
# football-data.co.uk (CSV, sin clave). Se actualiza dos veces por semana e
# incluye cuotas de cierre de Pinnacle, útiles para medir el modelo contra
# el mercado.
# --------------------------------------------------------------------------- #
def _leer_csv_fd(texto: str, temporada: int | None, div: str | None = None) -> pd.DataFrame:
    raw = pd.read_csv(io.StringIO(texto), encoding_errors="ignore", on_bad_lines="skip")
    if div is not None:
        raw = raw[raw.get("Div") == div] if "Div" in raw else raw.iloc[0:0]
    raw = raw.dropna(subset=["HomeTeam", "AwayTeam"])
    fechas = pd.to_datetime(raw["Date"], dayfirst=True, errors="coerce", format="mixed")
    df = pd.DataFrame({"fecha": fechas, "local": raw["HomeTeam"], "visitante": raw["AwayTeam"],
                       "gl": raw.get("FTHG"), "gv": raw.get("FTAG")})
    for destino, opciones in {"c1": ["PSCH", "PSH", "AvgCH", "AvgH", "B365H"],
                              "cx": ["PSCD", "PSD", "AvgCD", "AvgD", "B365D"],
                              "c2": ["PSCA", "PSA", "AvgCA", "AvgA", "B365A"]}.items():
        col = next((c for c in opciones if c in raw.columns), None)
        df[destino] = pd.to_numeric(raw[col], errors="coerce") if col else np.nan
    if temporada is not None:
        df["temporada"] = temporada
    return df.dropna(subset=["fecha"])


def footballdata(codigo: str, temporadas: list[int]) -> pd.DataFrame:
    partes = []
    for t in temporadas:
        s = f"{str(t)[-2:]}{str(t + 1)[-2:]}"
        url = f"https://www.football-data.co.uk/mmz4281/{s}/{codigo}.csv"
        try:
            partes.append(_leer_csv_fd(_descargar(url, f"fd_{s}_{codigo}.csv").decode("latin-1"), t))
        except FuenteNoDisponible:
            continue
    if not partes:
        raise FuenteNoDisponible(f"football-data.co.uk {codigo}: sin datos")
    df = pd.concat(partes)
    # Próximos partidos: un único CSV con todas las ligas, filtrado por división
    try:
        texto = _descargar("https://www.football-data.co.uk/fixtures.csv", "fd_fixtures.csv").decode("latin-1")
        fx = _leer_csv_fd(texto, max(temporadas), div=codigo)
        fx["gl"] = np.nan
        fx["gv"] = np.nan
        df = pd.concat([df, fx])
    except (FuenteNoDisponible, KeyError):
        pass
    return _limpiar(df.drop_duplicates(subset=["fecha", "local", "visitante"], keep="first"))


# --------------------------------------------------------------------------- #
# API-Football (api-sports.io). Necesita clave en la variable de entorno
# API_FOOTBALL_KEY. Plan gratuito: 100 solicitudes al día. Cubre la Liga
# Promerica y publica resultados minutos después de cada partido.
# --------------------------------------------------------------------------- #
API_URL = "https://v3.football.api-sports.io"
TERMINADO = {"FT", "AET", "PEN", "AWD", "WO"}
DESCARTADO = {"CANC", "ABD"}
CUOTA = {"usadas": 0, "restantes": None}


def _api(ruta: str, cache: str, max_edad_h: float) -> dict:
    """Llama a la API. Solo guarda en caché respuestas sin errores, para que un
    error de cuota no quede grabado como si fuera un dato válido."""
    archivo = CACHE / cache
    if archivo.exists() and (time.time() - archivo.stat().st_mtime) < max_edad_h * 3600:
        return json.loads(archivo.read_bytes())
    clave = os.environ.get("API_FOOTBALL_KEY")
    if not clave:
        raise FuenteNoDisponible("API-Football: falta la variable API_FOOTBALL_KEY")
    req = urllib.request.Request(f"{API_URL}{ruta}", headers={"x-apisports-key": clave,
                                                                "User-Agent": "pizarra-poisson/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            cuerpo = r.read()
            restantes = r.headers.get("x-ratelimit-requests-remaining")
        CUOTA["usadas"] += 1
        if restantes is not None:
            CUOTA["restantes"] = int(restantes)
        data = json.loads(cuerpo)
        if data.get("errors"):
            raise FuenteNoDisponible(f"API-Football: {data['errors']}")
        CACHE.mkdir(parents=True, exist_ok=True)
        archivo.write_bytes(cuerpo)
        return data
    except (FuenteNoDisponible, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        if archivo.exists():
            print(f"  · aviso: {e}; uso la copia guardada de {cache}")
            return json.loads(archivo.read_bytes())
        raise e if isinstance(e, FuenteNoDisponible) else FuenteNoDisponible(f"API-Football: {e}")


def _sin_tildes(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def resolver_apifootball(cfg: dict) -> tuple[int, int | None]:
    """Devuelve (id de liga, temporada en curso según la API).

    En config.toml, `apifootball = "auto"` busca la liga por país y nombre
    (apifootball_pais, apifootball_nombre). `temporada_actual = "auto"` toma la
    temporada que la API marca como vigente. Ambas consultas se guardan 7 días.
    """
    ident = cfg.get("apifootball")
    if ident in (None, ""):
        raise FuenteNoDisponible("API-Football: falta el id de liga en config.toml")
    if str(ident).lower() == "auto":
        pais = cfg.get("apifootball_pais", "")
        data = _api(f"/leagues?country={pais}&type=league", f"apif_ligas_{pais}.json", 24 * 7)
        objetivo = _sin_tildes(cfg.get("apifootball_nombre", "Primera Division"))
        cand = [r for r in data.get("response", []) if objetivo in _sin_tildes(r["league"]["name"])]
        if not cand:
            raise FuenteNoDisponible(f"API-Football: no encuentro '{objetivo}' en {pais}")
        info = cand[0]
    else:
        data = _api(f"/leagues?id={int(ident)}", f"apif_liga_{int(ident)}.json", 24 * 7)
        if not data.get("response"):
            raise FuenteNoDisponible(f"API-Football: la liga {ident} no existe")
        info = data["response"][0]
    actual = next((s["year"] for s in info.get("seasons", []) if s.get("current")), None)
    return int(info["league"]["id"]), actual


def apifootball(liga_id: int, temporadas: list[int], cache_actual_h: float = 1.0) -> pd.DataFrame:
    filas = []
    ultima = max(temporadas)
    for t in temporadas:
        # Temporadas terminadas no cambian: se guardan un mes. La actual, ~1 hora.
        edad = cache_actual_h if t == ultima else 24 * 30
        try:
            data = _api(f"/fixtures?league={liga_id}&season={t}", f"apif_{liga_id}_{t}.json", edad)
        except FuenteNoDisponible as e:
            if t == ultima:
                # Sin la temporada en curso los datos estarían viejos: mejor pasar a la siguiente fuente
                raise FuenteNoDisponible(f"API-Football no entrega la temporada {t} ({e}). "
                                         "Revisa tu plan con: python cli.py probar-api") from e
            print(f"  · aviso: {e}")
            continue
        for f in data.get("response", []):
            estado = f["fixture"]["status"]["short"]
            if estado in DESCARTADO:
                continue
            jugado = estado in TERMINADO
            filas.append({"fecha": pd.Timestamp(f["fixture"]["date"]).tz_convert(None),
                          "local": f["teams"]["home"]["name"], "visitante": f["teams"]["away"]["name"],
                          "gl": f["goals"]["home"] if jugado else np.nan,
                          "gv": f["goals"]["away"] if jugado else np.nan,
                          "temporada": t, "fid": int(f["fixture"]["id"]), "inicio": f["fixture"]["date"]})
    if not filas:
        raise FuenteNoDisponible(f"API-Football liga {liga_id}: sin datos")
    return _limpiar(pd.DataFrame(filas))


def estado_api() -> dict:
    """Plan, cuota del día y si la temporada en curso está disponible (1-2 solicitudes)."""
    st = _api("/status", "apif_status.json", 0)
    r = st.get("response") or {}
    out = {"cuenta": (r.get("account") or {}).get("email"),
           "plan": (r.get("subscription") or {}).get("plan"),
           "vence": (r.get("subscription") or {}).get("end"),
           "usadas_hoy": (r.get("requests") or {}).get("current"),
           "limite_dia": (r.get("requests") or {}).get("limit_day")}
    return out


def probar_temporada(liga_id: int, temporada: int) -> tuple[bool, str]:
    try:
        data = _api(f"/fixtures?league={liga_id}&season={temporada}", f"apif_{liga_id}_{temporada}.json", 0)
    except FuenteNoDisponible as e:
        return False, str(e)
    n = len(data.get("response", []))
    jugados = sum(1 for f in data["response"] if f["fixture"]["status"]["short"] in TERMINADO)
    return n > 0, f"{n} partidos, {jugados} con resultado"


def buscar_ligas(pais: str) -> list[dict]:
    data = _api(f"/leagues?country={pais}", f"apif_ligas_todas_{pais}.json", 24)
    out = []
    for r in data.get("response", []):
        temps = [s["year"] for s in r.get("seasons", [])]
        out.append({"id": r["league"]["id"], "nombre": r["league"]["name"], "tipo": r["league"]["type"],
                    "temporadas": temps[-4:]})
    return out


# --------------------------------------------------------------------------- #
# CSV propio: fecha,local,visitante,goles_local,goles_visitante
# Deja los goles vacíos para partidos por jugar.
# --------------------------------------------------------------------------- #
def csv_local(ruta: str) -> pd.DataFrame:
    p = Path(ruta)
    if not p.is_absolute():
        p = RAIZ / p
    if not p.exists():
        raise FuenteNoDisponible(f"CSV {p} no existe")
    raw = pd.read_csv(p, comment="#")
    if raw.empty:
        raise FuenteNoDisponible(f"CSV {p} está vacío")
    df = raw.rename(columns={"goles_local": "gl", "goles_visitante": "gv"})
    if "temporada" not in df:
        df["temporada"] = pd.to_datetime(df["fecha"]).dt.year
    return _limpiar(df)


# --------------------------------------------------------------------------- #
def temporadas_liga(cfg: dict, historial: int) -> list[int]:
    """Temporadas a descargar: la actual y `historial` anteriores. Con
    temporada_actual = "auto" se pregunta a API-Football cuál está vigente."""
    actual = cfg.get("temporada_actual", "auto")
    if str(actual).lower() == "auto":
        actual = None
        if "apifootball" in cfg.get("fuentes", []):
            try:
                _, actual = resolver_apifootball(cfg)
            except FuenteNoDisponible as e:
                print(f"  · aviso: {e}")
        if not actual:   # sin API: deducir por la fecha
            hoy = date.today()
            europeo = cfg.get("formato_temporada", "europeo") == "europeo"
            actual = hoy.year - 1 if europeo and hoy.month < 7 else hoy.year
    actual = int(actual)
    return list(range(actual - historial, actual + 1))


def cargar_liga(cfg: dict, temporadas: list[int]) -> tuple[pd.DataFrame, str]:
    """Prueba las fuentes de la liga en orden y devuelve la primera que funcione."""
    errores = []
    for fuente in cfg.get("fuentes", []):
        try:
            if fuente == "footballdata":
                df = footballdata(cfg["footballdata"], temporadas)
            elif fuente == "openfootball":
                df = openfootball(cfg["openfootball"], temporadas)
            elif fuente == "apifootball":
                liga_id, _ = resolver_apifootball(cfg)
                cfg["_apifootball_id"] = liga_id
                df = apifootball(liga_id, temporadas, float(cfg.get("cache_horas", 1.0)))
            elif fuente == "csv":
                df = csv_local(cfg["csv"])
            else:
                raise FuenteNoDisponible(f"fuente desconocida: {fuente}")
            df = aplicar_alias(df, cfg.get("alias", {}))
            return df, fuente
        except FuenteNoDisponible as e:
            errores.append(str(e))
    raise FuenteNoDisponible(" | ".join(errores) or "sin fuentes configuradas")


_SUFIJOS = re.compile(r"\s+(FC|CF|AFC|SC|CD|UD|SAD)$|^(AFC|FC|CF|CD|UD|RC|RCD|SD|CA)\s+")


def nombre_corto(n: str) -> str:
    """'Liverpool FC' → 'Liverpool', 'RC Celta de Vigo' → 'Celta de Vigo'."""
    anterior = None
    while anterior != n:
        anterior, n = n, _SUFIJOS.sub("", n).strip()
    return n


def aplicar_alias(df: pd.DataFrame, alias: dict) -> pd.DataFrame:
    f = lambda n: alias.get(n) or alias.get(nombre_corto(n)) or nombre_corto(n)
    df = df.copy()
    df["local"] = df["local"].map(f)
    df["visitante"] = df["visitante"].map(f)
    return df
