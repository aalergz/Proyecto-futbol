"""Validación fuera de muestra (walk-forward).

Cada semana se reajusta el modelo solo con partidos anteriores a esa semana y
se predicen los de la semana. Nunca se mira un resultado antes de predecirlo.
Se compara contra una línea base que usa solo las frecuencias históricas de la
liga (sin distinguir equipos) y, si la fuente trae cuotas, contra el mercado.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from .modelo import ajustar, predecir


def _brier_1x2(p: np.ndarray, res: np.ndarray) -> float:
    onehot = np.eye(3)[res]
    return float(((p - onehot) ** 2).sum(axis=1).mean())


def _logloss_1x2(p: np.ndarray, res: np.ndarray) -> float:
    return float(-np.log(np.clip(p[np.arange(len(res)), res], 1e-12, 1)).mean())


def _brier_bin(p: np.ndarray, y: np.ndarray) -> float:
    return float(((p - y) ** 2).mean())


def validar(df: pd.DataFrame, desde, xi=0.0019, reg=0.02, paso_dias=7, min_partidos_equipo=3,
            umbral_ve=0.05, **opciones) -> dict:
    """opciones: se pasan a ajustar (peso_goles, reg_local)."""
    jug = df.dropna(subset=["gl", "gv"]).copy()
    jug["fecha"] = pd.to_datetime(jug["fecha"])
    inicio = pd.Timestamp(desde)
    fin = jug["fecha"].max()
    filas, prev = [], None
    t = inicio
    while t <= fin:
        tr = jug[jug["fecha"] < t]
        te = jug[(jug["fecha"] >= t) & (jug["fecha"] < t + timedelta(days=paso_dias))]
        t += timedelta(days=paso_dias)
        if te.empty or len(tr) < 100:
            continue
        r = ajustar(tr, fecha_ref=(t - timedelta(days=paso_dias)).date(), xi=xi, reg=reg, inicial=prev, **opciones)
        prev = r
        base = tr[tr["fecha"] >= tr["fecha"].max() - timedelta(days=365)]
        b1 = (base["gl"] > base["gv"]).mean(); bx = (base["gl"] == base["gv"]).mean()
        b25 = ((base["gl"] + base["gv"]) > 2.5).mean(); bb = ((base["gl"] > 0) & (base["gv"] > 0)).mean()
        for _, m in te.iterrows():
            if r.partidos.get(m["local"], 0) < min_partidos_equipo or \
               r.partidos.get(m["visitante"], 0) < min_partidos_equipo:
                continue
            p = predecir(r, m["local"], m["visitante"])
            fila = {"fecha": m["fecha"].date(), "local": m["local"], "visitante": m["visitante"],
                    "gl": int(m["gl"]), "gv": int(m["gv"]), **{k: p[k] for k in ("lh", "la", "p1", "px", "p2", "o25", "btts")},
                    "b1": b1, "bx": bx, "b2": 1 - b1 - bx, "b25": b25, "bbtts": bb}
            for c in ("c1", "cx", "c2", "co25", "cu25"):
                if c in m:
                    fila[c] = m[c]
            filas.append(fila)

    if not filas:
        return {"resumen": {"partidos": 0}, "detalle": pd.DataFrame()}
    P = pd.DataFrame(filas)
    res = np.where(P["gl"] > P["gv"], 0, np.where(P["gl"] == P["gv"], 1, 2))
    y25 = ((P["gl"] + P["gv"]) > 2.5).to_numpy(float)
    ybt = ((P["gl"] > 0) & (P["gv"] > 0)).to_numpy(float)
    pm = P[["p1", "px", "p2"]].to_numpy()
    pb = P[["b1", "bx", "b2"]].to_numpy()

    out = {
        "partidos": int(len(P)),
        "desde": str(P["fecha"].min()), "hasta": str(P["fecha"].max()),
        "mercados": {
            "1X2": {"modelo": _brier_1x2(pm, res), "base": _brier_1x2(pb, res),
                    "logloss_modelo": _logloss_1x2(pm, res), "logloss_base": _logloss_1x2(pb, res)},
            "Más de 2.5": {"modelo": _brier_bin(P["o25"].to_numpy(), y25), "base": _brier_bin(P["b25"].to_numpy(), y25)},
            "Ambos anotan": {"modelo": _brier_bin(P["btts"].to_numpy(), ybt), "base": _brier_bin(P["bbtts"].to_numpy(), ybt)},
        },
        "acierto_1x2": float((pm.argmax(axis=1) == res).mean()),
    }
    for k, v in out["mercados"].items():
        v["mejora"] = 1 - v["modelo"] / v["base"]

    # Contra el mercado y apuestas simuladas, si hay cuotas
    if {"c1", "cx", "c2"} <= set(P.columns):
        Q = P.dropna(subset=["c1", "cx", "c2"])
        if len(Q) >= 30:
            cuotas = Q[["c1", "cx", "c2"]].to_numpy()
            imp = 1 / cuotas
            nv = imp / imp.sum(axis=1, keepdims=True)
            rq = res[Q.index]
            pq = Q[["p1", "px", "p2"]].to_numpy()
            out["mercados"]["1X2"]["mercado"] = _brier_1x2(nv, rq)
            ve = pq * cuotas - 1
            k = ve.argmax(axis=1)
            elegido = ve[np.arange(len(Q)), k] > umbral_ve
            gan = np.where(k[elegido] == rq[elegido], cuotas[np.arange(len(Q)), k][elegido] - 1, -1)
            out["apuestas_simuladas"] = {
                "umbral_ve": umbral_ve, "apuestas": int(elegido.sum()),
                "roi": float(gan.mean()) if len(gan) else 0.0,
                "nota": "Apuesta plana de 1 unidad al resultado 1X2 con mayor VE si supera el umbral, a cuota de cierre.",
            }
    return {"resumen": out, "detalle": P}


# --------------------------------------------------------------------------- #
# Córners y tarjetas
# --------------------------------------------------------------------------- #
def validar_conteo(df: pd.DataFrame, desde, columnas, linea, xi=0.0019, reg=0.1, paso_dias=7,
                   reg_arbitro=None) -> dict:
    """Valida el modelo de un conteo (córners o tarjetas) en la línea dada:
    P(total > linea) del modelo frente a la frecuencia de la liga en el último año."""
    from scipy.stats import poisson
    jug = df.dropna(subset=list(columnas)).copy()
    jug["fecha"] = pd.to_datetime(jug["fecha"])
    t, fin, filas, prev = pd.Timestamp(desde), jug["fecha"].max(), [], None
    while t <= fin:
        tr = jug[jug["fecha"] < t]
        te = jug[(jug["fecha"] >= t) & (jug["fecha"] < t + timedelta(days=paso_dias))]
        t += timedelta(days=paso_dias)
        if te.empty or len(tr) < 100:
            continue
        r = ajustar(tr, fecha_ref=(t - timedelta(days=paso_dias)).date(), xi=xi, reg=reg, inicial=prev,
                    columnas=tuple(columnas), dixon_coles=False, reg_arbitro=reg_arbitro)
        prev = r
        base = tr[tr["fecha"] >= tr["fecha"].max() - timedelta(days=365)]
        pb = float(((base[columnas[0]] + base[columnas[1]]) > linea).mean())
        for _, m in te.iterrows():
            if m["local"] not in r.idx or m["visitante"] not in r.idx:
                continue
            arb = m.get("arbitro") if reg_arbitro is not None else None
            lh, la = r.lambdas(m["local"], m["visitante"], arb if isinstance(arb, str) else None)
            filas.append({"p": float(poisson.sf(np.floor(linea), lh + la)), "b": pb,
                          "y": float(m[columnas[0]] + m[columnas[1]] > linea)})
    if not filas:
        return {"partidos": 0}
    P = pd.DataFrame(filas)
    out = {"partidos": int(len(P)), "linea": linea,
           "modelo": _brier_bin(P["p"].to_numpy(), P["y"].to_numpy()),
           "base": _brier_bin(P["b"].to_numpy(), P["y"].to_numpy()),
           "prob_media": float(P["p"].mean()), "frecuencia_real": float(P["y"].mean())}
    out["mejora"] = 1 - out["modelo"] / out["base"]
    return out
