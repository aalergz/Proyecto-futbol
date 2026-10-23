"""Selecciones del motor (simples y combinadas) y su historial.

Las mismas reglas se aplican a los próximos partidos y, hacia atrás, a todos los
partidos de la validación, para mostrar qué habría pasado de seguirlas: aciertos y
rendimiento con las cuotas reales. Las reglas no se ajustaron para que ese
historial salga bien.

Mercados: 1, X, 2, doble oportunidad (1X, X2, 12), más y menos de 2.5 goles.
La cuota de la doble oportunidad se deriva de las cuotas 1X2 de la casa:
1 / (1/c1 + 1/cx), que conserva su margen.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

CUOTA_MIN_SEGURA = 1.40     # por debajo, ganar paga demasiado poco
PROB_MIN_SEGURA = 0.55
# Niveles de las recomendaciones del día: (cuota mínima, probabilidad mínima de consenso).
# Elegidos con el historial de 2024-25 y 2025-26 en las cinco ligas.
NIVELES = {"normal": (1.40, 0.55), "conservador": (1.20, 0.70), "muy_conservador": (1.05, 0.80)}
VE_MIN = 0.05               # valor esperado mínimo para "el modelo ve valor"
PROB_MIN_VALOR = 0.20       # evita apuestas muy improbables


def _ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 1


def candidatos(p: dict, c: dict) -> list[dict]:
    """p: probabilidades del modelo (p1, px, p2, o25). c: cuotas (c1, cx, c2, co25, cu25)."""
    out = []
    c1, cx, c2 = c.get("c1"), c.get("cx"), c.get("c2")
    co, cu = c.get("co25"), c.get("cu25")
    # Probabilidad del mercado sin el margen de la casa
    pm = {}
    if _ok(c1) and _ok(cx) and _ok(c2):
        s1 = 1 / c1 + 1 / cx + 1 / c2
        q1, qx, q2 = 1 / c1 / s1, 1 / cx / s1, 1 / c2 / s1
        pm = {"1": q1, "X": qx, "2": q2, "1X": q1 + qx, "X2": q2 + qx, "12": q1 + q2}
    if _ok(co) and _ok(cu):
        so = 1 / co + 1 / cu
        pm.update({"o25": 1 / co / so, "u25": 1 / cu / so})

    def add(clave, etiqueta, prob, cuota):
        if _ok(cuota) and prob == prob:
            out.append({"mercado": clave, "etiqueta": etiqueta, "prob": float(prob), "cuota": round(float(cuota), 2),
                        "ve": float(prob * cuota - 1), "prob_mercado": pm.get(clave)})
    add("1", "Gana {local}", p["p1"], c1)
    add("X", "Empate", p["px"], cx)
    add("2", "Gana {visitante}", p["p2"], c2)
    if _ok(c1) and _ok(cx):
        add("1X", "{local} o empate", p["p1"] + p["px"], 1 / (1 / c1 + 1 / cx))
    if _ok(cx) and _ok(c2):
        add("X2", "{visitante} o empate", p["p2"] + p["px"], 1 / (1 / c2 + 1 / cx))
    if _ok(c1) and _ok(c2):
        add("12", "No hay empate", p["p1"] + p["p2"], 1 / (1 / c1 + 1 / c2))
    add("o25", "Más de 2.5 goles", p["o25"], c.get("co25"))
    add("u25", "Menos de 2.5 goles", 1 - p["o25"], c.get("cu25"))
    return out


def consenso(x: dict) -> float:
    """Promedio de la probabilidad del modelo y la del mercado (sin margen)."""
    return (x["prob"] + x["prob_mercado"]) / 2 if x.get("prob_mercado") is not None else x["prob"]


def segura(cands: list[dict], modo: str = "consenso", cuota_min: float | None = None,
           prob_min: float | None = None) -> dict | None:
    """La selección más probable en la que modelo y mercado coinciden."""
    clave = consenso if modo == "consenso" else (lambda x: x["prob"])
    cuota_min = CUOTA_MIN_SEGURA if cuota_min is None else cuota_min
    prob_min = PROB_MIN_SEGURA if prob_min is None else prob_min
    v = [x for x in cands if x["cuota"] >= cuota_min and clave(x) >= prob_min
         and (modo != "consenso" or x.get("prob_mercado") is not None)]
    return max(v, key=clave) if v else None


def con_valor(cands: list[dict]) -> dict | None:
    v = [x for x in cands if x["ve"] >= VE_MIN and x["prob"] >= PROB_MIN_VALOR]
    return max(v, key=lambda x: x["ve"]) if v else None


def combinar(selecciones: list[dict], n: int) -> dict | None:
    """Las n selecciones seguras más probables, de partidos distintos."""
    if len(selecciones) < n:
        return None
    patas = sorted(selecciones, key=lambda x: -consenso(x))[:n]
    prob = float(np.prod([x["prob"] for x in patas]))
    cuota = float(np.prod([x["cuota"] for x in patas]))
    return {"patas": patas, "prob": prob, "cuota": round(cuota, 2), "ve": prob * cuota - 1}


def gano(mercado: str, gl: int, gv: int) -> bool:
    return {"1": gl > gv, "X": gl == gv, "2": gl < gv, "1X": gl >= gv, "X2": gl <= gv, "12": gl != gv,
            "o25": gl + gv > 2, "u25": gl + gv < 3}[mercado]


def _resumen(apuestas: list[tuple[bool, float]]) -> dict:
    if not apuestas:
        return {"apuestas": 0}
    g = np.array([a for a, _ in apuestas]); c = np.array([b for _, b in apuestas])
    ganancia = np.where(g, c - 1, -1.0)
    roi = float(ganancia.mean())
    err = float(1.645 * ganancia.std(ddof=1) / np.sqrt(len(g))) if len(g) > 1 else float("nan")
    return {"apuestas": int(len(g)), "aciertos": float(g.mean()), "cuota_media": float(c.mean()),
            "roi": roi, "roi_min": roi - err, "roi_max": roi + err}   # intervalo del 90%


def historial(detalle: pd.DataFrame, modo: str = "consenso") -> dict:
    """Aplica las reglas a la validación (partidos predichos sin ver su resultado)."""
    if detalle is None or detalle.empty or "c1" not in detalle:
        return {}
    seg, val, dobles, triples = [], [], [], []
    d = detalle.copy()
    d["semana"] = pd.to_datetime(d["fecha"]).dt.to_period("W")
    for _, semana in d.groupby("semana"):
        segs = []
        for m in semana.itertuples():
            cs = candidatos({"p1": m.p1, "px": m.px, "p2": m.p2, "o25": m.o25},
                            {k: getattr(m, k, None) for k in ("c1", "cx", "c2", "co25", "cu25")})
            s, v = segura(cs, modo), con_valor(cs)
            if s:
                s = {**s, "ok": gano(s["mercado"], m.gl, m.gv)}
                seg.append((s["ok"], s["cuota"])); segs.append(s)
            if v:
                val.append((gano(v["mercado"], m.gl, m.gv), v["cuota"]))
        for n, lista in ((2, dobles), (3, triples)):
            cb = combinar(segs, n)
            if cb:
                lista.append((all(x["ok"] for x in cb["patas"]), cb["cuota"]))
    return {"seguras": _resumen(seg), "valor": _resumen(val), "doble": _resumen(dobles), "triple": _resumen(triples),
            "desde": str(d["fecha"].min()), "hasta": str(d["fecha"].max())}


def historial_dia(detalles: dict[str, pd.DataFrame]) -> dict:
    """Las reglas del día en cada nivel, más la de más valor del día."""
    out = {"niveles": {}}
    for nivel, (cmin, pmin) in NIVELES.items():
        h = _historial_dia(detalles, cmin, pmin)
        if not h:
            return {}
        out["niveles"][nivel] = {k: h[k] for k in ("mejor", "doble", "triple")}
        out["niveles"][nivel]["regla"] = {"cuota_min": cmin, "prob_min": pmin}
        if nivel == "normal":
            out.update({k: h[k] for k in ("valor", "dias", "ligas", "desde", "hasta")})
    return out


def _historial_dia(detalles: dict[str, pd.DataFrame], cuota_min: float, prob_min: float) -> dict:
    """Reglas "del día" con todas las ligas juntas: cada día, la selección segura
    más probable, la doble y la triple con las más probables, y la de más valor."""
    partes = [d.assign(liga=k) for k, d in detalles.items() if d is not None and not d.empty and "c1" in d]
    if not partes:
        return {}
    d = pd.concat(partes, ignore_index=True)
    res = {"mejor": [], "doble": [], "triple": [], "valor": []}
    dias = 0
    for _, dia in d.groupby(pd.to_datetime(d["fecha"]).dt.date):
        segs, vals = [], []
        for m in dia.itertuples():
            cs = candidatos({"p1": m.p1, "px": m.px, "p2": m.p2, "o25": m.o25},
                            {k: getattr(m, k, None) for k in ("c1", "cx", "c2", "co25", "cu25")})
            s, v = segura(cs, cuota_min=cuota_min, prob_min=prob_min), con_valor(cs)
            if s:
                segs.append({**s, "ok": gano(s["mercado"], m.gl, m.gv)})
            if v:
                vals.append({**v, "ok": gano(v["mercado"], m.gl, m.gv)})
        if not segs and not vals:
            continue
        dias += 1
        segs.sort(key=lambda x: -consenso(x))
        if segs:
            res["mejor"].append((segs[0]["ok"], segs[0]["cuota"]))
        for n, clave in ((2, "doble"), (3, "triple")):
            if len(segs) >= n:
                patas = segs[:n]
                res[clave].append((all(x["ok"] for x in patas), float(np.prod([x["cuota"] for x in patas]))))
        if vals:
            best = max(vals, key=lambda x: x["ve"])
            res["valor"].append((best["ok"], best["cuota"]))
    out = {k: _resumen(v) for k, v in res.items()}
    out.update({"dias": dias, "ligas": sorted(detalles), "desde": str(d["fecha"].min()), "hasta": str(d["fecha"].max())})
    return out


def proximas(proximos: list[dict], dias: int = 8) -> dict:
    """Selecciones para los próximos partidos que tienen cuotas."""
    con_cuota = [m for m in proximos if m.get("cuotas") and not m.get("pendiente")]
    if not con_cuota:
        return {"seguras": [], "valor": [], "combinadas": []}
    inicio = pd.Timestamp(con_cuota[0]["fecha"])
    con_cuota = [m for m in con_cuota if pd.Timestamp(m["fecha"]) <= inicio + pd.Timedelta(days=dias)]
    seguras, valor, partidos = [], [], []
    for m in con_cuota:
        cs = candidatos(m, m["cuotas"])
        base = {"fecha": m["fecha"], "local": m["local"], "visitante": m["visitante"]}
        if m.get("inicio"):
            base["inicio"] = m["inicio"]
        # Todos los mercados del partido, para que la web aplique cada nivel del día
        partidos.append({**base, "mercados": [{k: (round(v, 4) if isinstance(v, float) else v)
                                               for k, v in {**c, "etiqueta": c["etiqueta"].format(local=m["local"], visitante=m["visitante"])}.items()}
                                              for c in cs if c.get("prob_mercado") is not None]})
        for regla, lista in ((segura, seguras), (con_valor, valor)):
            x = regla(cs)
            if x:
                lista.append({**base, **x, "etiqueta": x["etiqueta"].format(local=m["local"], visitante=m["visitante"])})
    seguras.sort(key=lambda x: -consenso(x))
    valor.sort(key=lambda x: -x["ve"])
    combinadas = [cb for cb in (combinar(seguras, 2), combinar(seguras, 3)) if cb]
    r = lambda x: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()}
    # Todas las del periodo: la web elige las del día y muestra las mejores de la semana
    return {"seguras": [r(x) for x in seguras], "valor": [r(x) for x in valor], "partidos": partidos,
            "combinadas": [{**r({k: v for k, v in cb.items() if k != "patas"}), "patas": [r(p) for p in cb["patas"]]}
                           for cb in combinadas]}
