"""Pruebas de la fuente API-Football con respuestas simuladas (sin red)."""
import io
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from motor import fuentes
from motor.exportar import bloque_liga
from motor.modelo import ajustar

EQUIPOS = ["Saprissa", "Alajuelense", "Herediano", "Cartaginés", "San Carlos", "Pérez Zeledón"]


def _fixtures(temporada, jugados=True):
    base = datetime(temporada, 7, 15, 20, 0, tzinfo=timezone.utc)
    out, fid = [], temporada * 1000
    for k, (a, b) in enumerate(itertools.permutations(EQUIPOS, 2)):
        fid += 1
        hecho = jugados or k < 12
        out.append({
            "fixture": {"id": fid, "date": (base + timedelta(days=3 * k)).isoformat(),
                        "status": {"short": "FT" if hecho else "NS"}},
            "teams": {"home": {"name": a}, "away": {"name": b}},
            "goals": {"home": (k * 7) % 4 if hecho else None, "away": (k * 3) % 3 if hecho else None},
        })
    out.append({"fixture": {"id": 1, "date": base.isoformat(), "status": {"short": "CANC"}},
                "teams": {"home": {"name": "Saprissa"}, "away": {"name": "Herediano"}},
                "goals": {"home": None, "away": None}})
    return out


class Resp(io.BytesIO):
    def __init__(self, data, restantes="97"):
        super().__init__(json.dumps(data).encode())
        self.headers = {"x-ratelimit-requests-remaining": restantes}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(fuentes, "CACHE", tmp_path)
    monkeypatch.setenv("API_FOOTBALL_KEY", "prueba")
    llamadas = []

    def urlopen(req, timeout=30):
        url = req.full_url
        llamadas.append(url)
        assert req.headers.get("X-apisports-key") == "prueba"
        if "/leagues?country=Costa-Rica" in url:
            return Resp({"errors": [], "response": [
                {"league": {"id": 7, "name": "Copa"}, "seasons": [{"year": 2026, "current": True}]},
                {"league": {"id": 162, "name": "Primera División"},
                 "seasons": [{"year": 2025, "current": False}, {"year": 2026, "current": True}]}]})
        if "season=2025" in url:
            return Resp({"errors": [], "response": _fixtures(2025)})
        if "season=2026" in url:
            return Resp({"errors": [], "response": _fixtures(2026, jugados=False)})
        return Resp({"errors": {"plan": "sin acceso"}, "response": []})

    monkeypatch.setattr(fuentes.urllib.request, "urlopen", urlopen)
    fuentes.CUOTA.update(usadas=0, restantes=None)
    return llamadas


CFG = {"nombre": "Liga Promerica", "fuentes": ["apifootball"], "apifootball": "auto",
       "apifootball_pais": "Costa-Rica", "apifootball_nombre": "Primera Division",
       "temporada_actual": "auto", "formato_temporada": "anual"}


def test_detecta_liga_y_temporada(api):
    assert fuentes.resolver_apifootball(dict(CFG)) == (162, 2026)
    assert fuentes.temporadas_liga(dict(CFG), 1) == [2025, 2026]


def test_carga_partidos_y_exporta_ids(api):
    cfg = dict(CFG)
    df, fuente = fuentes.cargar_liga(cfg, [2025, 2026])
    assert fuente == "apifootball" and cfg["_apifootball_id"] == 162
    assert len(df) == 60                                  # el partido cancelado se descarta
    assert df["gl"].notna().sum() == 42 and "fid" in df
    r = ajustar(df.dropna(subset=["gl", "gv"]), xi=0.002, reg=0.1)
    b = bloque_liga("crc", {**cfg, "temporada_actual": 2026}, df, r, fuente, None, hoy=datetime(2026, 1, 1).date())
    assert b["apifootball_id"] == 162
    assert b["proximos"] and "fid" in b["proximos"][0] and "inicio" in b["proximos"][0]
    assert fuentes.CUOTA["usadas"] == 3 and fuentes.CUOTA["restantes"] == 97


