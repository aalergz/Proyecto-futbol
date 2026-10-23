"""Modelo Dixon-Coles con decaimiento temporal y regularización.

log λ_local     = c + h + ataque[local]     + defensa[visitante]
log λ_visitante = c     + ataque[visitante] + defensa[local]

Con sum(ataque) = sum(defensa) = 0, exp(c + h) es la media de goles del local
y exp(c) la del visitante. Así cada rating se lee como un multiplicador:
ataque 1.40 = marca 40% más que la media; defensa 0.70 = concede 30% menos.
Es exactamente el formato que usa la Pizarra (media × ataque × defensa).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy.optimize import minimize, minimize_scalar
from scipy.special import gammaln

MAX_GOLES = 10


@dataclass
class Ratings:
    equipos: list[str]
    ataque: np.ndarray        # escala logarítmica, centrada en 0
    defensa: np.ndarray       # escala logarítmica, centrada en 0
    c: float
    h: float
    rho: float
    partidos: dict[str, int] = field(default_factory=dict)
    fecha_ref: date | None = None
    n_partidos: int = 0
    local: np.ndarray | None = None   # ventaja de local propia de cada equipo (log), opcional
    arbitros: dict[str, float] = field(default_factory=dict)          # efecto de cada árbitro (log)
    partidos_arbitro: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.idx = {e: i for i, e in enumerate(self.equipos)}
        if self.local is None:
            self.local = np.zeros(len(self.equipos))

    @property
    def media_local(self) -> float:
        return float(np.exp(self.c + self.h))

    @property
    def media_visitante(self) -> float:
        return float(np.exp(self.c))

    def multiplicadores(self, equipo: str) -> tuple[float, float]:
        i = self.idx[equipo]
        return float(np.exp(self.ataque[i])), float(np.exp(self.defensa[i]))

    def lambdas(self, local: str, visitante: str, arbitro: str | None = None) -> tuple[float, float]:
        i, j = self.idx[local], self.idx[visitante]
        r = self.arbitros.get(arbitro, 0.0) if arbitro else 0.0   # árbitro desconocido = promedio
        lh = np.exp(self.c + self.h + self.local[i] + self.ataque[i] + self.defensa[j] + r)
        la = np.exp(self.c + self.ataque[j] + self.defensa[i] + r)
        return float(lh), float(la)

    def vector(self) -> np.ndarray:
        """Parámetros como vector, para usarlos de punto de partida en otro ajuste."""
        return np.concatenate([self.ataque, self.defensa, [self.c, self.h, self.rho]])


# --------------------------------------------------------------------------- #
# Probabilidades
# --------------------------------------------------------------------------- #
def _poisson(l: float, n: int = MAX_GOLES) -> np.ndarray:
    k = np.arange(n + 1)
    return np.exp(k * np.log(max(l, 1e-12)) - l - gammaln(k + 1))


def matriz(lh: float, la: float, rho: float, n: int = MAX_GOLES) -> np.ndarray:
    """Matriz (n+1)×(n+1) de marcadores: filas = goles local, columnas = visitante."""
    M = np.outer(_poisson(lh, n), _poisson(la, n))
    M[0, 0] *= 1 - lh * la * rho
    M[0, 1] *= 1 + lh * rho
    M[1, 0] *= 1 + la * rho
    M[1, 1] *= 1 - rho
    M = np.clip(M, 0, None)
    return M / M.sum()


def mercados(M: np.ndarray) -> dict[str, float]:
    i, j = np.indices(M.shape)
    return {
        "p1": float(M[i > j].sum()),
        "px": float(M[i == j].sum()),
        "p2": float(M[i < j].sum()),
        "o25": float(M[(i + j) > 2.5].sum()),
        "btts": float(M[(i > 0) & (j > 0)].sum()),
    }


def predecir(r: Ratings, local: str, visitante: str) -> dict[str, float]:
    lh, la = r.lambdas(local, visitante)
    M = matriz(lh, la, r.rho)
    top = np.unravel_index(np.argmax(M[:7, :7]), (7, 7))
    return {"lh": lh, "la": la, **mercados(M), "marcador": f"{top[0]}-{top[1]}", "p_marcador": float(M[top])}


# --------------------------------------------------------------------------- #
# Ajuste por máxima verosimilitud
# --------------------------------------------------------------------------- #
def pesos_temporales(fechas: pd.Series, fecha_ref: date, xi: float) -> np.ndarray:
    dias = (pd.Timestamp(fecha_ref) - pd.to_datetime(fechas)).dt.days.to_numpy()
    return np.exp(-xi * np.clip(dias, 0, None))


def objetivos(df: pd.DataFrame, peso_goles: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Goles "corregidos" para ajustar la fuerza de los equipos.

    Los goles tienen mucha suerte. Los goles esperados (xG) o los remates a puerta
    miden mejor lo que el equipo generó. Se mezcla:
        objetivo = peso_goles × goles + (1 − peso_goles) × indicador
    donde indicador = xG si existe; si no, remates a puerta × conversión de la liga.
    """
    gl = df["gl"].to_numpy(float)
    gv = df["gv"].to_numpy(float)
    if peso_goles >= 1 or "tl" not in df:
        return gl, gv, float("nan")
    tl = df["tl"].to_numpy(float)
    tv = df["tv"].to_numpy(float)
    ok = ~np.isnan(tl) & ~np.isnan(tv)
    conv = (gl[ok].sum() + gv[ok].sum()) / max(tl[ok].sum() + tv[ok].sum(), 1) if ok.any() else np.nan
    il = np.where(ok, tl * conv, gl)
    iv = np.where(ok, tv * conv, gv)
    if "xgl" in df:
        xl = df["xgl"].to_numpy(float)
        xv = df["xgv"].to_numpy(float)
        hay = ~np.isnan(xl) & ~np.isnan(xv)
        il = np.where(hay, xl, il)
        iv = np.where(hay, xv, iv)
    w = peso_goles
    return w * gl + (1 - w) * il, w * gv + (1 - w) * iv, float(conv)


