"""Auto-reservation, payouts, final odds and ROI. Page fixtures are trimmed copies of the official
keiba.go.jp pages captured on 2026-10-20 (top page, 浦和/盛岡 race lists, 浦和4R/5R results, 浦和5R odds)."""
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

import nar_pages as n
import roi
from test_reliability import FakeDB, load_app_without_database

FIX = Path(__file__).with_name('fixtures')


def page(name):
    return BeautifulSoup((FIX / f'{name}.html').read_text(encoding='utf-8'), 'html.parser')


class OfficialPageParsers(unittest.TestCase):
    def test_venues_for_today_and_tomorrow(self):
        v = n.parse_venue_days(page('top'), ['2026-10-20', '2026-10-21'])
        self.assertEqual(sorted(c for d, c in v if d == '2026-10-20'), ['10', '18', '22', '36'])
        self.assertIn(('2026-10-21', '18'), v)
        self.assertFalse(any(d == '2026-10-19' for d, _ in v))

    def test_race_list_times_and_changes(self):
        rows = {r['race']: r for r in n.parse_race_list(page('racelist_urawa'))}
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[7]['time'], '15:27')   # official time; the manual reservation said 15:25
        self.assertTrue(rows[1]['changed'])
        self.assertEqual(rows[12]['time'], '18:20')

    def test_official_cancellations_come_from_top_grid(self):
        grid = n.parse_today_grid(page('top'))
        self.assertEqual(n.cancelled_races(grid, '10'), {1, 2})   # 盛岡1R・2R 取止
        self.assertEqual(n.cancelled_races(grid, '18'), set())

    def test_payouts_with_dead_heat(self):
        p = n.parse_payouts(page('result_urawa5'))
        self.assertEqual(p['単勝'], [{'combo': [3], 'yen': 4190, 'pop': 8}])
        self.assertEqual([e['combo'][0] for e in p['複勝']], [3, 2, 9])
        self.assertEqual(len(p['三連単']), 2)             # 2着同着 -> two trifecta payouts
        self.assertEqual(p['三連複'][0], {'combo': [2, 3, 9], 'yen': 45870, 'pop': 70})
        p4 = n.parse_payouts(page('result_urawa4'))
        self.assertEqual(p4['ワイド'][2], {'combo': [5, 11], 'yen': 730, 'pop': 8})

    def test_final_win_place_odds(self):
        wp = n.parse_win_place(page('odds_urawa5_final'))
        self.assertEqual(len(wp), 12)
        self.assertEqual(wp[3], {'win': 41.9, 'place_low': 5.1, 'place_high': 9.6})   # matches 単勝 4,190円

    def test_saga_is_stored_with_form_code(self):
        self.assertEqual(n.stored_baba('32'), '55')
        self.assertEqual(n.stored_baba('3'), '3')
        self.assertEqual(n.stored_baba('18'), '18')


class RoiTests(unittest.TestCase):
    def test_returns_on_real_payouts(self):
        pay = n.parse_payouts(page('result_urawa5'))
        self.assertEqual(roi.race_outcome([3, 2, 9], pay, 'place'), (300, 770 + 200 + 410))
        self.assertEqual(roi.race_outcome([3, 2, 9], pay, 'wide'), (300, 3580 + 3640 + 1640))
        self.assertEqual(roi.race_outcome([9, 3, 2], pay, 'trio'), (100, 45870))
        self.assertEqual(roi.race_outcome([1, 5, 2], pay, 'win'), (300, 0))
        self.assertEqual(roi.race_outcome([1, 5, 2], pay, 'quinella'), (300, 0))

    def test_evaluate_compares_on_same_races_with_interval(self):
        pay = n.parse_payouts(page('result_urawa4'))
        races = [{'race_key': f'r{i}', 'picks': [2, 11, 5], 'favorites': [2, 11, 1], 'payouts': pay} for i in range(25)]
        races.append({'race_key': 'no-picks', 'picks': [], 'favorites': [1, 2, 3], 'payouts': pay})
        out = roi.evaluate(races)
        self.assertEqual(out['races'], 25)
        win = next(s for s in out['strategies'] if s['key'] == 'win')
        self.assertAlmostEqual(win['scope']['roi'], 130 / 300, places=4)
        self.assertEqual(win['scope']['ci95'], [round(130 / 300, 4)] * 2)
        trio = next(s for s in out['strategies'] if s['key'] == 'trio')
        self.assertEqual(trio['scope']['roi'], 15.7)
        self.assertEqual(trio['favorite']['roi'], 0)

    def test_late_money_buckets(self):
        rows = roi.late_money([{'odds3': 10, 'final': 7, 'placed': True, 'place_yen': 300},
                               {'odds3': 10, 'final': 9, 'placed': False}, {'odds3': 10, 'final': 10, 'placed': True, 'place_yen': 150},
                               {'odds3': 10, 'final': 15, 'placed': False}, {'odds3': None, 'final': 3, 'placed': True}])
        self.assertEqual([r['horses'] for r in rows], [1, 1, 1, 1])
        self.assertEqual(rows[0]['place_roi'], 3.0)


class ScheduleSyncTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_app_without_database()
        self.pages = {'TodayRaceInfoTop': page('top'), ('RaceList', '18'): page('racelist_urawa'),
                      ('RaceList', '10'): page('racelist_morioka')}

    def fake_soup(self, path, q):
        if path == 'TodayRaceInfoTop':
            return self.pages[path]
        if q.get('k_raceDate') == '2026/10/20' and (path, q['k_babaCode']) in self.pages:
            return self.pages[(path, q['k_babaCode'])]
        return BeautifulSoup('<table><tr><td>ご指定のレース一覧の情報がありません</td></tr></table>', 'html.parser')

    def run_sync(self, existing, now):
        db = FakeDB([("SELECT start_iso,status FROM races WHERE race_key=%s", lambda p: [existing[p[0]]] if p[0] in existing else []),
                     ("UPDATE races SET status='cancelled' WHERE race_key=%s AND status='reserved'",
                      lambda p: [{'race_key': p[0]}] if existing.get(p[0], {}).get('status') == 'reserved' else [])])
        logs = []
        log = lambda *a: logs.append(a)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with patch.dict(self.ns, con=Mock(return_value=db), soup=self.fake_soup, datetime=Clock):
            out = self.ns['sync_schedule'](log, force=True)
        return out, db, logs

    def test_imports_future_races_follows_time_change_and_respects_cancel(self):
        JST = self.ns['JST']
        now = datetime(2026, 10, 20, 15, 10, tzinfo=JST)
        existing = {'2026-10-20:18:7': {'start_iso': '2026-10-20T15:25:00+09:00', 'status': 'reserved'},
                    '2026-10-20:18:8': {'start_iso': '2026-10-20T16:00:00+09:00', 'status': 'cancelled'},
                    '2026-10-20:10:1': {'start_iso': '2026-10-20T11:40:00+09:00', 'status': 'reserved'}}
        out, db, logs = self.run_sync(existing, now)
        inserts = [p for sql, p in db.calls if sql.lstrip().startswith('INSERT INTO races')]
        keys = {p[0] for p in inserts}
        self.assertIn('2026-10-20:18:9', keys)
        self.assertNotIn('2026-10-20:18:8', keys)          # user cancelled: never revived
        self.assertNotIn('2026-10-20:18:6', keys)          # already run: never backfilled
        self.assertIn('2026-10-20:10:8', keys)             # 盛岡8R 15:50
        self.assertTrue(all(p[3] in ('浦和', '盛岡') for p in inserts))
        updates = [p for sql, p in db.calls if sql.startswith('UPDATE races SET start_iso')]
        self.assertEqual(updates, [('2026-10-20T15:27:00+09:00', '2026-10-20:18:7')])
        kinds = [a[2] for a in logs]
        self.assertIn('POST_TIME_CHANGED', kinds)
        self.assertIn('RACE_CANCELLED_OFFICIAL', kinds)     # 盛岡1R 取止
        self.assertEqual(out['time_changed'], 1)
        self.assertFalse(any(w in sql.upper() for sql, _ in db.calls for w in ('DELETE', 'DROP', 'TRUNCATE')))

    def test_not_rerun_within_interval(self):
        logs = []
        with patch.dict(self.ns, soup=Mock(side_effect=RuntimeError('offline'))):
            self.ns['sync_schedule'](lambda *a: logs.append(a), force=True)
            self.assertIsNone(self.ns['sync_schedule'](lambda *a: logs.append(a)))
        self.assertEqual(logs[-1][2], 'ERR_SCHEDULE')


class ResultPayoutTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_app_without_database()
        self.now = datetime.now(self.ns['JST'])

    def race(self):
        return {'race_key': '2026-10-20:18:5', 'date': '2026-10-20', 'baba': '18', 'race': 5,
                'start_iso': (self.now - timedelta(minutes=30)).isoformat(), 'result_checked': 0}

    def test_result_saved_with_official_payouts(self):
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", []), ("COALESCE(result_checked,0)=0", [self.race()])])
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=page('result_urawa5')),
                        store_learning_samples=Mock(return_value=12)):
            ev = self.ns['due'](capture=False)
        self.assertIn('2026-10-20:18:5:RESULT:[3, 2, 9]', ev)
        ins = [p for sql, p in db.calls if sql.startswith('INSERT INTO results')][0]
        saved = json.loads(ins[5])
        self.assertEqual(saved['payouts']['単勝'][0]['yen'], 4190)
        self.assertEqual(saved['places'], [1, 2, 2])

    def test_order_without_refunds_waits_then_saves_after_grace(self):
        no_pay = BeautifulSoup(str(page('result_urawa5').find('table')), 'html.parser')  # finishing table only
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", []), ("COALESCE(result_checked,0)=0", [self.race()])])
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=no_pay), store_learning_samples=Mock(return_value=12)):
            self.assertEqual([e for e in self.ns['due'](capture=False) if 'RESULT' in e], [])
            self.ns['_RESULT_RETRY'].clear()
            self.ns['_RESULT_FIRST_COMPLETE']['2026-10-20:18:5'] = self.now - timedelta(minutes=25)
            self.assertIn('2026-10-20:18:5:RESULT:[3, 2, 9]', self.ns['due'](capture=False))
        kinds = [p[3] for sql, p in db.calls if 'INSERT INTO capture_log' in sql]
        self.assertIn('RESULT_WAIT_PAYOUT', kinds)
        self.assertIn('PAYOUT_MISSING', kinds)

    def test_training_is_batched(self):
        logs = []
        mt = Mock(return_value={'status': 'COLLECTING'})
        with patch.dict(self.ns, maybe_train=mt):
            self.assertIsNone(self.ns['_train_if_due'](lambda *a: logs.append(a)))   # nothing queued
            self.ns['_request_training']()
            self.ns['_train_if_due'](lambda *a: logs.append(a))
            self.ns['_request_training']()
            self.ns['_train_if_due'](lambda *a: logs.append(a))                      # within 30 min: waits
        mt.assert_called_once()

    def test_final_odds_stored_after_race(self):
        r = {**self.race(), 'status': 'complete'}
        db = FakeDB([("NOT EXISTS (SELECT 1 FROM final_odds", [r])])
        logs = []
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=page('odds_urawa5_final'))):
            self.ns['_final_odds_step'](lambda *a: logs.append(a))
        ins = [p for sql, p in db.calls if sql.startswith('INSERT INTO final_odds')]
        self.assertEqual(json.loads(ins[0][2])['horses']['3']['win'], 41.9)
        self.assertEqual(logs[-1][2], 'FINAL_ODDS')

    def test_payout_backfill_keeps_result_columns(self):
        row = {**self.race(), 'result_payload': '[3, 2, 9]'}
        db = FakeDB([("x.payload NOT LIKE", [row])])
        logs = []
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=page('result_urawa5'))):
            self.ns['_payout_backfill_step'](lambda *a: logs.append(a))
        upd = [(sql, p) for sql, p in db.calls if sql.startswith('UPDATE results')]
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0][0], 'UPDATE results SET payload=%s WHERE race_key=%s')
        saved = json.loads(upd[0][1][0])
        self.assertEqual(saved['places'], [1, 2, 2])
        self.assertIn('三連複', saved['payouts'])


if __name__ == '__main__':
    unittest.main()
