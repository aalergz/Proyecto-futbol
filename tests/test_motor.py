"""Pruebas: python -m pytest -q"""
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from motor import fuentes
from motor.modelo import ajustar, matriz, mercados, predecir


def liga_sintetica(n=16, temporadas=2, seed=1, rho=-0.08):
    """Genera una liga con fuerzas conocidas para comprobar que el ajuste las recupera."""
    rng = np.random.default_rng(seed)
    equipos = [f"E{i:02d}" for i in range(n)]
    a = rng.normal(0, 0.3, n); a -= a.mean()
    d = rng.normal(0, 0.25, n); d -= d.mean()
    c, h = np.log(1.15), np.log(1.30)
    filas, f = [], date(2024, 8, 10)
    for _ in range(temporadas):
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                lh, la = np.exp(c + h + a[i] + d[j]), np.exp(c + a[j] + d[i])
                M = matriz(lh, la, rho)
                k = rng.choice(M.size, p=M.ravel())
                filas.append({"fecha": pd.Timestamp(f), "local": equipos[i], "visitante": equipos[j],
                              "gl": k // M.shape[1], "gv": k % M.shape[1]})
                f += timedelta(hours=11)
    return pd.DataFrame(filas), equipos, a, d, c, h


def test_matriz_suma_uno_y_mercados_coherentes():
    M = matriz(1.6, 1.1, -0.08)
    assert abs(M.sum() - 1) < 1e-12
    m = mercados(M)
    assert abs(m["p1"] + m["px"] + m["p2"] - 1) < 1e-12
    assert m["p1"] > m["p2"]


def test_ajuste_recupera_fuerzas_conocidas():
    df, equipos, a, d, c, h = liga_sintetica()
    r = ajustar(df, xi=0.0, reg=0.0)
    a_fit = np.array([r.ataque[r.idx[e]] for e in equipos])
    d_fit = np.array([r.defensa[r.idx[e]] for e in equipos])
    assert np.corrcoef(a, a_fit)[0, 1] > 0.85
    assert np.corrcoef(d, d_fit)[0, 1] > 0.80
    assert abs(r.media_local - np.exp(c + h)) < 0.15
    assert abs(r.media_visitante - np.exp(c)) < 0.12


def test_gradiente_analitico():
    from scipy.optimize import check_grad
    import motor.modelo as mm
    df, *_ = liga_sintetica(n=8, temporadas=1)
    capturado = {}
    real = mm.minimize

    def espia(fun, x0, **kw):
        capturado["f"] = fun
        capturado["x"] = x0 + np.random.default_rng(0).normal(0, 0.1, len(x0))
        return real(fun, x0, **kw)

    mm.minimize = espia
    try:
        ajustar(df, xi=0.001, reg=0.05)
    finally:
        mm.minimize = real
    f = capturado["f"]
    err = check_grad(lambda t: f(t)[0], lambda t: f(t)[1], capturado["x"])
    assert err < 1e-5


def test_regularizacion_frena_a_un_recien_llegado():
    df, *_ = liga_sintetica(n=10, temporadas=1)
    extra = pd.DataFrame([{"fecha": df["fecha"].max() + pd.Timedelta(days=1), "local": "Nuevo",
                           "visitante": "E00", "gl": 5, "gv": 0}])
    libre = ajustar(pd.concat([df, extra]), xi=0, reg=0.0)
    frenado = ajustar(pd.concat([df, extra]), xi=0, reg=0.5)
    assert frenado.ataque[frenado.idx["Nuevo"]] < libre.ataque[libre.idx["Nuevo"]]


def test_nombre_corto():
    assert fuentes.nombre_corto("Liverpool FC") == "Liverpool"
    assert fuentes.nombre_corto("AFC Bournemouth") == "Bournemouth"
    assert fuentes.nombre_corto("RC Celta de Vigo") == "Celta de Vigo"


def test_csv_football_data(tmp_path, monkeypatch):
    texto = ("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA\n"
             "E0,15/08/2025,20:00,Liverpool,Bournemouth,4,2,H,1.30,6.00,9.50\n"
             "E0,16/08/2025,12:30,Aston Villa,Newcastle,0,0,D,2.40,3.40,2.90\n")
    df = fuentes._leer_csv_fd(texto, 2025)
    assert list(df["gl"]) == [4, 0] and df["c1"].iloc[0] == 1.30
    assert df["fecha"].iloc[0] == pd.Timestamp("2025-08-15")


def test_csv_local(tmp_path):
    p = tmp_path / "crc.csv"
    p.write_text("fecha,local,visitante,goles_local,goles_visitante\n"
                 "2026-07-20,Saprissa,Herediano,2,1\n2026-12-01,Alajuelense,Saprissa,,\n")
    df = fuentes.csv_local(str(p))
    assert df["gl"].isna().sum() == 1 and df["gl"].iloc[0] == 2
