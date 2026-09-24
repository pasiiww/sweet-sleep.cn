import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError

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
            self.assertEqual(limit, 6)
            self.assertIn(answers.PERSONA_PROMPT, prompt)
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
        with patch.object(agent_service, 'run', return_value={'messages': [AIMessage(content='我猜是100元')]}) as runner:
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '售价多少'}, {})
        self.assertIn(answers.PERSONA_PROMPT, runner.call_args.args[3])
        self.assertEqual(response['mode'], 'handoff')
        self.assertNotIn('100元', response['answer'])

    def test_ba_wiki_tool_adds_cited_evidence_for_the_answer(self):
        wiki_result = {'query': '日奈', 'source': 'GameKee', 'source_errors': [], 'results': [
            {'title': '日奈', 'content': '日奈是格黑娜学园风纪委员会委员长。', 'source': 'GameKee',
             'source_type': 'wiki', 'url': 'https://www.gamekee.com/ba/tj/59934.html', 'updated_at': 1}]}

        def fake_run(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 6)
            by_name = {item.name: item for item in tools}
            self.assertIn('search_ba_wiki', by_name)
            self.assertIn('GameKee', by_name['search_ba_wiki'].invoke({'search_query': '日奈'}) )
            return {'messages': [AIMessage(content='日奈是格黑娜风纪委员会委员长。')]}

        with patch.object(agent_service.ba_wiki, 'search', return_value=wiki_result), \
             patch.object(agent_service, 'run', side_effect=fake_run):
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '日奈是什么人'}, {})
        self.assertEqual(response['mode'], 'model')
        self.assertEqual(response['results'][0]['source_type'], 'wiki')
        self.assertEqual(response['results'][0]['url'], 'https://www.gamekee.com/ba/tj/59934.html')

    def test_group_context_chat_lookup_and_memory_are_group_shared(self):
        now = __import__('time').time()
        with app.db() as c:
            for index in range(3):
                c.execute('''INSERT INTO learning_events(kb_id,group_id,message_id,member_id,member_name,qq,content,at,received)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (self.kb, 'group-one', 'msg-'+str(index), 'member-'+str(index),
                     '历史昵称' if index == 1 else '', '', '群历史消息'+str(index), now-index*60, now))
        history_context = [{'role':'user','content':f'[群友{n}] 最近消息{n}'} for n in range(10)]
        first_data = {'kb_id':self.kb,'query':'请记住群里偏好简洁回答','origin':'qq_group',
                      'group_id':'group-one','user_id':'member-one','session_id':'opaque-session',
                      'group_context':history_context}
        observed = {}

        def save_memory(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 6)
            by_name = {item.name:item for item in tools}
            self.assertIn('get_recent_chat_messages', by_name)
            self.assertIn('manage_memory', by_name)
            payload = json.loads(messages[-1]['content'])
            self.assertEqual(payload['recent_group_context'], history_context)
            older = json.loads(by_name['get_recent_chat_messages'].invoke({'limit':1,'offset':1,'hours':24}))
            observed['older'] = older['group_messages']
            saved = json.loads(by_name['manage_memory'].invoke({'action':'save','content':'群里偏好简洁回答'}))
            observed['saved'] = saved
            return {'messages':[AIMessage(content='记下了。')]}

        with patch.object(agent_service, 'run', side_effect=save_memory):
            app.api('POST','/knowledge/api/agent/answer',first_data,{})
        self.assertEqual(observed['older'][0]['content'], '群历史消息1')
        self.assertEqual(observed['older'][0]['speaker'], '历史昵称')
        self.assertTrue(observed['saved']['saved'])

        second_data = first_data | {'query':'你好','user_id':'member-two','group_context':[]}
        def inspect_memory(cfg, tools, messages, prompt, limit):
            payload = json.loads(messages[-1]['content'])
            self.assertEqual(payload['long_term_memory'], ['群里偏好简洁回答'])
            self.assertNotIn('群里偏好简洁回答', prompt)
            return {'messages':[AIMessage(content='你好呀。')]}
        with patch.object(agent_service, 'run', side_effect=inspect_memory):
            response = app.api('POST','/knowledge/api/agent/answer',second_data,{})
        self.assertEqual(response['mode'], 'model')

        other_group = second_data | {'group_id':'group-two'}
        def inspect_no_memory(cfg, tools, messages, prompt, limit):
            self.assertEqual(json.loads(messages[-1]['content'])['long_term_memory'], [])
            return {'messages':[AIMessage(content='你好呀。')]}
        with patch.object(agent_service, 'run', side_effect=inspect_no_memory):
            app.api('POST','/knowledge/api/agent/answer',other_group,{})

    def test_memory_manage_api_is_scoped_and_user_controllable(self):
        def call(action, user='u1', group='g1'):
            return app.api('POST','/knowledge/api/agent/memory',{
                'kb_id':self.kb,'origin':'qq_group','user_id':user,'group_id':group,'action':action},{})
        first = app.api('POST','/knowledge/api/agent/memory',{
            'kb_id':self.kb,'origin':'qq_group','user_id':'u1','group_id':'g1',
            'action':'save','content':'群里使用中文回答'}, {})
        self.assertTrue(first['saved'])
        self.assertEqual(call('list','u2','g1')['items'], ['群里使用中文回答'])
        self.assertEqual(call('list','u1','g2')['items'], [])
        self.assertFalse(call('disable','u2','g1')['enabled'])
        self.assertEqual(call('list','u1','g1')['items'], ['群里使用中文回答'])
        self.assertEqual(call('clear','u2','g1')['deleted'], 1)
        self.assertEqual(call('list','u1','g1')['items'], [])

    def test_seventh_search_is_blocked_then_model_answers_without_tools(self):
        app.api('POST', f'/knowledge/api/bases/{self.kb}/documents',
                {'title': '凯伊毛绒', 'content': '凯伊毛绒售价100元。'}, {})

        def exhaust(cfg, tools, messages, prompt, limit):
            for term in ('凯伊毛绒', '凯伊售价', '毛绒价格', '凯伊价格', '凯伊商品', '凯伊价格查询'):
                tools[0].invoke({'search_query': term})
            raise agent_service.AgentLimitError from ToolCallLimitExceededError(
                thread_count=7, run_count=7, thread_limit=None, run_limit=6)

        direct_model = MagicMock()
        direct_model.invoke.return_value = AIMessage(content='凯伊毛绒售价100元。')
        with patch.object(agent_service, 'run', side_effect=exhaust), \
             patch.object(agent_service, 'model', return_value=direct_model) as factory:
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '凯伊毛绒多少钱'}, {})
        self.assertEqual(response['mode'], 'model')
        self.assertEqual(len(response['search_terms']), 6)
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs['tool_calling'])
        final_messages = direct_model.invoke.call_args.args[0]
        self.assertIn('售价100元', final_messages[-1]['content'])
        self.assertIn('不得请求继续搜索', final_messages[0]['content'])
        with app.db() as c:
            details = json.loads(c.execute('SELECT details FROM answer_traces WHERE id=?',
                                   (response['trace_id'],)).fetchone()[0])
        self.assertTrue(details['agent']['tool_limit_reached'])

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
        final_chat = agent_service.model({'model': 'deepseek-flash', 'api_key': 'fake'}, tool_calling=False)
        self.assertNotIn('parallel_tool_calls', final_chat._get_request_payload('final answer'))

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
                                  [{'role': 'user', 'content': '问答'}], 'Use tools', 6)
        self.assertEqual(len(calls), 6)


if __name__ == '__main__':
    unittest.main()
