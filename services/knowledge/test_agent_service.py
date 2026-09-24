import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import agent_service
import answers
import maintenance
import server as app


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.kb = app.api('POST', '/knowledge/api/bases', {'name': '店铺'}, {})['id']
        with app.db() as c:
            c.execute("UPDATE app_settings SET value=? WHERE name='answer'",
                      (json.dumps(answers.defaults() | {'api_key': 'fake'}),))

    def tearDown(self):
        self.temp.cleanup()

    def test_answer_can_search_twice_and_cannot_use_other_base(self):
        app.api('POST', f'/knowledge/api/bases/{self.kb}/documents',
                {'title': '凯伊毛绒', 'content': '凯伊毛绒售价100元。'}, {})
        app.api('POST', f'/knowledge/api/bases/{self.kb}/documents',
                {'title': '凯伊预售', 'content': '凯伊预售定金20元。'}, {})
        other = app.api('POST', '/knowledge/api/bases', {'name': '其他'}, {})['id']
        app.api('POST', f'/knowledge/api/bases/{other}/documents',
                {'title': '秘密', 'content': '跨库秘密价格999元。'}, {})

        def fake_run(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 4)
            self.assertIn('100元', tools[0].invoke({'search_query': '凯伊毛绒'}))
            self.assertIn('20元', tools[0].invoke({'search_query': '凯伊预售'}))
            self.assertNotIn('跨库秘密', tools[0].invoke({'search_query': '秘密'}))
            return {'messages': [AIMessage(content='售价100元，预售定金20元。')]}

        with patch.object(agent_service, 'run', side_effect=fake_run):
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '凯伊毛绒售价和定金是多少'}, {})
        self.assertEqual(response['mode'], 'model')
        self.assertEqual(len(response['search_terms']), 3)
        self.assertNotIn('跨库秘密', json.dumps(response, ensure_ascii=False))

    def test_answer_without_evidence_handoffs(self):
        with patch.object(agent_service, 'run', return_value={'messages': [AIMessage(content='我猜是100元')]}):
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '售价多少'}, {})
        self.assertEqual(response['mode'], 'handoff')
        self.assertNotIn('100元', response['answer'])

    def test_maintenance_read_before_write_and_idempotency(self):
        with app.db() as c:
            maintenance.settings(c, {'openids': ['authorized-user']})
            qa = app.save_qa(c, self.kb, {'question': '怎么下单', 'answer': '旧答案'})
        data = {'kb_id': self.kb, 'user_id': 'authorized-user', 'message_id': 'first',
                'query': '/modify qa 改成淘宝下单'}

        def fake_run(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 6)
            by_name = {t.name: t for t in tools}
            by_name['read_record'].invoke({'id': str(qa['id'])})
            by_name['update_record'].invoke({'id': str(qa['id']), 'question': '怎么下单', 'answer': '淘宝下单'})

        with patch.object(agent_service, 'run', side_effect=fake_run) as runner:
            first = app.api('POST', '/knowledge/api/agent/private-maintenance', data, {})
            second = app.api('POST', '/knowledge/api/agent/private-maintenance', data, {})
        self.assertEqual(first, second)
        self.assertEqual(first['reason'], 'saved')
        runner.assert_called_once()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT answer FROM qa_entries WHERE id=?', (qa['id'],)).fetchone()[0], '淘宝下单')

    def test_maintenance_denied_before_agent(self):
        with patch.object(agent_service, 'run') as runner:
            response = app.api('POST', '/knowledge/api/agent/private-maintenance',
                               {'kb_id': self.kb, 'user_id': 'unknown', 'message_id': 'one',
                                'query': '/modify qa test'}, {})
        self.assertFalse(response['active'])
        runner.assert_not_called()

    def test_reasoning_is_replayed_to_deepseek_but_not_traced(self):
        chat = agent_service.model({'model': 'deepseek-flash', 'api_key': 'fake'})
        ai = AIMessage(content='', tool_calls=[{'name': 'search_knowledge',
                       'args': {'search_query': '凯伊'}, 'id': 'call-one'}],
                       additional_kwargs={'reasoning_content': 'private reasoning'})
        payload = chat._get_request_payload([HumanMessage(content='query'), ai,
                    ToolMessage(content='result', tool_call_id='call-one')])
        self.assertEqual(payload['messages'][1]['reasoning_content'], 'private reasoning')
        details = {'model_calls': []}
        agent_service.record_model_calls(details, {'messages': [ai]})
        self.assertNotIn('private reasoning', json.dumps(details))

    def test_langchain_enforces_total_tool_limit(self):
        calls = []

        class LoopModel(BaseChatModel):
            turn: int = 0

            @property
            def _llm_type(self):
                return 'loop-model'

            def bind_tools(self, *args, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                self.turn += 1
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='',
                    tool_calls=[{'name': 'lookup', 'args': {'query': '凯伊'}, 'id': str(self.turn)}]))])

        @tool
        def lookup(query: str) -> str:
            """Search the knowledge base."""
            calls.append(query)
            return 'found'

        with patch.object(agent_service, 'model', return_value=LoopModel()):
            with self.assertRaises(agent_service.AgentLimitError):
                agent_service.run({'api_key': 'fake'}, [lookup],
                                  [{'role': 'user', 'content': '问答'}], 'Use tools', 4)
        self.assertEqual(len(calls), 4)


if __name__ == '__main__':
    unittest.main()
