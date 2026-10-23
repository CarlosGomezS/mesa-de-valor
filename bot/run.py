#!/usr/bin/env python3
"""Mesa de Valor — motor que ejecuta GitHub Actions cada hora.

Lee los ajustes cifrados que guarda la web, busca apuestas con valor contra Pinnacle,
lleva dos cuentas separadas (ficticia y real), avisa por Telegram y guarda el estado cifrado.

Variables de entorno: APP_PASSWORD (obligatoria), FORZAR=si para buscar ya.
"""
from __future__ import annotations

import base64
import html
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy as st  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "data"
UTC = timezone.utc
MAD = ZoneInfo("Europe/Madrid")
ITER = 210_000

DEFAULT_SETTINGS = {
    "odds_api_key": "", "telegram_token": "", "telegram_chat_id": "",
    "ficticio": {"activo": True, "banca_inicial": 50.0, "avisar": True},
    "real": {"activo": False, "banca_inicial": 50.0, "metodo": "valor"},
    "ligas": ["soccer_spain_la_liga", "soccer_spain_segunda_division", "soccer_efl_champ",
              "basketball_euroleague", "tennis_all", "handball_germany_bundesliga"],
    "casas": ["williamhill", "sport888", "betway", "betfair_sb_uk", "leovegas"],
    "resumen_diario": True,
    "avanzado": {"valor_minimo": 0.03, "valor_maximo": 0.15, "fraccion_kelly": 0.25, "tope": 0.04,
                 "minima": 0.5, "limite_diario": 0.10, "limite_semanal": 0.20, "parar_si_cae": 0.40,
                 "max_dia": 6, "max_exposicion": 0.30, "horas_vista": 30, "horas_entre_busquedas": 8,
                 "metodo": "power"},
}


# ============================ métodos ============================
# Cada método tiene su propia cartera ficticia con el mismo dinero inicial.
METHODS = {
    # --- valor contra Pinnacle (la referencia más precisa) ---
    "valor":       {"nombre": "Valor contra Pinnacle", "ref": "pinnacle"},
    "estricto":    {"nombre": "Valor alto (5 %+)", "ref": "pinnacle", "min_ev": 0.05, "max_odds": 4.0},
    "volumen":     {"nombre": "Volumen (1,5 %+)", "ref": "pinnacle", "min_ev": 0.015},
    "favoritos":   {"nombre": "Favoritos con valor", "ref": "pinnacle", "min_ev": 0.02, "max_odds": 2.2},
    "medias":      {"nombre": "Cuotas medias (1,6–3)", "ref": "pinnacle", "min_odds": 1.6, "max_odds": 3.0},
    "longshots":   {"nombre": "Sorpresas con valor", "ref": "pinnacle", "min_ev": 0.05, "min_odds": 4.0},
    "empates":     {"nombre": "Solo empates", "ref": "pinnacle", "min_ev": 0.02, "outcome": "draw"},
    "tenis":       {"nombre": "Solo tenis", "ref": "pinnacle", "sport": "tennis"},
    "balonmano":   {"nombre": "Solo balonmano", "ref": "pinnacle", "sport": "handball"},
    "cerca":       {"nombre": "Cerca del inicio (<6 h)", "ref": "pinnacle", "min_ev": 0.02, "max_h": 6},
    "anticipado":  {"nombre": "Con antelación (>12 h)", "ref": "pinnacle", "min_ev": 0.04, "min_h": 12},
    "steam":       {"nombre": "Movimiento de Pinnacle", "ref": "steam", "min_ev": 0.015},
    "futbol":      {"nombre": "Solo fútbol", "ref": "pinnacle", "sport": "soccer"},
    "baloncesto":  {"nombre": "Solo baloncesto", "ref": "pinnacle", "sport": "basketball"},
    "francotirador": {"nombre": "Francotirador (1 al día)", "ref": "pinnacle", "min_ev": 0.04, "max_dia": 1},
    # --- otras referencias ---
    "consenso":    {"nombre": "Consenso de casas", "ref": "consensus", "min_ev": 0.05, "stake": "flat"},
    "doble":       {"nombre": "Doble confirmación", "ref": "both"},
    # --- gestión del dinero ---
    "plano":       {"nombre": "Valor con importe fijo", "ref": "pinnacle", "stake": "flat"},
    "agresivo":    {"nombre": "Valor agresivo (½ Kelly)", "ref": "pinnacle", "kf": 0.5},
    # --- comparación ---
    "control":     {"nombre": "Control: siempre el favorito", "ref": "control", "stake": "flat"},
}
FLAT_PCT = 0.01


