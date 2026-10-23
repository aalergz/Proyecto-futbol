import pandas as pd
from motor import selecciones as sel

P = {"p1": 0.62, "px": 0.22, "p2": 0.16, "o25": 0.58}
C = {"c1": 1.70, "cx": 3.80, "c2": 5.20, "co25": 1.80, "cu25": 2.00}


def test_candidatos_y_probabilidad_de_mercado():
    cs = {c["mercado"]: c for c in sel.candidatos(P, C)}
    assert set(cs) == {"1", "X", "2", "1X", "X2", "12", "o25", "u25"}
    q = cs["1"]["prob_mercado"] + cs["X"]["prob_mercado"] + cs["2"]["prob_mercado"]
    assert abs(q - 1) < 1e-9                                  # sin margen
    assert abs(cs["1X"]["cuota"] - round(1 / (1 / 1.70 + 1 / 3.80), 2)) < 1e-9
    assert abs(cs["o25"]["ve"] - (0.58 * 1.80 - 1)) < 1e-9


def test_segura_usa_consenso_y_cuota_minima():
    s = sel.segura(sel.candidatos(P, C))
    assert s["cuota"] >= sel.CUOTA_MIN_SEGURA
    assert s["mercado"] == "1"          # 1X tiene más prob. pero paga menos de 1.40


def test_historial_y_combinadas():
    filas = []
    for i in range(12):
        filas.append({"fecha": pd.Timestamp("2026-01-05") + pd.Timedelta(days=i // 3 * 7 + i % 3),
                      "gl": 2 if i % 4 else 0, "gv": 0 if i % 4 else 1, **P, **C})
    h = sel.historial(pd.DataFrame(filas))
    assert h["seguras"]["apuestas"] == 12 and abs(h["seguras"]["aciertos"] - 0.75) < 1e-9
    assert h["doble"]["apuestas"] == 4 and h["triple"]["apuestas"] == 4
    assert h["seguras"]["roi_min"] <= h["seguras"]["roi"] <= h["seguras"]["roi_max"]
