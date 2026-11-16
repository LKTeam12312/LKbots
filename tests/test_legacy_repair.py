"""Offline protocol-contract tests; never contact Apple or use real credentials."""
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from requests import Response
from requests.structures import CaseInsensitiveDict

# Only this isolated test loader uses stubs, not production files.
pkg = types.ModuleType('pyicloud')
pkg.PyiCloudService = type('PyiCloudService', (), {})
sub = types.ModuleType('pyicloud.session')
class StubBaseSession:
    def _handle_request_error(self, status_code, response):
        return 'upstream'
sub.PyiCloudSession = StubBaseSession
with patch.dict(sys.modules, {'pyicloud': pkg, 'pyicloud.session': sub}):
    spec = importlib.util.spec_from_file_location('repair_under_test', Path(__file__).parents[1] / 'icloud_bot/legacy_repair.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def response(status=200, headers=None, body=None, url='https://idmsa.apple.com/appleauth/auth/signin/complete'):
    r = Response()
    r.status_code = status
    r.url = url
    r.headers = CaseInsensitiveDict(headers or {})
    r._content = json.dumps(body).encode() if body is not None else b''
    return r


def initial():
    return response(412, {
        'Location': 'https://appleid.apple.com/widget/account/repair?widgetKey=public-test-key',
        'scnt': 'idms-scnt', 'X-Apple-ID-Session-Id': 'idms-session',
        'X-Apple-Repair-Session-Token': 'repair-initial',
    }, {'authType': 'sa'})


def replies():
    return [
        response(headers={'scnt': 'shell-scnt'}),
        response(headers={'scnt': 'options-scnt', 'X-Apple-Session-Token': 'repair-options'},
                 body={'repairAttribute': 'hsa2_enrollment', 'requiredSteps': ['hsa2_enrollment']}),
        response(body={'mandatory': False, 'successful': False}),
        response(204, {'X-Apple-Session-Token': 'ordinary-token'}),
    ]


class FakeSession:
    def __init__(self, rs=None):
        self.responses = list(rs if rs is not None else replies())
        self.calls = []
        self.data = {'client_id': 'stable-client', 'session_token': 'old-token'}
        self.saved = 0
    def request_raw(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        r = self.responses.pop(0)
        self._update_session_data(r)
        return r
    def _update_session_data(self, r):
        for h, k in [('X-Apple-Session-Token', 'session_token'), ('scnt', 'scnt')]:
            if r.headers.get(h): self.data[k] = r.headers[h]
    def _save_session_data(self): self.saved += 1


class RepairTests(unittest.TestCase):
    def test_flow_and_scnt_contexts(self):
        s = FakeSession()
        r = module.complete_optional_repair(s, initial())
        self.assertEqual(r.status_code, 204)
        self.assertEqual(len(s.calls), 4)
        self.assertEqual(s.calls[1][2]['headers']['scnt'], 'shell-scnt')
        self.assertEqual(s.calls[2][2]['headers']['scnt'], 'options-scnt')
        self.assertEqual(s.calls[3][2]['headers']['scnt'], 'idms-scnt')
        self.assertEqual(s.calls[3][2]['data'], b'')
        self.assertEqual(s.calls[3][2]['headers']['X-Apple-Repair-Session-Token'], 'repair-options')
        self.assertEqual(s.data['session_token'], 'ordinary-token')
        self.assertTrue(all(c[2]['allow_redirects'] is False for c in s.calls))
    def test_untrusted_location_is_rejected_before_network(self):
        i = initial(); i.headers['Location'] = 'https://example.org/widget/account/repair?widgetKey=x'
        s = FakeSession()
        with self.assertRaises(module.LegacyRepairError): module.complete_optional_repair(s, i)
        self.assertEqual(s.calls, [])
    def test_missing_widget(self):
        i = initial(); i.headers['Location'] = 'https://appleid.apple.com/widget/account/repair'
        with self.assertRaises(module.LegacyRepairError): module.complete_optional_repair(FakeSession(), i)
    def test_missing_initial_header(self):
        i = initial(); del i.headers['scnt']
        with self.assertRaises(module.LegacyRepairError): module.complete_optional_repair(FakeSession(), i)
    def test_unknown_repair_does_not_call_setuplater(self):
        rs = replies(); rs[1]._content = b'{"requiredSteps":["verify_email"]}'
        s = FakeSession(rs)
        with self.assertRaises(module.LegacyRepairError): module.complete_optional_repair(s, initial())
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(s.data['session_token'], 'old-token')
    def test_mandatory_stops_before_complete(self):
        rs = replies(); rs[2]._content = b'{"mandatory":true}'
        s = FakeSession(rs)
        with self.assertRaises(module.LegacyRepairError): module.complete_optional_repair(s, initial())
        self.assertEqual(len(s.calls), 3)
        self.assertEqual(s.data['session_token'], 'old-token')
    def test_questions_are_not_silently_bypassed(self):
        rs = replies(); rs[1]._content = b'{"securityQuestions":{"questions":[{"id":1}]}}'
        s = FakeSession(rs)
        with self.assertRaises(module.LegacyRepairError): module.complete_optional_repair(s, initial())
        self.assertEqual(len(s.calls), 2)
    def test_complete_must_issue_real_token(self):
        rs = replies(); rs[3] = response(204)
        s = FakeSession(rs)
        with self.assertRaises(module.LegacyRepairError): module.complete_optional_repair(s, initial())
        self.assertEqual(s.data['session_token'], 'old-token')
    def test_failure_never_prints_secret_body(self):
        rs = replies(); rs[1] = response(429, body={'secret': 'DO_NOT_LEAK'})
        with self.assertRaises(module.LegacyRepairError) as e:
            module.complete_optional_repair(FakeSession(rs), initial())
        self.assertNotIn('DO_NOT_LEAK', str(e.exception))
    def test_hsa2_is_left_to_upstream(self):
        s = module.RepairSession()
        r = initial(); r._content = b'{"authType":"hsa2"}'
        self.assertEqual(s._handle_request_error(412, r), 'upstream')
    def test_wrong_endpoint_left_to_upstream(self):
        s = module.RepairSession(); r = initial(); r.url = 'https://idmsa.apple.com/appleauth/auth/verify/phone'
        self.assertEqual(s._handle_request_error(412, r), 'upstream')
    def test_disabled_flag(self):
        with patch.dict('os.environ', {'ICLOUD_ALLOW_OPTIONAL_REPAIR': '0'}):
            self.assertEqual(module.RepairSession()._handle_request_error(412, initial()), 'upstream')


if __name__ == '__main__':
    unittest.main()