# ============================ cifrado (compatible con la web) ============================

def _key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITER, 32)


def encrypt(obj, password: str) -> str:
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    ct = AESGCM(_key(password, salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()
    return json.dumps({"v": 1, "salt": b(salt), "iv": b(iv), "ct": b(ct)})


def decrypt(text: str, password: str):
    d = json.loads(text)
    u = base64.b64decode
    return json.loads(AESGCM(_key(password, u(d["salt"]))).decrypt(u(d["iv"]), u(d["ct"]), None))


def read_enc(name: str, password: str, default):
    p = DATA / name
    if not p.exists() or not p.read_text().strip():
        return default
    return decrypt(p.read_text(), password)


# ============================ utilidades ============================

def now() -> datetime:
    return datetime.now(UTC)


def iso(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def local(s: str) -> str:
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    d = pt(s).astimezone(MAD)
    return f"{dias[d.weekday()]} {d:%d/%m %H:%M}"


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def http_json(url: str, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null"), dict(r.headers)


# ============================ fuentes ============================

class Odds:
    BASE = "https://api.the-odds-api.com/v4"

    def __init__(self, key: str):
        self.key, self.remaining, self.used = key, None, 0

    def _get(self, path: str, q: dict):
        q = {"apiKey": self.key, **q}
        data, h = http_json(f"{self.BASE}{path}?{urllib.parse.urlencode(q)}")
        self.remaining = h.get("x-requests-remaining") or h.get("X-Requests-Remaining")
        self.used += 1
        return data or []

    def events_count(self, sport: str, frm: datetime, to: datetime) -> int:
        """Partidos de la liga en la ventana. Esta consulta es gratuita (no gasta créditos)."""
        q = {"apiKey": self.key, "commenceTimeFrom": frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "commenceTimeTo": to.strftime("%Y-%m-%dT%H:%M:%SZ")}
        data, _ = http_json(f"{self.BASE}/sports/{sport}/events?{urllib.parse.urlencode(q)}")
        return len(data or [])

    def odds(self, sport: str, books: list[str], frm: datetime | None = None, to: datetime | None = None):
        q = {"markets": "h2h", "oddsFormat": "decimal"}
        if frm and to:
            q["commenceTimeFrom"] = frm.strftime("%Y-%m-%dT%H:%M:%SZ")
            q["commenceTimeTo"] = to.strftime("%Y-%m-%dT%H:%M:%SZ")
        if books:
            q["bookmakers"] = ",".join(sorted(set(["pinnacle"] + books)))
        else:
            q["regions"] = "eu"
        return self._get(f"/sports/{sport}/odds/", q)

    def active_sports(self) -> list[str]:
        """Lista de competiciones activas. Gratuita."""
        data, _ = http_json(f"{self.BASE}/sports/?{urllib.parse.urlencode({'apiKey': self.key})}")
        return [x["key"] for x in (data or []) if x.get("active") and not x.get("has_outrights")]

    def scores(self, sport: str):
        return self._get(f"/sports/{sport}/scores/", {"daysFrom": 3})


def telegram_long(s: dict, sections: list[str]):
    """Envía varias secciones juntas, partiendo en mensajes de menos de 4.000 caracteres."""
    buf = ""
    for sec in sections:
        if buf and len(buf) + len(sec) + 2 > 3800:
            telegram(s, buf)
            buf = ""
        while len(sec) > 3800:
            telegram(s, sec[:3800])
            sec = sec[3800:]
        buf = f"{buf}\n\n{sec}" if buf else sec
    if buf:
        telegram(s, buf)


def h(x) -> str:
    return html.escape(str(x), quote=False)


def pc(x: float) -> str:
    return f"{x * 100:+.1f} %".replace(".", ",")


def eur(x: float) -> str:
    return f"{x:+.2f} €".replace(".", ",")


def telegram(s: dict, text: str):
    tok, chat = s.get("telegram_token"), str(s.get("telegram_chat_id") or "")
    if not tok or not chat:
        return
    try:
        http_json(f"https://api.telegram.org/bot{tok}/sendMessage", urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode())
    except Exception as e:
        print("Telegram falló:", e)


# ============================ referencia ============================

def reference(events: list[dict], method: str) -> dict:
    out = {}
    for ev in events:
        books = {}
        for b in ev.get("bookmakers", []):
            m = next((m for m in b.get("markets", []) if m["key"] == "h2h"), None)
            if m:
                books[b["key"]] = {"title": b["title"], "prices": {o["name"]: o["price"] for o in m["outcomes"]}}
        pin = books.get("pinnacle")
        if not pin or len(pin["prices"]) < 2:
            continue
        names = list(pin["prices"])
        fair = st.devig([pin["prices"][n] for n in names], method)
        # consenso: media de las probabilidades sin margen de todas las casas con el mercado completo
        cons = [0.0] * len(names)
        nb = 0
        for b in books.values():
            if all(n in b["prices"] and b["prices"][n] > 1 for n in names):
                for i, p in enumerate(st.devig([b["prices"][n] for n in names], "mult")):
                    cons[i] += p
                nb += 1
        out[ev["id"]] = {"home": ev["home_team"], "away": ev["away_team"], "commence": ev["commence_time"],
                         "fair": dict(zip(names, fair)), "books": books,
                         "consensus": dict(zip(names, [c / nb for c in cons])) if nb >= 3 else None}
    return out


# ============================ cuentas ============================

def bank0(s: dict, name: str) -> float:
    return s["real"]["banca_inicial"] if name == "real" else s["ficticio"]["banca_inicial"]


def ledger_bank(state: dict, s: dict, name: str) -> float:
    bets = state["ledgers"][name]["bets"]
    return bank0(s, name) + sum(b.get("pnl") or 0 for b in bets if b["status"] in ("ganada", "perdida", "nula"))


def risk_block(state: dict, s: dict, name: str, bank: float, m: dict | None = None) -> str | None:
    a, b0 = s["avanzado"], bank0(s, name)
    bets = state["ledgers"][name]["bets"]
    t = now()
    closed = [b for b in bets if b["status"] in ("ganada", "perdida") and b.get("settled")]
    day = sum(b["pnl"] for b in closed if pt(b["settled"]) > t - timedelta(days=1))
    week = sum(b["pnl"] for b in closed if pt(b["settled"]) > t - timedelta(days=7))
    if day <= -a["limite_diario"] * b0:
        return "límite de pérdida diario alcanzado"
    if week <= -a["limite_semanal"] * b0:
        return "límite de pérdida semanal alcanzado"
    if bank <= b0 * (1 - a["parar_si_cae"]):
        return "la banca ha caído demasiado: pausa y revisa"
    if sum(1 for b in bets if pt(b["created"]) > t - timedelta(days=1) and b["status"] != "descartada") >= ((m or {}).get("max_dia") or a["max_dia"]):
        return "máximo de apuestas en 24 h"
    return None


def exposure(state, name) -> float:
    return sum(b["stake"] for b in state["ledgers"][name]["bets"] if b["status"] in ("abierta", "propuesta"))


def find_value(ref: dict, s: dict, allowed: set, m: dict | None = None, sport: str = "") -> list[tuple]:
    a = s["avanzado"]
    m = m or METHODS["valor"]
    if m.get("sport") and not sport.startswith(m["sport"]):
        return []
    min_ev = m["min_ev"] if m.get("min_ev") is not None else a["valor_minimo"]
    found = []
    for eid, r in ref.items():
        mins = (pt(r["commence"]) - now()).total_seconds() / 60
        if mins < 20 or mins > a["horas_vista"] * 60:
            continue
        if (m.get("max_h") and mins > m["max_h"] * 60) or (m.get("min_h") and mins < m["min_h"] * 60):
            continue
        kind = m["ref"]
        probs = r["consensus"] if kind == "consensus" else r["fair"]
        if not probs or (kind == "both" and not r.get("consensus")):
            continue
        non_draw = [n for n in r["fair"] if n != "Draw"]
        fav = max(non_draw, key=lambda n: r["fair"][n])
        allowed_out = {"draw": {"Draw"}, "home": {r["home"]}, "away": {r["away"]}}.get(m.get("outcome"))
        best = None
        for key, b in r["books"].items():
            if key == "pinnacle" or key.startswith("betfair_ex") or key == "matchbook" or (allowed and key not in allowed):
                continue
            for name, price in b["prices"].items():
                p = probs.get(name)
                if not p:
                    continue
                if kind == "control":
                    if name == fav and (not best or price > best[4]):
                        best = (st.ev_back(r["fair"][name], price), key, b["title"], name, price, r["fair"][name])
                    continue
                if allowed_out and name not in allowed_out:
                    continue
                if (m.get("max_odds") and price > m["max_odds"]) or (m.get("min_odds") and price < m["min_odds"]):
                    continue
                ev = st.ev_back(p, price)
                if kind == "both" and st.ev_back(r["consensus"][name], price) < min_ev:
                    continue
                if kind == "steam":
                    prev = (r.get("prev") or {}).get(name)
                    if prev is None or p - prev < 0.02:      # Pinnacle ha subido su probabilidad 2 puntos o más
                        continue
                if min_ev <= ev <= a["valor_maximo"] and (not best or ev > best[0]):
                    best = (ev, key, b["title"], name, price, p)
        if best:
            found.append((eid, r) + best)
    return sorted(found, key=lambda x: -x[2])


def place(state, s, sport, cands, name: str, m: dict | None = None) -> list[dict]:
    """Ficticio: apuesta directamente. Real: crea una propuesta que tú confirmas."""
    a = s["avanzado"]
    m = m or METHODS["valor"]
    rules = st.StakeRules(a["fraccion_kelly"], a["tope"], a["minima"], True)
    bets = state["ledgers"][name]["bets"]
    new = []
    bank = ledger_bank(state, s, name)
    block = risk_block(state, s, name, bank, m)
    if block:
        note(state, f"{name}: sin apuestas nuevas ({block})")
        return new
    for eid, r, ev, key, title, outcome, price, p in cands:
        if any(b["event_id"] == eid and b["status"] != "descartada" for b in bets):
            continue
        if m.get("stake") == "flat":
            stake = round(max(a["minima"], bank * FLAT_PCT), 2) if bank >= a["minima"] else 0
        else:
            r_ = rules if not m.get("kf") else st.StakeRules(m["kf"], a["tope"], a["minima"], True)
            stake = st.size_stake(bank, st.kelly_back(p, price), r_)
        if stake <= 0 or exposure(state, name) + stake > a["max_exposicion"] * bank:
            continue
        if risk_block(state, s, name, bank, m):
            break
        b = {"id": secrets.token_hex(4), "created": iso(now()), "sport": sport, "event_id": eid,
             "event": f"{r['home']} - {r['away']}", "commence": r["commence"],
             "selection": "Empate" if outcome == "Draw" else outcome, "outcome_key": outcome,
             "book": title, "book_key": key, "odds": price, "min_odds": round((1 + a["valor_minimo"]) / p, 2),
             "stake": stake, "fair_p": round(p, 4), "ev": round(ev, 4), "close_fair_odds": None,
             "status": "propuesta" if name == "real" else "abierta", "pnl": None, "settled": None,
             "method": next((k for k, v in METHODS.items() if v is m), "valor")}
        bets.append(b)
        new.append(b)
    return new


def apply_inbox(state, inbox: list[dict]) -> list[str]:
    done = set(state.get("applied", []))
    msgs = []
    for act in inbox:
        if act.get("id") in done:
            continue
        for b in state["ledgers"]["real"]["bets"]:
            if b["id"] != act.get("bet"):
                continue
            if act["type"] == "hecha" and b["status"] == "propuesta":
                b["status"] = "abierta"
                b["odds"] = float(act.get("odds") or b["odds"])
                b["stake"] = float(act.get("stake") or b["stake"])
                b["ev"] = round(st.ev_back(b["fair_p"], b["odds"]), 4)
                msgs.append(f"📝 Anotada tu apuesta real: {b['event']} · {b['selection']} a {b['odds']:.2f} · {b['stake']:.2f} €")
            elif act["type"] == "no" and b["status"] == "propuesta":
                b["status"] = "descartada"
                msgs.append(f"🚫 Apuesta real descartada: {b['event']} · {b['selection']}")
            elif act["type"] == "resultado" and b["status"] == "abierta":
                settle_bet(b, act["result"])
        done.add(act.get("id"))
    state["applied"] = list(done)[-500:]
    return msgs


def settle_bet(b: dict, result: str):
    b["status"] = result
    b["pnl"] = round(b["stake"] * (b["odds"] - 1), 2) if result == "ganada" else (-b["stake"] if result == "perdida" else 0.0)
    b["settled"] = iso(now())


def note(state, text):
    state.setdefault("log", []).append({"ts": iso(now()), "text": text})
    state["log"] = state["log"][-60:]
    print(text)


# ============================ presupuesto de consultas ============================

def budget(state: dict, odds: "Odds", t: datetime) -> dict:
    """Reparte los créditos que quedan entre los días que faltan de mes, para no quedarse sin ellos."""
    day = t.astimezone(MAD).strftime("%Y-%m-%d")
    b = state.get("budget") or {}
    rem = int(odds.remaining) if str(odds.remaining or "").isdigit() else None
    if b.get("day") != day and rem is not None:
        nxt = (t.astimezone(MAD).replace(day=28) + timedelta(days=4)).replace(day=1)
        days_left = max(1, (nxt.date() - t.astimezone(MAD).date()).days)
        b = {"day": day, "start": rem, "allow": max(4, (rem - 10) // days_left), "warned": False}
        state["budget"] = b
    return b


def can_spend(state: dict, odds: "Odds") -> bool:
    b = state.get("budget") or {}
    rem = int(odds.remaining) if str(odds.remaining or "").isdigit() else None
    if rem is None or not b:
        return True
    if rem <= 10:
        return False
    return (b["start"] - rem) < b["allow"]


# ============================ ciclo ============================

def run(password: str, force: bool = False):
    s = merge(DEFAULT_SETTINGS, read_enc("settings.enc", password, {}))
    state = read_enc("state.enc", password, None) or {
        "version": 1, "ledgers": {"real": {"bets": []}},
        "snapshots": {}, "last_scan": None, "applied": [], "log": [], "last_summary": None}
    L = state["ledgers"]
    if "ficticio" in L:                       # versión anterior: una sola cartera ficticia
        L["f_valor"] = L.pop("ficticio")
    for k in METHODS:
        L.setdefault("f_" + k, {"bets": []})
    if s.get("reset_ficticio") and s["reset_ficticio"] != state.get("reset_ficticio"):
        for k in METHODS:
            L["f_" + k]["bets"] = []
        state["reset_ficticio"] = s["reset_ficticio"]
        note(state, "Modo ficticio reiniciado desde cero")
        telegram(s, "🔄 Modo ficticio reiniciado: todos los métodos vuelven a empezar con su dinero inicial.")
    inbox = read_enc("inbox.enc", password, [])
    inbox_msgs = apply_inbox(state, inbox)
    a = s["avanzado"]
    t = now()

    if not s.get("odds_api_key"):
        note(state, "Falta la clave de The Odds API en Ajustes.")
        telegram(s, "⚠️ Falta la clave de The Odds API en Ajustes. El bot no puede buscar.")
        save(state, password)
        return

    odds = Odds(s["odds_api_key"])
    books = s.get("casas") or []
    allowed = set(books)
    all_bets = [b for L in state["ledgers"].values() for b in L["bets"]]

    # 1) propuestas reales no confirmadas antes del inicio -> descartadas
    for b in state["ledgers"]["real"]["bets"]:
        if b["status"] == "propuesta" and pt(b["commence"]) < t:
            b["status"] = "descartada"

    # 2) liquidar partidos terminados
    report: list[str] = [h(x) for x in inbox_msgs]
    settled_now: list[tuple[str, dict, str]] = []     # (cartera, apuesta, marcador)
    owner = {id(b): name for name, Lg in state["ledgers"].items() for b in Lg["bets"]}
    due = {b["sport"] for b in all_bets if b["status"] == "abierta" and pt(b["commence"]) < t - timedelta(hours=2, minutes=30)}
    for sport in due:
        try:
            res = {x["id"]: x for x in odds.scores(sport) if x.get("completed") and x.get("scores")}
        except Exception as e:
            note(state, f"No pude leer resultados de {sport}: {e}")
            report.append(f"⚠️ No pude leer resultados de {h(sport)}: {h(e)}")
            continue
        for b in all_bets:
            x = res.get(b["event_id"])
            if b["status"] != "abierta" or b["sport"] != sport or not x:
                continue
            sc = {y["name"]: float(y["score"]) for y in x["scores"]}
            hs, w = sc.get(x["home_team"]), sc.get(x["away_team"])
            if hs is None or w is None:
                continue
            winner = x["home_team"] if hs > w else x["away_team"] if w > hs else "Draw"
            settle_bet(b, "ganada" if winner == b["outcome_key"] else "perdida")
            settled_now.append((owner.get(id(b), "?"), b, f"{x['home_team']} {int(hs)}-{int(w)} {x['away_team']}"))

    for name, Lg in state["ledgers"].items():
        for b in Lg["bets"]:
            if b["status"] == "abierta" and pt(b["commence"]) < t - timedelta(hours=48):
                settle_bet(b, "nula")
                settled_now.append((name, b, b["event"] + " (sin resultado: anulada, se devuelve el importe)"))
    if settled_now:
        by_match: dict[str, list] = {}
        for name, b, score in settled_now:
            by_match.setdefault(score, []).append((name, b))
        lines = ["🏁 <b>Resultados</b>"]
        for score, items in by_match.items():
            lines.append(f"\n<b>{h(score)}</b>")
            for name, b in sorted(items, key=lambda x: -(x[1]["pnl"] or 0)):
                who = "💶 REAL" if name == "real" else METHODS.get(name[2:], {}).get("nombre", name)
                icon = "✅" if b["status"] == "ganada" else "↩️" if b["status"] == "nula" else "❌"
                lines.append(f"{icon} {h(who)}: {h(b['selection'])} "
                             f"{b['odds']:.2f} → {eur(b['pnl'])}")
        report.append("\n".join(lines))

    # 3) cuota de cierre (CLV): partidos apostados que empiezan en la próxima hora y cuarto
    soon = {b["sport"] for b in all_bets if b["status"] in ("abierta",) and b.get("close_fair_odds") is None
            and t < pt(b["commence"]) <= t + timedelta(minutes=75)}

    # 4) búsqueda completa cada X horas (o si la pides desde la web)
    last = state.get("last_scan")
    full = force or not last or pt(last) <= t - timedelta(hours=a["horas_entre_busquedas"]) + timedelta(minutes=10)
    leagues = list(s["ligas"])
    if "tennis_all" in leagues:
        leagues.remove("tennis_all")
        try:
            leagues += [k for k in odds.active_sports() if k.startswith("tennis_")]
        except Exception as e:
            report.append(f"⚠️ No pude leer los torneos de tenis activos: {h(e)}")
    budget(state, odds, t)
    if full and not force and not can_spend(state, odds):
        full = False
        if not state["budget"].get("warned"):
            state["budget"]["warned"] = True
            report.append(f"💳 Hoy ya se han usado las consultas previstas ({state['budget']['allow']}). "
                          f"Sigo cobrando resultados, y mañana vuelvo a buscar. Quedan {odds.remaining}.")
    sports = list(dict.fromkeys((leagues if full else []) + list(soon)))
    scan_summary = []
    clv_now: dict[str, list] = {}
    new_fict: dict[str, list] = {}
    skipped = []
    real_msgs: list[str] = []
    for sport in sports:
        try:
            if sport in soon and not full:
                if not can_spend(state, odds):
                    continue
                ref = reference(odds.odds(sport, books, t, t + timedelta(minutes=90)), a["metodo"])
            else:
                frm, to = t, t + timedelta(hours=a["horas_vista"])
                if odds.events_count(sport, frm, to) == 0:   # gratis: si no hay partidos, no gastamos créditos
                    skipped.append(sport)
                    scan_summary.append((sport, 0, 0, 0))
                    continue
                ref = reference(odds.odds(sport, books, frm, to), a["metodo"])
        except Exception as e:
            note(state, f"No pude leer cuotas de {sport}: {e}")
            report.append(f"⚠️ No pude leer cuotas de {h(sport)}: {h(e)}")
            continue
        for eid, r in ref.items():
            r["prev"] = (state["snapshots"].get(eid) or {}).get("fair")
            state["snapshots"][eid] = {"fair": r["fair"], "ts": iso(t), "commence": r["commence"]}
        for b in all_bets:
            if b["sport"] == sport and b.get("close_fair_odds") is None and b["event_id"] in ref \
                    and t < pt(b["commence"]) <= t + timedelta(minutes=75) and b["status"] == "abierta":
                p = ref[b["event_id"]]["fair"].get(b["outcome_key"])
                if p:
                    b["close_fair_odds"] = round(1 / p, 3)
                    clv_now.setdefault(b["event"], []).append(b)
        if sport not in leagues or not full:
            continue
        cands = find_value(ref, s, allowed, METHODS["valor"], sport)
        in_window = sum(1 for r in ref.values()
                        if 20 <= (pt(r["commence"]) - t).total_seconds() / 60 <= a["horas_vista"] * 60)
        scan_summary.append((sport, len(ref), in_window, len(cands)))
        if s["ficticio"]["activo"]:
            for k, m in METHODS.items():
                for b in place(state, s, sport, cands if k == "valor" else find_value(ref, s, allowed, m, sport), "f_" + k, m):
                    new_fict.setdefault(b["event"], []).append((m["nombre"], b))
        if s["real"]["activo"]:
            rm = METHODS.get(s["real"].get("metodo"), METHODS["valor"])
            for b in place(state, s, sport, cands if rm is METHODS["valor"] else find_value(ref, s, allowed, rm, sport), "real", rm):
                real_msgs.append(f"💶 <b>APUESTA REAL</b>\n<b>{h(b['event'])}</b> · {local(b['commence'])}\n"
                                 f"Entra en <b>{h(b['book'])}</b> y apuesta a <b>{h(b['selection'])}</b>\n"
                                 f"Cuota: <b>{b['odds']:.2f}</b> (no bajes de {b['min_odds']:.2f})\n"
                                 f"Importe: <b>{b['stake']:.2f} €</b>\n"
                                 f"Después pulsa «La he hecho» en la web.")
    if clv_now:
        lines = ["📏 <b>Cuota de cierre tomada</b> (positivo = cogiste mejor precio que el mercado)"]
        for ev_name, bs in clv_now.items():
            b0_ = bs[0]
            lines.append(f"<b>{h(ev_name)}</b>: cierre justo {b0_['close_fair_odds']:.2f}")
            seen = set()
            for b in bs:
                key = (b["selection"], b["odds"])
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"  {h(b['selection'])} a {b['odds']:.2f} → CLV {pc(b['odds'] / b['close_fair_odds'] - 1)}")
        report.append("\n".join(lines))
    if new_fict and s["ficticio"].get("avisar", True):
        n_tot = sum(len(v) for v in new_fict.values())
        lines = [f"🧪 <b>Nuevas apuestas ficticias</b> ({n_tot})"]
        for ev_name, items in sorted(new_fict.items(), key=lambda kv: kv[1][0][1]["commence"]):
            lines.append(f"\n<b>{h(ev_name)}</b> · {local(items[0][1]['commence'])}")
            groups: dict[tuple, list] = {}
            for mname, b in items:
                groups.setdefault((b["selection"], b["odds"], b["book"]), []).append((mname, b))
            for (sel, o, book), ms in groups.items():
                lines.append(f"• {h(sel)} a {o:.2f} en {h(book)} (valor {pc(ms[0][1]['ev'])})")
                lines.append("   " + h(", ".join(f"{mn} {b['stake']:.2f}€".replace(".", ",") for mn, b in ms)))
        report.append("\n".join(lines))
    if full:
        state["last_scan"] = iso(t)
        names = {"soccer_spain_la_liga": "LaLiga", "soccer_epl": "Premier", "soccer_uefa_champs_league": "Champions",
                 "soccer_uefa_europa_league": "Europa League", "soccer_italy_serie_a": "Serie A",
                 "soccer_germany_bundesliga": "Bundesliga", "soccer_france_ligue_one": "Ligue 1",
                 "soccer_spain_segunda_division": "Segunda", "basketball_euroleague": "Euroliga", "basketball_nba": "NBA",
                 "soccer_efl_champ": "Championship", "handball_germany_bundesliga": "Balonmano Bundesliga"}
        parts = [f"{names.get(sp, sp.replace('tennis_atp_', 'Tenis ATP ').replace('tennis_wta_', 'Tenis WTA ').replace('_', ' '))}: {n} partidos, {w} en las próximas {a['horas_vista']} h, {c} con valor"
                 for sp, n, w, c in scan_summary]
        state["last_scan_summary"] = {"ts": iso(t), "lines": parts,
                                      "found": sum(c for *_, c in scan_summary)}
        note(state, "Búsqueda: " + " · ".join(parts) if parts else "Búsqueda: sin datos de cuotas")
        found = sum(c for *_, c in scan_summary)
        pauses = [x["text"] for x in state.get("log", []) if x["ts"] >= iso(t) and "sin apuestas nuevas" in x["text"]]
        msg = [f"🔎 <b>Búsqueda terminada</b> · {local(iso(t))}",
               f"Partidos con valor (método principal): {found}"]
        msg += [h(x) for x in parts] or ["Sin datos de cuotas"]
        if pauses:
            msg.append("⏸ Pausas: " + h("; ".join(p.split(": ", 1)[-1] for p in pauses)))
        msg.append(f"Consultas gastadas: {odds.used} · quedan {odds.remaining or '?'}")
        report.insert(0, "\n".join(msg))
    # limpiar fotos viejas
    state["snapshots"] = {k: v for k, v in state["snapshots"].items() if pt(v["commence"]) > t - timedelta(days=2)}

    # 5) resumen diario a las 21 h
    loc = t.astimezone(MAD)
    if s.get("resumen_diario") and loc.hour >= 21 and state.get("last_summary") != loc.strftime("%Y-%m-%d"):
        rows = []
        names = (["f_" + k for k in METHODS] if s["ficticio"]["activo"] else []) + (["real"] if s["real"]["activo"] else [])
        for name in names:
            bank = ledger_bank(state, s, name)
            bets = state["ledgers"][name]["bets"]
            n_done = sum(1 for b in bets if b["status"] in ("ganada", "perdida"))
            n_open = sum(1 for b in bets if b["status"] == "abierta")
            label = "💶 REAL" if name == "real" else METHODS[name[2:]]["nombre"]
            rows.append((bank - bank0(s, name), f"{h(label)}: {bank:.2f} € ({eur(bank - bank0(s, name))}) · "
                                                 f"{n_done} terminadas, {n_open} pendientes".replace(".", ",")))
        if rows:
            rows.sort(key=lambda r: -r[0])
            report.append("📊 <b>Resumen del día</b> (de más a menos ganancia)\n" + "\n".join(r[1] for r in rows))
        state["last_summary"] = loc.strftime("%Y-%m-%d")

    state["credits"] = odds.remaining
    state["updated"] = iso(t)
    note(state, f"Pasada {'completa' if full else 'corta'}: {odds.used} consultas · créditos restantes {odds.remaining}")
    save(state, password)
    for m_ in real_msgs:                       # las reales, cada una en su mensaje: requieren que actúes
        telegram(s, m_)
    if report:
        telegram_long(s, report)


def save(state, password):
    state["updated"] = iso(now())
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "state.enc").write_text(encrypt(state, password))


if __name__ == "__main__":
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        sys.exit("Falta el secreto APP_PASSWORD. Créalo desde la web, en el primer arranque.")
    try:
        run(pw, os.environ.get("FORZAR", "").lower() in ("si", "sí", "true", "1"))
    except Exception as e:  # un fallo no debe romper la ejecución de GitHub (eso manda correos)
        import traceback
        traceback.print_exc()
        try:
            s_ = merge(DEFAULT_SETTINGS, read_enc("settings.enc", pw, {}))
            telegram(s_, f"⚠️ <b>Error del bot</b>: {h(type(e).__name__)}: {h(e)}\nVolverá a intentarlo en la próxima pasada.")
        except Exception:
            pass
