import ast
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from capture_timing import capture_slot, capture_still_valid


def _ensure_psycopg_importable():
    # Unit tests mock every database call. Where the driver is not installed (offline sandboxes),
    # a stand-in module lets app.py import; CI installs the real driver and also runs test_pg_integration.
    try:
        import psycopg  # noqa: F401
    except ImportError:
        import sys, types
        mod = types.ModuleType('psycopg'); rows = types.ModuleType('psycopg.rows')
        rows.dict_row = object(); mod.rows = rows
        def connect(*a, **k):
            raise RuntimeError('psycopg not installed')
        mod.connect = connect
        sys.modules['psycopg'] = mod; sys.modules['psycopg.rows'] = rows


def load_app_without_database():
    _ensure_psycopg_importable()
    tree = ast.parse(Path(__file__).with_name('app.py').read_text())
    # Skip only the production schema initialization; routes/functions are unchanged.
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id in ('init', 'ensure_auto_loop'))]
    namespace = {'__name__': 'odds_test_app', '__file__': str(Path(__file__).with_name('app.py'))}
    exec(compile(tree, 'app.py', 'exec'), namespace)
    # No background scheduler in unit tests: it would race with patched database mocks.
    # Tests that exercise the loop reset _loop_state themselves.
    class _Idle:
        def is_alive(self):
            return True
    import os
    namespace['_loop_state'].update(pid=os.getpid(), thread=_Idle())
    return namespace


