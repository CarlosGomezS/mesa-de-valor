"""Laboratorio: aprende de forma continua qué reglas de apuesta ganan.

Cómo funciona
-------------
1. Observa. En cada búsqueda de cuotas (que el bot ya hace) guarda una «observación» por partido,
   resultado y casa: cuota, probabilidad de Pinnacle, consenso, horas que faltan, movimiento de Pinnacle.
   No gasta consultas extra.
2. Recuerda el desenlace. Cuando el bot lee resultados, los apunta en esos partidos; la última
   probabilidad de Pinnacle antes del inicio sirve como cuota de cierre (CLV).
3. Inventa métodos. Un método es un conjunto de reglas («genoma»): referencia, valor mínimo y máximo,
   rango de cuotas, deporte, tipo de resultado, horas antes del partido, movimiento de Pinnacle, importe.
4. Los pone a prueba con todo lo observado, separando el pasado (entrenamiento) de lo más reciente
   (validación), para no premiar métodos que solo funcionaron por casualidad.
5. Evoluciona. Cada día conserva los mejores, los cruza y los muta, y prueba ideas nuevas al azar.
   No para nunca: cada día hay más datos y la población se vuelve a examinar desde cero.
6. Propone. Si un método supera la validación, te pide aprobación para pasarlo a la cartera ficticia;
   si es excepcional, lo pasa solo y te avisa. Si un método promovido deja de funcionar, se retira.
"""
from __future__ import annotations

import base64
import json
import math
import random
import secrets
import zlib
from datetime import datetime, timezone

# ------------------------------------------------------------------ umbrales
POP_SIZE = 60            # métodos vivos en el laboratorio
NEW_PER_GEN = 40         # métodos nuevos por generación
MAX_ROWS = 150_000       # memoria máxima de observaciones (las más antiguas se olvidan)
MAX_EVENTS = 40_000
VAL_SHARE = 0.35         # parte más reciente reservada para validar
PROPOSE = {"n": 150, "lb": 0.0, "clv": 0.0}                    # pide tu aprobación
AUTO = {"n": 400, "lb": 0.03, "clv_t": 3.0, "train_lb": 0.0}    # «muy, muy bueno»: pasa solo
MAX_PROMOTED = 10
MAX_PENDING = 3
RETIRE_AFTER = 100       # apuestas en la cartera ficticia antes de juzgar un método promovido

SPORTS = ["soccer", "basketball", "tennis", "handball"]
SPORT_ES = {"soccer": "Fútbol", "basketball": "Baloncesto", "tennis": "Tenis", "handball": "Balonmano"}
OUT_ES = {"home": "local", "draw": "empate", "away": "visitante", "fav": "favorito", "dog": "no favorito"}
REF_ES = {"pinnacle": "Pinnacle", "consensus": "consenso", "both": "Pinnacle + consenso"}


