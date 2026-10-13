import unittest
import json
from unittest.mock import patch
from urllib import error
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

    def test_summary_does_not_read_or_write_impressions_or_public_memory(self):
        self.configure()
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with test_answers.app.db() as c:
            memories.apply(c, scope, 'save', '已有群公共记忆', member_openid='')
            memories.update_impression(c, scope, 'private-member', '已有成员印象')
            before = c.execute('SELECT * FROM conversation_memories').fetchall()
        output = json.dumps({'summary': '大家讨论了活动。',
            'memory_actions': [{'action': 'forget', 'content': '已有群公共记忆'}],
            'member_impressions': [{'key': 'speaker1', 'action': 'replace', 'content': '不能写入'}]})
        with patch.object(answers, '_model_call', return_value=output) as model:
            result = self.call('POST', 'group-summary', {'kb_id': self.kb, 'group_id': 'g',
                'transcript': '群友 [speaker1]：下周有活动',
                'members': [{'key': 'speaker1', 'openid': 'private-member', 'name': '群友'}]})
        self.assertTrue(result['ok'])
        self.assertEqual(result['memory_actions'], [])
        context = json.loads(model.call_args.args[1][1]['content'].split('\n', 1)[1])
        self.assertEqual(list(context), ['transcript'])
        self.assertNotIn('已有成员印象', str(model.call_args))
        self.assertNotIn('已有群公共记忆', str(model.call_args))
        with test_answers.app.db() as c:
            self.assertEqual(c.execute('SELECT * FROM conversation_memories').fetchall(), before)

    def test_summary_disabled_and_failure(self):
        with patch.object(answers,'model_call') as model:
            self.assertFalse(self.call('POST','group-summary',{'kb_id':self.kb,'transcript':'聊天'})['ok'])
            model.assert_not_called()
        self.configure()
        with patch.object(answers,'model_call',side_effect=answers.ModelError('network_error')):
            self.assertFalse(self.call('POST','group-summary',{'kb_id':self.kb,'transcript':'聊天'})['ok'])

    def test_summary_trace_success_redaction_and_delivery_receipt(self):
        self.configure(api_key='private-summary-key')
        request = {'kb_id': self.kb, 'group_id': 'g', 'origin': 'qq_group',
                   'user_id': 'requester', 'session_id': 'summary-session',
                   'transcript': '群友 [speaker1]：周五举办活动 private-summary-key',
                   'members': [{'key': 'speaker1', 'openid': 'private-member', 'name': '群友'}]}
        with patch.object(answers, '_model_call', return_value='{"summary":"周五举办活动 private-summary-key"}') as model:
            result = self.call('POST', 'group-summary', request)
        self.assertTrue(result['ok'])
        self.assertEqual(model.call_args.args[0]['_timeout'], 60)
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['question'], '/总结')
        self.assertEqual(detail['mode'], 'summary')
        self.assertEqual(detail['reason'], 'summary_generated')
        self.assertEqual(detail['delivery'], 'pending')
        self.assertEqual(detail['user_id'], 'requester')
        self.assertEqual(detail['group_id'], 'g')
        self.assertEqual(detail['session_id'], 'summary-session')
        self.assertGreaterEqual(detail['elapsed_ms'], 0)
        calls = detail['details']['model_calls']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['stage'], 'group_summary')
        self.assertEqual(calls[0]['timeout_seconds'], 60)
        self.assertIn('周五举办活动', calls[0]['messages'][1]['content'])
        self.assertNotIn('private-summary-key', json.dumps(detail))
        self.assertNotIn('private-member', json.dumps(detail))
        self.assertNotIn(result['trace_receipt'], json.dumps(detail))
        with self.assertRaises(test_answers.app.Problem):
            self.call('POST', 'trace-delivery', {'trace_id': result['trace_id'],
                'receipt': 'wrong', 'status': 'delivered', 'content': '总结'})
        self.call('POST', 'trace-delivery', {'trace_id': result['trace_id'],
            'receipt': result['trace_receipt'], 'status': 'delivered', 'content': '实际发出的总结'})
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['delivery'], 'delivered')
        self.assertEqual(detail['details']['delivery']['content'], '实际发出的总结')
        with test_answers.app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM daily_queries').fetchone()[0], 0)

    def test_summary_trace_records_disabled_busy_and_api_origin(self):
        request = {'kb_id': self.kb, 'transcript': '聊天'}
        result = self.call('POST', 'group-summary', request)
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['reason'], 'model_disabled')
        self.assertEqual(detail['delivery'], 'not_applicable')
        self.assertEqual(result['trace_receipt'], '')
        self.assertEqual(detail['details']['model_calls'], [])
        self.configure()
        with patch.object(test_answers.app, 'ANSWER_SLOTS') as slots:
            slots.acquire.return_value = False
            result = self.call('POST', 'group-summary', request)
            slots.release.assert_not_called()
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['reason'], 'model_busy')
        self.assertEqual(detail['details']['model_calls'], [])

    def test_summary_trace_records_model_errors_and_invalid_json(self):
        self.configure()
        for failure in ('network_timeout', 'invalid_key', 'rate_limited', 'output_truncated'):
            with self.subTest(reason=failure), patch.object(answers, '_model_call', side_effect=answers.ModelError(failure)):
                result = self.call('POST', 'group-summary', {'kb_id': self.kb, 'group_id': 'g', 'transcript': '聊天'})
            detail = self.call('GET', 'traces/' + result['trace_id'])
            self.assertFalse(result['ok'])
            self.assertEqual(detail['mode'], 'error')
            self.assertEqual(detail['reason'], failure)
            self.assertEqual(detail['details']['model_calls'][0]['error'], failure)
        with patch.object(answers, '_model_call', return_value='{"summary":'):
            result = self.call('POST', 'group-summary', {'kb_id': self.kb, 'transcript': '聊天'})
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['reason'], 'invalid_summary')
        self.assertEqual(detail['details']['model_calls'][0]['output'], '{"summary":')

    def test_summary_trace_records_internal_error_and_releases_slot(self):
        self.configure()
        with patch.object(answers, '_model_call', side_effect=RuntimeError('private-error')), \
                patch.object(test_answers.app, 'ANSWER_SLOTS') as slots:
            slots.acquire.return_value = True
            result = self.call('POST', 'group-summary', {'kb_id': self.kb, 'transcript': '聊天'})
            slots.release.assert_called_once()
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(detail['reason'], 'internal_error')
        self.assertEqual(detail['details']['error_type'], 'RuntimeError')
        self.assertNotIn('private-error', json.dumps(detail))

    def test_summary_network_timeout_is_distinguished_from_connection_failure(self):
        self.configure()
        for failure, reason in [(TimeoutError('private body'), 'network_timeout'),
                                (error.URLError(TimeoutError('private body')), 'network_timeout'),
                                (error.URLError('connection refused'), 'network_error')]:
            with self.subTest(reason=reason), patch.object(answers.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = failure
                result = self.call('POST', 'group-summary', {'kb_id': self.kb, 'transcript': '聊天'})
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 60)
            detail = self.call('GET', 'traces/' + result['trace_id'])
            self.assertEqual(detail['reason'], reason)
            self.assertEqual(detail['details']['model_calls'][0]['error'], reason)
            self.assertNotIn('private body', json.dumps(detail))
