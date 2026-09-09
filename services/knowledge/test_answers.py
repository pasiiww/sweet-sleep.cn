import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import answers
import server as app


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.kb = self.call('POST', 'bases', {'name': '测试'})['id']
        self.call('POST', f'bases/{self.kb}/documents', {'title': '营业说明', 'content': '营业时间为每天上午十点至晚上八点。', 'source': '已确认资料'})
        self.call('POST', f'bases/{self.kb}/documents', {'title': '其他说明', 'content': '可在线查询营业时间和配送规则。'})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, body=None):
        return app.api(method, '/knowledge/api/' + path, body or {}, {})

    def configure(self, **values):
        return self.call('PUT', 'answer-settings', {
            'enabled': True, 'model': 'deepseek-v4-flash', 'system_prompt': answers.DEFAULT_PROMPT,
            'api_key': 'test-key', 'handoff_groups': {'group123456': ['admin123456']}, **values})

    def ask(self, query='营业时间', group='group123456'):
        return self.call('POST', 'answer', {'kb_id': self.kb, 'query': query, 'group_id': group})

    def test_defaults_secret_preservation_and_embedding_isolation(self):
        cfg = self.call('GET', 'answer-settings')
        self.assertEqual(cfg['model'], 'deepseek-v4-flash')
        self.assertFalse(cfg['has_key'])
        with app.db() as c:
            embedding_before = app.config(c)
        self.configure()
        self.assertNotIn('api_key', self.call('GET', 'answer-settings'))
        self.configure(api_key='', system_prompt='新提示词')
        with app.db() as c:
            self.assertEqual(app.answer_config(c)['api_key'], 'test-key')
            self.assertEqual(app.config(c), embedding_before)
        self.configure(api_key='', clear_key=True)
        self.assertFalse(self.call('GET', 'answer-settings')['has_key'])

    def test_missing_key_and_disabled_return_only_top_document(self):
        with patch.object(answers, 'complete') as model:
            result = self.ask()
            self.assertEqual(result['reason'], 'missing_key')
            self.assertEqual(len(result['results']), 1)
            self.assertIn(result['results'][0]['content'], result['answer'])
            self.configure(enabled=False)
            self.assertEqual(self.ask()['reason'], 'disabled')
            model.assert_not_called()

    def test_no_hits_and_irrelevant_hits_handoff(self):
        self.configure()
        with patch.object(answers, 'complete') as model:
            result = self.ask('xyznotpresent')
            self.assertEqual(result['reason'], 'no_results')
            self.assertEqual(result['mention_openids'], ['admin123456'])
            model.assert_not_called()
        with patch.object(answers, 'complete', return_value={'supported': False}):
            result = self.ask()
            self.assertEqual(result['reason'], 'insufficient_evidence')
            self.assertNotIn('十点', result['answer'])
            self.assertEqual(self.ask(group='anothergroup')['mention_openids'], [])
            self.assertEqual(self.ask(group='')['mention_openids'], [])

    def test_model_failure_modes_fallback_and_status(self):
        self.configure()
        for code in ('invalid_key', 'insufficient_balance', 'network_error', 'rate_limited', 'invalid_response', 'invalid_evidence'):
            with self.subTest(code=code), patch.object(answers, 'complete', side_effect=answers.ModelError(code)):
                result = self.ask()
                self.assertEqual(result['mode'], 'document')
                self.assertEqual(result['reason'], code)
                self.assertEqual(len(result['results']), 1)
                self.assertNotIn(code, result['answer'])
                self.assertEqual(self.call('GET', 'answer-settings')['last_status']['reason'], code)

    def test_prompt_changes_apply_immediately(self):
        self.configure()
        def model(cfg, query, results):
            self.assertEqual(cfg['system_prompt'], '请用简短中文回答')
            return {'supported': True, 'answer': '根据资料，请查看营业说明。', 'citations': [1]}
        self.configure(system_prompt='请用简短中文回答')
        with patch.object(answers, 'complete', side_effect=model):
            result = self.ask()
        self.assertEqual(result['mode'], 'model')
        self.assertIn('参考资料', result['answer'])

    def test_output_validation_and_http_error_mapping(self):
        rows = [{'citation': 1, 'title': '营业说明', 'content': '上午十点营业。'}]
        with self.assertRaises(answers.ModelError):
            answers.validate_answer({'supported': True, 'answer': '八点开门', 'evidence': [{'citation': 1, 'quote': '八点开门'}]}, rows)
        cfg = answers.defaults() | {'api_key': 'test-key'}
        for status, code in ((401, 'invalid_key'), (402, 'insufficient_balance'), (429, 'rate_limited')):
            with patch.object(answers.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = HTTPError('https://api.deepseek.com', status, '', {}, io.BytesIO(b''))
                with self.assertRaisesRegex(answers.ModelError, code):
                    answers.complete(cfg, '何时营业', rows)

    def test_real_protocol_payload_and_response(self):
        rows = [{'citation': 1, 'title': '营业说明', 'content': '上午十点营业。'}]
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'supported': True, 'answer': '上午十点营业。', 'evidence': [{'citation': 1, 'quote': '上午十点营业'}]})}}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *_): return json.dumps(response).encode()
        with patch.object(answers.request, 'build_opener') as opener:
            opener.return_value.open.return_value = Response()
            result = answers.complete(answers.defaults() | {'api_key': 'test-key'}, '何时营业', rows)
            req = opener.return_value.open.call_args.args[0]
            payload = json.loads(req.data)
            self.assertEqual(req.full_url, 'https://api.deepseek.com/chat/completions')
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            self.assertIn(answers.OUTPUT_RULE, payload['messages'][0]['content'])
            self.assertTrue(result['supported'])

    def test_invalid_contacts_rejected(self):
        with self.assertRaises(app.Problem):
            self.configure(handoff_groups={'group123456': ['bad\"/><x>']})
        with self.assertRaises(app.Problem):
            self.configure(system_prompt='')


if __name__ == '__main__':
    unittest.main()
