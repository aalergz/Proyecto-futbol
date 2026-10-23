#!/usr/bin/env python3
"""Motor de la Pizarra Poisson.

  python cli.py actualizar                 descarga, ajusta, valida y escribe salida/ratings.json
  python cli.py actualizar --ligas eng     solo algunas ligas
  python cli.py actualizar --sin-validar   más rápido, sin backtest
  python cli.py predecir eng Arsenal Chelsea
  python cli.py validar esp --desde 2025-08-01 --csv salida/validacion_esp.csv
  python cli.py buscar-liga --pais Costa-Rica
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from motor.exportar import bloque_liga, envoltorio
from motor.fuentes import (CUOTA, FuenteNoDisponible, buscar_ligas, cargar_liga, estado_api,
                           probar_temporada, resolver_apifootball, temporadas_liga)
from motor.modelo import ajustar, predecir
from motor.validacion import validar

RAIZ = Path(__file__).resolve().parent
SALIDA = RAIZ / "salida" / "ratings.json"
WEB = RAIZ / "web" / "ratings.json"


def config() -> dict:
    with open(RAIZ / "config.toml", "rb") as f:
        return tomllib.load(f)


def temporadas(cfg_liga: dict, modelo: dict) -> list[int]:
    return temporadas_liga(cfg_liga, int(modelo.get("temporadas_historial", 2)))


def pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def cmd_actualizar(a):
    cfg = config(); mod = cfg["modelo"]
    claves = a.ligas or list(cfg["ligas"])
    previo = json.loads(SALIDA.read_text()) if SALIDA.exists() else {"ligas": {}}
    # Las ligas que no se actualizan en esta corrida conservan su versión anterior
    ligas = {k: v for k, v in previo.get("ligas", {}).items() if k in cfg["ligas"] and k not in claves}
    for k in claves:
        lc = dict(cfg["ligas"][k])
        print(f"▸ {lc['nombre']}")
        try:
            temps = temporadas(lc, mod)
            lc["temporada_actual"] = temps[-1]
            df, fuente = cargar_liga(lc, temps)
        except FuenteNoDisponible as e:
            print(f"  · sin datos: {e}")
            if k in previo.get("ligas", {}):
                print("  · conservo los ratings de la actualización anterior")
                ligas[k] = previo["ligas"][k]
            continue
        jug = df.dropna(subset=["gl", "gv"])
        print(f"  · {fuente}: {len(jug)} partidos jugados, {df['gl'].isna().sum()} por jugar, último {jug['fecha'].max():%Y-%m-%d}")
        t0 = time.time()
        try:
            r = ajustar(jug, fecha_ref=jug["fecha"].max().date(), xi=mod["xi"], reg=mod["regularizacion"])
        except ValueError as e:
            print(f"  · no se puede ajustar: {e}")
            if k in previo.get("ligas", {}):
                ligas[k] = previo["ligas"][k]
            continue
        print(f"  · ajuste: media local {r.media_local:.2f}, visitante {r.media_visitante:.2f}, ρ {r.rho:+.3f} ({time.time() - t0:.1f}s)")

        val = None
        dias = int(mod.get("validar_dias", 0))
        if dias and not a.sin_validar:
            t0 = time.time()
            desde = jug["fecha"].max() - timedelta(days=dias)
            v = validar(df, desde, xi=mod["xi"], reg=mod["regularizacion"])["resumen"]
            if v.get("partidos"):
                val = v
                m = v["mercados"]["1X2"]
                print(f"  · validación: {v['partidos']} partidos fuera de muestra, Brier 1X2 {m['modelo']:.4f} vs base {m['base']:.4f} "
                      f"({m['mejora'] * 100:+.1f}%) ({time.time() - t0:.0f}s)")
        elif k in previo.get("ligas", {}):
            val = previo["ligas"][k].get("validacion")

        ligas[k] = bloque_liga(k, lc, df, r, fuente, val)
        print(f"  · {len(ligas[k]['equipos'])} equipos, {len(ligas[k]['proximos'])} próximos partidos")

    out = envoltorio(ligas, {"xi": mod["xi"], "regularizacion": mod["regularizacion"]},
                     cfg.get("web", {}).get("en_vivo_url", ""))
    if CUOTA["usadas"]:
        print(f"  · API-Football: {CUOTA['usadas']} solicitudes en esta corrida, quedan {CUOTA['restantes']} hoy")
    sin_fecha = lambda d: {k: v for k, v in d.items() if k != "generado"}
    if SALIDA.exists() and sin_fecha(previo) == sin_fecha(json.loads(json.dumps(out, ensure_ascii=False))):
        print("✓ sin cambios desde la última actualización; no se reescribe ratings.json")
        return
    SALIDA.parent.mkdir(exist_ok=True)
    SALIDA.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    if WEB.parent.exists():
        shutil.copy(SALIDA, WEB)
    print(f"✓ {SALIDA.relative_to(RAIZ)} ({len(ligas)} ligas)")


def cmd_predecir(a):
    cfg = config(); mod = cfg["modelo"]; lc = cfg["ligas"][a.liga]
    df, _ = cargar_liga(lc, temporadas(lc, mod))
    jug = df.dropna(subset=["gl", "gv"])
    r = ajustar(jug, xi=mod["xi"], reg=mod["regularizacion"])
    def buscar(n):
        cand = [e for e in r.equipos if n.lower() in e.lower()]
        if not cand:
            sys.exit(f"No encuentro '{n}'. Equipos: {', '.join(r.equipos)}")
        return cand[0]
    l, v = buscar(a.local), buscar(a.visitante)
    p = predecir(r, l, v)
    print(f"{l} vs {v}")
    print(f"  goles esperados  {p['lh']:.2f} – {p['la']:.2f}")
    print(f"  1 / X / 2        {pct(p['p1'])}  {pct(p['px'])}  {pct(p['p2'])}")
    print(f"  cuota justa      {1/p['p1']:6.2f} {1/p['px']:6.2f} {1/p['p2']:6.2f}")
    print(f"  más de 2.5       {pct(p['o25'])}   ambos anotan {pct(p['btts'])}")
    print(f"  marcador         {p['marcador']} ({pct(p['p_marcador']).strip()})")


def cmd_validar(a):
    cfg = config(); mod = cfg["modelo"]; lc = cfg["ligas"][a.liga]
    df, fuente = cargar_liga(lc, temporadas(lc, mod))
    desde = pd.Timestamp(a.desde) if a.desde else df.dropna(subset=["gl"])["fecha"].max() - timedelta(days=365)
    out = validar(df, desde, xi=a.xi or mod["xi"], reg=mod["regularizacion"] if a.reg is None else a.reg)
    v = out["resumen"]
    if not v.get("partidos"):
        sys.exit("No hay suficientes partidos para validar en ese rango.")
    print(f"{lc['nombre']} · {fuente} · {v['partidos']} partidos fuera de muestra ({v['desde']} → {v['hasta']})")
    print(f"{'mercado':<14}{'modelo':>9}{'base':>9}{'mercado':>9}{'mejora':>9}")
    for k, m in v["mercados"].items():
        mk = f"{m['mercado']:.4f}" if "mercado" in m else "—"
        print(f"{k:<14}{m['modelo']:>9.4f}{m['base']:>9.4f}{mk:>9}{m['mejora'] * 100:>+8.1f}%")
    print(f"acierto del favorito 1X2: {pct(v['acierto_1x2']).strip()}")
    if "apuestas_simuladas" in v:
        s = v["apuestas_simuladas"]
        print(f"apuestas simuladas (VE > {s['umbral_ve']:.0%}): {s['apuestas']} · ROI {s['roi'] * 100:+.1f}%")
    if a.csv:
        out["detalle"].to_csv(a.csv, index=False)
        print(f"detalle → {a.csv}")


def cmd_probar_api(a):
    """Comprueba la clave y qué temporadas entrega tu plan. Gasta ~4 solicitudes."""
    try:
        e = estado_api()
    except FuenteNoDisponible as err:
        sys.exit(f"✗ {err}")
    print(f"✓ Clave válida · plan {e['plan']} · {e['usadas_hoy']}/{e['limite_dia']} solicitudes usadas hoy")
    cfg = config()
    todo_ok = True
    for k, lc in cfg["ligas"].items():
        if "apifootball" not in lc.get("fuentes", []):
            continue
        try:
            liga_id, actual = resolver_apifootball(dict(lc))
        except FuenteNoDisponible as err:
            print(f"✗ {lc['nombre']}: {err}"); todo_ok = False; continue
        ok, det = probar_temporada(liga_id, actual)
        todo_ok &= ok
        print(f"{'✓' if ok else '✗'} {lc['nombre']} · id {liga_id} · temporada {actual}: {det}")
    if not todo_ok:
        print("\nTu plan no entrega alguna temporada en curso. El motor seguirá funcionando con las otras\n"
              "fuentes (football-data.co.uk y openfootball), pero la Liga Promerica necesitará un plan de\n"
              "pago de API-Football o un CSV propio.")


def cmd_buscar(a):
    for l in buscar_ligas(a.pais):
        print(f"{l['id']:>6}  {l['nombre']:<40} {l['tipo']:<8} temporadas {l['temporadas']}")


def main():
    p = argparse.ArgumentParser(description="Motor de la Pizarra Poisson")
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("actualizar"); x.add_argument("--ligas", nargs="*"); x.add_argument("--sin-validar", action="store_true")
    x.set_defaults(f=cmd_actualizar)
    x = s.add_parser("predecir"); x.add_argument("liga"); x.add_argument("local"); x.add_argument("visitante")
    x.set_defaults(f=cmd_predecir)
    x = s.add_parser("validar"); x.add_argument("liga"); x.add_argument("--desde"); x.add_argument("--csv")
    x.add_argument("--xi", type=float); x.add_argument("--reg", type=float); x.set_defaults(f=cmd_validar)
    x = s.add_parser("probar-api"); x.set_defaults(f=cmd_probar_api)
    x = s.add_parser("buscar-liga"); x.add_argument("--pais", required=True); x.set_defaults(f=cmd_buscar)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
