"""Diagnostics added after the 2026-10-20 Urawa misses: every missed slot must carry a cause."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from test_reliability import FakeDB, load_app_without_database


class SlotHealthTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_app_without_database()
        self.JST = self.ns['JST']
        self.post = datetime(2026, 10, 20, 12, 20, tzinfo=self.JST)
        self.race = {'race_key': 'k', 'created_at': '2026-10-20T05:54:17+09:00'}
        self.after = self.post + timedelta(hours=1)

    def health(self, slot, now=None, snap=None, logs=(), ticks=(), alive=()):
        return self.ns['_slot_health'](slot, self.race, self.post, now or self.after, snap, list(logs), list(ticks), list(alive))

    def at(self, minutes_before):
        return self.post - timedelta(minutes=minutes_before)

    def test_server_down_is_named_as_cause(self):
        h = self.health(15)
        self.assertEqual((h['status'], h['reason']), ('MISSED', 'SCHEDULER_NOT_RUNNING'))
        self.assertIn('Render', h['reason_ja'])

    def test_running_but_not_attempted_is_distinguished(self):
        h = self.health(10, ticks=[self.at(10)], alive=[self.at(10)])
        self.assertEqual(h['reason'], 'NOT_ATTEMPTED')
        self.assertEqual(h['ticks_in_window'], 1)

    def test_fetch_error_detail_is_reported(self):
        err = {'at': self.at(5).isoformat(), 'slot': 5, 'kind': 'ERR', 'detail': 'RuntimeError: 主要オッズ取得不完全'}
        h = self.health(5, logs=[err], ticks=[self.at(5)], alive=[self.at(5)])
        self.assertEqual(h['reason'], 'ERR')
        self.assertIn('主要オッズ', h['detail'])

    def test_late_reservation_and_cancellation(self):
        self.race['created_at'] = self.at(5).isoformat()
        self.assertEqual(self.health(15)['reason'], 'RESERVED_AFTER_WINDOW')
        self.race['created_at'] = '2026-10-20T05:00:00+09:00'
        cancel = {'at': self.at(20).isoformat(), 'slot': None, 'kind': 'CANCELLED', 'detail': ''}
        self.assertEqual(self.health(15, logs=[cancel], alive=[self.at(15)])['reason'], 'CANCELLED')

    def test_open_pending_ok(self):
        self.assertEqual(self.health(3, now=self.at(20))['status'], 'PENDING')
        self.assertEqual(self.health(3, now=self.at(2.5))['status'], 'OPEN')
        snap = {'valid': True, 'fetched_at': self.at(3).isoformat(), 'capture_started_at': self.at(3.1).isoformat()}
        self.assertEqual(self.health(3, snap=snap)['status'], 'OK')
        self.assertEqual(self.health(3, snap={'valid': False, 'fetched_at': 'x'})['reason'], 'INVALID_PAYLOAD')


class CaptureHealthEndpointTests(unittest.TestCase):
    """Reproduces Urawa 4R on 2026-10-20: server asleep until 13:39, 10 and 5 captured afterwards."""

    def test_endpoint_reports_per_race_reasons_and_rate(self):
        ns = load_app_without_database()
        payload = json.dumps({'parser_version': ns['DATA_VERSION'], 'rows': [], 'counts': {'win': 11},
                              'capture_started_at': '2026-10-20T13:39:17+09:00'})
        races = [{'race_key': '2026-10-20:18:4', 'date': '2026-10-20', 'race': 4, 'baba_name': '浦和', 'status': 'reserved',
                  'start_iso': '2026-10-20T13:50:00+09:00', 'created_at': '2026-10-20T05:54:56+09:00'}]
        snaps = [{'race_key': '2026-10-20:18:4', 'slot': 10, 'fetched_at': '2026-10-20T13:39:22+09:00', 'payload': payload},
                 {'race_key': '2026-10-20:18:4', 'slot': 5, 'fetched_at': '2026-10-20T13:43:58+09:00', 'payload': payload}]
        process_rows = [{'at': '2026-10-20T13:39:14+09:00', 'kind': 'TICK', 'detail': '2026-10-20:18:4@10.75', 'pid': 40},
                        {'at': '2026-10-20T13:39:20+09:00', 'kind': 'SCHEDULER_GAP', 'pid': 43,
                         'detail': 'down_from=2026-10-20T13:00:30+09:00 up_at=2026-10-20T13:39:20+09:00 gap_s=2330'}]
        db = FakeDB([("FROM races WHERE date=%s", races),
                     ("FROM snapshots WHERE race_key = ANY", snaps),
                     ("race_key IS NULL ORDER BY at", process_rows),
                     ("FROM app_meta WHERE k='scheduler_beat'", [{'v': datetime.now(ns['JST']).isoformat()}])])
        with patch.dict(ns, con=Mock(return_value=db)):
            res = ns['app'].test_client().get('/api/capture-health?date=2026-10-20')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.json
        slots = {s['slot']: s for s in body['races'][0]['slots']}
        self.assertEqual(slots[15]['reason'], 'SCHEDULER_NOT_RUNNING')
        self.assertEqual(slots[10]['status'], 'OK')
        self.assertEqual(slots[5]['status'], 'OK')
        self.assertEqual(body['races'][0]['learning_missing_slots'], [15, 3])
        self.assertEqual(body['summary']['slots_ok'], 2)
        self.assertEqual(len(body['scheduler']['gaps_today']), 1)
        self.assertFalse(body['scheduler']['stale'])
        self.assertFalse(any(w in sql.upper() for sql, _ in db.calls for w in ('DELETE', 'UPDATE', 'INSERT', 'DROP')))

    def test_endpoint_db_failure_is_503(self):
        ns = load_app_without_database()
        with patch.dict(ns, con=Mock(side_effect=RuntimeError('db down'))):
            res = ns['app'].test_client().get('/api/capture-health')
        self.assertEqual(res.status_code, 503)


class HeartbeatTests(unittest.TestCase):
    def run_beat(self, previous, first):
        ns = load_app_without_database()
        db = FakeDB([("FROM app_meta WHERE k='scheduler_beat'", [{'v': previous}] if previous else [])])
        now = datetime(2026, 10, 20, 13, 39, 20, tzinfo=ns['JST'])
        with patch.dict(ns, con=Mock(return_value=db)):
            ns['_record_heartbeat'](now, first=first)
        return [p[3] for sql, p in db.calls if 'INSERT INTO capture_log' in sql], db

    def test_gap_after_sleep_is_logged_on_startup(self):
        kinds, db = self.run_beat('2026-10-20T13:00:30+09:00', first=True)
        self.assertEqual(kinds, ['PROCESS_START', 'SCHEDULER_GAP'])
        self.assertTrue(any('GREATEST' in sql for sql, _ in db.calls))

    def test_no_gap_for_quick_restart_and_beat_rows(self):
        self.assertEqual(self.run_beat('2026-10-20T13:39:00+09:00', first=True)[0], ['PROCESS_START'])
        self.assertEqual(self.run_beat('2026-10-20T13:39:00+09:00', first=False)[0], ['BEAT'])


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_app_without_database()
        self.now = datetime.now(self.ns['JST'])

    def test_empty_profile_backs_off_instead_of_every_tick(self):
        race = {'race_key': 'p', 'date': '2026-10-20', 'baba': '18', 'race': 9,
                'start_iso': (self.now + timedelta(minutes=60)).isoformat(), 'result_checked': 0}
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", [race])])
        getp = Mock(return_value={})
        with patch.dict(self.ns, con=Mock(return_value=db), load_profiles=Mock(return_value={}), get_profiles=getp):
            first = self.ns['due'](capture=False)
            second = self.ns['due'](capture=False)
        self.assertIn('p:PROFILE_EMPTY', first)
        self.assertNotIn('p:PROFILE_EMPTY', second)
        getp.assert_called_once()

    def test_result_without_learning_names_missing_slots(self):
        done = {'race_key': 'd', 'date': '2026-10-20', 'baba': '18', 'race': 2,
                'start_iso': (self.now - timedelta(minutes=30)).isoformat(), 'result_checked': 0}
        payload = json.dumps({'parser_version': self.ns['DATA_VERSION']})
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", []),
                     ("COALESCE(result_checked,0)=0", [done]),
                     ("SELECT slot,payload FROM snapshots WHERE race_key=%s", [{'slot': s, 'payload': payload} for s in (5, 4, 3)])])
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=None),
                        parse_payouts=Mock(return_value={'単勝': [{'combo': [1], 'yen': 300, 'pop': 1}]}), parse_result_places=Mock(return_value=[(1, 1), (2, 8), (3, 5)]), store_learning_samples=Mock(return_value=0)):
            self.ns['due'](capture=False)
        details = [p[4] for sql, p in db.calls if 'INSERT INTO capture_log' in sql and p[3] == 'RESULT']
        self.assertTrue(details and 'missing_slots=[15, 10]' in details[0], details)

    def test_learning_repair_only_considers_races_with_required_snapshots(self):
        db = FakeDB([])
        with patch.dict(self.ns, con=Mock(return_value=db)):
            self.ns['due'](capture=False)
        repair = [sql for sql, _ in db.calls if 'NOT EXISTS' in sql and 'learning_samples' in sql]
        self.assertTrue(repair and 's.slot IN (15,10,5,3))=4' in repair[0])


if __name__ == '__main__':
    unittest.main()
