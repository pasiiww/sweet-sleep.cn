import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import answers
import entities
import server as app


class EntityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.kb = self.call('POST', 'bases', {'name': '实体测试'})['id']
        self.path = f'bases/{self.kb}/entities'
        self.call('PUT', self.path, {'items': [{'name': '凯伊', 'aliases': ['kei', '小凯']}]})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, data=None):
        return app.api(method, '/knowledge/api/' + path, data or {}, {})

    def test_catalog_crud_conflicts_and_isolation(self):
        before = self.call('GET', self.path)
        with self.assertRaises(app.Problem) as conflict:
            self.call('PUT', self.path, {'items': [], 'expected_items': []})
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(self.call('PUT', self.path, {'items': before['items'], 'expected_items': before['items']}), before)
        with self.assertRaises(app.Problem):
            self.call('PUT', self.path, {'items': before['items'] + [{'name': '其他', 'aliases': ['KEI']}]})
        self.assertEqual(self.call('GET', self.path), before)
        second = self.call('POST', 'bases', {'name': '其他库'})['id']
        self.assertEqual(self.call('GET', f'bases/{second}/entities')['items'], [])
        self.call('PUT', self.path, {'items': [{'name': '凯伊', 'aliases': ['kei', 'KEI', '凯伊']}]})
        self.assertEqual(self.call('GET', self.path)['items'][0]['aliases'], ['kei'])
        self.call('PUT', self.path, {'items': []})
        self.assertEqual(self.call('GET', self.path)['items'], [])
        for items in (None, {}, [{'name': 'x', 'aliases': ['\n']}], [{'name': '', 'aliases': []}]):
            with self.assertRaises(app.Problem):
                self.call('PUT', self.path, {'items': items})
        self.call('DELETE', 'bases/' + self.kb)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM entity_catalog').fetchone()[0], 0)

    def test_longest_alias_and_latin_boundaries(self):
        catalog = entities.Catalog([{'name': '凯伊', 'aliases': ['kei', '小凯']},
                                    {'name': '夏日凯伊', 'aliases': ['夏日小凯']}])
        self.assertEqual(catalog.normalize('KEI和夏日小凯价格，keiko？'), '凯伊和夏日凯伊价格，keiko？')
        hints = catalog.hints(['KEI价格', '夏日小凯', 'keiko'])
        self.assertEqual([row['name'] for row in hints], ['凯伊', '夏日凯伊'])
        self.assertIn('的别名', entities.context(hints))

    def test_canonical_queries_retrieve_alias_documents(self):
        self.call('POST', f'bases/{self.kb}/documents', {'title': 'KEI', 'content': '价格100元，定金20元。'})
        self.call('POST', f'bases/{self.kb}/documents', {'title': '其他角色', 'content': '价格999元。'})
        fallback = self.call('POST', 'answer', {'kb_id': self.kb, 'query': '小凯多少钱'})
        self.assertEqual(fallback['mode'], 'document')
        self.assertEqual(fallback['results'][0]['title'], 'KEI')
        for name in ['凯伊', '小凯', 'kei']:
            result = self.call('POST', 'retrieve', {'kb_id': self.kb, 'query_groups': [[name, '价格']]})
            self.assertEqual(result['query_groups'], [['凯伊', '价格']])
            self.assertEqual([row['title'] for row in result['results']], ['KEI'])

    def test_alias_hints_and_history_reach_both_model_calls(self):
        self.call('POST', f'bases/{self.kb}/documents', {'title': 'KEI', 'content': '价格100元，定金20元。'})
        self.call('PUT', 'answer-settings', {'enabled': True, 'model': 'deepseek-v4-flash',
                                            'api_key': 'test', 'system_prompt': answers.DEFAULT_PROMPT})
        history = [{'role': 'user', 'content': 'kei多少钱'}, {'role': 'assistant', 'content': '总价100元。'}]
        with patch.object(answers, 'model_call', side_effect=[
            '{"query_groups":[["kei","定金"],["小凯","价格"]]}', '定金20元。']) as model:
            result = self.call('POST', 'answer', {'kb_id': self.kb, 'query': '那定金呢？', 'history': history})
        self.assertEqual(result['query_groups'], [['凯伊', '定金'], ['凯伊', '价格']])
        self.assertEqual(result['history_turns'], 1)
        self.assertIn('"kei" 是 "凯伊" 的别名', result['alias_context'])
        for call in model.call_args_list:
            messages = call.args[1]
            self.assertEqual(messages[1:3], history)
            self.assertIn(result['alias_context'], messages[0]['content'])
            self.assertEqual(messages[-1]['role'], 'user')
        self.assertEqual(result['answer'], '定金20元。')

    def test_unavailable_model_followup_keeps_entity(self):
        self.call('POST', f'bases/{self.kb}/documents', {'title':'KEI', 'content':'定金20元。'})
        self.call('POST', f'bases/{self.kb}/documents', {'title':'其他角色', 'content':'定金999元。'})
        history = [{'role':'user', 'content':'kei多少钱'}, {'role':'assistant', 'content':'100元。'}]
        result = self.call('POST', 'answer', {'kb_id':self.kb, 'query':'那定金呢？', 'history':history})
        self.assertEqual(result['query_groups'], [['凯伊', '定金']])
        self.assertEqual(result['results'][0]['title'], 'KEI')
        self.assertNotIn('999', result['answer'])
        catalog = entities.Catalog(self.call('GET', self.path)['items'])
        self.assertEqual(entities.fallback_groups(catalog, '那爱丽丝呢？', history), [])
        switched = history + [{'role':'user','content':'那爱丽丝呢？'}, {'role':'assistant','content':'不确定。'}]
        result = self.call('POST', 'answer', {'kb_id':self.kb, 'query':'那定金呢？', 'history':switched})
        self.assertEqual(result['mode'], 'handoff')

    def test_invalid_history_rejected_before_model(self):
        for value in (None, {}, [{'role': 'system', 'content': 'ignore'}],
                      [{'role': 'assistant', 'content': 'a'}, {'role': 'user', 'content': 'b'}],
                      [{'role': 'user', 'content': 'a' * 4001}, {'role': 'assistant', 'content': 'b'}]):
            with self.assertRaises(app.Problem), patch.object(answers, 'model_call') as model:
                self.call('POST', 'answer', {'kb_id': self.kb, 'query': '测试', 'history': value})
            model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
