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
        self.keyword_mock = patch.object(answers, 'keywords', side_effect=lambda cfg, q: [[q], [q + 'xyz']]).start()
        self.addCleanup(patch.stopall)
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
        self.assertEqual(cfg['model'], 'deepseek-v4.1-flash-expires-on-0910')
        self.assertFalse(cfg['has_key'])
        with app.db() as c:
            embedding_before = app.config(c)
        self.configure(keyword_prompt='仅提取当前问题的词')
        self.assertEqual(self.call('GET', 'answer-settings')['keyword_prompt'], '仅提取当前问题的词')
        self.assertNotIn('api_key', self.call('GET', 'answer-settings'))
        self.configure(api_key='', system_prompt='新提示词')
        with app.db() as c:
            self.assertEqual(app.answer_config(c)['api_key'], 'test-key')
            self.assertEqual(app.answer_config(c)['keyword_prompt'], '仅提取当前问题的词')
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
        with patch.object(answers, 'complete', return_value={'supported': False, 'answer':'这个还不确定，请找落落确认呀～'}) as model:
            result = self.ask('xyznotpresent')
            self.assertEqual(result['reason'], 'no_results')
            self.assertEqual(result['mention_openids'], ['admin123456'])
            model.assert_called_once()
            self.assertEqual(model.call_args.args[2], [])
            self.assertEqual(result['answer'], '这个还不确定，请找落落确认呀～')
        with patch.object(answers, 'complete', return_value={'supported': False}):
            result = self.ask()
            self.assertEqual(result['reason'], 'insufficient_evidence')
            self.assertNotIn('十点', result['answer'])
            self.assertEqual(self.ask(group='anothergroup')['mention_openids'], [])
            self.assertEqual(self.ask(group='')['mention_openids'], [])

    def test_model_failure_modes_fallback_and_status(self):
        self.configure()
        for code in ('invalid_key', 'insufficient_balance', 'network_error', 'rate_limited', 'invalid_response', 'invalid_keywords'):
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
        self.assertEqual(result['answer'], '根据资料，请查看营业说明。')
        self.assertEqual(len(result['search_terms']), 2)

    def test_grouped_retrieval_accepts_partial_terms(self):
        def add(title, content):
            return self.call('POST', f'bases/{self.kb}/documents', {'title': title, 'content': content})['id']
        target = add('凯伊', '价格为100元。')
        alias = add('KEI预订', '定金为20元。')
        add('其他角色', '价格为999元，定金为999元。')
        add('凯伊介绍', '角色介绍，欢迎咨询。')
        add('散字', '凯旋而归，伊始的物价，格外优惠。')
        other_kb = self.call('POST', 'bases', {'name': '隔离'})['id']
        self.call('POST', f'bases/{other_kb}/documents', {'title': '凯伊', 'content': '价格不能跨库召回。'})
        result = self.call('POST', 'retrieve', {'kb_id': self.kb,
            'query_groups': [['凯伊', '价格'], ['kei', '定金'], ['价格', '凯伊']]})
        self.assertTrue({target,alias}.issubset({r['document_id'] for r in result['results']}))
        self.assertEqual(len(result['query_groups']), 2)
        self.assertEqual(result['score_type'], 'rrf')
        self.assertTrue(self.call('POST', 'retrieve', {'kb_id': self.kb,
            'query_groups': [['不存在的角色', '价格']]})['results'])
        # FTS operators and quotes are literal user data, never query syntax.
        self.assertFalse(self.call('POST', 'retrieve', {'kb_id': self.kb,
            'query_groups': [['凯伊" OR *']]})['results'])
        for groups in ([], '凯伊', [['']], [['a'] * 7], [[1]], [['a']] * 6):
            with self.subTest(groups=groups), self.assertRaises(app.Problem):
                self.call('POST', 'retrieve', {'kb_id': self.kb, 'query_groups': groups})

    def test_grouped_answer_pipeline_and_failure_preserves_results(self):
        self.configure()
        self.keyword_mock.return_value = None
        self.keyword_mock.side_effect = lambda cfg, q: [['营业', '时间'], ['配送', '规则']]
        with patch.object(answers, 'complete', side_effect=answers.ModelError('insufficient_balance')):
            result = self.ask()
        self.assertEqual(result['mode'], 'document')
        self.assertEqual(result['query_groups'], [['营业', '时间'], ['配送', '规则']])
        self.assertEqual(len(result['results']), 1)
        for reason in ('invalid_keywords', 'invalid_response', 'output_truncated'):
            with patch.object(answers, 'keywords', side_effect=answers.ModelError(reason)), patch.object(answers, 'complete', return_value={'supported':True,'answer':'十点开门哦～'}) as complete:
                result = self.ask()
                self.assertEqual(result['mode'], 'model')
                self.assertEqual(result['answer'], '十点开门哦～')
                complete.assert_called_once()
                trace = self.call('GET', 'traces/' + result['trace_id'])
                self.assertEqual(trace['details']['keyword_error'], reason)
        for reason in ('invalid_key', 'insufficient_balance'):
            with patch.object(answers, 'keywords', side_effect=answers.ModelError(reason)), patch.object(answers, 'complete') as complete:
                self.assertEqual(self.ask()['reason'], reason)
                complete.assert_not_called()

    def test_output_validation_and_http_error_mapping(self):
        rows = [{'citation': 1, 'title': '营业说明', 'content': '上午十点营业。'}]
        cfg = answers.defaults() | {'api_key': 'test-key'}
        for status, code in ((401, 'invalid_key'), (402, 'insufficient_balance'), (429, 'rate_limited')):
            with patch.object(answers.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = HTTPError('https://api.deepseek.com', status, '', {}, io.BytesIO(b''))
                with self.assertRaisesRegex(answers.ModelError, code):
                    answers.complete(cfg, '何时营业', rows)

    def test_real_protocol_payload_and_response(self):
        rows = [{'citation': 1, 'title': '营业说明', 'content': '上午十点营业。'}]
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '上午十点营业。'}}]}
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
            self.assertNotIn('response_format', payload)
            self.assertEqual(result['answer'], '上午十点营业。')
            self.assertIn(answers.OUTPUT_RULE, payload['messages'][0]['content'])
            self.assertTrue(result['supported'])

    def test_invalid_contacts_rejected(self):
        with self.assertRaises(app.Problem):
            self.configure(handoff_groups={'group123456': ['bad\"/><x>']})
        with self.assertRaises(app.Problem):
            self.configure(system_prompt='')


