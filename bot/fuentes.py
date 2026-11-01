"""Fuentes de datos: The Odds API (cuotas) y football-data.org (calendario y resultados, gratis)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def pt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def http_json(url: str, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null"), dict(r.headers)


# ============================ fuentes ============================

ODDSAPI_MISSING = {"bet365"}       # Bet365 solo llega por OddsPapi


class QuotaOut(Exception):
    """La API rechaza las consultas (401): casi siempre, consultas del mes agotadas."""


class Odds:
    BASE = "https://api.the-odds-api.com/v4"

    def __init__(self, key: str, paused: bool = False):
        self.key, self.remaining, self.used, self.paused = key, None, 0, paused
        self.quota_msg = ""

    def _call(self, url: str):
        if self.paused:
            raise QuotaOut(self.quota_msg or "en pausa")
        try:
            return http_json(url)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            if e.code in (401, 429):
                self.paused, self.quota_msg = True, body
                raise QuotaOut(body) from e
            raise RuntimeError(f"HTTP {e.code}: {body}") from e

    def _get(self, path: str, q: dict):
        q = {"apiKey": self.key, **q}
        data, h = self._call(f"{self.BASE}{path}?{urllib.parse.urlencode(q)}")
        self.remaining = h.get("x-requests-remaining") or h.get("X-Requests-Remaining")
        self.used += 1
        return data or []

    def events_count(self, sport: str, frm: datetime, to: datetime) -> int:
        """Partidos de la liga en la ventana. Esta consulta es gratuita (no gasta créditos)."""
        q = {"apiKey": self.key, "commenceTimeFrom": frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "commenceTimeTo": to.strftime("%Y-%m-%dT%H:%M:%SZ")}
        data, _ = self._call(f"{self.BASE}/sports/{sport}/events?{urllib.parse.urlencode(q)}")
        return len(data or [])

    def event_times(self, sport: str, frm: datetime, to: datetime) -> list[str]:
        """Horas de inicio de los partidos de la liga en la ventana. Gratuita."""
        q = {"apiKey": self.key, "commenceTimeFrom": frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "commenceTimeTo": to.strftime("%Y-%m-%dT%H:%M:%SZ")}
        data, _ = self._call(f"{self.BASE}/sports/{sport}/events?{urllib.parse.urlencode(q)}")
        return [x.get("commence_time") for x in (data or []) if x.get("commence_time")]

    def odds(self, sport: str, books: list[str], frm: datetime | None = None, to: datetime | None = None):
        q = {"markets": "h2h", "oddsFormat": "decimal"}
        if frm and to:
            q["commenceTimeFrom"] = frm.strftime("%Y-%m-%dT%H:%M:%SZ")
            q["commenceTimeTo"] = to.strftime("%Y-%m-%dT%H:%M:%SZ")
        books = [b for b in books if b not in ODDSAPI_MISSING]      # casas que esta API no tiene
        if books:
            q["bookmakers"] = ",".join(sorted(set(["pinnacle"] + books)))
        else:
            q["regions"] = "eu"
        return self._get(f"/sports/{sport}/odds/", q)

    def active_sports(self) -> list[str]:
        """Lista de competiciones activas. Gratuita."""
        data, _ = self._call(f"{self.BASE}/sports/?{urllib.parse.urlencode({'apiKey': self.key})}")
        return [x["key"] for x in (data or []) if x.get("active") and not x.get("has_outrights")]

    def scores(self, sport: str):
        return self._get(f"/sports/{sport}/scores/", {"daysFrom": 3})


# ---------------- football-data.org: resultados de fútbol gratis ----------------
FD_CODES = {"soccer_spain_la_liga": "PD", "soccer_epl": "PL", "soccer_efl_champ": "ELC",
            "soccer_netherlands_eredivisie": "DED", "soccer_portugal_primeira_liga": "PPL",
            "soccer_germany_bundesliga": "BL1", "soccer_italy_serie_a": "SA", "soccer_france_ligue_one": "FL1",
            "soccer_uefa_champs_league": "CL"}


class FootballData:
    BASE = "https://api.football-data.org/v4"

    def __init__(self, key: str):
        self.key, self.calls = key, 0

    def upcoming(self, codes: list[str], d_from: datetime, d_to: datetime) -> dict:
        """Cuántos partidos por competición hay entre dos fechas. Gratis: sirve para no gastar consultas en ligas vacías."""
        q = urllib.parse.urlencode({"competitions": ",".join(codes), "dateFrom": d_from.strftime("%Y-%m-%d"),
                                    "dateTo": d_to.strftime("%Y-%m-%d")})
        req = urllib.request.Request(f"{self.BASE}/matches?{q}", headers={"X-Auth-Token": self.key})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.calls += 1
            matches = (json.loads(r.read().decode() or "{}") or {}).get("matches", [])
        out = {c: 0 for c in codes}
        for m in matches:
            if m.get("status") not in ("SCHEDULED", "TIMED"):
                continue
            mt = pt(m["utcDate"])
            if d_from <= mt <= d_to:
                code = (m.get("competition") or {}).get("code")
                if code in out:
                    out[code] += 1
        return out

    def finished(self, code: str, d_from: datetime, d_to: datetime) -> list[dict]:
        q = urllib.parse.urlencode({"status": "FINISHED", "dateFrom": d_from.strftime("%Y-%m-%d"),
                                    "dateTo": d_to.strftime("%Y-%m-%d")})
        req = urllib.request.Request(f"{self.BASE}/competitions/{code}/matches?{q}", headers={"X-Auth-Token": self.key})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.calls += 1
            return (json.loads(r.read().decode() or "{}") or {}).get("matches", [])
