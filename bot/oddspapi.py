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

BASES = {5: "https://v5.oddspapi.io/en", 4: "https://api.oddspapi.io/v4"}
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


# orden de importancia de tus casas cuando el plan obliga a pedirlas de una en una
PRIORITY = ["pinnacle", "bet365", "winamax_fr", "betfair_sb_uk", "betfair_ex_eu", "williamhill", "sport888",
            "betway", "leovegas", "betsson", "codere_it"]
MAX_BOOKS_IF_ONE = 2


class OPError(Exception):
    pass


class OPQuotaOut(OPError):
    pass


class OPBadKey(OPError):
    """La clave no vale para esta versión de la API (las claves de oddspapi.io son de la v4)."""


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9. ]", " ", s)).strip()


class OddsPapi:
    def __init__(self, key: str, cache: dict):
        self.key, self.cache, self.calls, self.dead = key, cache, 0, False
        self.ver = cache.get("ver") or 4
        self._last = 0.0

    # ---------------------------------------------------------------- llamadas
    def _get(self, path: str, **q):
        if self.dead:
            raise OPQuotaOut("en pausa")
        import time
        wait = 2.1 - (time.monotonic() - self._last)          # la v4 pide 1-2 s entre consultas
        if self.ver == 4 and wait > 0:
            time.sleep(wait)
        q["apiKey"] = self.key
        url = f"{BASES[self.ver]}/{path}?{urllib.parse.urlencode(q)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=30) as r:
                self.calls += 1
                self._last = time.monotonic()
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            self._last = time.monotonic()
            body = e.read().decode("utf-8", "ignore")[:200]
            if e.code == 401 and "key" in body.lower():
                raise OPBadKey(f"HTTP 401: {body}") from e
            if e.code in (402, 403, 429) or e.code == 401:
                self.dead = True
                raise OPQuotaOut(f"HTTP {e.code}: {body}") from e
            raise OPError(f"HTTP {e.code}: {body}") from e

    def _detect(self) -> list:
        """Prueba la v4 y, si la clave no vale ahí, la v5. Devuelve la lista de deportes."""
        if self.cache.get("ver"):
            self.ver = self.cache["ver"]
            return self._get("sports") or []
        for v in (4, 5):
            self.ver = v
            try:
                sports = self._get("sports") or []
                self.cache["ver"] = v
                return sports
            except OPBadKey:
                continue
        self.dead = True
        raise OPQuotaOut("La clave de OddsPapi no es válida en ninguna versión de la API")

    # ---------------------------------------------------------------- descubrimiento
    def discover(self, now: datetime, force: bool = False) -> list[str]:
        """Rellena la caché de deportes, ligas, mercados y casas. Devuelve notas para el diagnóstico."""
        c = self.cache
        if not force and c.get("ts") and datetime.fromisoformat(c["ts"]) > now - timedelta(days=7):
            return []
        notes = []
        sports = self._detect()
        notes.append(f"versión v{self.ver}")
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
            if self.ver == 4:
                if "_m" not in c:
                    c["_m"] = self._get("markets") or []
                ms = [m for m in c["_m"] if m.get("sportId") == sid[sport]]
            else:
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
        c.pop("_m", None)
        notes.append("mercados " + ", ".join(f"{k}={v['m']}" for k, v in markets.items()))

        books = {}
        bks = self._get("bookmakers") or []
        if isinstance(bks, dict):
            bks = list(bks.values())
        for b in bks:
            if not isinstance(b, dict) or not b.get("active", True):
                continue
            slug = b.get("slug") or b.get("bookmaker") or b.get("bookmakerSlug")
            name = _norm(b.get("bookmakerName") or b.get("name") or slug)
            dom = (b.get("domain") or b.get("url") or "").lower()
            if not slug:
                continue
            for our, words in BOOK_NAMES.items():
                if any(name == w or name.startswith(w) or _norm(slug) == w.replace(" ", "") for w in words):
                    # «betfair» a secas no debe confundirse con el exchange
                    if our == "betfair_sb_uk" and "exchange" in name:
                        continue
                    def rank(sl, dm):
                        x = f"{sl} {dm}".lower()
                        return 3 if ".es" in x else 2 if ".fr" in x and our == "winamax_fr" else 1 if "." not in sl else 0
                    if our not in books or rank(slug, dom) > books[our]["rank"]:
                        books[our] = {"slug": slug, "title": b.get("bookmakerName") or slug, "es": ".es" in f"{slug} {dom}",
                                      "rank": rank(slug, dom)}
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
        if self.ver == 5:
            data = self._get("fixtures/odds/main", tournamentId=tid, bookmakers=",".join(slug_to_us)) or []
            return convert(data, mk, slug_to_us, frm, to)
        # v4: nombres en /fixtures; cuotas en /odds-by-tournaments (máx. 3 casas, el exchange solo)
        try:
            fx = self._get("fixtures", tournamentId=tid, statusId=0,
                           **{"from": frm.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": to.strftime("%Y-%m-%dT%H:%M:%SZ")}) or []
        except OPError as e:
            if "404" in str(e):          # «no hay partidos»: no es un error
                return []
            raise
        fx = {f["fixtureId"]: f for f in fx if f.get("fixtureId")}
        if not fx:
            return []
        gs = int(c.get("gs") or 3)                  # casas por consulta que acepta tu plan (se aprende solo)
        pin = [sl for sl, (k, _) in slug_to_us.items() if k == "pinnacle"]
        mine = [sl for sl, (k, _) in sorted(slug_to_us.items(), key=lambda kv: PRIORITY.index(kv[1][0])
                                            if kv[1][0] in PRIORITY else 99) if k != "pinnacle"]
        if gs == 1:
            mine = mine[:MAX_BOOKS_IF_ONE]            # con 1 casa por consulta, solo las más útiles
        merged: dict = {}

        def fetch(group):
            for item in self._get("odds-by-tournaments", tournamentIds=tid, bookmakers=",".join(group)) or []:
                fid = item.get("fixtureId")
                if fid in fx:
                    merged.setdefault(fid, {}).update(item.get("bookmakerOdds") or {})

        slugs = pin + mine
        i = 0
        while i < len(slugs):
            exch = "betfair-ex" in slugs[i] or slugs[i].endswith("-ex")
            group = [slugs[i]] if exch else [x for x in slugs[i:i + gs] if not ("betfair-ex" in x or x.endswith("-ex"))]
            try:
                fetch(group)
                i += len(group)
            except OPError as e:
                if "number of bookmakers" in str(e).lower() and gs > 1:
                    gs -= 1
                    c["gs"] = gs
                    if gs == 1:
                        slugs = pin + mine[:MAX_BOOKS_IF_ONE]
                    continue
                if "404" in str(e):
                    i += len(group)
                    continue
                raise
        return convert_v4(fx, merged, mk, slug_to_us, frm, to)

    def find_fixture(self, league: str, home: str, away: str, start: datetime, same) -> str | None:
        """Busca en OddsPapi el partido de una apuesta hecha con otra fuente (por nombres y hora)."""
        c = self.cache
        sport = league.split("_")[0]
        frm, to = start - timedelta(hours=6), start + timedelta(hours=6)
        tid = c.get("tours", {}).get(league)
        sid = c.get("sports", {}).get(sport)
        key = f"{tid or 's' + str(sid)}|{frm:%Y%m%d%H}"
        mem = self.__dict__.setdefault("_fx_mem", {})
        if key not in mem:
            q = {"from": frm.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": to.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if tid:
                q["tournamentId"] = tid
            elif sid:
                q["sportId"] = sid
            else:
                return None
            try:
                mem[key] = self._get("fixtures", **q) or []
            except OPError as e:
                if "404" in str(e):
                    mem[key] = []
                else:
                    raise
        for f in mem[key]:
            try:
                fs = datetime.fromisoformat(str(f.get("startTime")).replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                continue
            if same(f.get("participant1Name") or "", f.get("participant2Name") or "", fs, home, away, start):
                return str(f.get("fixtureId"))
        return None

    def settlement(self, fixture_id: str, sport: str = "soccer") -> dict | None:
        """Resultado final de un partido (para cobrar apuestas que no encuentran otras fuentes)."""
        if self.ver == 4:
            mk = self.cache.get("markets", {}).get(sport)
            d = self._get("settlements", fixtureId=fixture_id) or {}
            if not mk:
                return None
            outs = (((d.get("markets") or {}).get(str(mk["m"])) or {}).get("outcomes") or {})
            res = {int(o): ((v.get("players") or {}).get("0") or {}).get("result") for o, v in outs.items()}
            if not res or any(r in (None, "UNDECIDED") for r in res.values()):
                return None
            won = [o for o, r in res.items() if r == "WIN"]
            if len(won) != 1 or won[0] not in mk["o"]:
                return None
            idx = mk["o"].index(won[0])
            three = len(mk["o"]) == 3
            hs, as_ = {0: (1, 0), 1: (0, 0) if three else (0, 1), 2: (0, 1)}[idx]
            return {"home": None, "away": None, "hs": hs, "as": as_, "no_score": True}
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


def convert_v4(fx: dict, merged: dict, mk: dict, slug_to_us: dict, frm: datetime, to: datetime) -> list[dict]:
    out = []
    names = ["home", "draw", "away"] if len(mk["o"]) == 3 else ["home", "away"]
    for fid, f in fx.items():
        try:
            start = datetime.fromisoformat(str(f.get("startTime")).replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            continue
        if not (frm <= start <= to):
            continue
        home, away = f.get("participant1Name"), f.get("participant2Name")
        if not home or not away:
            continue
        label = {"home": home, "away": away, "draw": "Draw"}
        books = []
        for slug, bo in (merged.get(fid) or {}).items():
            if slug not in slug_to_us or not isinstance(bo, dict) or bo.get("suspended") or bo.get("bookmakerIsActive") is False:
                continue
            m = (bo.get("markets") or {}).get(str(mk["m"])) or {}
            if m.get("marketActive") is False:
                continue
            prices = {}
            for i, oid in enumerate(mk["o"]):
                pl = (((m.get("outcomes") or {}).get(str(oid)) or {}).get("players") or {}).get("0") or {}
                if pl.get("active", True) and pl.get("price") and float(pl["price"]) > 1:
                    prices[label[names[i]]] = round(float(pl["price"]), 3)
            if len(prices) == len(names):
                key, title = slug_to_us[slug]
                books.append({"key": key, "title": title,
                              "markets": [{"key": "h2h", "outcomes": [{"name": n, "price": v} for n, v in prices.items()]}]})
        if any(b["key"] == "pinnacle" for b in books):
            out.append({"id": "op_" + str(fid), "home_team": home, "away_team": away,
                        "commence_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "bookmakers": books, "source": "oddspapi"})
    return out