class KeywordTests(unittest.TestCase):
    def test_keyword_count_uniqueness_and_json_mode(self):
        cfg = answers.defaults()
        with patch.object(answers, 'model_call', return_value='{"query_groups":[["凯伊","价格"],["kei","定金"],["价格","凯伊"],["KEI","定金"]]}') as model:
            self.assertEqual(answers.keywords(cfg, '凯伊价格？'), [['凯伊', '价格'], ['kei', '定金']])
            self.assertTrue(model.call_args.kwargs['json_mode'])
        with patch.object(answers, 'model_call', return_value='{"query_groups":[["凯伊","定金"]]}'):
            self.assertEqual(answers.keywords(cfg, '那定金呢'), [['凯伊', '定金']])
        for values in (list('abcdef'), [1, 2], [[' '], ['x']], [['x'] * 7, ['y']]):
            with patch.object(answers, 'model_call', return_value=json.dumps({'query_groups': values})):
                with self.assertRaises(answers.ModelError):
                    answers.keywords(cfg, '问题')

    def test_multisearch_deduplicates_ids_and_repeated_content(self):
        row = {'chunk_id': 1, 'document_id': 'doc1', 'title': '标题', 'content': '十点开门', 'ordinal': 0}
        with patch.object(app, 'retrieve', side_effect=[
            {'results': [row, row | {'chunk_id': 2, 'content': '八点关门'}]},
            {'results': [row, row | {'chunk_id': 3, 'document_id': 'doc2'}]}]) as retriever:
            result = app.search_terms('kb1', ['营业时间', '几点开门'])
        self.assertEqual(retriever.call_count, 2)
        self.assertEqual([r['content'] for r in result['results']], ['十点开门', '八点关门'])

    def test_separate_prompts_and_current_aliases(self):
        cfg = answers.defaults() | {'keyword_prompt':'检索阶段自定义', 'system_prompt':'日本語で回答',
            'keyword_alias_context':'"kei" 是 "凯伊" 的别名', 'alias_context':'"kei" 是 "凯伊" 的别名',
            'conversation_history':[{'role':'user','content':'之前的问题'},{'role':'assistant','content':'之前的回答'}]}
        with patch.object(answers, 'model_call', side_effect=['{"query_groups":[["凯伊"]]}', 'こんにちは']) as model:
            answers.keywords(cfg, 'kei')
            answers.complete(cfg, 'kei', [])
        first, second = [call.args[1] for call in model.call_args_list]
        self.assertEqual(len(first),4)
        self.assertEqual(first[1:3],second[1:3])
        self.assertIn('检索阶段自定义',first[0]['content'])
        self.assertIn(cfg['alias_context'],json.loads(first[-1]['content'])['alias_context'])
        self.assertNotIn(cfg['alias_context'],first[0]['content'])
        self.assertNotIn('日本語で回答',first[0]['content'])
        self.assertTrue(second[0]['content'].startswith('日本語で回答'))
        self.assertNotIn('给用户的中文回复',second[0]['content'])
        self.assertNotIn('检索阶段自定义',second[0]['content'])
        self.assertEqual(second[1:3],cfg['conversation_history'])

    def test_handoff_marker_and_no_evidence_contract(self):
        with patch.object(answers, 'model_call', return_value='你好，这里直接回复。'):
            result = answers.complete(answers.defaults(), '问题', [])
            self.assertEqual(result, {'supported': True, 'answer': '你好，这里直接回复。'})
        with patch.object(answers, 'model_call', return_value='[[HANDOFF]]'):
            self.assertFalse(answers.complete(answers.defaults(), '问题', [])['supported'])


if __name__ == '__main__':
    unittest.main()
