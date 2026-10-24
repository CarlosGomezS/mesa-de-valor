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

    def events_count(self, sport, frm, to):
        return len(FakeOdds.events) if sport == "soccer_spain_la_liga" else 0

    def odds(self, sport, books, frm=None, to=None):
        FakeOdds.calls.append(("odds", sport))
        return FakeOdds.events if sport == "soccer_spain_la_liga" else []

    def active_sports(self):
        return ["tennis_atp_paris_masters", "soccer_spain_la_liga"]

    def scores(self, sport):
        FakeOdds.calls.append(("scores", sport))
        return FakeOdds.scores_data


class Migration(unittest.TestCase):
    def test_old_single_ledger_is_kept(self):
        tmp = tempfile.TemporaryDirectory()
        run.DATA = Path(tmp.name)
        run.Odds = FakeOdds
        run.telegram = lambda s, t: None
        FakeOdds.events = []
        (run.DATA / "settings.enc").write_text(run.encrypt({"odds_api_key": "k"}, PW))
        old = {"version": 1, "ledgers": {"ficticio": {"bets": [{"id": "x", "status": "ganada", "pnl": 3.0, "sport": "soccer_epl", "event_id": "z", "stake": 1, "commence": "2026-10-01T10:00:00Z", "created": "2026-10-01T09:00:00Z", "settled": "2026-10-01T13:00:00Z"}]},
                                         "real": {"bets": []}}, "snapshots": {}, "last_scan": None, "applied": [], "log": []}
        (run.DATA / "state.enc").write_text(run.encrypt(old, PW))
        run.run(PW)
        st = run.decrypt((run.DATA / "state.enc").read_text(), PW)
        self.assertEqual(st["ledgers"]["f_valor"]["bets"][0]["id"], "x")
        self.assertIn("f_control", st["ledgers"])
        tmp.cleanup()


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
        settings = {"odds_api_key": "k", "real": {"activo": True, "banca_inicial": 50.0, "metodo": "valor"},
                    "ligas": ["soccer_spain_la_liga", "soccer_epl"], "casas": ["unibet_fr"]}
        (run.DATA / "settings.enc").write_text(run.encrypt(settings, PW))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return run.decrypt((run.DATA / "state.enc").read_text(), PW)

    def test_full_flow(self):
        run.run(PW)
        st = self.state()
        f, r = st["ledgers"]["f_valor"]["bets"], st["ledgers"]["real"]["bets"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["status"], "abierta")
        self.assertEqual((f[0]["selection"], f[0]["book"]), ("Real Betis", "Unibet"))
        self.assertEqual(r[0]["status"], "propuesta")
        self.assertTrue(any("APUESTA REAL" in m for m in self.sent))
        self.assertTrue(any("ficticias" in m for m in self.sent))
        L = st["ledgers"]
        self.assertEqual(len(L["f_estricto"]["bets"]), 1)      # +6,6 % y cuota 2,55: entra
        self.assertEqual(len(L["f_favoritos"]["bets"]), 0)     # cuota > 2,2: fuera
        self.assertEqual(L["f_control"]["bets"][0]["selection"], "Real Betis")
        self.assertEqual(L["f_plano"]["bets"][0]["stake"], 0.5)   # 1 % de 50 € < mínimo 0,5 -> 0,5
        self.assertEqual(len(L["f_consenso"]["bets"]), 0)      # solo 2 casas: sin consenso

        # segunda pasada inmediata: corta, sin repetir ni gastar consultas
        FakeOdds.calls = []
        run.run(PW)
        self.assertEqual(FakeOdds.calls, [])   # pasada corta: no gasta créditos
        self.assertEqual(len(self.state()["ledgers"]["f_valor"]["bets"]), 1)

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
        self.assertEqual(st["ledgers"]["f_valor"]["bets"][0]["status"], "ganada")

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
        self.assertEqual(self.state()["ledgers"]["f_valor"]["bets"], [])


if __name__ == "__main__":
    unittest.main()


class Methods(unittest.TestCase):
    def setUp(self):
        self.s = run.merge(run.DEFAULT_SETTINGS, {})
        start = datetime.now(UTC) + timedelta(hours=3)
        evs = [ev("e1", start, (2.30, 3.30, 3.30), (2.55, 3.10, 3.60))]
        for b in evs[0]["bookmakers"]:
            b["key"] = "pinnacle" if b["key"] == "pinnacle" else "williamhill"
        self.ref = run.reference(evs, "power")

    def pick(self, k, sport="soccer_spain_la_liga"):
        return [(c[5], c[6]) for c in run.find_value(self.ref, self.s, set(), run.METHODS[k], sport)]

    def test_twenty_methods(self):
        self.assertEqual(len(run.METHODS), 20)
        self.assertIn("tenis", run.METHODS)
        self.assertEqual(self.pick("tenis"), [])
        self.assertEqual(self.pick("tenis", "tennis_atp_paris_masters"), [("Real Betis", 2.55)])

    def test_filters(self):
        self.assertEqual(self.pick("valor"), [("Real Betis", 2.55)])
        self.assertEqual(self.pick("empates"), [("Draw", 3.6)])
        self.assertEqual(self.pick("baloncesto"), [])
        self.assertEqual(self.pick("anticipado"), [])         # empieza en 3 h
        self.assertEqual(self.pick("cerca"), [("Real Betis", 2.55)])
        self.assertEqual(self.pick("steam"), [])              # sin foto anterior

    def test_steam(self):
        r = next(iter(self.ref.values()))
        r["prev"] = {k: v - (0.03 if k == "Real Betis" else -0.015) for k, v in r["fair"].items()}
        self.assertEqual(self.pick("steam"), [("Real Betis", 2.55)])


