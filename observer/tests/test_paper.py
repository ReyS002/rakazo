import unittest

import fakes
from sections import paper


class Paper(unittest.TestCase):
    def run_paper(self, **box_kw):
        self.box = fakes.Box(**box_kw)
        self.addCleanup(self.box.close)
        return paper.collect(self.box.deps())

    def hosts(self):
        return [url.split("/")[2] for _, url, _, _ in self.box.opener.calls]

    def test_contract(self):
        res = self.run_paper()
        data, ok, err = res  # bull-live is refused -> partial
        self.assertTrue(ok)
        labels = {a["account_label"]: a for a in data["alpaca"]}
        self.assertEqual(list(labels), ["velez-trading-bot", "bull-pilot", "bull-live"])
        a = labels["velez-trading-bot"]
        self.assertEqual(set(a), {"account_label", "ok", "equity", "cash", "buying_power", "positions", "fills_today",
                                  "open_orders"})
        self.assertEqual((a["equity"], a["cash"], a["buying_power"]), (100123.45, 50000.0, 200000.0))
        self.assertEqual(a["positions"], [{"symbol": "AAPL", "qty": 10.0, "avg_entry": 230.1, "mark": 231.5,
                                           "unrealized_pl": 14.0}])
        self.assertEqual(set(a["fills_today"][0]), {"time", "symbol", "side", "qty", "price"})
        self.assertEqual(a["open_orders"], [{"symbol": "AAPL", "side": "sell", "qty": 10.0, "type": "stop",
                                             "stop": 225.0, "limit": None}])
        o = data["oanda_practice"]
        self.assertEqual(set(o), {"ok", "account_suffix", "currency", "balance", "nav", "open_trades", "pending_orders",
                                  "recent_transactions", "environment"})
        self.assertEqual((o["account_suffix"], o["environment"], o["nav"]), ("001", "practice", 100050.5))
        self.assertEqual(len(o["recent_transactions"]), 20)
        self.assertEqual(o["recent_transactions"][0]["time"], "2026-10-26T12:00:00Z")
        self.assertEqual(a["fills_today"][0]["time"], "2026-10-26T14:31:00Z")
        self.assertEqual(o["open_trades"][0]["stop"], 1.08)
        self.assertEqual(set(o["pending_orders"][0]), {"instrument", "units", "type", "price"})
        self.assertIn("alpaca bull-live: not paper", err)

    def test_alpaca_live_host_refused_without_request(self):
        data, ok, err = self.run_paper()
        live = [a for a in data["alpaca"] if a["account_label"] == "bull-live"][0]
        self.assertEqual(live, {"account_label": "bull-live", "ok": False, "error": "not paper"})
        sent_keys = {h.get("Apca-api-key-id") for _, _, h, _ in self.box.opener.calls}
        self.assertNotIn(fakes.ALPACA_LIVE_KID, sent_keys)
        self.assertEqual(sent_keys - {None}, {fakes.ALPACA_KID, fakes.ALPACA2_KID})

    def test_alpaca_paper_base_overridden_to_live_refused(self):
        data, ok, err = self.run_paper(alpaca_base="https://api.alpaca.markets")
        a = [a for a in data["alpaca"] if a["account_label"] == "velez-trading-bot"][0]
        self.assertEqual(a["error"], "not paper")
        self.assertNotIn("api.alpaca.markets", self.hosts())

    def test_alpaca_lookalike_host_refused(self):
        data, _, _ = self.run_paper(alpaca_base="https://paper-api.alpaca.markets.evil.example/")
        a = [a for a in data["alpaca"] if a["account_label"] == "velez-trading-bot"][0]
        self.assertFalse(a["ok"])

    def test_alpaca_no_base_needs_paper_key_id(self):
        self.assertTrue(paper.alpaca_is_paper("PKX", None))
        self.assertFalse(paper.alpaca_is_paper("AKX", None))
        self.assertFalse(paper.alpaca_is_paper("PKX", "https://api.alpaca.markets"))

    def test_oanda_live_refused(self):
        data, ok, err = self.run_paper(oanda_base="https://api-fxtrade.oanda.com")
        self.assertEqual(data["oanda_practice"], {"ok": False, "error": "not practice"})
        self.assertIn("oanda: not practice", err)
        self.assertNotIn("api-fxtrade.oanda.com", self.hosts())
        self.assertNotIn("api-fxpractice.oanda.com", self.hosts())
        self.assertTrue(ok)  # alpaca still fine

    def test_oanda_missing_base_refused(self):
        data, _, _ = self.run_paper(oanda_base="")
        self.assertEqual(data["oanda_practice"]["error"], "not practice")

    def test_one_account_failure_does_not_break_others(self):
        self.box = fakes.Box()
        self.addCleanup(self.box.close)
        self.box.opener.fail_substr = (fakes.ALPACA2_KID,)
        data, ok, err = paper.collect(self.box.deps())
        by = {a["account_label"]: a for a in data["alpaca"]}
        self.assertTrue(by["velez-trading-bot"]["ok"])
        self.assertEqual(by["bull-pilot"], {"account_label": "bull-pilot", "ok": False, "error": "HttpError (HTTP 500)"})
        self.assertTrue(data["oanda_practice"]["ok"])
        self.assertTrue(ok)

    def test_everything_failing_is_not_ok(self):
        self.box = fakes.Box(oanda_base="https://api-fxtrade.oanda.com")
        self.addCleanup(self.box.close)
        self.box.opener.fail_substr = ("alpaca",)
        data, ok, err = paper.collect(self.box.deps())
        self.assertFalse(ok)

    def test_oanda_auth_header_only(self):
        self.run_paper()
        oanda_calls = [c for c in self.box.opener.calls if "fxpractice" in c[1]]
        self.assertEqual(len(oanda_calls), 4)
        for _, url, headers, _ in oanda_calls:
            self.assertEqual(headers["Authorization"], "Bearer " + fakes.OANDA_KEY)
            self.assertNotIn(fakes.OANDA_KEY, url)


if __name__ == "__main__":
    unittest.main()
