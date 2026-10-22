#!/usr/bin/env python3
"""Mesa de Valor — motor que ejecuta GitHub Actions cada hora.

Lee los ajustes cifrados que guarda la web, busca apuestas con valor contra Pinnacle,
lleva dos cuentas separadas (ficticia y real), avisa por Telegram y guarda el estado cifrado.

Variables de entorno: APP_PASSWORD (obligatoria), FORZAR=si para buscar ya.
"""
from __future__ import annotations

import base64
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
    "real": {"activo": False, "banca_inicial": 50.0},
    "ligas": ["soccer_spain_la_liga", "soccer_epl", "soccer_uefa_champs_league"],
    "casas": ["williamhill", "sport888", "betway", "betfair_sb_uk", "leovegas"],
    "resumen_diario": True,
    "avanzado": {"valor_minimo": 0.03, "valor_maximo": 0.15, "fraccion_kelly": 0.25, "tope": 0.04,
                 "minima": 0.5, "limite_diario": 0.10, "limite_semanal": 0.20, "parar_si_cae": 0.40,
                 "max_dia": 6, "max_exposicion": 0.30, "horas_vista": 30, "horas_entre_busquedas": 6,
                 "metodo": "power"},
}


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

    def odds(self, sport: str, books: list[str]):
        q = {"markets": "h2h", "oddsFormat": "decimal"}
        if books:
            q["bookmakers"] = ",".join(sorted(set(["pinnacle"] + books)))
        else:
            q["regions"] = "eu"
        return self._get(f"/sports/{sport}/odds/", q)

    def scores(self, sport: str):
        return self._get(f"/sports/{sport}/scores/", {"daysFrom": 3})


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
        out[ev["id"]] = {"home": ev["home_team"], "away": ev["away_team"], "commence": ev["commence_time"],
                         "fair": dict(zip(names, fair)), "books": books}
    return out


# ============================ cuentas ============================

def ledger_bank(state: dict, s: dict, name: str) -> float:
    bets = state["ledgers"][name]["bets"]
    return s[name]["banca_inicial"] + sum(b.get("pnl") or 0 for b in bets if b["status"] in ("ganada", "perdida", "nula"))


def risk_block(state: dict, s: dict, name: str, bank: float) -> str | None:
    a, b0 = s["avanzado"], s[name]["banca_inicial"]
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
    if sum(1 for b in bets if pt(b["created"]) > t - timedelta(days=1) and b["status"] != "descartada") >= a["max_dia"]:
        return "máximo de apuestas en 24 h"
    return None


def exposure(state, name) -> float:
    return sum(b["stake"] for b in state["ledgers"][name]["bets"] if b["status"] in ("abierta", "propuesta"))


def find_value(ref: dict, s: dict, allowed: set) -> list[tuple]:
    a = s["avanzado"]
    found = []
    for eid, r in ref.items():
        mins = (pt(r["commence"]) - now()).total_seconds() / 60
        if mins < 20 or mins > a["horas_vista"] * 60:
            continue
        best = None
        for key, b in r["books"].items():
            if key == "pinnacle" or key.startswith("betfair_ex") or key == "matchbook" or (allowed and key not in allowed):
                continue
            for name, price in b["prices"].items():
                p = r["fair"].get(name)
                if not p:
                    continue
                ev = st.ev_back(p, price)
                if a["valor_minimo"] <= ev <= a["valor_maximo"] and (not best or ev > best[0]):
                    best = (ev, key, b["title"], name, price, p)
        if best:
            found.append((eid, r) + best)
    return sorted(found, key=lambda x: -x[2])


