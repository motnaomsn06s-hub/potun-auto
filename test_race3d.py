import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_reliability import load_app_without_database

class Race3DTests(unittest.TestCase):
    def test_renderer_and_dependency_are_served(self):
        ns = load_app_without_database()
        ns['app'].root_path = str(Path(__file__).resolve().parent)
        client = ns['app'].test_client()
        for url, marker in [('/race3d.js', b'clarity-4'), ('/assets/three.min.js', b'WebGLRenderer')]:
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

if __name__ == '__main__':
    unittest.main()