class ReliabilityTests(unittest.TestCase):
    def test_window_boundaries(self):
        for minutes, expected in [(15.72,15),(13.75,15),(13.74,None),
            (11.2,10),(8.9,None),(6.2,5),(4.61,5),(4.6,4),
            (3.61,4),(3.6,3),(1.85,3),(1.84,None),(0,None),(-1,None)]:
            with self.subTest(minutes=minutes):
                self.assertEqual(capture_slot(minutes), expected)

    def test_delayed_fetch_is_not_backfilled(self):
        post = datetime(2026,10,7,19,20,tzinfo=timezone(timedelta(hours=9)))
        self.assertTrue(capture_still_valid(5,post-timedelta(minutes=5.8),post-timedelta(minutes=5),post))
        self.assertFalse(capture_still_valid(5,post-timedelta(minutes=5),post-timedelta(minutes=4.5),post))
        self.assertFalse(capture_still_valid(3,post-timedelta(minutes=3),post+timedelta(seconds=1),post))

    def test_database_failure_is_not_empty_success(self):
        ns = load_app_without_database()
        with patch.dict(ns, con=Mock(side_effect=RuntimeError('private-db-host'))):
            response = ns['app'].test_client().get('/api/races')
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json['error'])
        self.assertNotIn('private-db-host', response.get_data(as_text=True))

    def test_due_rejects_late_capture_before_database_write(self):
        ns = load_app_without_database()
        now = datetime.now(ns['JST'])
        race = {'race_key':'test','start_iso':(now+timedelta(minutes=5.5)).isoformat(),'result_checked':0}
        db=Mock()
        db.execute.return_value.fetchall.return_value=[race]
        db.execute.return_value.fetchone.return_value=None
        data={'fetched_at':(now+timedelta(minutes=2)).isoformat()}
        with patch.dict(ns,con=Mock(return_value=db),load_profiles=Mock(return_value=[1]),take=Mock(return_value=data)):
            events=ns['due']()
        self.assertIn('test:5:SKIPPED_LATE', events)
        self.assertFalse(any('INSERT INTO snapshots' in call.args[0] for call in db.execute.call_args_list))


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
    def fetchall(self):
        return list(self.rows)
    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDB:
    """Answers queries by SQL substring so each code path gets realistic rows."""
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        for needle, rows in self.routes:
            if needle in sql:
                return FakeCursor(rows(params) if callable(rows) else rows)
        return FakeCursor([])
    def commit(self):
        pass
    def close(self):
        pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_app_without_database()
        self.now = datetime.now(self.ns['JST'])

    def test_results_are_fetched_for_finished_races(self):
        # Regression: due() only selected races up to 6 minutes after post time but required
        # mins < -10 to fetch results, so results/learning never ran.
        done = {'race_key': '2026-10-17:31:10', 'date': '2026-10-17', 'baba': '31', 'race': 10,
                'start_iso': (self.now - timedelta(minutes=25)).isoformat(), 'result_checked': 0, 'status': 'reserved'}
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", []),
                     ("COALESCE(result_checked,0)=0", [done])])
        store = Mock(return_value=12)
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=None),
                        parse_result_places=Mock(return_value=[(1, 3), (2, 7), (3, 1)]), store_learning_samples=store,
                        maybe_train=Mock(return_value={'status': 'COLLECTING'})):
            events = self.ns['due']()
        self.assertIn('2026-10-17:31:10:RESULT:[3, 7, 1]', events)
        store.assert_called_once_with('2026-10-17:31:10', [3, 7, 1])
        self.assertTrue(any("UPDATE races SET result_checked=1" in sql for sql, _ in db.calls))
        self.assertTrue(any("INSERT INTO capture_log" in sql and p[3] == 'RESULT' for sql, p in db.calls))

    def test_future_reservations_do_not_block_results(self):
        # A reservation 2 hours ahead must not postpone result fetching forever.
        future = {'race_key': 'f', 'start_iso': (self.now + timedelta(hours=2)).isoformat(), 'result_checked': 0}
        done = {'race_key': 'd', 'date': '2026-10-17', 'baba': '31', 'race': 9,
                'start_iso': (self.now - timedelta(minutes=40)).isoformat(), 'result_checked': 0}
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", [future]),
                     ("COALESCE(result_checked,0)=0", [done])])
        with patch.dict(self.ns, con=Mock(return_value=db), soup=Mock(return_value=None), load_profiles=Mock(return_value={1: {}}),
                        parse_result_places=Mock(return_value=[(1, 1), (2, 2), (3, 3)]), store_learning_samples=Mock(return_value=10),
                        maybe_train=Mock(return_value={})):
            events = self.ns['due']()
        self.assertIn('d:RESULT:[1, 2, 3]', events)

    def test_results_wait_while_capture_window_is_open(self):
        live = {'race_key': 'live', 'start_iso': (self.now + timedelta(minutes=5.5)).isoformat(), 'result_checked': 0}
        done = {'race_key': 'd', 'date': '2026-10-17', 'baba': '31', 'race': 9,
                'start_iso': (self.now - timedelta(minutes=40)).isoformat(), 'result_checked': 0}
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", [live]),
                     ("COALESCE(result_checked,0)=0", [done])])
        soup = Mock(return_value=None)
        take = Mock(side_effect=lambda r: {'fetched_at': datetime.now(self.ns['JST']).isoformat(), 'counts': {}})
        with patch.dict(self.ns, con=Mock(return_value=db), soup=soup, take=take,
                        save_predictions=Mock(return_value=[]), parse_result_places=Mock(return_value=[(1, 1), (2, 2), (3, 3)])):
            events = self.ns['due']()
        self.assertIn('live:5', events)
        soup.assert_not_called()

    def test_capture_error_is_logged_with_slot(self):
        live = {'race_key': 'live', 'start_iso': (self.now + timedelta(minutes=10)).isoformat(), 'result_checked': 0}
        db = FakeDB([("start_iso >= %s AND start_iso <= %s", [live])])
        with patch.dict(self.ns, con=Mock(return_value=db), take=Mock(side_effect=RuntimeError('単勝オッズ取得失敗'))):
            events = self.ns['due']()
        self.assertTrue(any(e.startswith('ERR snapshot live:10') for e in events))
        logged = [p for sql, p in db.calls if 'INSERT INTO capture_log' in sql]
        self.assertTrue(any(p[1] == 'live' and p[2] == 10 and p[3] == 'ERR' for p in logged))
        self.assertTrue(any(p[3] == 'TICK' and 'live@' in p[4] for p in logged))

    def test_races_lists_history_except_october_7_with_paging(self):
        rows = [{'race_key': f'k{i}', 'date': '2026-10-0%d' % (i % 9 + 1), 'start_iso': f'2026-10-0{i % 9 + 1}T15:00:00+09:00',
                 'status': 'complete', 'baba_name': 'X', 'race': i} for i in range(3)]
        db = FakeDB([("FROM races WHERE status!='cancelled'", rows),
                     ("FROM snapshots WHERE race_key = ANY", [{'race_key': 'k0', 'slot': 15, 'payload': '{"parser_version": 8, "rows": []}'}]),
                     ("FROM results WHERE race_key = ANY", [])])
        with patch.dict(self.ns, con=Mock(return_value=db)):
            res = self.ns['app'].test_client().get('/api/races?limit=2')
        self.assertEqual(res.status_code, 200)
        body = res.json
        self.assertEqual(len(body['races']), 2)
        self.assertTrue(body['has_more'])
        self.assertEqual(body['next_before'], f"{rows[1]['start_iso']}|k1")
        self.assertEqual(body['races'][0]['current_slots'], [15])
        sql, params = db.calls[0]
        self.assertIn('date!=%s', sql)
        self.assertEqual(params[0], '2026-10-07')
        self.assertEqual(len(db.calls), 3)  # no per-race queries
        with patch.dict(self.ns, con=Mock(return_value=db)):
            self.ns['app'].test_client().get('/api/races?limit=2&before=' + body['next_before'].replace('+', '%2B'))
        sql, params = db.calls[3]
        self.assertIn('(start_iso,race_key) < (%s,%s)', sql)
        self.assertEqual(params[1:3], [rows[1]['start_iso'], 'k1'])

    def test_cancel_only_changes_status_and_confirms(self):
        db = FakeDB([("UPDATE races SET status='cancelled'", [{'race_key': 'k'}]),
                     ("SELECT status FROM races", [{'status': 'cancelled'}])])
        with patch.dict(self.ns, con=Mock(return_value=db)):
            res = self.ns['app'].test_client().post('/api/cancel', json={'race_key': 'k'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json['status'], 'cancelled')
        self.assertFalse(any(word in sql.upper() for sql, _ in db.calls for word in ('DELETE', 'DROP', 'TRUNCATE')))

    def test_auto_loop_is_restarted_in_forked_process(self):
        ns = self.ns
        started = []
        class T:
            def __init__(self, target=None, daemon=None, name=None):
                self.alive = False
            def start(self):
                self.alive = True; started.append(self)
            def is_alive(self):
                return self.alive
        ns['_loop_state'].update(pid=-1)  # as if inherited from a parent process
        with patch.object(ns['threading'], 'Thread', T):
            ns['ensure_auto_loop']()
            ns['ensure_auto_loop']()  # second call in same process is a no-op
        self.assertEqual(len(started), 1)
        self.assertEqual(ns['_loop_state']['pid'], __import__('os').getpid())
        started[0].alive = False  # loop thread died
        with patch.object(ns['threading'], 'Thread', T):
            ns['ensure_auto_loop']()
        self.assertEqual(len(started), 2)


if __name__ == '__main__':
    unittest.main()
