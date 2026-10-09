import base64
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError

import agent_service
import answers
import memories
import maintenance
import mcp_server
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

    def test_member_impression_parser_accepts_reordered_attributes_and_hides_invalid_tags(self):
        updates, visible = agent_service.extract_member_impressions(
            '回复内容。<member_impression action="append" target="quoted2">喜欢日奈</member_impression>'
            '<member_impression extra="ignored">不应显示</member_impression>')
        self.assertEqual(updates, [('quoted2', 'append', '喜欢日奈')])
        self.assertEqual(visible, '回复内容。')

    def test_member_impression_is_hidden_saved_and_only_injected_for_owner(self):
        request = {'kb_id': self.kb, 'origin': 'qq_group', 'group_id': 'g', 'user_id': 'a',
                   'query': '我喜欢日奈', 'current_member_text': '我喜欢日奈'}
        with patch.object(agent_service, 'run', return_value={'messages': [AIMessage(
                content='日奈很可爱呀。\n<member_impression>喜欢日奈</member_impression>')]}):
            result = app.api('POST', '/knowledge/api/agent/answer', request, {})
        self.assertEqual(result['answer'], '日奈很可爱呀。')
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with app.db() as c:
            self.assertIn('日奈', memories.impression(c, scope, 'a'))
        for owner, expected in [('a', '群友印象：喜欢日奈'), ('b', '')]:
            with patch.object(agent_service, 'run', return_value={'messages': [AIMessage(content='你好呀') ]}) as runner:
                app.api('POST', '/knowledge/api/agent/answer', request | {'user_id': owner, 'query': '你好'}, {})
            context = json.loads(runner.call_args.args[2][-1]['content'])
            self.assertEqual(context['current_member_impression'], expected)
            self.assertNotIn('群友印象', str(context['long_term_memory']))
        with app.db() as c:
            memories.apply(c, scope, 'disable')
        with patch.object(agent_service, 'run', return_value={'messages': [AIMessage(
                content='你好\n<member_impression>不应保存</member_impression>')]}) as runner:
            result = app.api('POST', '/knowledge/api/agent/answer', request, {})
        self.assertNotIn('member_impression', result['answer'])
        self.assertEqual(json.loads(runner.call_args.args[2][-1]['content'])['current_member_impression'], '')

    def test_mentioned_member_impression_is_injected_for_group_replies(self):
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with app.db() as c:
            memories.update_impression(c, scope, 'target-openid', '喜欢日奈')
        request = {'kb_id': self.kb, 'origin': 'qq_group', 'group_id': 'g',
                   'user_id': 'speaker-openid', 'query': '小明觉得这个怎么样',
                   'member_name': '落落',
                   'mentioned_members': [{'openid': 'target-openid', 'name': '小明'}]}
        with patch.object(agent_service, 'run', return_value={'messages': [AIMessage(content='我记得。')]}) as runner:
            result = app.api('POST', '/knowledge/api/agent/answer', request, {})
        context = json.loads(runner.call_args.args[2][-1]['content'])
        self.assertEqual(context['mentioned_member_impressions'], [
            {'key': 'mentioned1', 'name': '小明', 'impression': '群友印象：喜欢日奈'}])
        self.assertNotIn('target-openid', json.dumps(context, ensure_ascii=False))
        trace = app.api('GET', '/knowledge/api/traces/' + result['trace_id'], {}, {})
        trace_context = trace['details']['conversation_context']
        self.assertEqual(trace_context['message_text'], '小明觉得这个怎么样')
        self.assertEqual(trace_context['speaker']['name'], '落落')
        self.assertEqual(trace_context['mentioned_members'], [
            {'name': '小明', 'impression': '群友印象：喜欢日奈', 'impression_injected': True}])
        trace_list = app.api('GET', '/knowledge/api/traces', {}, {})
        self.assertEqual(trace_list['items'][0]['mentioned_names'], ['小明'])
        self.assertNotIn('target-openid', json.dumps(trace, ensure_ascii=False))

    def test_private_answer_injects_own_group_profile_and_searches_public_memory_on_demand(self):
        group_scope = memories.scope(self.kb, 'qq_group', '', 'group-one')
        with app.db() as c:
            memories.register_scope(c, self.kb, 'qq_group', '', 'group-one')
            memories.remember_member(c, group_scope, 'private-user', '小明')
            memories.apply(c, group_scope, 'save', '群内约定：周末看电影', member_openid='')
            memories.apply(c, group_scope, 'save', '本人喜欢无糖奶茶', member_openid='private-user')
            memories.apply(c, group_scope, 'save', '另一位群友喜欢辣味', member_openid='other-user')
            memories.update_impression(c, group_scope, 'private-user', '喜欢日奈')
            memories.update_impression(c, group_scope, 'other-user', '喜欢星野')
        observed = {}
        def inspect(cfg, tools, messages, prompt, limit):
            observed['tools'] = {item.name: item for item in tools}
            return {'messages': [AIMessage(content='记得。')]}
        with patch.object(agent_service, 'run', side_effect=inspect) as runner:
            app.api('POST', '/knowledge/api/agent/answer', {
                'kb_id': self.kb, 'origin': 'qq_private', 'user_id': 'private-user',
                'query': '我喜欢什么？'}, {})
        context = json.loads(runner.call_args.args[2][-1]['content'])
        self.assertNotIn('群内约定：周末看电影', context['long_term_memory'])
        self.assertTrue(any(item.endswith('本人喜欢无糖奶茶') for item in context['long_term_memory']))
        self.assertTrue(any(item.endswith('群友印象：喜欢日奈') for item in context['long_term_memory']))
        self.assertEqual(context['current_member_identity']['first_nickname'], '小明')
        self.assertNotIn('另一位群友喜欢辣味', context['long_term_memory'])
        self.assertNotIn('喜欢星野', context['long_term_memory'])
        self.assertIn('get_group_memories', observed['tools'])
        result = json.loads(observed['tools']['get_group_memories'].invoke(
            {'search_query': '群内约定 周末看电影'}))
        self.assertEqual(result['memories'], ['【群聊公共记忆】群内约定：周末看电影'])

    def test_answer_can_search_twice_and_cannot_use_other_base(self):
        app.api('POST', f'/knowledge/api/bases/{self.kb}/documents',
                {'title': '凯伊毛绒', 'content': '凯伊毛绒售价100元。'}, {})
        app.api('POST', f'/knowledge/api/bases/{self.kb}/documents',
                {'title': '凯伊预售', 'content': '凯伊预售定金20元。'}, {})
        other = app.api('POST', '/knowledge/api/bases', {'name': '其他'}, {})['id']
        app.api('POST', f'/knowledge/api/bases/{other}/documents',
                {'title': '秘密', 'content': '跨库秘密价格999元。'}, {})

        def fake_run(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 8)
            self.assertIn(answers.PERSONA_PROMPT, prompt)
            first = tools[0].invoke({'search_query': '凯伊毛绒'})
            self.assertIn('100元', first)
            # A search pass must not repeat records already surfaced by the first pass.
            second = tools[0].invoke({'search_query': '凯伊预售'})
            self.assertNotIn('跨库秘密', second)
            self.assertNotIn('跨库秘密', tools[0].invoke({'search_query': '秘密'}))
            return {'messages': [AIMessage(content='售价100元，预售定金20元。')]}

        with patch.object(agent_service, 'run', side_effect=fake_run):
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '凯伊毛绒售价和定金是多少'}, {})
        with app.db() as c:
            trace = json.loads(c.execute('SELECT details FROM answer_traces WHERE id=?',
                                          (response['trace_id'],)).fetchone()[0])
        self.assertEqual(response['mode'], 'model', (response, trace.get('agent')))
        self.assertEqual(len(response['search_terms']), 3)
        self.assertNotIn('跨库秘密', json.dumps(response, ensure_ascii=False))

    def test_answer_without_evidence_handoffs(self):
        def empty_search(cfg, tools, messages, prompt, limit):
            self.assertIn('只有明确的店铺或BA事实问题经过检索', prompt)
            self.assertEqual(json.loads(tools[0].invoke({'search_query': '售价多少'})), {'results': []})
            return {'messages': [AIMessage(content='没有找到可靠资料。 [[HANDOFF]]')]}

        with patch.object(agent_service, 'run', side_effect=empty_search) as runner:
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '售价多少'}, {})
        self.assertIn(answers.PERSONA_PROMPT, runner.call_args.args[3])
        self.assertEqual(response['mode'], 'handoff')
        self.assertNotIn('100元', response['answer'])

    def test_smalltalk_without_knowledge_search_gets_normal_chat_response(self):
        answer = '辛苦啦～先休息一会儿，想聊什么我都在听♡'

        def casual_chat(cfg, tools, messages, prompt, limit):
            self.assertIn('问候、感谢、闲聊、分享感受或一般交流时正常接话', prompt)
            self.assertIn('不要主动介绍身份', prompt)
            self.assertIn('不要在结尾附加', prompt)
            return {'messages': [AIMessage(content=answer)]}

        with patch.object(agent_service, 'run', side_effect=casual_chat):
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '今天有点累，想歇会儿'}, {})
        self.assertEqual(response['mode'], 'model')
        self.assertEqual(response['answer'], answer)
        self.assertEqual(response['search_terms'], [])
        self.assertFalse(response['handoff'])

    def test_group_vision_input_reaches_model_but_is_not_saved_to_trace(self):
        data_url = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nsmall').decode()
        captured = {}

        def see_image(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 8)
            self.assertIsInstance(messages[-1]['content'], list)
            self.assertEqual(messages[-1]['content'][0]['type'], 'text')
            self.assertEqual(messages[-1]['content'][1], {
                'type': 'image_url', 'image_url': {'url': data_url, 'detail': 'auto'}})
            captured['content'] = messages[-1]['content']
            tools[0].invoke({'search_query': '图片识别'})
            return {'messages': [AIMessage(content='图里有一只猫。')]}

        with patch.object(app, 'search_terms', return_value={'results': [{
                'chunk_id': 'image-evidence', 'title': '图片说明', 'content': '图片问答资料',
                'question': '', 'updated_at': '', 'source_type': 'document',
                'document_id': 'image-document', 'citation': 1}]}), \
             patch.object(agent_service, 'run', side_effect=see_image):
            response = app.api('POST', '/knowledge/api/agent/answer', {
                'kb_id': self.kb, 'query': '图里有什么', 'origin': 'qq_group',
                'group_id': 'group-one', 'user_id': 'member-one',
                'image_data_urls': [data_url]}, {})

        self.assertEqual(response['mode'], 'model')
        with app.db() as c:
            details = json.loads(c.execute('SELECT details FROM answer_traces WHERE id=?',
                                           (response['trace_id'],)).fetchone()[0])
        self.assertEqual(details['vision_image_count'], 1)
        self.assertNotIn(data_url, json.dumps(details, ensure_ascii=False))

    def test_vision_image_validation_rejects_bad_and_multiple_images(self):
        data_url = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nsmall').decode()
        self.assertEqual(agent_service.validate_vision_images([data_url]), [data_url])
        for value in (['bad'], [data_url, data_url], ['data:image/png;base64,not-base64']):
            with self.assertRaises(ValueError):
                agent_service.validate_vision_images(value)

    def test_ba_wiki_tool_adds_cited_evidence_for_the_answer(self):
        wiki_result = {'query': '日奈', 'source': 'GameKee', 'source_errors': [], 'results': [
            {'title': '日奈', 'content': '日奈是格黑娜学园风纪委员会委员长。', 'source': 'GameKee',
             'source_type': 'wiki', 'url': 'https://www.gamekee.com/ba/tj/59934.html', 'updated_at': 1}]}

        def fake_run(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 8)
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
                      'group_context':history_context,'member_name':'成员一初始昵称'}
        observed = {}

        def save_memory(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 8)
            by_name = {item.name:item for item in tools}
            self.assertIn('get_recent_chat_messages', by_name)
            self.assertIn('manage_memory', by_name)
            self.assertIn('get_group_memories', by_name)
            payload = json.loads(messages[-1]['content'])
            self.assertEqual(payload['recent_group_context'], history_context)
            self.assertNotIn('群里约定', str(payload['long_term_memory']))
            older = json.loads(by_name['get_recent_chat_messages'].invoke({'limit':1,'offset':1,'hours':24}))
            observed['older'] = older['group_messages']
            saved = json.loads(by_name['manage_memory'].invoke({'action':'save','content':'群里偏好简洁回答'}))
            observed['saved'] = saved
            public_saved = json.loads(by_name['manage_memory'].invoke({
                'action':'save','content':'群里约定周末一起看电影','subject':'group'}))
            observed['public_saved'] = public_saved
            return {'messages':[AIMessage(content='记下了。')]}

        with patch.object(agent_service, 'run', side_effect=save_memory):
            app.api('POST','/knowledge/api/agent/answer',first_data,{})
        self.assertEqual(observed['older'][0]['content'], '群历史消息1')
        self.assertEqual(observed['older'][0]['speaker'], '历史昵称')
        self.assertTrue(observed['saved']['saved'])
        self.assertTrue(observed['public_saved']['saved'])

        second_data = first_data | {'query':'你好','user_id':'member-two','group_context':[],'member_name':'成员二初始昵称'}
        def inspect_memory(cfg, tools, messages, prompt, limit):
            payload = json.loads(messages[-1]['content'])
            self.assertEqual(len(payload['long_term_memory']), 1)
            self.assertIn('首次记录昵称', payload['long_term_memory'][0])
            self.assertTrue(payload['long_term_memory'][0].endswith('群里偏好简洁回答'))
            self.assertNotIn('周末一起看电影', str(payload['long_term_memory']))
            self.assertIn('get_group_memories', {item.name for item in tools})
            public = json.loads(next(item for item in tools if item.name == 'get_group_memories').invoke(
                {'search_query':'周末一起看电影'}))
            self.assertEqual(public['memories'], ['【群聊公共记忆】群里约定周末一起看电影'])
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

    def test_first_nickname_is_used_for_member_memory_and_identity_recall(self):
        memory_scope = memories.scope(self.kb, 'qq_group', 'member-one', 'group-one')
        with app.db() as c:
            memories.remember_member(c, memory_scope, 'member-one', '落落最初昵称')
            memories.apply(c, memory_scope, 'save', '本人自称“落落”。', member_openid='member-one')
        data = {'kb_id':self.kb,'query':'落落是谁','origin':'qq_group','group_id':'group-one',
                'user_id':'member-one','member_name':'修改后的群昵称'}

        def recall(cfg, tools, messages, prompt, limit):
            payload = json.loads(messages[-1]['content'])
            self.assertEqual(payload['current_member_identity']['first_nickname'], '落落最初昵称')
            self.assertIn('落落最初昵称', payload['long_term_memory'][0])
            self.assertNotIn('member-one', payload['long_term_memory'][0])
            self.assertIn('不得把“当前这位群友”', prompt)
            return {'messages':[AIMessage(content='落落是最初昵称为“落落最初昵称”的群友自称。')]}

        with patch.object(agent_service, 'run', side_effect=recall):
            response = app.api('POST', '/knowledge/api/agent/answer', data, {})
        self.assertEqual(response['mode'], 'model')
        self.assertIn('落落最初昵称', response['answer'])

    def test_memory_manage_api_is_scoped_and_user_controllable(self):
        def call(action, user='u1', group='g1'):
            return app.api('POST','/knowledge/api/agent/memory',{
                'kb_id':self.kb,'origin':'qq_group','user_id':user,'group_id':group,'action':action},{})
        first = app.api('POST','/knowledge/api/agent/memory',{
            'kb_id':self.kb,'origin':'qq_group','user_id':'u1','group_id':'g1',
            'action':'save','content':'群里使用中文回答'}, {})
        self.assertTrue(first['saved'])
        self.assertTrue(call('list','u2','g1')['items'][0].endswith('群里使用中文回答'))
        self.assertEqual(call('list','u1','g2')['items'], [])
        self.assertFalse(call('disable','u2','g1')['enabled'])
        self.assertTrue(call('list','u1','g1')['items'][0].endswith('群里使用中文回答'))
        self.assertEqual(call('clear','u2','g1')['deleted'], 1)
        self.assertEqual(call('list','u1','g1')['items'], [])

    def test_admin_group_memory_view_manages_member_items_inside_group(self):
        save = lambda user, content, name='': app.api('POST', '/knowledge/api/agent/memory', {
            'kb_id': self.kb, 'origin': 'qq_group', 'user_id': user, 'group_id': 'g-admin',
            'member_name': name, 'action': 'save', 'content': content}, {})
        save('member-one', '喜欢简洁回答', '小风')
        save('member-two', '常问凯伊周边', '猫猫')
        save('', '群里统一使用中文', '')

        groups = app.api('GET', '/knowledge/api/group-memories', {}, {'kb_id': [self.kb]})
        group = next(item for item in groups['groups'] if item['group_id'] == 'g-admin')
        self.assertEqual(group['memory_count'], 3)
        listing = app.api('GET', '/knowledge/api/group-memories', {},
                          {'kb_id': [self.kb], 'group_id': ['g-admin']})
        self.assertEqual(len(listing['items']), 3)
        member = next(item for item in listing['items'] if item['first_nickname'] == '小风')
        self.assertNotIn('member-one', member)
        self.assertTrue(member['member_key'].startswith('成员-'))

        deleted = app.api('DELETE', '/knowledge/api/group-memories', {
            'kb_id': self.kb, 'group_id': 'g-admin', 'item_id': member['id']}, {})
        self.assertEqual(deleted['deleted'], 1)
        self.assertEqual(len(deleted['items']), 2)
        disabled = app.api('PUT', '/knowledge/api/group-memories', {
            'kb_id': self.kb, 'group_id': 'g-admin', 'enabled': False}, {})
        self.assertFalse(disabled['enabled'])
        cleared = app.api('DELETE', '/knowledge/api/group-memories', {
            'kb_id': self.kb, 'group_id': 'g-admin'}, {})
        self.assertEqual(cleared['deleted'], 2)
        self.assertEqual(cleared['items'], [])

    def test_ninth_search_is_blocked_then_model_answers_without_tools(self):
        app.api('POST', f'/knowledge/api/bases/{self.kb}/documents',
                {'title': '凯伊毛绒', 'content': '凯伊毛绒售价100元。'}, {})

        def exhaust(cfg, tools, messages, prompt, limit):
            for term in ('凯伊毛绒', '凯伊售价', '毛绒价格', '凯伊价格', '凯伊商品', '凯伊价格查询', '凯伊预售', '凯伊尾款'):
                tools[0].invoke({'search_query': term})
            raise agent_service.AgentLimitError from ToolCallLimitExceededError(
                thread_count=9, run_count=9, thread_limit=None, run_limit=8)

        direct_model = MagicMock()
        direct_model.ainvoke = AsyncMock(return_value=AIMessage(content='凯伊毛绒售价100元。'))
        with patch.object(agent_service, 'run', side_effect=exhaust), \
             patch.object(agent_service, 'model', return_value=direct_model) as factory:
            response = app.api('POST', '/knowledge/api/agent/answer',
                               {'kb_id': self.kb, 'query': '凯伊毛绒多少钱'}, {})
        self.assertEqual(response['mode'], 'model')
        self.assertEqual(len(response['search_terms']), 8)
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs['tool_calling'])
        final_messages = direct_model.ainvoke.call_args.args[0]
        self.assertIn('售价100元', final_messages[-1]['content'])
        self.assertIn('不得请求继续搜索', final_messages[-2]['content'])
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
            self.assertEqual(limit, 8)
            by_name = {t.name: t for t in tools}
            by_name['read_record'].invoke({'id': str(qa['id'])})
            by_name['update_record'].invoke({'id': str(qa['id']), 'question': '怎么下单', 'answer': '淘宝下单'})
            return {'messages': [AIMessage(content='QA 已修改。')]}

        with patch.object(agent_service, 'run', side_effect=fake_run) as runner:
            first = app.api('POST', '/knowledge/api/agent/private-maintenance', data, {})
            second = app.api('POST', '/knowledge/api/agent/private-maintenance', data, {})
        self.assertEqual(first, second)
        self.assertEqual(first['reason'], 'saved')
        runner.assert_called_once()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT answer FROM qa_entries WHERE id=?', (qa['id'],)).fetchone()[0], '淘宝下单')

    def test_private_agent_can_update_knowledge_and_moderate_in_one_run(self):
        with app.db() as c:
            maintenance.settings(c, {'openids': ['authorized-user']})
            qa = app.save_qa(c, self.kb, {'question': '怎么下单', 'answer': '旧答案'})
        data = {'kb_id': self.kb, 'user_id': 'authorized-user', 'message_id': 'multi-action',
                'query': '/modify qa 更新购买说明并处理群消息'}

        def fake_run(cfg, tools, messages, prompt, limit):
            self.assertEqual(limit, 8)
            self.assertIn('知识修改成功后可以继续完成', prompt)
            by_name = {item.name: item for item in tools}
            by_name['read_record'].invoke({'id': str(qa['id'])})
            by_name['update_record'].invoke({'id': str(qa['id']), 'question': '怎么下单', 'answer': '新版淘宝下单'})
            result = by_name['moderate_group_message'].invoke({
                'group_id': 'group_123', 'message_id': 'msg_123', 'terms': ['中出'],
                'candidates': ['新的短语'], 'recall': True, 'warn': True})
            self.assertIn('record_harassment_count', result)
            self.assertIn('recall_group_message', result)
            self.assertIn('send_group_warning', result)
            self.assertNotIn('mute_group_member', result)
            return {'messages': [AIMessage(content='知识已更新，命中已记数，消息已处理。')]}

        with patch.object(mcp_server.knowledge, 'ADMIN_TOKEN', 'admin'), \
             patch.object(agent_service.tool_service, 'qq_moderation_request', side_effect=[
                 {'sent': True}, {'recalled': True}]) as qq_action, \
             patch.object(agent_service, 'run', side_effect=fake_run):
            response = app.api('POST', '/knowledge/api/agent/private-maintenance', data, {})
        self.assertEqual(response['reason'], 'saved')
        self.assertIn('知识已更新', response['answer'])
        self.assertEqual(qq_action.call_count, 2)
        self.assertEqual([call.args[1] for call in qq_action.call_args_list], ['warn', 'recall'])
        self.assertEqual(app.api('GET', '/knowledge/api/moderation-recalls', {}, {})['total'], 1)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT answer FROM qa_entries WHERE id=?', (qa['id'],)).fetchone()[0], '新版淘宝下单')
            detail = json.loads(c.execute('SELECT details FROM answer_traces WHERE id=?',
                                  (response['trace_id'],)).fetchone()[0])
        operations = detail['maintenance']['operations']
        self.assertEqual([row['tool'] for row in operations], [
            'read_record', 'update_record', 'moderate_group_message'])

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
        execution = agent_service.AgentExecution(details)
        asyncio.run(execution.call_model(AsyncMock(return_value=ai)))
        self.assertNotIn('private reasoning', json.dumps(details))
        final_chat = agent_service.model({'model': 'deepseek-flash', 'api_key': 'fake'}, tool_calling=False)
        self.assertNotIn('parallel_tool_calls', final_chat._get_request_payload('final answer'))

    def test_deepseek_model_serializes_ephemeral_image_content(self):
        data_url = 'data:image/png;base64,iVBORw0KGg=='
        chat = agent_service.model({'model': 'deepseek-flash', 'api_key': 'fake'})
        payload = chat._get_request_payload([HumanMessage(content=[
            {'type': 'text', 'text': '看图回答'},
            {'type': 'image_url', 'image_url': {'url': data_url, 'detail': 'auto'}},
        ])])
        self.assertEqual(payload['messages'][0]['content'][1]['type'], 'image_url')
        self.assertEqual(payload['messages'][0]['content'][1]['image_url']['url'], data_url)

    def test_deepseek_final_preserves_wire_prefix_and_disables_tool_choice(self):
        @tool
        def lookup(query: str) -> str:
            """Read a fact."""
            raise AssertionError('Serialization must not execute tools')

        chat = agent_service.model({'model': 'deepseek-flash', 'api_key': 'fake'})
        prefix = [HumanMessage(content='问题'), AIMessage(content='',
                  tool_calls=[{'name':'lookup','args':{'query':'资料'},'id':'one'}],
                  additional_kwargs={'reasoning_content':'ephemeral reasoning'}),
                  ToolMessage(content='已查到的事实',tool_call_id='one')]
        regular = chat.bind_tools([lookup],parallel_tool_calls=False)
        final = chat.bind_tools([lookup],tool_choice='none',parallel_tool_calls=False)
        before = chat._get_request_payload(prefix,**regular.kwargs)
        after = chat._get_request_payload([*prefix,HumanMessage(content='请直接回答')],**final.kwargs)
        self.assertEqual(after['tools'],before['tools'])
        self.assertEqual(after['messages'][:-1],before['messages'])
        self.assertEqual(after['tool_choice'],'none')

    def test_deepseek_cache_usage_survives_sdk_conversion(self):
        chat = agent_service.model({'model':'deepseek-flash','api_key':'fake'})
        response = {'id':'cache-test','model':'deepseek-flash','object':'chat.completion','created':1,
                    'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'OK'}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':1,'total_tokens':101,
                             'prompt_cache_hit_tokens':64,'prompt_cache_miss_tokens':36}}
        message = chat._create_chat_result(response).generations[0].message
        details = {}
        execution = agent_service.AgentExecution(details)
        asyncio.run(execution.call_model(AsyncMock(return_value=message)))
        usage = details['model_calls'][0]['usage']
        self.assertEqual(usage['input_tokens'],100)
        self.assertEqual(usage['input_token_details']['cache_read'],64)

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
                                  [{'role': 'user', 'content': '问答'}], 'Use tools', 8)
        self.assertEqual(len(calls), 8)


if __name__ == '__main__':
    unittest.main()
