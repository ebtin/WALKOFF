import unittest
import json
from server import flaskServer as flask_server


class TestAppBlueprint(unittest.TestCase):
    def setUp(self):
        self.app = flask_server.app.test_client(self)
        self.app.testing = True
        self.app.post('/login', data=dict(email='admin', password='admin'), follow_redirects=True)
        response = self.app.post('/key', data=dict(email='admin', password='admin'),
                                 follow_redirects=True).get_data(as_text=True)

        self.key = json.loads(response)["auth_token"]
        self.headers = {"Authentication-Token": self.key}

    def test_list_functions(self):
        expected_actions = ['helloWorld', 'repeatBackToMe', 'returnPlusOne']
        response = self.app.post('/apps/HelloWorld/actions', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = json.loads(response.get_data(as_text=True))
        self.assertEqual(len(response['actions']), len(expected_actions))
        self.assertSetEqual(set(response['actions']), set(expected_actions))

    def test_function_aliases(self):
        expected_json = {"helloWorld": ["helloworld", "hello world", "hello", "greeting", "HelloWorld", "hello_world"],
                         "repeatBackToMe": ["parrot", "Parrot", "RepeatBackToMe", "repeat_back_to_me", "repeat"],
                         "returnPlusOne": ["plus one", "PlusOne", "plus_one", "plusone", "++", "increment"]}

        response = self.app.post('/apps/HelloWorld/actions/aliases', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = json.loads(response.get_data(as_text=True))
        self.assertDictEqual(response, expected_json)