def test_cache_evita_llamadas_repetidas(api):
    fuentes.cargar_liga(dict(CFG), [2025, 2026])
    n = len(api)
    fuentes.cargar_liga(dict(CFG), [2025, 2026])
    assert len(api) == n


def test_error_de_la_api_no_queda_en_cache(api, tmp_path):
    with pytest.raises(fuentes.FuenteNoDisponible):
        fuentes._api("/otra", "otra.json", 1)
    assert not (tmp_path / "otra.json").exists()


def test_sin_temporada_actual_pasa_a_otra_fuente(api, monkeypatch, tmp_path):
    real = fuentes.urllib.request.urlopen

    def urlopen(req, timeout=30):
        if "season=2026" in req.full_url:
            return Resp({"errors": {"plan": "Free plans do not have access to this season"}, "response": []})
        return real(req, timeout)

    monkeypatch.setattr(fuentes.urllib.request, "urlopen", urlopen)
    csv = tmp_path / "crc.csv"
    csv.write_text("fecha,local,visitante,goles_local,goles_visitante\n2026-07-20,Saprissa,Herediano,2,1\n")
    cfg = {**CFG, "fuentes": ["apifootball", "csv"], "csv": str(csv)}
    df, fuente = fuentes.cargar_liga(cfg, [2025, 2026])
    assert fuente == "csv"


def test_completar_calendario_con_openfootball(monkeypatch):
    import pandas as pd
    from datetime import date
    hoy = pd.Timestamp(date.today())
    # football-data: nombres cortos, 2 partidos jugados y 1 programado
    fd = pd.DataFrame({"fecha": [hoy - pd.Timedelta(days=9), hoy - pd.Timedelta(days=2), hoy + pd.Timedelta(days=2)],
                       "local": ["Leeds", "Hull", "Arsenal"], "visitante": ["Arsenal", "Leeds", "Hull"],
                       "gl": [1, 0, None], "gv": [2, 0, None], "temporada": 2026})
    # openfootball: nombres largos, calendario completo, atrasado en resultados
    pares = [("Leeds United", "Arsenal"), ("Hull City", "Leeds United"), ("Arsenal", "Hull City"),
             ("Arsenal", "Leeds United"), ("Leeds United", "Hull City"), ("Hull City", "Arsenal")]
    of = pd.DataFrame({"fecha": [hoy - pd.Timedelta(days=9 - 3 * i) for i in range(6)],
                       "local": [a for a, _ in pares], "visitante": [b for _, b in pares],
                       "gl": [1] + [None] * 5, "gv": [2] + [None] * 5, "temporada": 2026})
    monkeypatch.setattr(fuentes, "openfootball", lambda codigo, temps: of)
    out = fuentes.completar_calendario(fd, {"openfootball": "en.1"}, [2025, 2026], "footballdata")
    pend = out[out["gl"].isna()]
    assert len(out) == 6 and len(pend) == 4
    assert set(pend["local"]) <= {"Arsenal", "Leeds", "Hull"}
    assert (pend["fecha"] >= hoy).all()


def test_bom_en_fixtures():
    texto = "ï»¿Div,Date,Time,HomeTeam,AwayTeam\nE0,25/10/2026,15:00,Arsenal,Chelsea\nSP1,25/10/2026,16:15,Barcelona,Real Madrid\n"
    df = fuentes._leer_csv_fd(texto, 2026, div="E0")
    assert list(df["local"]) == ["Arsenal"]


def test_bloqueo_de_plan_se_recuerda(api, monkeypatch):
    def urlopen(req, timeout=30):
        api.append(req.full_url)
        return Resp({"errors": {"plan": "Free plans do not have access to this season, try from 2022 to 2024."}, "response": []})
    monkeypatch.setattr(fuentes.urllib.request, "urlopen", urlopen)
    with pytest.raises(fuentes.FuenteNoDisponible):
        fuentes.apifootball(39, [2025, 2026])
    n = len(api)
    assert n == 1                      # pidió solo la temporada actual
    with pytest.raises(fuentes.FuenteNoDisponible, match="24 h"):
        fuentes.apifootball(39, [2025, 2026])
    assert len(api) == n               # la segunda vez no gastó solicitudes