def ajustar(
    df: pd.DataFrame,
    fecha_ref: date | None = None,
    xi: float = 0.0019,
    reg: float = 0.02,
    inicial: Ratings | None = None,
    prior_nuevo: tuple[float, float] = (-0.15, 0.15),
    peso_goles: float = 1.0,
    columnas: tuple[str, str] = ("gl", "gv"),
    dixon_coles: bool = True,
    reg_local: float | None = None,
    reg_arbitro: float | None = None,
) -> Ratings:
    """Ajusta el modelo con partidos jugados antes de `fecha_ref`.

    df: columnas fecha, local, visitante, gl, gv (y opcionalmente tl, tv, xgl, xgv).
    xi: decaimiento por día. 0.0019 ≈ un partido de hace un año pesa la mitad.
    reg: penalización L2 que acerca a su punto de partida a los equipos con pocos
         partidos, para que un par de goleadas no los dispare.
    prior_nuevo: punto de partida (log ataque, log defensa) de un equipo que solo
         aparece en la última temporada de los datos, típicamente un ascendido.
    peso_goles: 1 = solo goles; menos de 1 mezcla xG o remates a puerta (ver objetivos).
    columnas: qué se cuenta. ("crl", "crv") ajusta córners; ("tjl", "tjv"), tarjetas.
    dixon_coles: corrige 0-0, 1-0, 0-1 y 1-1 (solo tiene sentido con goles).
    reg_local: si no es None, cada equipo tiene su propia ventaja de local,
         penalizada con esta fuerza hacia la ventaja media de la liga.
    reg_arbitro: si no es None y hay columna "arbitro", cada árbitro multiplica
         el conteo de los dos equipos (pensado para tarjetas), penalizado hacia 1.
    """
    df = df.dropna(subset=list(columnas))
    if fecha_ref is None:
        fecha_ref = pd.to_datetime(df["fecha"]).max().date()
    df = df[pd.to_datetime(df["fecha"]) < pd.Timestamp(fecha_ref) + pd.Timedelta(days=1)]
    if len(df) < 20:
        raise ValueError(f"Hacen falta al menos 20 partidos jugados; hay {len(df)}.")

    equipos = sorted(set(df["local"]) | set(df["visitante"]))
    idx = {e: k for k, e in enumerate(equipos)}
    n = len(equipos)
    mu_a = np.zeros(n)
    mu_d = np.zeros(n)
    if "temporada" in df and df["temporada"].nunique() > 1:
        ultima = df["temporada"].max()
        previos = set(df.loc[df["temporada"] < ultima, "local"]) | set(df.loc[df["temporada"] < ultima, "visitante"])
        for e, k in idx.items():
            if e not in previos:
                mu_a[k], mu_d[k] = prior_nuevo if columnas == ("gl", "gv") else (0.0, 0.0)
    hi = df["local"].map(idx).to_numpy()
    ai = df["visitante"].map(idx).to_numpy()
    if columnas == ("gl", "gv"):
        x, y, _ = objetivos(df, peso_goles)
        gx = df["gl"].to_numpy(float)
        gy = df["gv"].to_numpy(float)
    else:
        x = df[columnas[0]].to_numpy(float)
        y = df[columnas[1]].to_numpy(float)
        gx, gy = x, y
    w = pesos_temporales(df["fecha"], fecha_ref, xi)
    w = w / w.sum()

    # Con objetivos no enteros, ρ se estima después, con los goles reales
    dc_en_ajuste = dixon_coles and np.array_equal(x, gx) and np.array_equal(y, gy)
    m00 = (gx == 0) & (gy == 0)
    m01 = (gx == 0) & (gy == 1)
    m10 = (gx == 1) & (gy == 0)
    m11 = (gx == 1) & (gy == 1)
    const = gammaln(x + 1) + gammaln(y + 1)
    nl = n if reg_local is not None else 0
    rl = reg_local or 0.0
    arbs: list[str] = []
    if reg_arbitro is not None and "arbitro" in df:
        col = df["arbitro"].astype("string")
        arbs = sorted(set(col.dropna()))
    nr = len(arbs)
    if nr:
        aidx = {a: k for k, a in enumerate(arbs)}
        ri = df["arbitro"].astype("string").map(aidx).fillna(-1).to_numpy(int)
        tiene = ri >= 0
        ri_ok = np.where(tiene, ri, 0)
    ra = reg_arbitro or 0.0

    def tau_y_derivadas(lh, la, rho):
        tau = np.ones_like(lh)
        dt_l = np.zeros_like(lh)
        dt_a = np.zeros_like(lh)
        dt_r = np.zeros_like(lh)
        tau[m00] = 1 - lh[m00] * la[m00] * rho
        dt_l[m00] = -la[m00] * rho
        dt_a[m00] = -lh[m00] * rho
        dt_r[m00] = -lh[m00] * la[m00]
        tau[m01] = 1 + lh[m01] * rho
        dt_l[m01] = rho
        dt_r[m01] = lh[m01]
        tau[m10] = 1 + la[m10] * rho
        dt_a[m10] = rho
        dt_r[m10] = la[m10]
        tau[m11] = 1 - rho
        dt_r[m11] = -1
        return np.clip(tau, 1e-10, None), dt_l, dt_a, dt_r

    def objetivo(theta):
        a = theta[:n] - theta[:n].mean()
        d = theta[n:2 * n] - theta[n:2 * n].mean()
        hl = theta[2 * n:2 * n + nl] if nl else 0.0
        rr = theta[2 * n + nl:2 * n + nl + nr] if nr else None
        c, h, rho = theta[2 * n + nl + nr:]
        ef = np.where(tiene, rr[ri_ok], 0.0) if nr else 0.0
        lh = np.exp(c + h + (hl[hi] if nl else 0.0) + a[hi] + d[ai] + ef)
        la = np.exp(c + a[ai] + d[hi] + ef)
        if dc_en_ajuste:
            tau, dt_l, dt_a, dt_r = tau_y_derivadas(lh, la, rho)
        else:
            tau = np.ones_like(lh); dt_l = dt_a = dt_r = np.zeros_like(lh)

        ll = np.log(tau) + x * np.log(lh) - lh + y * np.log(la) - la - const
        ea, ed = a - mu_a, d - mu_d
        nll = -(w * ll).sum() + reg * (ea @ ea + ed @ ed) / n
        if nl:
            nll += rl * (hl @ hl) / n
        if nr:
            nll += ra * (rr @ rr) / nr

        gh = w * (x - lh + lh * dt_l / tau)        # ∂ll/∂η_local
        ga = w * (y - la + la * dt_a / tau)        # ∂ll/∂η_visitante
        g_a = np.bincount(hi, gh, n) + np.bincount(ai, ga, n)
        g_d = np.bincount(ai, gh, n) + np.bincount(hi, ga, n)
        g_a = -g_a + 2 * reg * ea / n
        g_d = -g_d + 2 * reg * ed / n
        g_a -= g_a.mean()   # derivada a través del centrado a = θ − media(θ)
        g_d -= g_d.mean()
        partes = [g_a, g_d]
        if nl:
            partes.append(-np.bincount(hi, gh, n) + 2 * rl * hl / n)
        if nr:
            partes.append(-np.bincount(ri_ok[tiene], (gh + ga)[tiene], nr) + 2 * ra * rr / nr)
        g_c = -(gh.sum() + ga.sum())
        g_h = -gh.sum()
        g_r = -(w * dt_r / tau).sum() if dc_en_ajuste else 0.0
        return nll, np.concatenate(partes + [[g_c, g_h, g_r]])

    theta0 = np.zeros(2 * n + nl + nr + 3)
    theta0[2 * n + nl + nr] = np.log(max(y.mean(), 0.3))
    theta0[2 * n + nl + nr + 1] = np.log(max(x.mean(), 0.3) / max(y.mean(), 0.3))
    theta0[2 * n + nl + nr + 2] = -0.05 if dc_en_ajuste else 0.0
    if inicial is not None:   # arranque en caliente: acelera mucho la validación
        for e, k in idx.items():
            if e in inicial.idx:
                theta0[k] = inicial.ataque[inicial.idx[e]]
                theta0[n + k] = inicial.defensa[inicial.idx[e]]
                if nl:
                    theta0[2 * n + k] = inicial.local[inicial.idx[e]]
        for k, arb in enumerate(arbs):
            theta0[2 * n + nl + k] = inicial.arbitros.get(arb, 0.0)
        theta0[2 * n + nl + nr:2 * n + nl + nr + 2] = [inicial.c, inicial.h]
        if dc_en_ajuste:
            theta0[-1] = inicial.rho

    bounds = [(None, None)] * (2 * n + nl + nr + 2) + [(-0.25, 0.15) if dc_en_ajuste else (0.0, 0.0)]
    res = minimize(objetivo, theta0, jac=True, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 2000, "gtol": 1e-9})
    th = res.x
    a = th[:n] - th[:n].mean()
    d = th[n:2 * n] - th[n:2 * n].mean()
    hl = th[2 * n:2 * n + nl] if nl else np.zeros(n)
    rr = th[2 * n + nl:2 * n + nl + nr]
    c, h, rho = (float(v) for v in th[2 * n + nl + nr:])

    if dixon_coles and not dc_en_ajuste:
        # ρ con los goles reales, dejando fijas las fuerzas
        ef = np.where(tiene, rr[ri_ok], 0.0) if nr else 0.0
        lh = np.exp(c + h + hl[hi] + a[hi] + d[ai] + ef)
        la = np.exp(c + a[ai] + d[hi] + ef)
        f = lambda r: -(w * np.log(tau_y_derivadas(lh, la, r)[0])).sum()
        rho = float(minimize_scalar(f, bounds=(-0.25, 0.15), method="bounded").x)

    jugados = pd.concat([df["local"], df["visitante"]]).value_counts().to_dict()
    pa = df["arbitro"].value_counts().to_dict() if nr else {}
    return Ratings(equipos, a, d, c, h, rho,
                   partidos={e: int(jugados.get(e, 0)) for e in equipos},
                   fecha_ref=fecha_ref, n_partidos=len(df), local=hl,
                   arbitros={arb: float(rr[k]) for k, arb in enumerate(arbs)},
                   partidos_arbitro={arb: int(pa.get(arb, 0)) for arb in arbs})
