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
from scipy.optimize import minimize
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

    def __post_init__(self):
        self.idx = {e: i for i, e in enumerate(self.equipos)}

    @property
    def media_local(self) -> float:
        return float(np.exp(self.c + self.h))

    @property
    def media_visitante(self) -> float:
        return float(np.exp(self.c))

    def multiplicadores(self, equipo: str) -> tuple[float, float]:
        i = self.idx[equipo]
        return float(np.exp(self.ataque[i])), float(np.exp(self.defensa[i]))

    def lambdas(self, local: str, visitante: str) -> tuple[float, float]:
        i, j = self.idx[local], self.idx[visitante]
        lh = np.exp(self.c + self.h + self.ataque[i] + self.defensa[j])
        la = np.exp(self.c + self.ataque[j] + self.defensa[i])
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


def ajustar(
    df: pd.DataFrame,
    fecha_ref: date | None = None,
    xi: float = 0.0019,
    reg: float = 0.02,
    inicial: Ratings | None = None,
    prior_nuevo: tuple[float, float] = (-0.15, 0.15),
) -> Ratings:
    """Ajusta Dixon-Coles con partidos jugados antes de `fecha_ref`.

    df: columnas fecha, local, visitante, gl, gv (solo partidos jugados).
    xi: decaimiento por día. 0.0019 ≈ un partido de hace un año pesa la mitad.
    reg: penalización L2 que acerca a su punto de partida a los equipos con pocos
         partidos, para que un par de goleadas no los dispare.
    prior_nuevo: punto de partida (log ataque, log defensa) de un equipo que solo
         aparece en la última temporada de los datos, típicamente un ascendido:
         (−0.15, +0.15) ≈ marca 14% menos y concede 16% más que la media.
    """
    df = df.dropna(subset=["gl", "gv"])
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
                mu_a[k], mu_d[k] = prior_nuevo
    hi = df["local"].map(idx).to_numpy()
    ai = df["visitante"].map(idx).to_numpy()
    x = df["gl"].to_numpy(dtype=float)
    y = df["gv"].to_numpy(dtype=float)
    w = pesos_temporales(df["fecha"], fecha_ref, xi)
    w = w / w.sum()

    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)
    const = gammaln(x + 1) + gammaln(y + 1)

    def objetivo(theta):
        a = theta[:n] - theta[:n].mean()
        d = theta[n:2 * n] - theta[n:2 * n].mean()
        c, h, rho = theta[2 * n:]
        lh = np.exp(c + h + a[hi] + d[ai])
        la = np.exp(c + a[ai] + d[hi])

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
        tau = np.clip(tau, 1e-10, None)

        ll = np.log(tau) + x * np.log(lh) - lh + y * np.log(la) - la - const
        ea, ed = a - mu_a, d - mu_d
        nll = -(w * ll).sum() + reg * (ea @ ea + ed @ ed) / n

        gh = w * (x - lh + lh * dt_l / tau)        # ∂ll/∂η_local
        ga = w * (y - la + la * dt_a / tau)        # ∂ll/∂η_visitante
        g_a = np.bincount(hi, gh, n) + np.bincount(ai, ga, n)
        g_d = np.bincount(ai, gh, n) + np.bincount(hi, ga, n)
        g_a = -g_a + 2 * reg * ea / n
        g_d = -g_d + 2 * reg * ed / n
        g_a -= g_a.mean()   # derivada a través del centrado a = θ − media(θ)
        g_d -= g_d.mean()
        g_c = -(gh.sum() + ga.sum())
        g_h = -gh.sum()
        g_r = -(w * dt_r / tau).sum()
        return nll, np.concatenate([g_a, g_d, [g_c, g_h, g_r]])

    theta0 = np.zeros(2 * n + 3)
    theta0[2 * n] = np.log(max(y.mean(), 0.3))
    theta0[2 * n + 1] = np.log(max(x.mean(), 0.3) / max(y.mean(), 0.3))
    theta0[2 * n + 2] = -0.05
    if inicial is not None:   # arranque en caliente: acelera mucho la validación
        for e, k in idx.items():
            if e in inicial.idx:
                theta0[k] = inicial.ataque[inicial.idx[e]]
                theta0[n + k] = inicial.defensa[inicial.idx[e]]
        theta0[2 * n:] = [inicial.c, inicial.h, inicial.rho]

    bounds = [(None, None)] * (2 * n + 2) + [(-0.25, 0.15)]
    res = minimize(objetivo, theta0, jac=True, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 2000, "gtol": 1e-9})
    th = res.x
    a = th[:n] - th[:n].mean()
    d = th[n:2 * n] - th[n:2 * n].mean()
    jugados = pd.concat([df["local"], df["visitante"]]).value_counts().to_dict()
    return Ratings(equipos, a, d, float(th[2 * n]), float(th[2 * n + 1]), float(th[2 * n + 2]),
                   partidos={e: int(jugados.get(e, 0)) for e in equipos},
                   fecha_ref=fecha_ref, n_partidos=len(df))
