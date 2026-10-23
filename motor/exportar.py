"""Convierte un ajuste en el bloque de liga que lee la Pizarra (ratings.json)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from .modelo import Ratings, predecir


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


def bloque_liga(clave: str, cfg: dict, df: pd.DataFrame, r: Ratings, fuente: str,
                validacion: dict | None, hoy: date | None = None) -> dict:
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
        r.idx = {e: i for i, e in enumerate(r.equipos)}

    equipos = []
    for e in equipos_temp:
        at, de = r.multiplicadores(e)
        equipos.append({"nombre": e, "ataque": round(at, 4), "defensa": round(de, 4),
                        "partidos": r.partidos.get(e, 0), "sin_datos": e in sin_datos})
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
        fid = getattr(m, "fid", None)
        if fid is not None and fid == fid:   # fid == fid descarta NaN
            extra["fid"] = int(fid)
        if isinstance(getattr(m, "inicio", None), str):
            extra["inicio"] = m.inicio
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