def place(state, s, sport, cands, name: str) -> list[dict]:
    """Ficticio: apuesta directamente. Real: crea una propuesta que tú confirmas."""
    a = s["avanzado"]
    rules = st.StakeRules(a["fraccion_kelly"], a["tope"], a["minima"], True)
    bets = state["ledgers"][name]["bets"]
    new = []
    bank = ledger_bank(state, s, name)
    block = risk_block(state, s, name, bank)
    if block:
        note(state, f"{name}: sin apuestas nuevas ({block})")
        return new
    for eid, r, ev, key, title, outcome, price, p in cands:
        if any(b["event_id"] == eid and b["status"] != "descartada" for b in bets):
            continue
        stake = st.size_stake(bank, st.kelly_back(p, price), rules)
        if stake <= 0 or exposure(state, name) + stake > a["max_exposicion"] * bank:
            continue
        if risk_block(state, s, name, bank):
            break
        b = {"id": secrets.token_hex(4), "created": iso(now()), "sport": sport, "event_id": eid,
             "event": f"{r['home']} - {r['away']}", "commence": r["commence"],
             "selection": "Empate" if outcome == "Draw" else outcome, "outcome_key": outcome,
             "book": title, "book_key": key, "odds": price, "min_odds": round((1 + a["valor_minimo"]) / p, 2),
             "stake": stake, "fair_p": round(p, 4), "ev": round(ev, 4), "close_fair_odds": None,
             "status": "abierta" if name == "ficticio" else "propuesta", "pnl": None, "settled": None}
        bets.append(b)
        new.append(b)
    return new


def apply_inbox(state, inbox: list[dict]):
    done = set(state.get("applied", []))
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
            elif act["type"] == "no" and b["status"] == "propuesta":
                b["status"] = "descartada"
            elif act["type"] == "resultado" and b["status"] == "abierta":
                settle_bet(b, act["result"])
        done.add(act.get("id"))
    state["applied"] = list(done)[-500:]


def settle_bet(b: dict, result: str):
    b["status"] = result
    b["pnl"] = round(b["stake"] * (b["odds"] - 1), 2) if result == "ganada" else (-b["stake"] if result == "perdida" else 0.0)
    b["settled"] = iso(now())


def note(state, text):
    state.setdefault("log", []).append({"ts": iso(now()), "text": text})
    state["log"] = state["log"][-60:]
    print(text)


# ============================ ciclo ============================

