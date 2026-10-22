"""Matemáticas de la estrategia: quitar margen, valor esperado, Kelly y emparejar partidos.

Todo es puro (sin red), para poder probarlo.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime


# ---------- probabilidades justas ----------

def devig(odds: list[float], method: str = "power") -> list[float]:
    """Convierte cuotas con margen en probabilidades que suman 1.

    power: resuelve k tal que sum((1/o)^k) = 1. Corrige mejor el sesgo
    favorito/no favorito que el método proporcional.
    """
    if len(odds) < 2 or any(o <= 1 for o in odds):
        raise ValueError("se necesitan al menos dos cuotas mayores que 1")
    imp = [1 / o for o in odds]
    total = sum(imp)
    if method == "mult":
        return [x / total for x in imp]
    lo, hi = 0.2, 6.0
    for _ in range(100):
        k = (lo + hi) / 2
        s = sum(x ** k for x in imp)
        if s > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    probs = [x ** k for x in imp]
    s = sum(probs)
    return [p / s for p in probs]


# ---------- valor esperado ----------

def ev_back(p: float, odds: float, commission: float = 0.0) -> float:
    """EV por euro apostado a favor. La comisión se cobra sobre la ganancia."""
    return p * (odds - 1) * (1 - commission) - (1 - p)


def ev_lay_per_liability(p: float, odds: float, commission: float = 0.0) -> float:
    """EV por euro de responsabilidad al apostar en contra a cuota `odds`."""
    b = (1 - commission) / (odds - 1)
    return (1 - p) * b - p


# ---------- Kelly ----------

def kelly_back(p: float, odds: float, commission: float = 0.0) -> float:
    b = (odds - 1) * (1 - commission)
    if b <= 0:
        return 0.0
    return max(0.0, (p * b - (1 - p)) / b)


def kelly_lay_liability(p: float, odds: float, commission: float = 0.0) -> float:
    """Fracción de la banca a arriesgar como responsabilidad en una apuesta en contra."""
    b = (1 - commission) / (odds - 1)
    return max(0.0, ((1 - p) * b - p) / b)


@dataclass
class StakeRules:
    kelly_fraction: float = 0.25
    max_pct: float = 0.03          # tope por apuesta, fracción de banca
    min_stake: float = 2.0         # mínimo de la casa / exchange
    allow_round_up: bool = True    # subir al mínimo si medio Kelly lo justifica


def size_stake(bank: float, full_kelly: float, rules: StakeRules) -> float:
    """Devuelve el importe en euros, o 0 si no se debe apostar."""
    if full_kelly <= 0 or bank <= 0:
        return 0.0
    stake = bank * full_kelly * rules.kelly_fraction
    stake = min(stake, bank * rules.max_pct)
    if stake < rules.min_stake:
        # Con bancas pequeñas el mínimo suele superar el Kelly fraccional.
        # Solo se sube al mínimo si incluso medio Kelly lo permite y no rompe el tope duro.
        if (rules.allow_round_up and bank * full_kelly * 0.5 >= rules.min_stake
                and rules.min_stake <= bank * max(rules.max_pct, 0.05)):
            return rules.min_stake
        return 0.0
    return math.floor(stake * 100) / 100


# ---------- emparejar nombres de equipos ----------

_STOP = {"fc", "cf", "cd", "ud", "sd", "rc", "rcd", "ca", "sc", "ac", "as", "ss", "afc",
         "club", "real", "de", "del", "la", "el", "the", "calcio", "sporting", "deportivo", "balompie"}


def norm_team(name: str, aliases: dict[str, str] | None = None) -> set[str]:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = " ".join(n.split())
    if aliases:
        n = aliases.get(n, n)
    toks = {t for t in n.split() if t not in _STOP}
    return toks or set(n.split())


def team_similarity(a: str, b: str, aliases: dict[str, str] | None = None) -> float:
    ta, tb = norm_team(a, aliases), norm_team(b, aliases)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb))


def same_event(home_a: str, away_a: str, start_a: datetime,
               home_b: str, away_b: str, start_b: datetime,
               aliases: dict[str, str] | None = None,
               max_minutes: int = 20) -> bool:
    if abs((start_a - start_b).total_seconds()) > max_minutes * 60:
        return False
    return (team_similarity(home_a, home_b, aliases) >= 0.5
            and team_similarity(away_a, away_b, aliases) >= 0.5)


# ---------- CLV ----------

def clv(odds_taken: float, closing_fair_odds: float) -> float:
    """Cuánto mejor fue tu cuota que la cuota justa de cierre (positivo = bien)."""
    return odds_taken / closing_fair_odds - 1
