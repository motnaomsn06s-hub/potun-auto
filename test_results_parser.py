"""Result parsing against the structure of the official NAR RaceMarkTable page.

Rows mirror 浦和 2026-10-20: 4R (normal finish) and 5R (2着同着: 2番・9番, no 3着 row).
Payout tables follow the finishing table on the real page and must not be mistaken for finishers.
"""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from test_reliability import FakeDB, load_app_without_database

HEAD = "<tr><th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>所属</th><th>性齢</th><th>負担 重量</th><th>騎手</th><th>調教師</th></tr>"


def page(finish_rows, payout_rows=()):
    fin = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in finish_rows)
    pay = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in payout_rows)
    return BeautifulSoup(f"<table>{HEAD}{fin}</table><table>{pay}</table>", "html.parser")


R4 = page([("1", "2", "2", "タカイチナダル", "浦和", "牝 4", "54.0", "吉留孝", "薮口一"),
           ("2", "8", "11", "アンパト", "浦和", "セン 6", "56.0", "福原杏", "川島豊"),
           ("3", "5", "5", "ニシノリピート", "浦和", "牝 5", "54.0", "岡田大", "冨田敏"),
           ("4", "8", "10", "マイカグラ", "川崎", "牝 6", "54.0", "田中涼", "武井和")],
          [("単勝", "2", "130円", "1人気"), ("複勝", "2", "100円", "1人気"), ("11", "170円", "3人気"), ("5", "230円", "5人気")])
R5 = page([("1", "3", "3", "コムダビチュード", "船橋", "牝 5", "54.0", "仲野光", "玉井等"),
           ("2", "2", "2", "オーデンリッキー", "浦和", "牝 5", "54.0", "秋元耕", "小澤宏"),
           ("2", "7", "9", "マロンルピナス", "川崎", "牝 6", "54.0", "池谷匠", "村田順"),
           ("4", "4", "4", "マサッキー", "浦和", "牝 3", "54.0", "櫻井光", "入口由")],
          [("単勝", "3", "4,190円", "8人気"), ("複勝", "3", "770円", "9人気"), ("2", "200円", "3人気"), ("9", "410円", "6人気"),
           ("三連単", "3-2-9", "222,000円", "484人気"), ("3-9-2", "308,310円", "548人気")])


class ResultParserTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_app_without_database()

    def test_normal_finish(self):
        places = self.ns['parse_result_places'](R4)
        self.assertEqual(places, [(1, 2), (2, 11), (3, 5)])
        self.assertTrue(self.ns['result_complete'](places))
        self.assertEqual(self.ns['parse_result'](R4), [2, 11, 5])

    def test_dead_heat_for_second_is_complete(self):
        places = self.ns['parse_result_places'](R5)
        self.assertEqual(places, [(1, 3), (2, 2), (2, 9)])
        self.assertTrue(self.ns['result_complete'](places))
        self.assertEqual(self.ns['parse_result'](R5), [3, 2, 9])
        payload = json.loads(self.ns['_result_payload'](places))
        self.assertEqual(payload, {'order': [3, 2, 9], 'places': [1, 2, 2], 'dead_heat': True})

    def test_other_dead_heats_and_incomplete(self):
        rc = self.ns['result_complete']
        self.assertTrue(rc([(1, 4), (1, 7), (3, 2)]))            # 1着同着
        self.assertTrue(rc([(1, 4), (2, 7), (3, 2), (3, 9)]))    # 3着同着: four horses placed
        self.assertFalse(rc([(1, 4), (2, 7)]))                    # page not final yet
        self.assertFalse(rc([(1, 4), (3, 2), (3, 9)]))            # inconsistent: 2着 missing
        self.assertFalse(rc([]))

    def test_due_saves_dead_heat_result_and_labels_all_placed_horses(self):
        now = datetime.now(self.ns['JST'])
        done = {'race_key': '2026-10-20:18:5', 'date': '2026-10-20', 'baba': '18', 'race': 5,
                'start_iso': (now - timedelta(minutes=30)).isoformat(), 'result_checked': 0}
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", []), ("COALESCE(result_checked,0)=0", [done])])
        store = Mock(return_value=12)
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=R5), store_learning_samples=store,
                        maybe_train=Mock(return_value={'status': 'COLLECTING'})):
            events = self.ns['due'](capture=False)
        self.assertIn('2026-10-20:18:5:RESULT:[3, 2, 9]', events)
        store.assert_called_once_with('2026-10-20:18:5', [3, 2, 9])
        ins = [p for sql, p in db.calls if sql.startswith('INSERT INTO results')]
        self.assertEqual(ins[0][1:4], (3, 2, 9))
        self.assertEqual(json.loads(ins[0][5])['places'], [1, 2, 2])
        self.assertTrue(any("UPDATE races SET result_checked=1,status='complete'" in sql for sql, _ in db.calls))


if __name__ == '__main__':
    unittest.main()