def _now_min() -> int:
    return int(datetime.now(timezone.utc).timestamp() // 60)


def _min(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() // 60)


def is_exchange(book: str) -> bool:
    return book.startswith("betfair_ex") or book == "matchbook"


# ------------------------------------------------------------------ almacenamiento
def empty() -> dict:
    # rows: [t_obs, event_id, out(h/d/a), fav, book, odds, p_pin, p_cons, hours, drift]
    # events: id -> [sport, commence_min, close_h, close_d, close_a, result(h/d/a/None)]
    return {"v": 1, "rows": [], "events": {}, "seen": {}}


def pack(lab: dict) -> bytes:
    return zlib.compress(json.dumps(lab, separators=(",", ":")).encode(), 9)


def unpack(b: bytes) -> dict:
    return json.loads(zlib.decompress(b))


# ------------------------------------------------------------------ 1) observar
def observe(lab: dict, ref: dict, sport: str, books: set, t_min: int | None = None, horizon_h: float = 48):
    """Guarda observaciones de una descarga de cuotas ya hecha por el bot."""
    t_min = t_min or _now_min()
    seen = lab["seen"]
    for eid, r in ref.items():
        c = _min(r["commence"])
        hours = (c - t_min) / 60
        if hours <= 0.3 or hours > horizon_h:
            continue
        names = {"h": r["home"], "a": r["away"], "d": "Draw"}
        fair, cons, prev = r["fair"], r.get("consensus") or {}, r.get("prev") or {}
        ev = lab["events"].setdefault(eid, [sport, c, None, None, None, None])
        ev[1] = c
        for i, o in enumerate("hda"):          # la última foto antes del inicio hace de cierre
            if names[o] in fair:
                ev[2 + i] = round(fair[names[o]], 4)
        non_draw = [o for o in "ha" if names[o] in fair]
        fav = max(non_draw, key=lambda o: fair[names[o]]) if non_draw else None
        for key, b in r["books"].items():
            if key == "pinnacle" or (books and key not in books):
                continue
            for o in "hda":
                n = names[o]
                if n not in fair or n not in b["prices"]:
                    continue
                odds = b["prices"][n]
                sk = f"{eid}|{o}|{key}"
                if seen.get(sk) == odds:            # misma cuota que la última vez: nada nuevo
                    continue
                seen[sk] = odds
                p = fair[n]
                drift = round(p - prev[n], 4) if n in prev else None
                lab["rows"].append([t_min, eid, o, 1 if o == fav else 0, key, odds, round(p, 4),
                                    round(cons[n], 4) if n in cons else None, round(hours, 2), drift])


def record_results(lab: dict, results: dict):
    """results: event_id -> 'h' | 'd' | 'a'."""
    for eid, res in results.items():
        if eid in lab["events"]:
            lab["events"][eid][5] = res


def unresolved_sports(lab: dict, now_min: int | None = None) -> set:
    now_min = now_min or _now_min()
    return {e[0] for e in lab["events"].values() if e[5] is None and now_min - e[1] > 180 and now_min - e[1] < 3 * 1440}


def trim(lab: dict):
    if len(lab["rows"]) > MAX_ROWS:
        lab["rows"] = lab["rows"][-MAX_ROWS:]
    if len(lab["events"]) > MAX_EVENTS:
        keep = sorted(lab["events"].items(), key=lambda kv: kv[1][1])[-MAX_EVENTS:]
        lab["events"] = dict(keep)
    alive = {r[1] for r in lab["rows"]}
    lab["seen"] = {k: v for k, v in lab["seen"].items() if k.split("|")[0] in alive}


# ------------------------------------------------------------------ 3) genomas
def random_genome(rng: random.Random, leagues: list[str]) -> dict:
    min_ev = round(rng.choice([0.0, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.07, 0.09]), 3)
    min_odds = rng.choice([1.01, 1.3, 1.5, 1.7, 2.0, 2.5, 3.0, 4.0])
    g = {
        "ref": rng.choice(["pinnacle", "pinnacle", "consensus", "both"]),
        "min_ev": min_ev,
        "max_ev": round(min_ev + rng.choice([0.03, 0.05, 0.08, 0.12, 0.2]), 3),
        "min_odds": min_odds,
        "max_odds": rng.choice([None, round(min_odds * rng.choice([1.3, 1.6, 2, 3]), 2)]),
        "outcome": rng.choice([None, None, "home", "draw", "away", "fav", "dog"]),
        "sport": rng.choice([None, None] + SPORTS),
        "league": None,
        "min_h": rng.choice([0, 0, 1, 3, 6, 12]),
        "max_h": None,
        "drift": rng.choice([None, None, None, "up", "down"]),
        "kf": rng.choice([0.1, 0.2, 0.25, 0.33, 0.5]),
    }
    g["max_h"] = rng.choice([None, g["min_h"] + rng.choice([3, 6, 12, 24])])
    if leagues and rng.random() < 0.15:
        g["league"] = rng.choice(leagues)
        g["sport"] = g["league"].split("_")[0]
    return g


def mutate(g: dict, rng: random.Random, leagues: list[str]) -> dict:
    g = dict(g)
    r = random_genome(rng, leagues)
    for _ in range(rng.choice([1, 1, 2, 3])):
        k = rng.choice(list(r))
        if k == "min_ev":
            g["min_ev"] = round(max(0.0, g["min_ev"] + rng.choice([-0.01, -0.005, 0.005, 0.01, 0.02])), 3)
            g["max_ev"] = max(g["max_ev"], g["min_ev"] + 0.02)
        elif k == "max_ev":
            g["max_ev"] = round(max(g["min_ev"] + 0.02, g["max_ev"] + rng.choice([-0.03, -0.01, 0.01, 0.03])), 3)
        elif k in ("min_odds", "max_odds"):
            g["min_odds"], g["max_odds"] = r["min_odds"], r["max_odds"]
        elif k in ("min_h", "max_h"):
            g["min_h"], g["max_h"] = r["min_h"], r["max_h"]
        elif k in ("sport", "league"):
            g["sport"], g["league"] = r["sport"], r["league"]
        else:
            g[k] = r[k]
    return g


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    g = {k: (a if rng.random() < 0.5 else b)[k] for k in a}
    g["max_ev"] = max(g["max_ev"], g["min_ev"] + 0.02)
    if g["max_odds"] and g["max_odds"] <= g["min_odds"]:
        g["max_odds"] = None
    if g["max_h"] is not None and g["max_h"] <= g["min_h"]:
        g["max_h"] = None
    if g["league"] and g["sport"] != g["league"].split("_")[0]:
        g["league"] = None
    return g


def key_of(g: dict) -> str:
    return json.dumps(g, sort_keys=True)


def describe(g: dict) -> str:
    parts = [SPORT_ES.get(g["sport"], "Todos los deportes") if not g["league"] else g["league"].split("_", 1)[1].replace("_", " ").title()]
    if g["outcome"]:
        parts.append("solo " + OUT_ES[g["outcome"]])
    parts.append(f"valor {g['min_ev'] * 100:.1f}–{g['max_ev'] * 100:.0f} % ({REF_ES[g['ref']]})".replace(".", ","))
    parts.append(f"cuota {g['min_odds']:.2f}–{g['max_odds']:.2f}".replace(".", ",") if g["max_odds"]
                 else f"cuota desde {g['min_odds']:.2f}".replace(".", ","))
    if g["min_h"] or g["max_h"]:
        parts.append(f"{g['min_h']}–{g['max_h']} h antes" if g["max_h"] else f"más de {g['min_h']} h antes")
    if g["drift"]:
        parts.append("Pinnacle subiendo" if g["drift"] == "up" else "Pinnacle bajando")
    parts.append(f"Kelly {g['kf']:.2f}".replace(".", ","))
    return " · ".join(parts)


# ------------------------------------------------------------------ 4) evaluar
def row_ev(g: dict, row) -> float | None:
    _, _, o, fav, book, odds, p_pin, p_cons, hours, drift = row
    com = 0.05 if is_exchange(book) else 0.0
    if g["ref"] == "consensus":
        if p_cons is None:
            return None
        p = p_cons
    else:
        p = p_pin
    ev = p * (odds - 1) * (1 - com) - (1 - p)
    if g["ref"] == "both":
        if p_cons is None or p_cons * (odds - 1) * (1 - com) - (1 - p_cons) < g["min_ev"]:
            return None
    return ev


def matches(g: dict, row, sport: str) -> float | None:
    _, _, o, fav, book, odds, p_pin, p_cons, hours, drift = row
    if g["league"] and sport != g["league"]:
        return None
    if g["sport"] and not sport.startswith(g["sport"]):
        return None
    out = g["outcome"]
    if out == "home" and o != "h" or out == "away" and o != "a" or out == "draw" and o != "d":
        return None
    if out == "fav" and not fav or out == "dog" and (fav or o == "d"):
        return None
    if odds < g["min_odds"] or (g["max_odds"] and odds > g["max_odds"]):
        return None
    if hours < g["min_h"] or (g["max_h"] is not None and hours > g["max_h"]):
        return None
    if g["drift"] == "up" and (drift is None or drift < 0.01):
        return None
    if g["drift"] == "down" and (drift is None or drift > -0.01):
        return None
    ev = row_ev(g, row)
    if ev is None or ev < g["min_ev"] or ev > g["max_ev"]:
        return None
    return ev


def _stats(rets: list[float], clvs: list[float]) -> dict:
    n = len(rets)
    if not n:
        return {"n": 0, "roi": 0.0, "lb": -1.0, "clv": 0.0, "clv_t": 0.0, "n_clv": 0}
    m = sum(rets) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (n - 1)) if n > 1 else 1.0
    lb = m - 1.65 * sd / math.sqrt(n)
    nc = len(clvs)
    cm = sum(clvs) / nc if nc else 0.0
    csd = math.sqrt(sum((x - cm) ** 2 for x in clvs) / (nc - 1)) if nc > 1 else 0.0
    ct = cm / (csd / math.sqrt(nc)) if nc > 1 and csd > 0 else 0.0
    return {"n": n, "roi": round(m, 4), "lb": round(lb, 4), "sd": round(sd, 3),
            "clv": round(cm, 4), "clv_t": round(ct, 2), "n_clv": nc}


def evaluate(g: dict, lab: dict, split_min: int) -> dict:
    """Simula el método sobre todo lo observado: una apuesta por partido, en la primera ocasión que lo ve."""
    events = lab["events"]
    taken: dict[str, tuple] = {}
    for row in lab["rows"]:
        eid = row[1]
        ev_info = events.get(eid)
        if not ev_info:
            continue
        t0 = taken.get(eid)
        if t0 and t0[0] != row[0]:           # ya apostó en este partido en una búsqueda anterior
            continue
        ev = matches(g, row, ev_info[0])
        if ev is None:
            continue
        if not t0 or ev > t0[1]:
            taken[eid] = (row[0], ev, row)
    train_r, train_c, val_r, val_c = [], [], [], []
    for eid, (_, _, row) in taken.items():
        info = events[eid]
        res = info[5]
        odds, o = row[5], row[2]
        com = 0.05 if is_exchange(row[4]) else 0.0
        close = info[2 + "hda".index(o)]
        is_val = info[1] >= split_min
        if res in ("h", "d", "a"):
            (val_r if is_val else train_r).append((odds - 1) * (1 - com) if res == o else -1.0)
        if close:
            (val_c if is_val else train_c).append(odds * close - 1)
    return {"train": _stats(train_r, train_c), "val": _stats(val_r, val_c)}


def split_point(lab: dict) -> int:
    done = sorted(e[1] for e in lab["events"].values() if e[5] is not None)
    if not done:
        return 10 ** 12
    return done[int(len(done) * (1 - VAL_SHARE))] if len(done) > 10 else done[-1] + 1


SHRINK = 250   # cuántas apuestas «imaginarias» al 0 % se suman: frena a las reglas con poca muestra


def fitness(m: dict) -> float:
    tr = m["train"]
    if tr["n"] < 40:
        return -1.0 + tr["n"] / 1000          # todavía sin datos: no se descarta, se espera
    n = tr["n"]
    shrunk = tr["roi"] * n / (n + SHRINK)       # una rentabilidad alta con pocas apuestas vale poco
    return shrunk - tr.get("sd", 1.0) / math.sqrt(n) + 0.5 * max(-0.05, min(0.05, tr["clv"]))


def behaviour(p: dict) -> tuple:
    """Dos métodos que hacen exactamente las mismas apuestas son el mismo método."""
    t, v = p["m"]["train"], p["m"]["val"]
    return (t["n"], t["roi"], v["n"], v["roi"])


# ------------------------------------------------------------------ 5) evolucionar
def evolve(state_lab: dict, lab: dict, leagues: list[str], seed: int | None = None) -> dict:
    rng = random.Random(seed)
    pop = state_lab.setdefault("pop", [])
    tried = state_lab.setdefault("tried", 0)
    known = {key_of(p["g"]) for p in pop}
    banned = set(state_lab.get("rejected_keys", []))
    split = split_point(lab)

    for p in pop:                              # se re-examina todo con los datos nuevos
        p["m"] = evaluate(p["g"], lab, split)
    pop.sort(key=lambda p: -fitness(p["m"]))
    parents = [p["g"] for p in pop[:12]] or [random_genome(rng, leagues) for _ in range(6)]
    new = []
    while len(new) < NEW_PER_GEN:
        r = rng.random()
        if r < 0.55:
            g = mutate(rng.choice(parents), rng, leagues)
        elif r < 0.75 and len(parents) > 1:
            g = crossover(*rng.sample(parents, 2), rng)
        else:
            g = random_genome(rng, leagues)
        k = key_of(g)
        if k in known or k in banned:
            continue
        known.add(k)
        new.append({"id": secrets.token_hex(3), "g": g, "born": state_lab.get("gen", 0) + 1})
    for p in new:
        p["m"] = evaluate(p["g"], lab, split)
    tried += len(new)
    pop = sorted(pop + new, key=lambda p: -fitness(p["m"]))
    seen_b, uniq = set(), []
    for p in pop:                              # fuera clones
        b = behaviour(p)
        if b in seen_b and p["m"]["train"]["n"] >= 40:
            continue
        seen_b.add(b)
        uniq.append(p)
    pop = uniq
    # se guardan los mejores y algunos jóvenes sin datos aún, para no perder ideas por falta de muestra
    keep = pop[:POP_SIZE - 10] + [p for p in pop[POP_SIZE - 10:] if p["m"]["train"]["n"] < 40][:10]
    state_lab["pop"] = keep
    state_lab["tried"] = tried
    state_lab["gen"] = state_lab.get("gen", 0) + 1
    state_lab["split"] = split
    return state_lab


def classify(m: dict) -> str | None:
    v, tr = m["val"], m["train"]
    if v["n"] >= AUTO["n"] and v["lb"] >= AUTO["lb"] and v["clv_t"] >= AUTO["clv_t"] and tr["lb"] >= AUTO["train_lb"]:
        return "auto"
    if (v["n"] >= PROPOSE["n"] and v["lb"] > PROPOSE["lb"] and tr["n"] >= PROPOSE["n"] and tr["lb"] > 0
            and (v["n_clv"] < 50 or v["clv"] > PROPOSE["clv"])):
        return "propose"
    return None


def similar(a: dict, b: dict) -> bool:
    same = all(a[k] == b[k] for k in ("ref", "sport", "league", "outcome", "drift"))
    return same and abs(a["min_ev"] - b["min_ev"]) < 0.011


def to_method(p: dict) -> dict:
    """Traduce un genoma a las reglas que entiende el buscador de apuestas del bot."""
    g = p["g"]
    m = {"nombre": f"Lab #{p['id']}", "ref": g["ref"], "min_ev": g["min_ev"], "max_ev": g["max_ev"],
         "min_odds": g["min_odds"], "max_odds": g["max_odds"], "outcome": g["outcome"], "sport": g["sport"],
         "league": g["league"], "min_h": g["min_h"] or None, "max_h": g["max_h"], "drift": g["drift"], "kf": g["kf"]}
    return m


# ------------------------------------------------------------------ 6) proponer, promover, retirar
def pct(x: float) -> str:
    return f"{x * 100:+.1f} %".replace(".", ",")


def review(state_lab: dict, live: dict) -> list[tuple[str, str]]:
    """Decide propuestas, promociones automáticas y retiradas. Devuelve [(tipo, texto)].

    live: id del método promovido -> estadísticas de su cartera ficticia (method_stats del bot).
    """
    msgs = []
    promoted = state_lab.setdefault("promoted", [])
    pending = state_lab.setdefault("pending", [])
    active = [p for p in promoted if not p.get("retired")]

    # retirar los promovidos que, ya en la cartera ficticia, han dejado de funcionar
    for p in active:
        x = live.get(p["id"])
        if x and x["n"] >= RETIRE_AFTER and (x["clv"] < 0 or x["roi"] < -0.05):
            p["retired"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            msgs.append(("retirado", f"🗑 <b>Lab #{p['id']}</b> retirado: en la cartera ficticia lleva {x['n']} apuestas, "
                                     f"rentabilidad {pct(x['roi'])} y CLV {pct(x['clv'])}. El laboratorio sigue buscando otros."))
    active = [p for p in promoted if not p.get("retired")]

    pend_ids = {q["id"] for q in pending}
    for p in state_lab.get("pop", []):
        if p["id"] in pend_ids or any(q["id"] == p["id"] for q in promoted):
            continue
        if any(similar(p["g"], q["g"]) for q in active + pending):
            continue
        kind = classify(p["m"])
        if not kind:
            continue
        v = p["m"]["val"]
        desc = describe(p["g"])
        if kind == "auto" and len(active) < MAX_PROMOTED:
            p2 = {"id": p["id"], "g": p["g"], "m": p["m"], "desc": desc,
                  "since": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "how": "automático"}
            promoted.append(p2)
            active.append(p2)
            msgs.append(("auto", f"🚀 <b>Lab #{p['id']} pasa solo a la cartera ficticia</b> (resultado excepcional)\n{desc}\n"
                                 f"Validación: {v['n']} apuestas, rentabilidad {pct(v['roi'])} (mínimo probable {pct(v['lb'])}), "
                                 f"CLV {pct(v['clv'])}."))
        elif kind in ("propose", "auto") and len(pending) < MAX_PENDING:
            pending.append({"id": p["id"], "g": p["g"], "m": p["m"], "desc": desc,
                            "since": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
            pend_ids.add(p["id"])
            msgs.append(("propuesta", f"🧪 <b>El laboratorio propone Lab #{p['id']}</b>\n{desc}\n"
                                      f"Validación: {v['n']} apuestas, rentabilidad {pct(v['roi'])} (mínimo probable {pct(v['lb'])}), "
                                      f"CLV {pct(v['clv'])}.\nApruébalo o recházalo en la web, pestaña Lab."))
    # las propuestas que ya no superan la validación con los datos nuevos se caen solas
    pop_by_id = {p["id"]: p for p in state_lab.get("pop", [])}
    for q in list(pending):
        p = pop_by_id.get(q["id"])
        if p:
            q["m"] = p["m"]
        if p and not classify(p["m"]):
            pending.remove(q)
            msgs.append(("caducada", f"↩️ Lab #{q['id']} ya no supera la validación con los datos nuevos; retiro la propuesta."))
    return msgs


def decide(state_lab: dict, pid: str, ok: bool) -> str | None:
    pending = state_lab.setdefault("pending", [])
    q = next((x for x in pending if x["id"] == pid), None)
    if not q:
        return None
    pending.remove(q)
    if ok:
        q.update(since=datetime.now(timezone.utc).strftime("%Y-%m-%d"), how="aprobado por ti")
        state_lab.setdefault("promoted", []).append(q)
        return f"✅ Lab #{pid} aprobado: empieza a apostar en la cartera ficticia."
    state_lab.setdefault("rejected_keys", []).append(key_of(q["g"]))
    state_lab["rejected_keys"] = state_lab["rejected_keys"][-500:]
    state_lab["pop"] = [p for p in state_lab.get("pop", []) if p["id"] != pid]
    return f"🚫 Lab #{pid} rechazado: el laboratorio no lo volverá a proponer."


def summary(state_lab: dict, lab: dict) -> dict:
    """Lo que necesita la web (pequeño, va en el estado cifrado)."""
    pop = sorted(state_lab.get("pop", []), key=lambda p: -fitness(p["m"]))
    return {
        "gen": state_lab.get("gen", 0), "tried": state_lab.get("tried", 0),
        "rows": len(lab["rows"]), "events": len(lab["events"]),
        "resolved": sum(1 for e in lab["events"].values() if e[5] is not None),
        "last": state_lab.get("last_day"),
        "top": [{"id": p["id"], "desc": describe(p["g"]), "m": p["m"], "born": p.get("born")} for p in pop[:15]],
        "pending": [{"id": q["id"], "desc": q["desc"], "m": q["m"], "since": q["since"]} for q in state_lab.get("pending", [])],
        "promoted": [{"id": q["id"], "desc": q["desc"], "m": q["m"], "since": q["since"], "how": q.get("how"),
                      "retired": q.get("retired")} for q in state_lab.get("promoted", [])],
    }
