import unittest
import favorite_audit as fa


class FavoriteCheckTests(unittest.TestCase):
    def test_heavy_favorite_and_verdicts(self):
        rows = [{"horse": 4, "odds": 1.4, "base": -0.6}, {"horse": 7, "odds": 5.0, "base": 0.3}]
        self.assertEqual(fa.check_rows(rows)["verdict"], "危険")
        self.assertEqual(fa.check_rows([{"horse": 4, "odds": 1.4, "base": -0.35}])["verdict"], "注意")
        self.assertEqual(fa.check_rows([{"horse": 4, "odds": 1.9, "base": -0.1}])["verdict"], "信頼")
        self.assertIsNone(fa.check_rows([{"horse": 4, "odds": 2.0, "base": -0.9}]))   # 2.0倍は対象外
        self.assertIsNone(fa.check_rows([]))

    def test_validation_needs_enough_danger_cases_and_a_clear_gap(self):
        few = [{"verdict": "危険", "placed": False, "place_yen": 0}] * 10 + [{"verdict": "信頼", "placed": True, "place_yen": 110}] * 30
        v = fa.validation(few)
        self.assertFalse(v["enabled"]); self.assertEqual(v["need"], 40)
        many = ([{"verdict": "危険", "placed": i < 25, "place_yen": 100 if i < 25 else 0} for i in range(50)] +
                [{"verdict": "信頼", "placed": i < 80, "place_yen": 100} for i in range(100)])
        v = fa.validation(many)
        self.assertTrue(v["enabled"]); self.assertLess(v["p_value"], 0.05); self.assertEqual(v["gap"], 0.3)
        same = ([{"verdict": "危険", "placed": i < 38, "place_yen": 100} for i in range(50)] +
                [{"verdict": "信頼", "placed": i < 80, "place_yen": 100} for i in range(100)])
        self.assertFalse(fa.validation(same)["enabled"])

    def test_demote_only_when_enabled_and_first(self):
        picks = [{"horse": 4}, {"horse": 7}, {"horse": 2}, {"horse": 9}]
        chk = {"horse": 4, "verdict": "危険"}
        self.assertEqual(fa.demote(picks, chk, False), picks)
        self.assertEqual([p["horse"] for p in fa.demote(picks, chk, True)], [7, 2, 4, 9])
        self.assertEqual(fa.demote(picks, {"horse": 7, "verdict": "危険"}, True), picks)
        self.assertEqual(fa.demote(picks, {"horse": 4, "verdict": "注意"}, True), picks)

    def test_audit_uses_fixed_rule(self):
        rows = [{"base3": -0.7, "placed": False, "place_yen": 0}, {"base3": -0.1, "placed": True, "place_yen": 110}]
        out = fa.audit(rows)
        by = {g["verdict"]: g for g in out["by_verdict"]}
        self.assertEqual((by["危険"]["horses"], by["信頼"]["horses"], by["信頼"]["place_roi"]), (1, 1, 1.1))


if __name__ == "__main__":
    unittest.main()
