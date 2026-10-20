"""End-to-end checks against a real PostgreSQL (run in CI with a disposable database).

Skipped unless DATABASE_URL is set. Never point this at the production database.
"""
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

DB = os.environ.get('DATABASE_URL')


@unittest.skipUnless(DB and os.environ.get('ODDS_SCOPE_TEST_DB') == '1', 'needs disposable PostgreSQL')
class PostgresIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app  # runs init(): creates schema exactly as production does
        cls.app = app

    def sql(self, q, p=()):
        c = self.app.con()
        try:
            cur = c.execute(q, p)
            rows = cur.fetchall() if cur.description else []
            c.commit()
            return rows
        finally:
            c.close()

    def test_capture_health_on_real_schema(self):
        a = self.app
        day = '2001-01-02'  # in the past, so windows are closed
        post = datetime(2001, 1, 2, 13, 50, tzinfo=a.JST)
        key = f'{day}:18:4'
        self.sql("INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked) VALUES(%s,%s,'18','浦和',4,%s,'reserved',%s,0)",
                 (key, day, post.isoformat(), (post - timedelta(hours=8)).isoformat()))
        p = json.dumps({'parser_version': a.DATA_VERSION, 'rows': [], 'counts': {'win': 11, 'Q': 55, 'E': 107, 'W': 55, 'R': 165, 'T': 962}})
        self.sql("INSERT INTO snapshots VALUES(%s,10,%s,%s)", (key, (post - timedelta(minutes=10.7)).isoformat(), p))
        self.sql("INSERT INTO capture_log(at,race_key,slot,kind,detail,duration_ms,pid) VALUES(%s,NULL,NULL,'TICK',%s,NULL,1)",
                 ((post - timedelta(minutes=10.75)).isoformat(), f'{key}@10.75'))
        self.sql("INSERT INTO capture_log(at,race_key,slot,kind,detail,duration_ms,pid) VALUES(%s,%s,5,'ERR','RuntimeError: 主要オッズ取得不完全',1200,1)",
                 ((post - timedelta(minutes=5.5)).isoformat(), key))
        res = a.app.test_client().get(f'/api/capture-health?date={day}')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        slots = {s['slot']: s for s in res.json['races'][0]['slots']}
        self.assertEqual(slots[15]['reason'], 'SCHEDULER_NOT_RUNNING')
        self.assertEqual(slots[10]['status'], 'OK')
        self.assertEqual(slots[5]['reason'], 'ERR')
        self.assertEqual(res.json['races'][0]['markets']['10']['T'], 962)

    def test_heartbeat_gap_and_monotonic_beat(self):
        a = self.app
        self.sql("INSERT INTO app_meta(k,v) VALUES('scheduler_beat','2099-01-03T10:00:00+09:00') ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v")
        up = datetime(2099, 1, 3, 10, 40, tzinfo=a.JST)
        a._record_heartbeat(up, first=True)
        rows = self.sql("SELECT kind,detail FROM capture_log WHERE kind IN ('PROCESS_START','SCHEDULER_GAP') AND at=%s ORDER BY kind", (up.isoformat(),))
        self.assertEqual([r['kind'] for r in rows], ['PROCESS_START', 'SCHEDULER_GAP'])
        a._record_heartbeat(up - timedelta(minutes=5), first=False)  # older beat from a slower worker
        self.assertEqual(self.sql("SELECT v FROM app_meta WHERE k='scheduler_beat'")[0]['v'], up.isoformat())

    def test_maintenance_queries_run_and_learning_repair_is_filtered(self):
        a = self.app
        key = '2099-01-04:18:1'
        self.sql("INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked) VALUES(%s,'2099-01-04','18','浦和',1,'2099-01-04T12:20:00+09:00','complete','2099-01-04T05:00:00+09:00',1)", (key,))
        self.sql("INSERT INTO results VALUES(%s,9,2,1,%s,'[9,2,1]')", (key, '2099-01-04T12:30:00+09:00'))
        called = []
        with patch.object(a, 'store_learning_samples', side_effect=lambda k, rs: called.append(k) or 0), \
             patch.object(a, 'soup', side_effect=RuntimeError('network disabled in test')):
            a.due(capture=False)
        self.assertNotIn(key, called)  # no snapshots -> not retried every tick
        errs = self.sql("SELECT detail FROM capture_log WHERE kind='ERR_LEARNING_REPAIR'")
        self.assertEqual(errs, [])

    def test_cancel_is_timestamped_and_keeps_data(self):
        a = self.app
        key = '2099-01-05:18:2'
        self.sql("INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked) VALUES(%s,'2099-01-05','18','浦和',2,'2099-01-05T12:50:00+09:00','reserved','2099-01-05T05:00:00+09:00',0)", (key,))
        self.sql("INSERT INTO snapshots VALUES(%s,15,'x','{}')", (key,))
        res = a.app.test_client().post('/api/cancel', json={'race_key': key})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.sql("SELECT 1 FROM capture_log WHERE race_key=%s AND kind='CANCELLED'", (key,))), 1)
        self.assertEqual(len(self.sql("SELECT 1 FROM snapshots WHERE race_key=%s", (key,))), 1)

    def test_new_maintenance_sql_and_roi_on_real_schema(self):
        from pathlib import Path
        from bs4 import BeautifulSoup
        a = self.app
        fx = lambda n: BeautifulSoup((Path(__file__).with_name('fixtures') / f'{n}.html').read_text(encoding='utf-8'), 'html.parser')
        key = '2001-02-03:18:5'
        start = datetime.now(a.JST) - timedelta(minutes=40)
        self.sql("INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked,source) VALUES(%s,'2001-02-03','18','浦和',5,%s,'complete',%s,1,'auto')",
                 (key, start.isoformat(), (start - timedelta(hours=5)).isoformat()))
        rows = [{'horse': h, 'odds': o, 'pop': i + 1} for i, (h, o) in enumerate([(1, 1.6), (5, 4.4), (2, 6.1), (6, 17.0), (12, 17.6), (7, 21.9), (9, 28.1), (3, 41.9)])]
        snap = json.dumps({'parser_version': a.DATA_VERSION, 'rows': rows})
        self.sql("INSERT INTO snapshots VALUES(%s,3,%s,%s)", (key, start.isoformat(), snap))
        self.sql("INSERT INTO results VALUES(%s,3,2,9,%s,'[3, 2, 9]')", (key, start.isoformat()))
        for rank, h in enumerate((3, 2, 9), 1):
            self.sql("INSERT INTO predictions(race_key,horse,rank,score,pop,odds,d1,d2,agree,created_at) VALUES(%s,%s,%s,1,1,1,0,0,0,%s)", (key, h, rank, start.isoformat()))
        logs = []
        log = lambda *x: logs.append(x)
        with patch.object(a, 'soup', side_effect=lambda path, q: fx('result_urawa5') if path == 'RaceMarkTable' else fx('odds_urawa5_final')):
            for _ in range(25):  # other completed rows from earlier tests may come first
                a._PAYOUT_RETRY.clear()
                a._payout_backfill_step(log)
                cur = json.loads(self.sql("SELECT payload FROM results WHERE race_key=%s", (key,))[0]['payload'])
                if isinstance(cur, dict) and cur.get('payouts'):
                    break
            a._final_odds_step(log, limit=10)
        pl = json.loads(self.sql("SELECT payload FROM results WHERE race_key=%s", (key,))[0]['payload'])
        self.assertEqual(pl['payouts']['単勝'][0]['yen'], 4190)
        self.assertEqual(self.sql("SELECT first_horse,second_horse,third_horse FROM results WHERE race_key=%s", (key,))[0], {'first_horse': 3, 'second_horse': 2, 'third_horse': 9})
        self.assertEqual(len(self.sql("SELECT 1 FROM final_odds WHERE race_key=%s", (key,))), 1)
        res = a.app.test_client().get('/api/roi?fresh=1')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.json
        self.assertGreaterEqual(body['results_with_payouts'], 1)
        self.assertGreaterEqual(body['late_money_horses'], 8)
        q = a.app.test_client().get('/api/races?limit=200')
        mine = [r for r in q.json['races'] if r['race_key'] == key][0]
        self.assertEqual(mine['current_slots'], [3])
        self.assertEqual(mine['source'], 'auto')
        self.assertFalse(a._capture_pending([{'race_key': key, 'start_iso': start.isoformat()}]))
        self.assertEqual([x for x in logs if x[2].startswith('ERR')], [])

    def test_schedule_sync_writes_reservations(self):
        from pathlib import Path
        from bs4 import BeautifulSoup
        a = self.app
        fx = lambda n: BeautifulSoup((Path(__file__).with_name('fixtures') / f'{n}.html').read_text(encoding='utf-8'), 'html.parser')
        now = datetime(2026, 10, 20, 15, 10, tzinfo=a.JST)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        def fake(path, q):
            if path == 'TodayRaceInfoTop':
                return fx('top')
            if q.get('k_raceDate') == '2026/10/20' and q['k_babaCode'] == '18':
                return fx('racelist_urawa')
            return BeautifulSoup('<table><tr><td>x</td></tr></table>', 'html.parser')
        logs = []
        with patch.object(a, 'soup', side_effect=fake), patch.object(a, 'datetime', Clock):
            a.sync_schedule(lambda *x: logs.append(x), force=True)
            a.sync_schedule(lambda *x: logs.append(x), force=True)   # idempotent
        rows = self.sql("SELECT race_key,start_iso,source,baba_name FROM races WHERE race_key LIKE '2026-10-20:18:%%' ORDER BY race")
        # 15:10: 7R (15:27) onward is still capturable; 6R (14:50) has started and is not backfilled.
        self.assertEqual([r['race_key'] for r in rows], [f'2026-10-20:18:{i}' for i in range(7, 13)])
        self.assertTrue(all(r['source'] == 'auto' and r['baba_name'] == '浦和' for r in rows))
        self.assertEqual([x for x in logs if x[2].startswith('ERR')], [])

    def test_advisory_lease_is_exclusive(self):
        a = self.app
        inner = []
        a._leased_work('itest', lambda: inner.append(a._leased_work('itest', lambda: 'second')))
        self.assertEqual(inner, [None])


if __name__ == '__main__':
    unittest.main()
