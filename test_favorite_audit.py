import unittest
import favorite_audit as fa


class FavoriteAuditTests(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(fa.signals({"odds3": 1.4, "odds15": 1.2, "base3": -0.9})[0], "危険")
        self.assertEqual(fa.signals({"odds3": 1.4, "odds15": 1.4, "base3": -0.9})[0], "注意")
        self.assertEqual(fa.signals({"odds3": 1.4, "odds15": 1.7, "base3": 0.8})[0], "信頼")
        self.assertEqual(fa.signals({"odds3": 1.4, "odds15": None, "base3": 0.0})[0], "普通")

    def test_audit_counts_and_roi(self):
        rows = [{"odds3": 1.4, "odds15": 1.2, "base3": -0.9, "placed": False, "place_yen": 0},
                {"odds3": 1.5, "odds15": 1.5, "base3": 0.9, "placed": True, "place_yen": 110},
                {"odds3": 1.6, "odds15": 1.6, "base3": 0.9, "placed": True, "place_yen": None}]
        out = fa.audit(rows)
        by = {g["verdict"]: g for g in out["by_verdict"]}
        self.assertEqual((out["heavy_favorites"], out["placed"]), (3, 2))
        self.assertEqual((by["危険"]["horses"], by["危険"]["place_rate"]), (1, 0.0))
        self.assertEqual((by["信頼"]["horses"], by["信頼"]["place_rate"], by["信頼"]["place_roi"]), (2, 1.0, 1.1))


if __name__ == "__main__":
    unittest.main()
