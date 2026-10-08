import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_reliability import load_app_without_database

class Race3DTests(unittest.TestCase):
    def test_renderer_and_dependency_are_served(self):
        ns = load_app_without_database()
        ns['app'].root_path = str(Path(__file__).resolve().parent)
        client = ns['app'].test_client()
        for url, marker in [('/race3d.js', b'final-6'), ('/assets/three.min.js', b'WebGLRenderer')]:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn('javascript', response.content_type)
                self.assertIn(marker, response.data)
                response.close()

    def test_course_direction_survives_prediction_api(self):
        ns = load_app_without_database()
        course = {'distance':1600, 'direction':'left', 'surface':'ダート'}
        profile = {1:{'name':'Test', 'frame':1, 'style':'先・好位', 'course_meta':course}}
        with patch.dict(ns, load_profiles=Mock(return_value=profile)):
            result = ns['race_flow_summary']('fixture')
        self.assertEqual(result['course'], course)
        self.assertEqual(len(result['horses']), 1)

class FlowEvidenceTests(unittest.TestCase):
    def test_similar_distance_has_more_influence(self):
        ns=load_app_without_database()
        runs=[{'distance':1200,'field':12,'corners':[1,1,1],'finish':1}, {'distance':2000,'field':12,'corners':[10,10,10],'finish':10}]
        short=ns['_flow_path']('中団',4,3,1,{'recent_runs':runs,'current_distance':1200},12)[0]
        long=ns['_flow_path']('中団',4,3,1,{'recent_runs':runs,'current_distance':2000},12)[0]
        self.assertLess(short[0],long[0])
        self.assertTrue(all(1<=v<=12 for v in short+long))

    def test_missing_history_is_not_confidence(self):
        ns=load_app_without_database()
        evidence=ns['_flow_evidence']({},12)
        self.assertEqual(evidence['sample_runs'],0)
        self.assertIsNone(evidence['early_position_range'])
        self.assertEqual(evidence['stability'],'資料不足')

    def test_choose_available_direction_and_scale_evidence(self):
        ns=load_app_without_database()
        profiles={1:{'style':'中団','course_meta':{'direction':None}},2:{'style':'差・追','course_meta':{'direction':'left'},'recent_runs':[{'corners':[10,8,5],'field':10,'finish':4}]}}
        with patch.dict(ns,load_profiles=Mock(return_value=profiles)):
            result=ns['race_flow_summary']('fixture')
        self.assertEqual(result['course']['direction'],'left')
        self.assertEqual(result['flow_confidence'],50)
        self.assertEqual(result['horses'][1]['evidence']['early_position_range'],[2.0,2.0])

if __name__ == '__main__':
    unittest.main()
