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