def run(password: str, force: bool = False):
    s = merge(DEFAULT_SETTINGS, read_enc("settings.enc", password, {}))
    state = read_enc("state.enc", password, None) or {
        "version": 1, "ledgers": {"ficticio": {"bets": []}, "real": {"bets": []}},
        "snapshots": {}, "last_scan": None, "applied": [], "log": [], "last_summary": None}
    if s.get("reset_ficticio") and s["reset_ficticio"] != state.get("reset_ficticio"):
        state["ledgers"]["ficticio"]["bets"] = []
        state["reset_ficticio"] = s["reset_ficticio"]
        note(state, "Modo ficticio reiniciado desde cero")
    inbox = read_enc("inbox.enc", password, [])
    apply_inbox(state, inbox)
    a = s["avanzado"]
    t = now()

    if not s.get("odds_api_key"):
        note(state, "Falta la clave de The Odds API en Ajustes.")
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
    due = {b["sport"] for b in all_bets if b["status"] == "abierta" and pt(b["commence"]) < t - timedelta(hours=2, minutes=30)}
    for sport in due:
        try:
            res = {x["id"]: x for x in odds.scores(sport) if x.get("completed") and x.get("scores")}
        except Exception as e:
            note(state, f"No pude leer resultados de {sport}: {e}")
            continue
        for b in all_bets:
            x = res.get(b["event_id"])
            if b["status"] != "abierta" or b["sport"] != sport or not x:
                continue
            sc = {y["name"]: float(y["score"]) for y in x["scores"]}
            h, w = sc.get(x["home_team"]), sc.get(x["away_team"])
            if h is None or w is None:
                continue
            winner = x["home_team"] if h > w else x["away_team"] if w > h else "Draw"
            settle_bet(b, "ganada" if winner == b["outcome_key"] else "perdida")
            if b in state["ledgers"]["real"]["bets"]:
                telegram(s, f"{'✅' if b['status'] == 'ganada' else '❌'} <b>{b['event']}</b>: {b['selection']} "
                            f"{'ganada' if b['status'] == 'ganada' else 'perdida'} ({b['pnl']:+.2f} €)")

    # 3) cuota de cierre (CLV): partidos apostados que empiezan en la próxima hora y cuarto
    soon = {b["sport"] for b in all_bets if b["status"] in ("abierta",) and b.get("close_fair_odds") is None
            and t < pt(b["commence"]) <= t + timedelta(minutes=75)}

    # 4) búsqueda completa cada X horas (o si la pides desde la web)
    last = state.get("last_scan")
    full = force or not last or pt(last) <= t - timedelta(hours=a["horas_entre_busquedas"]) + timedelta(minutes=10)
    sports = list(dict.fromkeys((s["ligas"] if full else []) + list(soon)))
    for sport in sports:
        try:
            ref = reference(odds.odds(sport, books), a["metodo"])
        except Exception as e:
            note(state, f"No pude leer cuotas de {sport}: {e}")
            continue
        for eid, r in ref.items():
            state["snapshots"][eid] = {"fair": r["fair"], "ts": iso(t), "commence": r["commence"]}
        for b in all_bets:
            if b["sport"] == sport and b.get("close_fair_odds") is None and b["event_id"] in ref \
                    and t < pt(b["commence"]) <= t + timedelta(minutes=75) and b["status"] == "abierta":
                p = ref[b["event_id"]]["fair"].get(b["outcome_key"])
                if p:
                    b["close_fair_odds"] = round(1 / p, 3)
        if sport not in s["ligas"] or not full:
            continue
        cands = find_value(ref, s, allowed)
        if s["ficticio"]["activo"]:
            for b in place(state, s, sport, cands, "ficticio"):
                if s["ficticio"].get("avisar"):
                    telegram(s, f"🧪 <b>FICTICIO</b> · {b['event']} ({local(b['commence'])})\n"
                                f"{b['selection']} a {b['odds']:.2f} en {b['book']} · {b['stake']:.2f} € · valor {b['ev']:+.1%}")
        if s["real"]["activo"]:
            for b in place(state, s, sport, cands, "real"):
                telegram(s, f"💶 <b>APUESTA REAL</b>\n<b>{b['event']}</b> · {local(b['commence'])}\n"
                            f"Entra en <b>{b['book']}</b> y apuesta a <b>{b['selection']}</b>\n"
                            f"Cuota: <b>{b['odds']:.2f}</b> (no bajes de {b['min_odds']:.2f})\n"
                            f"Importe: <b>{b['stake']:.2f} €</b>\n"
                            f"Después pulsa «La he hecho» en la web.")
    if full:
        state["last_scan"] = iso(t)
    # limpiar fotos viejas
    state["snapshots"] = {k: v for k, v in state["snapshots"].items() if pt(v["commence"]) > t - timedelta(days=2)}

    # 5) resumen diario a las 21 h
    loc = t.astimezone(MAD)
    if s.get("resumen_diario") and loc.hour >= 21 and state.get("last_summary") != loc.strftime("%Y-%m-%d"):
        lines = []
        for name in ("ficticio", "real"):
            if not s[name]["activo"]:
                continue
            bank = ledger_bank(state, s, name)
            lines.append(f"{'🧪 Ficticio' if name == 'ficticio' else '💶 Real'}: {bank:.2f} € "
                         f"({bank - s[name]['banca_inicial']:+.2f} €)")
        if lines:
            telegram(s, "<b>Resumen del día</b>\n" + "\n".join(lines))
        state["last_summary"] = loc.strftime("%Y-%m-%d")

    state["credits"] = odds.remaining
    state["updated"] = iso(t)
    note(state, f"Pasada {'completa' if full else 'corta'}: {odds.used} consultas · créditos restantes {odds.remaining}")
    save(state, password)


def save(state, password):
    state["updated"] = iso(now())
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "state.enc").write_text(encrypt(state, password))


if __name__ == "__main__":
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        sys.exit("Falta el secreto APP_PASSWORD. Créalo desde la web, en el primer arranque.")
    run(pw, os.environ.get("FORZAR", "").lower() in ("si", "sí", "true", "1"))
