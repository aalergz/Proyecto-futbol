"""Convierte un ajuste en el bloque de liga que lee la Pizarra (ratings.json)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from .modelo import Ratings, predecir
from .selecciones import proximas


def tabla_actual(temp: pd.DataFrame) -> dict[str, dict]:
    jug = temp.dropna(subset=["gl", "gv"])
    t: dict[str, dict] = {}
    for e in sorted(set(temp["local"]) | set(temp["visitante"])):
        t[e] = {"pj": 0, "pts": 0, "gf": 0, "gc": 0}
    for _, m in jug.iterrows():
        a, b, x, y = m["local"], m["visitante"], int(m["gl"]), int(m["gv"])
        for e, f, c in ((a, x, y), (b, y, x)):
            t[e]["pj"] += 1; t[e]["gf"] += f; t[e]["gc"] += c
            t[e]["pts"] += 3 if f > c else 1 if f == c else 0
    return t


def estadisticas_arbitros(df: pd.DataFrame, modelo_tarjetas: Ratings | None) -> dict:
    """Historial de cada árbitro: tarjetas por partido y las que se esperaban por los
    equipos que le tocaron (según el modelo de tarjetas). Es informativo: en la
    validación, usar el árbitro empeoró la predicción, así que no la modifica."""
    if "arbitro" not in df or modelo_tarjetas is None:
        return {}
    jug = df.dropna(subset=["tjl", "tjv"])
    jug = jug[jug["arbitro"].notna()]
    if jug.empty:
        return {}
    filas = []
    for m in jug.itertuples():
        esp = np.nan
        if m.local in modelo_tarjetas.idx and m.visitante in modelo_tarjetas.idx:
            esp = sum(modelo_tarjetas.lambdas(m.local, m.visitante))
        filas.append((str(m.arbitro), m.tjl + m.tjv, esp))
    t = pd.DataFrame(filas, columns=["arbitro", "real", "esperado"])
    g = t.groupby("arbitro").agg(partidos=("real", "size"), tarjetas=("real", "mean"), esperadas=("esperado", "mean"))
    g = g[g["partidos"] >= 10]   # con menos partidos el promedio es casi puro azar
    return {a: {"partidos": int(v.partidos), "tarjetas": round(float(v.tarjetas), 2),
                "esperadas": round(float(v.esperadas), 2) if v.esperadas == v.esperadas else None}
            for a, v in g.iterrows()}


def bloque_liga(clave: str, cfg: dict, df: pd.DataFrame, r: Ratings, fuente: str,
                validacion: dict | None, hoy: date | None = None, conteos: dict | None = None,
                historial_sel: dict | None = None) -> dict:
    conteos = conteos or {}
    hoy = pd.Timestamp(hoy or date.today())
    temporada = cfg.get("temporada_actual") or int(df["temporada"].max())
    temp = df[df["temporada"] == temporada]
    if temp.empty:
        temporada = int(df["temporada"].max())
        temp = df[df["temporada"] == temporada]
    equipos_temp = sorted(set(temp["local"]) | set(temp["visitante"]))

    # Equipos sin ningún partido en la historia (ascendidos sin datos):
    # se les asigna el promedio de los tres peores para no inventar fuerza.
    sin_datos = [e for e in equipos_temp if e not in r.idx]
    if sin_datos:
        net = r.ataque - r.defensa
        peores = np.argsort(net)[:3]
        a0, d0 = float(r.ataque[peores].mean()), float(r.defensa[peores].mean())
        for e in sin_datos:
            r.equipos.append(e)
            r.ataque = np.append(r.ataque, a0)
            r.defensa = np.append(r.defensa, d0)
            r.partidos[e] = 0
            r.local = np.append(r.local, 0.0)
        r.idx = {e: i for i, e in enumerate(r.equipos)}

    equipos = []
    for e in equipos_temp:
        at, de = r.multiplicadores(e)
        fila = {"nombre": e, "ataque": round(at, 4), "defensa": round(de, 4),
                "local": round(float(np.exp(r.local[r.idx[e]])), 4),
                "partidos": r.partidos.get(e, 0), "sin_datos": e in sin_datos}
        for nombre, c in conteos.items():
            if e in c.idx:   # a favor / en contra, como multiplicadores
                fila[nombre] = [round(v, 4) for v in c.multiplicadores(e)]
        equipos.append(fila)
    equipos.sort(key=lambda x: x["ataque"] - x["defensa"], reverse=True)

    tabla = tabla_actual(temp)
    pend = temp[temp["gl"].isna()].sort_values("fecha")
    restantes = [[m.local, m.visitante] for m in pend.itertuples()]

    proximos = []
    futuros = pend[pend["fecha"] >= hoy]
    lista = futuros if not futuros.empty else pend
    for m in lista.head(30).itertuples():
        p = predecir(r, m.local, m.visitante)
        extra = {}
        for nombre, c in conteos.items():
            if m.local in c.idx and m.visitante in c.idx:
                extra[nombre] = [round(v, 2) for v in c.lambdas(m.local, m.visitante)]
        fid = getattr(m, "fid", None)
        if fid is not None and fid == fid:   # fid == fid descarta NaN
            extra["fid"] = int(fid)
        if isinstance(getattr(m, "inicio", None), str):
            extra["inicio"] = m.inicio
        if isinstance(getattr(m, "arbitro", None), str) and m.arbitro.strip():
            extra["arbitro"] = m.arbitro.strip()
        cuotas = {k: round(float(getattr(m, k)), 2) for k in ("c1", "cx", "c2", "co25", "cu25")
                  if getattr(m, k, None) is not None and getattr(m, k) == getattr(m, k) and getattr(m, k) > 1}
        if cuotas:
            extra["cuotas"] = cuotas
        proximos.append({
            "fecha": m.fecha.strftime("%Y-%m-%d"), "local": m.local, "visitante": m.visitante,
            "pendiente": bool(m.fecha < hoy), **extra,
            **{k: round(v, 4) if isinstance(v, float) else v for k, v in p.items()},
        })

    jugados_temp = temp.dropna(subset=["gl", "gv"])
    return {
        "nombre": cfg["nombre"],
        "temporada": f"{temporada}-{str(temporada + 1)[-2:]}" if cfg.get("formato_temporada", "europeo") == "europeo" else str(temporada),
        "fuente": fuente,
        "home": round(r.media_local, 4),
        "away": round(r.media_visitante, 4),
        "rho": round(r.rho, 4),
        "conteos": {n: {"home": round(c.media_local, 4), "away": round(c.media_visitante, 4)} for n, c in conteos.items()},
        "arbitros": estadisticas_arbitros(df, conteos.get("tarjetas")),
        "selecciones": {**proximas(proximos), "historial": historial_sel or {}},
        "top": cfg.get("top", {"n": 4, "label": "Top 4"}),
        "bot": cfg.get("bot", {"n": 3, "label": "Descenso"}),
        "partidos_modelo": r.n_partidos,
        "ultimo_resultado": jugados_temp["fecha"].max().strftime("%Y-%m-%d") if not jugados_temp.empty else None,
        "equipos": equipos,
        "tabla": tabla,
        "restantes": restantes,
        "proximos": proximos,
        "validacion": validacion,
        "apifootball_id": cfg.get("_apifootball_id") or (int(cfg["apifootball"]) if str(cfg.get("apifootball", "")).isdigit() else None),
        "alias": cfg.get("alias", {}),
    }


def envoltorio(ligas: dict, parametros: dict, en_vivo_url: str = "") -> dict:
    return {
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modelo": "Dixon-Coles con decaimiento temporal",
        "parametros": parametros,
        "en_vivo_url": en_vivo_url,
        "ligas": ligas,
    }
