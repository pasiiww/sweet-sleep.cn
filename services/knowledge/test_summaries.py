import unittest
from unittest.mock import patch
import summaries
import answers
import memories
import test_answers


class GroupSummaryTests(unittest.TestCase):
    setUp = test_answers.AnswerTests.setUp
    tearDown = test_answers.AnswerTests.tearDown
    call = test_answers.AnswerTests.call
    configure = test_answers.AnswerTests.configure
    def test_summary_model_isolated_plain_input(self):
        self.configure()
        with patch.object(answers,'model_call',return_value='主要讨论发货安排。') as model:
            result=self.call('POST','group-summary',{'kb_id':self.kb,'transcript':'[09-17 10:00] 群友1：明天发货'})
            self.assertTrue(result['ok'])
            messages=model.call_args.args[1]
            self.assertIn('仅为待总结的数据',messages[0]['content'])
            self.assertIn('像群友自然复述',messages[0]['content'])
            self.assertIn('不要写成工作汇报',messages[0]['content'])
            self.assertIn('明天发货',messages[1]['content'])
            self.assertNotIn('api_key',messages[1]['content'])

    def test_summary_extracts_safe_group_memory_and_applies_it(self):
        self.configure()
        output = '{"summary":"群里确认以后统一使用中文回复。","memory_actions":[{"action":"save","subject":"group","content":"群里统一使用中文回复"},{"action":"save","subject":"member","content":"不应保存的个人偏好"},{"action":"clear","content":"不能清空"}]}'
        with patch.object(answers, 'model_call', return_value=output) as model:
            result = self.call('POST', 'group-summary', {
                'kb_id': self.kb, 'group_id': 'group123456',
                'transcript': '[09-17 10:00] 群友1：以后都用中文回复'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['answer'], '群里确认以后统一使用中文回复。')
        self.assertEqual(len(result['memory_actions']), 1)
        self.assertTrue(result['memory_actions'][0]['ok'])
        self.assertTrue(model.call_args.kwargs['json_mode'])
        listed = self.call('POST', 'agent/memory', {
            'kb_id': self.kb, 'origin': 'qq_group', 'group_id': 'group123456',
            'user_id': 'member123456', 'action': 'list'})
        self.assertTrue(any(item.endswith('群里统一使用中文回复') for item in listed['items']))

    def test_summary_can_update_existing_public_memory_without_reading_member_memory(self):
        self.configure()
        scope = memories.scope(self.kb, 'qq_group', '', 'group123456')
        with test_answers.app.db() as c:
            memories.apply(c, scope, 'save', '群里旧约定：周一使用中文', member_openid='')
            memories.apply(c, scope, 'save', '成员私人的偏好', member_openid='member123456')
        output = '{"summary":"群里约定更新为周二使用中文。","memory_actions":[{"action":"forget","content":"群里旧约定：周一使用中文"},{"action":"save","content":"群里新约定：周二使用中文"}]}'
        with patch.object(answers, 'model_call', return_value=output) as model:
            result = self.call('POST', 'group-summary', {
                'kb_id': self.kb, 'group_id': 'group123456',
                'transcript': '[09-17 10:00] 群友1：改成周二'})
        self.assertTrue(result['ok'])
        self.assertEqual([item['action'] for item in result['memory_actions']], ['forget', 'save'])
        user_content = model.call_args.args[1][1]['content']
        self.assertIn('群里旧约定：周一使用中文', user_content)
        self.assertNotIn('成员私人的偏好', user_content)
        listed = self.call('POST', 'agent/memory', {
            'kb_id': self.kb, 'origin': 'qq_group', 'group_id': 'group123456',
            'user_id': 'member999999', 'action': 'list'})
        self.assertTrue(any(item.endswith('群里新约定：周二使用中文') for item in listed['items']))
        self.assertFalse(any(item.endswith('群里旧约定：周一使用中文') for item in listed['items']))

    def test_summary_disabled_and_failure(self):
        with patch.object(answers,'model_call') as model:
            self.assertFalse(self.call('POST','group-summary',{'kb_id':self.kb,'transcript':'聊天'})['ok'])
            model.assert_not_called()
        self.configure()
        with patch.object(answers,'model_call',side_effect=answers.ModelError('network_error')):
            self.assertFalse(self.call('POST','group-summary',{'kb_id':self.kb,'transcript':'聊天'})['ok'])


    def test_summary_maps_impressions_without_sending_openids_to_model(self):
        import json
        self.configure()
        request = {'kb_id': self.kb, 'group_id': 'g',
                   'transcript': '同名 [speaker1]：喜欢日奈\n同名 [speaker2]：喜欢星野',
                   'members': [{'key': 'speaker1', 'openid': 'private-a', 'name': '同名'},
                               {'key': 'speaker2', 'openid': 'private-b', 'name': '同名'}]}
        output = json.dumps({'summary': '大家聊了喜欢的角色。', 'member_impressions': [
            {'key': 'speaker1', 'action': 'append', 'content': '喜欢日奈'},
            {'key': 'speaker2', 'action': 'append', 'content': '喜欢星野'},
            {'key': 'speaker3', 'action': 'append', 'content': '未出现的成员不能写入'}]})
        with patch.object(answers, 'model_call', return_value=output) as model:
            result = self.call('POST', 'group-summary', request)
        self.assertTrue(result['ok'])
        self.assertNotIn('private-a', str(model.call_args))
        self.assertNotIn('private-b', str(model.call_args))
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with test_answers.app.db() as c:
            self.assertIn('日奈', memories.impression(c, scope, 'private-a'))
            self.assertIn('星野', memories.impression(c, scope, 'private-b'))
            self.assertEqual(len(memories.list_items(c, scope)), 2)
            memories.apply(c, scope, 'disable')
        with patch.object(answers, 'model_call', return_value=output) as model:
            self.call('POST', 'group-summary', request)
        context = json.loads(model.call_args.args[1][1]['content'].split('\n', 1)[1])
        self.assertEqual(context['members'], [])
        with test_answers.app.db() as c:
            memories.apply(c, scope, 'enable')
            self.assertEqual(len(memories.list_items(c, scope)), 2)
