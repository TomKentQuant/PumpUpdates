import os
import tempfile
import unittest

os.environ.setdefault("DATABASE_PATH", ":memory:")
from tracker import Store, Observation, changes, fingerprint, is_allowed_url, normalize


class TrackerTests(unittest.TestCase):
    def test_safe_hosts(self):
        self.assertTrue(is_allowed_url("https://pump.fun/"))
        self.assertTrue(is_allowed_url("https://frontend-api-v3.pump.fun/coins/latest"))
        for url in ("http://pump.fun", "https://evil.com", "https://pump.fun.evil.com", "https://127.0.0.1", "https://pump.fun:1234", "https://user:pw@pump.fun", "https://pump.fun@evil.com"):
            self.assertFalse(is_allowed_url(url), url)

    def test_normalized_json(self):
        self.assertEqual(fingerprint({"b": "x  y", "a": 1}), fingerprint({"a": 1, "b": "x y"}))

    def test_changes(self):
        output = changes({"a": 1}, {"a": 2, "b": 3})
        self.assertTrue(any("a:" in x for x in output))
        self.assertTrue(any("+ b" in x for x in output))

    def test_baseline_and_changes(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d + "/test.sqlite")
            first = Observation("x", "APP STORE", "https://apps.apple.com/", {"version": "1"}, "iOS")
            self.assertFalse(s.put(first))
            self.assertFalse(s.put(first))
            self.assertEqual(len(s.pending()), 0)
            self.assertTrue(s.put(Observation("x", "APP STORE", first.url, {"version": "2"}, "iOS")))
            pending = s.pending()
            self.assertEqual(len(pending), 1)
            s.mark_sent(pending[0][0])
            self.assertEqual(s.pending(), [])
            s.watch("https://pump.fun/foo", "web")
            self.assertEqual(len(s.watches()), 1)
            self.assertTrue(s.unwatch("https://pump.fun/foo"))
            s.db.close()


if __name__ == "__main__":
    unittest.main()

class V3Tests(unittest.TestCase):
    def test_asset_churn_silent_but_saved(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d + "/v3.sqlite")
            def obs(paths):
                return Observation("web:assets:https://pump.fun/", "WEB ASSETS", "https://pump.fun/", {"files": paths}, "Assets")
            self.assertFalse(s.put(obs(["old.js"])))
            self.assertFalse(s.put(obs(["new.js"])))
            self.assertEqual(s.pending(), [])
            self.assertEqual(s.get_snapshot("web:assets:https://pump.fun/")["files"], ["new.js"])
            s.db.close()

    def test_signal_baseline_and_repeat_suppression(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d + "/v3.sqlite")
            def obs(signals):
                return Observation("web:features:https://pump.fun/", "WEB FEATURE SIGNALS", "https://pump.fun/", {"signals": signals}, "Features")
            sample = {"limitorder": {"asset": "https://pump.fun/a.js", "excerpt": "createLimitOrder"}}
            self.assertFalse(s.put(obs({})))
            self.assertTrue(s.put(obs(sample)))
            self.assertEqual(len(s.pending()), 1)
            s.mark_sent(s.pending()[0][0])
            self.assertFalse(s.put(obs(sample)))
            # Same feature name in a new compiled asset is not a new feature.
            other = {"limitorder": {"asset": "https://pump.fun/b.js", "excerpt": "showLimitOrder"}}
            self.assertFalse(s.put(obs(other)))
            self.assertEqual(len(s.pending()), 0)
            s.db.close()

    def test_feature_clues(self):
        from tracker import feature_clues
        self.assertIn("limitorder", feature_clues("function createLimitOrder(){ return '/api/limit-order' }"))
        self.assertEqual(feature_clues("This is a livestream video example."), {})

class V4WebsiteTests(unittest.TestCase):
    def test_perps_announcement(self):
        from tracker import website_delta
        before = {"title": "Pump.fun", "description": "Trade meme coins", "text": "Explore tokens and find the latest coins."}
        after = {"title": "Pump.fun", "description": "Trade meme coins", "text": "Explore tokens and find the latest coins. Pump just added perps to the app. Everyone will be able to LONG or SHORT crypto, stocks."}
        result = website_delta(before, after)
        self.assertIn("perps", result.lower())
        self.assertIn("LONG or SHORT", result)

    def test_noise_silent_and_database_dedup(self):
        from tracker import website_delta
        old = {"title": "Pump.fun", "description": "", "text": "Bitcoin price $10000 and 5 users"}
        noisy = {"title": "Pump.fun", "description": "", "text": "Bitcoin price $10001 and 6 users"}
        self.assertEqual(website_delta(old, noisy), "")
        with tempfile.TemporaryDirectory() as d:
            store = Store(d + "/v4.sqlite")
            key = "web:text:https://pump.fun/"
            def obs(value):
                return Observation(key, "WEBSITE", "https://pump.fun/", value, "Website content: /")
            self.assertFalse(store.put(obs(old)))
            self.assertFalse(store.put(obs(noisy)))
            self.assertFalse(store.pending())
            noteworthy = {"title":"Pump.fun","description":"", "text":"Bitcoin price $10001 and 6 users. Introducing perpetuals trading on the app."}
            self.assertTrue(store.put(obs(noteworthy)))
            self.assertEqual(len(store.pending()), 1)
            self.assertFalse(store.put(obs(noteworthy)))
            store.db.close()
