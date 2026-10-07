import ast
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from capture_timing import capture_slot, capture_still_valid


def load_app_without_database():
    tree = ast.parse(Path(__file__).with_name('app.py').read_text())
    # Skip only the production schema initialization; routes/functions are unchanged.
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == 'init')]
    namespace = {'__name__': 'odds_test_app', '__file__': str(Path(__file__).with_name('app.py'))}
    exec(compile(tree, 'app.py', 'exec'), namespace)
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


if __name__ == '__main__':
    unittest.main()
