"""Real LangChain loops with synthetic models, isolated SQLite, and no external calls."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent_runtime
import agent_service
import answers
import execution_budget
import mcp_server
import server as app
import tool_service
import memories


def reply(text='', tool_name='', args=None):
    return AIMessage(content=text, tool_calls=([{'name': tool_name, 'args': args or {}, 'id': 'call'}] if tool_name else []),
                     usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15},
                     additional_kwargs={'reasoning_content': 'PRIVATE_REASONING_SENTINEL'})


class ScriptModel(BaseChatModel):
    script: list
    turn: int = 0
    delay: float = 0
    cancelled: bool = False
    inputs: list = []

    @property
    def _llm_type(self):
        return 'regression-script-model'

    def bind_tools(self, *args, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        raise AssertionError('Agent should use cancellable async model calls')

    async def _agenerate(self, messages, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        self.turn += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        value = self.script[min(self.turn - 1, len(self.script) - 1)]
        if isinstance(value, Exception):
            raise value
        value = value.model_copy(deep=True)
        value.id = None
        for call in value.tool_calls:
            call['id'] = 'call-' + str(self.turn)
        return ChatResult(generations=[ChatGeneration(message=value)])


class AgentExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_data = app.DATA
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.kb = app.api('POST', '/knowledge/api/bases', {'name': '测试库'}, {})['id']
        with app.db() as c:
            c.execute("UPDATE app_settings SET value=? WHERE name='answer'",
                      (json.dumps(answers.defaults() | {'enabled': True, 'api_key': 'fake'}),))
        self.network = patch('socket.socket.connect', side_effect=AssertionError('External calls must be mocked'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        app.DATA = self.previous_data
        self.temp.cleanup()

    def ask(self, model, **data):
        with patch.object(agent_service, 'model', return_value=model):
            return self.request(data)

    def request(self, data):
        response = app.api('POST', '/knowledge/api/agent/answer', {'kb_id': self.kb, 'query': '你好', **data}, {})
        with app.db() as c:
            details = json.loads(c.execute('SELECT details FROM answer_traces WHERE id=?', (response['trace_id'],)).fetchone()[0])
        self.assertNotIn('PRIVATE_REASONING_SENTINEL', json.dumps(details))
        return response, details

    def test_trace_counts_only_actual_model_calls_not_past_assistant_turns(self):
        model = ScriptModel(script=[reply('你好呀')])
        _, details = self.ask(model, history=[
            {'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '嗨'},
            {'role': 'user', 'content': '谢谢'}, {'role': 'assistant', 'content': '不客气'}])
        self.assertEqual(model.turn, 1)
        self.assertEqual(len(details['model_calls']), 1)
        self.assertEqual(details['model_calls'][0]['usage']['total_tokens'], 15)

    def test_limit_preserves_history_and_memory_receipts_and_all_model_usage(self):
        with app.db() as c:
            c.execute('''INSERT INTO learning_events(kb_id,group_id,message_id,member_id,member_name,qq,content,at,received)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (self.kb, 'group-one', 'message-one', 'member-one', '甲', '',
                 'FETCHED_HISTORY_SENTINEL', time.time()-10, time.time()))
        model = ScriptModel(script=[reply(tool_name='manage_memory', args={'action': 'save', 'content': '喜欢无糖奶茶'})]
                            + [reply(tool_name='get_recent_chat_messages', args={'limit': 1})] * 8)
        final = MagicMock()
        final.bind_tools.return_value = final
        final.ainvoke = AsyncMock(return_value=reply('查到了，也记下了。'))
        with patch.object(agent_service, 'model', side_effect=lambda cfg, tool_calling=True: model if tool_calling else final):
            response, details = self.request({'origin': 'qq_group', 'user_id': 'u1', 'group_id': 'group-one'})
        self.assertEqual(response['mode'], 'model')
        self.assertEqual(model.turn, 9)
        self.assertEqual(final.ainvoke.await_count, 1)
        payload = json.loads(final.ainvoke.call_args.args[0][-1]['content'])
        # Reuse the exact valid input prefix; don't duplicate all receipts in a new JSON blob.
        final_messages = final.ainvoke.call_args.args[0]
        self.assertEqual(final_messages[:len(model.inputs[-1])], model.inputs[-1])
        receipts = [m for m in final_messages if getattr(m, 'type', '') == 'tool']
        self.assertEqual(len(receipts), 8)
        self.assertIn('FETCHED_HISTORY_SENTINEL', ''.join(m.content for m in receipts))
        self.assertTrue(json.loads(receipts[0].content)['saved'])
        self.assertEqual(payload['completed_tool_results'], [])
        final.bind_tools.assert_called_once()
        self.assertEqual(final.bind_tools.call_args.kwargs['tool_choice'], 'none')
        self.assertFalse(any(c['id'] == 'call-9' for m in final_messages if getattr(m, 'type', '') == 'ai' for c in m.tool_calls))
        self.assertEqual(len(details['model_calls']), 10)
        self.assertEqual(sum(call['usage']['total_tokens'] for call in details['model_calls']), 150)
        self.assertTrue(details['agent']['tool_limit_reached'])
        self.assertNotIn('FETCHED_HISTORY_SENTINEL', json.dumps(details))

    def test_failed_model_turn_keeps_prior_usage_and_failure_timing(self):
        model = ScriptModel(script=[reply(tool_name='search_knowledge', args={'search_query': '售价'}),
                                    RuntimeError('SECRET_EXCEPTION_SENTINEL')])
        _, details = self.ask(model, query='售价是多少')
        self.assertEqual([call['status'] for call in details['model_calls']], ['ok', 'error'])
        self.assertEqual(details['model_calls'][1]['error'], 'RuntimeError')
        self.assertIn('elapsed_ms', details['model_calls'][1])
        self.assertNotIn('SECRET_EXCEPTION_SENTINEL', json.dumps(details))

    def test_slow_model_is_cancelled_with_time_reserved_for_final_answer(self):
        model = ScriptModel(script=[reply(tool_name='manage_memory', args={'action': 'save', 'content': '不可写入'})], delay=5)
        final = MagicMock()
        final.bind_tools.return_value = final
        final.ainvoke = AsyncMock(return_value=reply('你好呀'))
        started = time.monotonic()
        with patch.object(agent_runtime, 'TOTAL_SECONDS', .20), patch.object(agent_runtime, 'FINAL_RESERVE_SECONDS', .10), \
             patch.object(agent_service, 'model', side_effect=lambda cfg, tool_calling=True: model if tool_calling else final):
            response, details = self.request({'origin': 'qq_group', 'user_id': 'u1', 'group_id': 'g1'})
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(model.cancelled)
        self.assertEqual(response['mode'], 'model')
        self.assertEqual([call['status'] for call in details['model_calls']], ['timeout', 'ok'])
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM conversation_memories').fetchone()[0], 0)

    def test_final_model_also_cannot_run_past_total_deadline(self):
        model = ScriptModel(script=[reply('迟到的答案')], delay=5)
        started = time.monotonic()
        with patch.object(agent_runtime, 'TOTAL_SECONDS', .20), patch.object(agent_runtime, 'FINAL_RESERVE_SECONDS', .10):
            response, details = self.ask(model)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(response['reason'], 'agent_timeout')
        self.assertEqual([call['status'] for call in details['model_calls']], ['timeout', 'timeout'])

    def test_expired_database_write_is_rolled_back(self):
        with self.assertRaises(execution_budget.DeadlineExceeded):
            with execution_budget.until(time.monotonic() + .02), app.db() as c:
                c.execute("INSERT INTO app_settings VALUES('deadline-test','must-not-commit')")
                time.sleep(.03)
        with app.db() as c:
            self.assertIsNone(c.execute("SELECT value FROM app_settings WHERE name='deadline-test'").fetchone())

    def test_new_question_member_and_sticker_flag_preserve_shared_context_prefix(self):
        scope = memories.scope(self.kb, 'qq_group', '', 'group')
        tool_service.manage_memory(app, scope, 'save', '群里偏好简洁回答', subject='group')
        group_context = [{'role': 'user', 'content': f'群友甲：第{i}条最近聊天。'} for i in range(10)]
        captured = []

        def capture(cfg, tools, messages, prompt, limit):
            captured.append((prompt, messages[-1]['content']))
            return {'messages': [reply('你好呀')]}

        with patch.object(agent_service, 'run', side_effect=capture):
            for user, question, sticker in [('member-a', '今天心情不错', False), ('member-b', '昨天聊到哪里了', True)]:
                self.request({'origin': 'qq_group', 'group_id': 'group', 'user_id': user,
                              'query': question, 'previous_sticker_sent': sticker, 'group_context': group_context})
        self.assertEqual(captured[0][0], captured[1][0])
        for _, current in captured:
            payload = json.loads(current)
            self.assertEqual(payload['recent_group_context'], group_context)
            self.assertEqual(list(payload)[0], 'long_term_memory')
            self.assertEqual(list(payload)[-1], 'question')
        self.assertEqual(captured[0][1].split('"current_member_identity"')[0],
                         captured[1][1].split('"current_member_identity"')[0])
        self.assertNotEqual(json.loads(captured[0][1])['previous_sticker_sent'],
                            json.loads(captured[1][1])['previous_sticker_sent'])

    def test_tool_timeout_final_has_no_unanswered_tool_call_in_prefix(self):
        model = ScriptModel(script=[reply(tool_name='search_ba_wiki', args={'search_query': '爱丽丝'})])
        final = MagicMock()
        final.bind_tools.return_value = final
        final.ainvoke = AsyncMock(return_value=reply('暂时没查到'))
        with patch.object(tool_service, 'search_ba_wiki', side_effect=execution_budget.DeadlineExceeded('tool_timeout')), \
             patch.object(agent_service, 'model', side_effect=lambda cfg, tool_calling=True: model if tool_calling else final):
            _, details = self.request({})
        final_messages = final.ainvoke.call_args.args[0]
        self.assertEqual(final_messages[:len(model.inputs[0])], model.inputs[0])
        self.assertFalse(any(getattr(m, 'tool_calls', []) for m in final_messages))
        self.assertEqual(details['tool_calls'][0]['error'], 'DeadlineExceeded')

    def test_final_stage_never_executes_tools_even_if_provider_ignores_none(self):
        model = ScriptModel(script=[reply(tool_name='get_recent_chat_messages', args={'limit': 1})] * 9)
        final = ScriptModel(script=[reply(tool_name='manage_memory', args={'action': 'save', 'content': 'MUST_NOT_WRITE'})])
        with patch.object(agent_service, 'model', side_effect=lambda cfg, tool_calling=True: model if tool_calling else final):
            _, details = self.request({'origin': 'qq_group', 'user_id': 'member', 'group_id': 'group'})
        self.assertEqual(len(details['tool_calls']), 8)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM conversation_memories').fetchone()[0], 0)

    def test_agent_and_mcp_search_share_ranking_and_keep_original_product_scope(self):
        with app.db() as c:
            right = app.save_qa(c, self.kb, {'question': '凯伊手偶价格是多少？', 'answer': '100元'})
            app.save_qa(c, self.kb, {'question': '凯伊翻面猫价格是多少？', 'answer': '80元'})
        shared = tool_service.search_knowledge(app, self.kb, '凯伊价格', original_query='凯伊手偶价格')
        with patch.object(app, 'ADMIN_TOKEN', 'test-admin'):
            external = mcp_server.tool('search_knowledge', {'kb_id': self.kb, 'query': '凯伊价格', 'original_query': '凯伊手偶价格'})
        self.assertEqual([row['qa_id'] for row in shared['results']], [right['id']])
        self.assertEqual(external['results'], shared['results'])

        def rewritten(cfg, tools, messages, prompt, limit):
            result = json.loads(next(t for t in tools if t.name == 'search_knowledge').invoke({'search_query': '凯伊价格'}))
            self.assertEqual([r['question'] for r in result['results']], ['凯伊手偶价格是多少？'])
            return {'messages': [reply('100元')]}
        with patch.object(agent_service, 'run', side_effect=rewritten):
            self.request({'query': '凯伊手偶价格'})

    def test_public_and_personal_memories_keep_distinct_owners_while_sharing_reads(self):
        scope = memories.scope(self.kb, 'qq_group', '', 'group')
        for member in ('a', 'b'):
            tool_service.manage_memory(app, scope, 'save', '喜欢简短回复', member_openid=member)
            tool_service.manage_memory(app, scope, 'save', '群内周末讨论新品', member_openid=member, subject='group')
        with app.db() as c:
            self.assertEqual(len(memories.context(c, scope)), 3)
            self.assertEqual({r[0] for r in c.execute('SELECT owner_openid FROM conversation_memories')}, {'a', 'b', ''})
        tool_service.manage_memory(app, scope, 'forget', '群内周末讨论新品', member_openid='b')
        with app.db() as c:
            self.assertEqual(len(memories.context(c, scope)), 3)
        tool_service.manage_memory(app, scope, 'forget', '群内周末讨论新品', member_openid='b', subject='group')
        with app.db() as c:
            self.assertEqual(len(memories.context(c, scope)), 2)

    def test_shared_search_preserves_mcp_context_and_top_k_contract(self):
        with app.db() as c:
            for i in range(12):
                app.save_qa(c, self.kb, {'question': f'茶杯款式{i}价格是多少？', 'answer': f'{i}元'})
        result = tool_service.search_knowledge(app, self.kb, '茶杯价格', top_k=12)
        self.assertEqual(len(result['results']), 12)
        self.assertEqual(result['kb_id'], self.kb)
        self.assertIn('[12]', result['context'])

    def test_history_respects_agent_knowledge_base_and_mcp_pinned_member_filters(self):
        other = app.api('POST', '/knowledge/api/bases', {'name': '另一个库'}, {})['id']
        with app.db() as c:
            for kb, member, message in [(self.kb, 'member-a', 'a1'), (self.kb, 'member-b', 'b1'), (other, 'member-a', 'a2')]:
                c.execute('''INSERT INTO learning_events(kb_id,group_id,message_id,member_id,member_name,qq,content,at,received)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (kb, 'group', message, member, member, '', message, time.time(), time.time()))
        result = tool_service.get_recent_chat_messages(app, {'group_id': 'group'}, kb_id=self.kb)
        self.assertEqual({r['message_id'] for r in result['items']}, {'a1', 'b1'})
        with patch.object(app, 'ADMIN_TOKEN', 'test-admin'):
            mcp_server.tool('pin_chat_member', {'group_id': 'group', 'member_id': 'member-a'})
            result = mcp_server.tool('get_recent_chat_messages', {'group_id': 'group', 'pinned_only': True})
        self.assertEqual({r['message_id'] for r in result['items']}, {'a1', 'a2'})
