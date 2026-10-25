"""Segunda fuente de cuotas: OddsPapi (v5).

Plan gratuito: unas 250 consultas al mes. Una consulta de cuotas trae hasta 100 partidos de una liga
con todas las casas, incluidas Pinnacle y Bet365.

OddsPapi no publica la lista de identificadores de ligas, mercados y casas, así que este módulo
los descubre con la clave del usuario la primera vez (y los renueva cada semana) y los guarda
en el estado del bot. Las respuestas se convierten al mismo formato que The Odds API, de modo
que el resto del bot no nota de qué fuente vienen las cuotas.
"""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://v5.oddspapi.io/en"
UTC = timezone.utc

# nombre aproximado de cada liga en OddsPapi: (deporte, palabras del nombre, país o categoría)
LEAGUES = {
    "soccer_spain_la_liga": ("soccer", ["laliga", "la liga", "primera division"], "spain"),
    "soccer_spain_segunda_division": ("soccer", ["laliga 2", "laliga hypermotion", "segunda", "la liga 2"], "spain"),
    "soccer_efl_champ": ("soccer", ["championship"], "england"),
    "soccer_epl": ("soccer", ["premier league"], "england"),
    "soccer_netherlands_eredivisie": ("soccer", ["eredivisie"], "netherlands"),
    "soccer_portugal_primeira_liga": ("soccer", ["liga portugal", "primeira liga"], "portugal"),
    "soccer_germany_bundesliga2": ("soccer", ["2. bundesliga", "2 bundesliga"], "germany"),
    "soccer_germany_bundesliga": ("soccer", ["bundesliga"], "germany"),
    "soccer_italy_serie_a": ("soccer", ["serie a"], "italy"),
    "soccer_france_ligue_one": ("soccer", ["ligue 1"], "france"),
    "soccer_uefa_champs_league": ("soccer", ["champions league"], "international"),
    "soccer_uefa_europa_league": ("soccer", ["europa league"], "international"),
    "basketball_euroleague": ("basketball", ["euroleague", "euroliga"], "international"),
    "basketball_nba": ("basketball", ["nba"], "usa"),
    "handball_germany_bundesliga": ("handball", ["bundesliga", "hbl"], "germany"),
}
SPORT_NAMES = {"soccer": ["soccer", "football"], "basketball": ["basketball"], "tennis": ["tennis"], "handball": ["handball"]}

# casas del usuario -> cómo se llaman en OddsPapi (se prefiere la versión española si existe)
BOOK_NAMES = {
    "pinnacle": ["pinnacle"], "bet365": ["bet365"], "winamax_fr": ["winamax"],
    "betfair_ex_eu": ["betfair exchange", "betfair ex"], "betfair_sb_uk": ["betfair sportsbook", "betfair"],
    "williamhill": ["william hill", "williamhill"], "sport888": ["888sport", "888"], "betway": ["betway"],
    "leovegas": ["leovegas"], "betsson": ["betsson"], "codere_it": ["codere"],
}


class OPError(Exception):
    pass


class OPQuotaOut(OPError):
    pass


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9. ]", " ", s)).strip()


