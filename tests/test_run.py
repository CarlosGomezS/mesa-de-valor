"""Prueba de extremo a extremo del motor sin red: ficticio, propuesta real, confirmación, CLV y cobro."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import run  # noqa: E402

PW = "clave-de-prueba"
UTC = timezone.utc


def ev(eid, start, pin, unibet):
    t = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    mk = lambda pr: [{"key": "h2h", "outcomes": [{"name": "Real Betis", "price": pr[0]},
                                                 {"name": "Sevilla FC", "price": pr[1]}, {"name": "Draw", "price": pr[2]}]}]
    return {"id": eid, "home_team": "Real Betis", "away_team": "Sevilla FC", "commence_time": t,
            "bookmakers": [{"key": "pinnacle", "title": "Pinnacle", "markets": mk(pin)},
                           {"key": "unibet_fr", "title": "Unibet", "markets": mk(unibet)}]}


class FakeOdds:
    events, scores_data, calls = [], [], []

    def __init__(self, key):
        self.remaining, self.used = "480", 0

    def odds(self, sport, books):
        FakeOdds.calls.append(("odds", sport))
        return FakeOdds.events if sport == "soccer_spain_la_liga" else []

    def scores(self, sport):
        FakeOdds.calls.append(("scores", sport))
        return FakeOdds.scores_data


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        run.DATA = Path(self.tmp.name)
        run.Odds = FakeOdds
        self.sent = []
        run.telegram = lambda s, t: self.sent.append(t)
        FakeOdds.calls = []
        self.start = datetime.now(UTC) + timedelta(hours=5)
        FakeOdds.events = [ev("e1", self.start, (2.30, 3.30, 3.30), (2.55, 3.10, 3.20))]
        settings = {"odds_api_key": "k", "real": {"activo": True, "banca_inicial": 50.0},
                    "ligas": ["soccer_spain_la_liga", "soccer_epl"], "casas": ["unibet_fr"]}
        (run.DATA / "settings.enc").write_text(run.encrypt(settings, PW))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return run.decrypt((run.DATA / "state.enc").read_text(), PW)

    def test_full_flow(self):
        run.run(PW)
        st = self.state()
        f, r = st["ledgers"]["ficticio"]["bets"], st["ledgers"]["real"]["bets"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["status"], "abierta")
        self.assertEqual((f[0]["selection"], f[0]["book"]), ("Real Betis", "Unibet"))
        self.assertEqual(r[0]["status"], "propuesta")
        self.assertTrue(any("APUESTA REAL" in m for m in self.sent))
        self.assertTrue(any("FICTICIO" in m for m in self.sent))

        # segunda pasada inmediata: corta, sin repetir ni gastar consultas
        FakeOdds.calls = []
        run.run(PW)
        self.assertEqual(FakeOdds.calls, [])
        self.assertEqual(len(self.state()["ledgers"]["ficticio"]["bets"]), 1)

        # confirmo desde la web la apuesta real a 2.50 con 1 €
        bid = r[0]["id"]
        (run.DATA / "inbox.enc").write_text(run.encrypt([{"id": "a1", "bet": bid, "type": "hecha", "odds": 2.5, "stake": 1.0}], PW))
        # el partido empieza en 40 min: debe tomar la cuota de cierre
        st = self.state()
        soon = (datetime.now(UTC) + timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for L in st["ledgers"].values():
            for b in L["bets"]:
                b["commence"] = soon
        (run.DATA / "state.enc").write_text(run.encrypt(st, PW))
        FakeOdds.events = [ev("e1", datetime.now(UTC) + timedelta(minutes=40), (2.20, 3.40, 3.50), (2.40, 3.2, 3.3))]
        run.run(PW)
        st = self.state()
        rb = st["ledgers"]["real"]["bets"][0]
        self.assertEqual((rb["status"], rb["odds"], rb["stake"]), ("abierta", 2.5, 1.0))
        self.assertIsNotNone(rb["close_fair_odds"])
        self.assertGreater(rb["odds"] / rb["close_fair_odds"] - 1, 0)   # batimos el cierre

        # el partido terminó 2-1: se cobra solo
        past = (datetime.now(UTC) - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for L in st["ledgers"].values():
            for b in L["bets"]:
                b["commence"] = past
        (run.DATA / "state.enc").write_text(run.encrypt(st, PW))
        FakeOdds.scores_data = [{"id": "e1", "completed": True, "home_team": "Real Betis", "away_team": "Sevilla FC",
                                 "scores": [{"name": "Real Betis", "score": "2"}, {"name": "Sevilla FC", "score": "1"}]}]
        run.run(PW)
        st = self.state()
        rb = st["ledgers"]["real"]["bets"][0]
        self.assertEqual(rb["status"], "ganada")
        self.assertAlmostEqual(rb["pnl"], 1.5)
        self.assertEqual(st["ledgers"]["ficticio"]["bets"][0]["status"], "ganada")

    def test_unconfirmed_proposal_expires(self):
        run.run(PW)
        st = self.state()
        st["ledgers"]["real"]["bets"][0]["commence"] = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (run.DATA / "state.enc").write_text(run.encrypt(st, PW))
        run.run(PW)
        self.assertEqual(self.state()["ledgers"]["real"]["bets"][0]["status"], "descartada")

    def test_reset_fictitious(self):
        run.run(PW)
        s = run.decrypt((run.DATA / "settings.enc").read_text(), PW)
        s["reset_ficticio"] = 123
        (run.DATA / "settings.enc").write_text(run.encrypt(s, PW))
        FakeOdds.events = []
        run.run(PW, force=True)
        self.assertEqual(self.state()["ledgers"]["ficticio"]["bets"], [])


if __name__ == "__main__":
    unittest.main()
