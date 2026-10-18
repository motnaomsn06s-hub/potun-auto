import threading
import unittest
from unittest.mock import Mock, patch
from test_reliability import load_app_without_database

class SchedulerTests(unittest.TestCase):
 def test_slow_race_does_not_block_other_race_and_no_duplicate(self):
  ns=load_app_without_database();release=threading.Event();entered={k:threading.Event() for k in ('slow','fast')}
  def capture(r):
   entered[r['race_key']].set()
   if r['race_key']=='slow':release.wait(3)
   return []
  try:
   with patch.dict(ns,_leased_work=lambda key,fn:fn(),_capture_race=capture):
    self.assertTrue(ns['_dispatch_capture']({'race_key':'slow'}));self.assertTrue(entered['slow'].wait(1))
    self.assertFalse(ns['_dispatch_capture']({'race_key':'slow'}))
    self.assertTrue(ns['_dispatch_capture']({'race_key':'fast'}));self.assertTrue(entered['fast'].wait(1))
  finally:
   release.set()
   if ns['_capture_pool']:ns['_capture_pool'].shutdown(wait=True)
 def test_database_lease_blocks_other_process(self):
  ns=load_app_without_database();db=Mock();db.execute.return_value.fetchone.return_value={'locked':False};fn=Mock()
  with patch.dict(ns,con=Mock(return_value=db)):ns['_leased_work']('capture:r',fn)
  fn.assert_not_called();db.close.assert_called_once()
 def test_database_lease_released_on_error(self):
  ns=load_app_without_database();db=Mock();db.execute.return_value.fetchone.return_value={'locked':True}
  with patch.dict(ns,con=Mock(return_value=db)):
   with self.assertRaises(ValueError):ns['_leased_work']('r',Mock(side_effect=ValueError('error')))
  db.close.assert_called_once()
 def test_dispatch_tick_separates_maintenance(self):
  ns=load_app_without_database();due=Mock(return_value=[]);maintenance=Mock();lock=Mock()
  with patch.dict(ns,due=due,_start_maintenance=maintenance,_tick_lock=lock):ns['_run_due']()
  due.assert_called_once_with(maintenance=False,async_capture=True);maintenance.assert_called_once();lock.release.assert_called_once()

if __name__=='__main__':unittest.main()