class Budget(unittest.TestCase):
    def test_allowance(self):
        class O: remaining = "300"
        st_ = {}
        b = run.budget(st_, O, datetime(2026, 10, 23, 12, tzinfo=UTC))
        self.assertEqual(b["allow"], (300 - 10) // 9)   # 9 días hasta el 1 de noviembre
        self.assertTrue(run.can_spend(st_, O))
        O.remaining = str(300 - b["allow"])
        self.assertFalse(run.can_spend(st_, O))

    def test_void_after_48h(self):
        tmp = tempfile.TemporaryDirectory()
        run.DATA = Path(tmp.name); run.Odds = FakeOdds; sent = []; run.telegram = lambda s, t: sent.append(t)
        FakeOdds.events = []; FakeOdds.scores_data = []
        (run.DATA / "settings.enc").write_text(run.encrypt({"odds_api_key": "k"}, PW))
        old = (datetime.now(UTC) - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bet = {"id": "q", "status": "abierta", "pnl": None, "sport": "tennis_atp_paris_masters", "event_id": "z", "event": "A - B",
               "stake": 2, "odds": 2.0, "selection": "A", "commence": old, "created": old, "settled": None}
        state = {"version": 1, "ledgers": {"real": {"bets": []}, "f_valor": {"bets": [bet]}}, "snapshots": {},
                 "last_scan": None, "applied": [], "log": []}
        (run.DATA / "state.enc").write_text(run.encrypt(state, PW))
        run.run(PW)
        b = run.decrypt((run.DATA / "state.enc").read_text(), PW)["ledgers"]["f_valor"]["bets"][0]
        self.assertEqual((b["status"], b["pnl"]), ("nula", 0.0))
        self.assertTrue(any("anulada" in m for m in sent))
        tmp.cleanup()


class Autopilot(unittest.TestCase):
    def bets(self, n, clv, win_every):
        out = []
        for i in range(n):
            won = i % win_every == 0
            out.append({"status": "ganada" if won else "perdida", "stake": 1.0, "odds": 2.1,
                        "pnl": 1.1 if won else -1.0, "close_fair_odds": 2.1 / (1 + clv + (0.01 if i % 2 else -0.01))})
        return out

    def test_needs_proof(self):
        st_ = {"ledgers": {"f_" + k: {"bets": []} for k in run.METHODS}}
        best, msgs = run.autopilot(st_)
        self.assertIsNone(best)
        self.assertEqual(msgs, [])

    def test_picks_proven_method(self):
        st_ = {"ledgers": {"f_" + k: {"bets": []} for k in run.METHODS}}
        st_["ledgers"]["f_consenso"]["bets"] = self.bets(120, 0.04, 2)       # gana y bate el cierre
        st_["ledgers"]["f_volumen"]["bets"] = self.bets(120, -0.02, 2)       # gana por suerte, CLV negativo
        st_["ledgers"]["f_control"]["bets"] = self.bets(200, 0.05, 2)        # el control nunca se elige
        best, msgs = run.autopilot(st_)
        self.assertEqual(best, "consenso")
        self.assertTrue(msgs and "demostrado" in msgs[0])
        st_["ledgers"]["f_consenso"]["bets"] += self.bets(100, -0.03, 3)     # pierde la ventaja
        best, msgs = run.autopilot(st_)
        self.assertIsNone(best)
        self.assertTrue("perdido" in msgs[0])


class Exchange(unittest.TestCase):
    def test_exchange_only_if_ticked_and_commission(self):
        s = run.merge(run.DEFAULT_SETTINGS, {})
        start = datetime.now(UTC) + timedelta(hours=3)
        e = ev("e1", start, (2.30, 3.30, 3.30), (2.55, 3.10, 3.20))
        e["bookmakers"][1]["key"], e["bookmakers"][1]["title"] = "betfair_ex_eu", "Betfair Exchange"
        ref = run.reference([e], "power")
        self.assertEqual(run.find_value(ref, s, {"winamax_fr"}), [])           # exchange sin marcar: nada
        c = run.find_value(ref, s, {"betfair_ex_eu"})
        self.assertEqual(c[0][4], "Betfair Exchange")
        p = c[0][7]
        self.assertAlmostEqual(c[0][2], p * 1.55 * 0.95 - (1 - p))           # valor descontando 5 % de comisión
        b = {"stake": 2.0, "odds": 2.55, "com": 0.05}
        run.settle_bet(b, "ganada")
        self.assertAlmostEqual(b["pnl"], round(2 * 1.55 * 0.95, 2))