class OddsPapi:
    def __init__(self, key: str, cache: dict):
        self.key, self.cache, self.calls, self.dead = key, cache, 0, False

    # ---------------------------------------------------------------- llamadas
    def _get(self, path: str, **q):
        if self.dead:
            raise OPQuotaOut("en pausa")
        q["apiKey"] = self.key
        url = f"{BASE}/{path}?{urllib.parse.urlencode(q)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=30) as r:
                self.calls += 1
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            if e.code in (401, 402, 403, 429):
                self.dead = True
                raise OPQuotaOut(f"HTTP {e.code}: {body}") from e
            raise OPError(f"HTTP {e.code}: {body}") from e

    # ---------------------------------------------------------------- descubrimiento
    def discover(self, now: datetime, force: bool = False) -> list[str]:
        """Rellena la caché de deportes, ligas, mercados y casas. Devuelve notas para el diagnóstico."""
        c = self.cache
        if not force and c.get("ts") and datetime.fromisoformat(c["ts"]) > now - timedelta(days=7):
            return []
        notes = []
        sports = self._get("sports") or []
        sid = {}
        for sp in sports:
            name = _norm(sp.get("sportName"))
            for k, words in SPORT_NAMES.items():
                if k not in sid and any(name == w or name.startswith(w) for w in words):
                    sid[k] = sp.get("sportId")
        sid.setdefault("soccer", 10)
        notes.append("deportes " + ", ".join(f"{k}={v}" for k, v in sid.items()))

        tours = {}
        for league, (sport, words, cat) in LEAGUES.items():
            if sport not in sid:
                continue
            lst = c.setdefault("_t", {}).get(sport)
            if lst is None:
                lst = self._get("tournaments", sportId=sid[sport]) or []
                c["_t"][sport] = lst
            best = None
            for t in lst:
                n, cn = _norm(t.get("tournamentName")), _norm(t.get("categoryName"))
                if cat not in cn and not (cat == "international" and cn in ("", "international", "europe", "world")):
                    continue
                for w in words:
                    if n == w or n.startswith(w + " ") or n == w.replace(" ", ""):
                        score = (n == w, -len(n))
                        if not best or score > best[0]:
                            best = (score, t["tournamentId"])
            if best:
                tours[league] = best[1]
        c.pop("_t", None)
        notes.append(f"ligas encontradas {len(tours)}/{len(LEAGUES)}")

        markets = {"soccer": {"m": 101, "o": [101, 102, 103]}}
        for sport in ("basketball", "tennis", "handball"):
            if sport not in sid:
                continue
            ms = self._get("markets", sportId=sid[sport]) or []
            pick = None
            for m in ms:
                if m.get("playerProp") or (m.get("handicap") not in (None, 0, 0.0)):
                    continue
                mt, name = _norm(m.get("marketType")), _norm(m.get("marketName"))
                n_out = m.get("marketLength") or len(m.get("outcomes") or [])
                want3 = sport == "handball"
                if want3 and mt == "1x2" and n_out == 3 and (m.get("period") in (None, "fulltime", "result")):
                    pick = m if not pick or m["marketId"] < pick["marketId"] else pick
                if not want3 and n_out == 2 and mt in ("moneyline", "1x2", "winner", "12") \
                        and ("overtime" in name or "winner" in name or m.get("period") in ("result", "fulltime", None)):
                    if not pick or ("overtime" in name and "overtime" not in _norm(pick.get("marketName"))):
                        pick = m
            if pick:
                markets[sport] = {"m": pick["marketId"], "o": [o["outcomeId"] for o in pick.get("outcomes", [])]}
        notes.append("mercados " + ", ".join(f"{k}={v['m']}" for k, v in markets.items()))

        books = {}
        for b in self._get("bookmakers") or []:
            if not b.get("active", True):
                continue
            name, slug, dom = _norm(b.get("bookmakerName")), b.get("slug"), (b.get("domain") or "").lower()
            for our, words in BOOK_NAMES.items():
                if any(name == w or name.startswith(w) or _norm(slug) == w.replace(" ", "") for w in words):
                    # «betfair» a secas no debe confundirse con el exchange
                    if our == "betfair_sb_uk" and "exchange" in name:
                        continue
                    better = dom.endswith(".es") or (our not in books)
                    if better:
                        books[our] = {"slug": slug, "title": b.get("bookmakerName"), "es": dom.endswith(".es")}
        notes.append("casas " + ", ".join(f"{k}->{v['slug']}{' (.es)' if v['es'] else ''}" for k, v in books.items()))
        c.update(ts=now.isoformat(), sports=sid, tours=tours, markets=markets, books=books)
        return notes

    # ---------------------------------------------------------------- cuotas
    def odds(self, league: str, our_books: list[str], frm: datetime, to: datetime) -> list[dict]:
        """Cuotas de una liga, en el formato de The Odds API."""
        c = self.cache
        sport = league.split("_")[0]
        tid = c.get("tours", {}).get(league)
        mk = c.get("markets", {}).get(sport)
        if not tid or not mk:
            return []
        wanted = {"pinnacle"} | set(our_books)
        slug_to_us = {v["slug"]: (k, v["title"]) for k, v in c.get("books", {}).items() if k in wanted}
        if not slug_to_us:
            return []
        data = self._get("fixtures/odds/main", tournamentId=tid, bookmakers=",".join(slug_to_us)) or []
        return convert(data, mk, slug_to_us, frm, to)

    def settlement(self, fixture_id: str) -> dict | None:
        """Resultado final de un partido (para cobrar apuestas que no encuentran otras fuentes)."""
        d = self._get("fixtures/settlement", fixtureId=fixture_id) or {}
        sc = (d.get("scores") or {})
        ft = sc.get("result") or sc.get("fulltime")
        if not ft or (d.get("status") or {}).get("statusId") != 2:
            return None
        p = d.get("participants") or {}
        return {"home": p.get("participant1Name"), "away": p.get("participant2Name"),
                "hs": ft.get("participant1Score"), "as": ft.get("participant2Score")}


def convert(data: list, mk: dict, slug_to_us: dict, frm: datetime, to: datetime) -> list[dict]:
    out = []
    names = ["home", "draw", "away"] if len(mk["o"]) == 3 else ["home", "away"]
    for f in data:
        st = f.get("startTime")
        if not st:
            continue
        start = datetime.fromtimestamp(st, UTC)
        if not (frm <= start <= to) or (f.get("status") or {}).get("statusId", 0) != 0:
            continue
        p = f.get("participants") or {}
        home, away = p.get("participant1Name"), p.get("participant2Name")
        if not home or not away:
            continue
        label = {"home": home, "away": away, "draw": "Draw"}
        books = []
        for slug, quotes in (f.get("odds") or {}).items():
            if slug not in slug_to_us:
                continue
            prices = {}
            for q in (quotes or {}).values():
                if q.get("marketId") != mk["m"] or not q.get("active", True) or q.get("marketActive") is False:
                    continue
                if q.get("playerId") not in (0, None):
                    continue
                try:
                    idx = mk["o"].index(q.get("outcomeId"))
                except ValueError:
                    continue
                if q.get("price") and q["price"] > 1:
                    prices[label[names[idx]]] = round(float(q["price"]), 3)
            if len(prices) == len(names):
                key, title = slug_to_us[slug]
                books.append({"key": key, "title": title,
                              "markets": [{"key": "h2h", "outcomes": [{"name": n, "price": v} for n, v in prices.items()]}]})
        if any(b["key"] == "pinnacle" for b in books):
            out.append({"id": "op_" + str(f["fixtureId"]), "home_team": home, "away_team": away,
                        "commence_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "bookmakers": books, "source": "oddspapi"})
    return out
