import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import signal_eval as se
from test_reliability import FakeDB, load_app_without_database


def rows(grade, n, placed, yen=150, rank=2, pop=3):
    return [{"race_key": f"{grade}{i}", "horse": 1, "rank": rank, "grade": grade, "pop": pop,
             "placed": i < placed, "place_yen": yen if i < placed else 0, "win_yen": 0} for i in range(n)]


class SignalEvalTests(unittest.TestCase):
    def test_buy_needs_prospective_volume_and_interval_above_100(self):
        good = rows("S", 120, 90, yen=180)               # place ROI 1.35, interval well above 1
        out = se.evaluate(good, True)
        s = out["by_grade"][0]
        self.assertEqual((s["grade"], s["horses"], s["verdict"]), ("S", 120, "買い候補"))
        self.assertGreater(s["place_ci"][0], 1.0)
        self.assertEqual(se.evaluate(good, False)["by_grade"][0]["verdict"], "参考")
        self.assertEqual(se.evaluate(rows("S", 40, 30, yen=180), True)["by_grade"][0]["verdict"], "データ不足")
        bad = se.evaluate(rows("D", 150, 20, yen=200), True)["by_grade"][4]
        self.assertEqual(bad["verdict"], "見送り")
        self.assertEqual(se.evaluate([], True)["by_grade"][2]["verdict"], "—")

    def test_top1_and_pop(self):
        out = se.evaluate(rows("A", 10, 5, rank=1, pop=2) + rows("A", 10, 1, rank=4, pop=8), True)
        a = out["by_grade"][1]; t = out["top1"][1]
        self.assertEqual((a["horses"], a["avg_pop"], t["horses"], t["place_rate"]), (20, 5.0, 10, 0.5))

    def test_result_lookup_dead_heat_and_payouts(self):
        r = {"first_horse": 3, "second_horse": 2, "third_horse": 9,
             "payload": json.dumps({"order": [3, 2, 9], "places": [1, 2, 2], "payouts": {"複勝": [{"combo": [3], "yen": 770}, {"combo": [2], "yen": 200}], "単勝": [{"combo": [3], "yen": 4190}]}})}
        top, place, win, priced = se.result_lookup(r)
        self.assertEqual((top, place[2], win[3], priced), ({2, 3, 9}, 200, 4190, True))
        self.assertFalse(se.result_lookup({"first_horse": 1, "second_horse": 2, "third_horse": 3, "payload": "[1,2,3]"})[3])


class SignalAppTests(unittest.TestCase):
    def test_retro_step_uses_no_learned_model_and_stores_rows(self):
        ns = load_app_without_database()
        db = FakeDB([("NOT EXISTS (SELECT 1 FROM signal_retro", [{"race_key": "k1"}])])
        ranked = [{"horse": 7, "signal_grade": "A", "signal_index": 66, "pop": 2, "odds": 4.1}, {"horse": 3, "signal_grade": "D", "signal_index": 30, "pop": 9, "odds": 40}]
        rf = Mock(return_value=ranked)
        with patch.dict(ns, con=Mock(return_value=db), final_features=Mock(return_value=[{"x": 1}]), rank_features=rf):
            self.assertEqual(ns["_signal_retro_step"](lambda *a: None), 1)
        self.assertEqual(rf.call_args.args[1], {})
        ins = [p for sql, p in db.calls if sql.startswith("INSERT INTO signal_retro")][0]
        self.assertEqual([(x["horse"], x["rank"], x["grade"]) for x in json.loads(ins[1])], [(7, 1, "A"), (3, 2, "D")])

    def test_save_predictions_records_grades_once_at_final(self):
        ns = load_app_without_database()
        ranked = [{"horse": h, "score": 60 - h, "pop": h, "odds": 2.0 + h, "signal_grade": g, "signal_index": 50, "confirmations": 2} for h, g in ((4, "A"), (7, "B"), (2, "C"), (9, "D"))]
        db = FakeDB([("SELECT 1 FROM signal_records", []), ("SELECT 1 FROM place_value_picks", [{"x": 1}])])
        with patch.dict(ns, con=Mock(return_value=db), stage_rankings=Mock(return_value=ranked), favorite_state=Mock(return_value={"enabled": False})):
            ns["save_predictions"]("k", 3)
        recs = [p for sql, p in db.calls if sql.startswith("INSERT INTO signal_records")]
        self.assertEqual([(p[1], p[2], p[3]) for p in recs], [(4, 1, "A"), (7, 2, "B"), (2, 3, "C"), (9, 4, "D")])
        db2 = FakeDB([("SELECT 1 FROM signal_records", [{"x": 1}]), ("SELECT 1 FROM place_value_picks", [{"x": 1}])])
        with patch.dict(ns, con=Mock(return_value=db2), stage_rankings=Mock(return_value=ranked), favorite_state=Mock(return_value={"enabled": False})):
            ns["save_predictions"]("k", 3)
        self.assertFalse(any(sql.startswith("INSERT INTO signal_records") for sql, _ in db2.calls))
        db3 = FakeDB([])
        with patch.dict(ns, con=Mock(return_value=db3), stage_rankings=Mock(return_value=ranked), favorite_state=Mock(return_value={"enabled": False})):
            ns["save_predictions"]("k", 5)
        self.assertFalse(any(sql.startswith("INSERT INTO signal_records") for sql, _ in db3.calls))


if __name__ == "__main__":
    unittest.main()
