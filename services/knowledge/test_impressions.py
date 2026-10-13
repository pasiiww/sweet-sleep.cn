import json
import time
import unittest
from unittest.mock import patch

import answers
import impressions
import memories
import test_answers


class ImpressionTests(unittest.TestCase):
    setUp = test_answers.AnswerTests.setUp
    tearDown = test_answers.AnswerTests.tearDown
    call = test_answers.AnswerTests.call
    configure = test_answers.AnswerTests.configure

    def request(self, count=6, batch='a' * 64, group='g'):
        return {'kb_id': self.kb, 'group_id': group, 'batch_id': batch,
                'transcript': '\n'.join(f'[消息{i}] 同名 [speaker{i}]：喜欢角色{i}' for i in range(1, count + 1)),
                'message_ids': [f'message-{i}' for i in range(1, count + 1)],
                'message_times': [time.time() - 30 for _ in range(count)],
                'members': [{'key': f'speaker{i}', 'openid': f'private-{i}', 'name': '同名'} for i in range(1, count + 1)]}

    def test_bounded_updates_and_atomic_idempotent_batch(self):
        self.configure(api_key='private-key')
        self.call('PUT', 'moderation-settings', {'sensitive_words': [], 'impression_harassment_recall_enabled': True})
        request = self.request(18)
        request['message_times'][0] = time.time() - impressions.HARASSMENT_SCAN_WINDOW_SECONDS - 5
        output = json.dumps({'member_impressions': [
            {'key': f'speaker{i}', 'action': 'append', 'content': f'喜欢角色{i}'} for i in range(1, 19)],
            'sexual_harassment_indices': [1, 9, 10, 18, 99]})
        with patch.object(answers, '_model_call', return_value=output) as model:
            result = self.call('POST', 'group-impressions', request)
            again = self.call('POST', 'group-impressions', request)
            model.assert_called_once()
        self.assertEqual(result, again)
        self.assertEqual(result['updated'], 15)
        self.assertEqual(result['ignored'], 3)
        self.assertEqual(result['harassment_message_ids'], ['message-9', 'message-10', 'message-18'])
        self.assertTrue(result['harassment_recall_enabled'])
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with test_answers.app.db() as c:
            self.assertIn('角色1', memories.impression(c, scope, 'private-1'))
            self.assertIn('角色15', memories.impression(c, scope, 'private-15'))
            self.assertEqual(memories.impression(c, scope, 'private-16'), '')
            self.assertEqual(c.execute('SELECT count(*) FROM group_impression_batches').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM daily_queries').fetchone()[0], 0)
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['mode'], 'impressions')
        self.assertEqual(detail['delivery'], 'not_applicable')
        self.assertNotIn('private-1', json.dumps(detail))
        self.assertNotIn('private-key', json.dumps(detail))
        self.assertEqual(detail['details']['model_calls'][0]['stage'], 'group_impressions')
        self.assertIn('每轮最多15名成员', model.call_args.args[1][0]['content'])
        self.assertIn('最近2分钟', model.call_args.args[1][0]['content'])
        self.assertEqual(detail['details']['impressions']['harassment_indices'], [9, 10, 18])
        model_messages = json.dumps(model.call_args.args[1], ensure_ascii=False)
        self.assertNotIn('message-9', model_messages)
        self.assertNotIn('private-9', model_messages)
        self.assertEqual(detail['details']['impressions']['harassment_scan_count'], 17)
        filtered = test_answers.app.api('GET', '/knowledge/api/traces', {}, {'harassment': ['1']})
        self.assertEqual(filtered['total'], 1)

    def test_long_duplicate_invalid_and_unmapped_updates_preserve_existing(self):
        self.configure()
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with test_answers.app.db() as c:
            memories.update_impression(c, scope, 'private-1', '已有印象')
        updates = [{'key': 'speaker1', 'action': 'replace', 'content': '长' * 81},
                   {'key': 'speaker99', 'action': 'replace', 'content': '未映射'},
                   {'key': 'speaker2', 'action': 'clear', 'content': '无效'},
                   {'key': 'speaker2', 'action': 'replace', 'content': '短' * 80},
                   {'key': 'speaker2', 'action': 'replace', 'content': '重复操作'},
                   {'key': 'speaker3', 'action': 'append', 'content': '喜欢绘画'}]
        with patch.object(answers, '_model_call', return_value=json.dumps({
                'member_impressions': updates, 'sexual_harassment_indices': []})):
            result = self.call('POST', 'group-impressions', self.request(3))
        self.assertEqual(result['updated'], 2)
        self.assertEqual(result['ignored'], 4)
        with test_answers.app.db() as c:
            self.assertEqual(memories.impression(c, scope, 'private-1'), '群友印象：已有印象')
            self.assertEqual(memories.impression(c, scope, 'private-2'), '群友印象：' + '短' * 80)

    def test_failed_batch_retries_and_disabled_memory_still_checks_harassment(self):
        self.configure()
        with patch.object(answers, '_model_call', side_effect=answers.ModelError('network_timeout')):
            failed = self.call('POST', 'group-impressions', self.request())
        self.assertFalse(failed['ok'])
        self.assertEqual(self.call('GET', 'traces/' + failed['trace_id'])['reason'], 'network_timeout')
        with test_answers.app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM group_impression_batches').fetchone()[0], 0)
            memories.apply(c, memories.scope(self.kb, 'qq_group', '', 'g'), 'disable')
        output = json.dumps({'member_impressions': [], 'sexual_harassment_indices': [1]})
        with patch.object(answers, '_model_call', return_value=output) as model:
            result = self.call('POST', 'group-impressions', self.request())
            model.assert_called_once()
        self.assertTrue(result['ok'])
        self.assertEqual(result['reason'], 'memory_disabled')
        self.assertEqual(result['harassment_message_ids'], [])
        self.assertEqual(result['harassment_flagged'], 1)
        self.assertFalse(result['harassment_recall_enabled'])

    def test_update_transaction_rolls_back_and_same_batch_can_retry(self):
        self.configure()
        output = json.dumps({'member_impressions': [{'key': 'speaker1', 'action': 'append', 'content': '喜欢绘画'}],
                             'sexual_harassment_indices': []})
        with patch.object(answers, '_model_call', return_value=output), \
                patch.object(impressions, '_apply', side_effect=RuntimeError('simulated database failure')):
            result = self.call('POST', 'group-impressions', self.request())
            self.assertFalse(result['ok'])
            self.assertEqual(self.call('GET', 'traces/' + result['trace_id'])['reason'], 'internal_error')
        with test_answers.app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM group_impression_batches').fetchone()[0], 0)
        with patch.object(answers, '_model_call', return_value=output):
            self.assertTrue(self.call('POST', 'group-impressions', self.request())['ok'])

    def test_roster_validation_and_invalid_model_json(self):
        self.configure()
        request = self.request()
        request['members'][1]['openid'] = request['members'][0]['openid']
        with self.assertRaises(test_answers.app.Problem):
            self.call('POST', 'group-impressions', request)
        with patch.object(answers, '_model_call', return_value='{"summary":"wrong task"}'):
            result = self.call('POST', 'group-impressions', self.request())
        self.assertEqual(result['reason'], 'invalid_impressions')

    def test_message_time_and_id_alignment_restricts_recalls_to_recent_messages(self):
        request = self.request(12)
        request['transcript'] = request['transcript'].replace('[消息1]', '[消息2]', 1)
        with self.assertRaises(test_answers.app.Problem):
            self.call('POST', 'group-impressions', request)
        request = self.request(12)
        request['message_times'][0] = time.time() - impressions.HARASSMENT_SCAN_WINDOW_SECONDS - 1
        with self.assertRaises(test_answers.app.Problem):
            self.call('POST', 'group-impressions', request | {'message_times': request['message_times'][:-1]})
        self.configure()
        self.call('PUT', 'moderation-settings', {'sensitive_words': [], 'impression_harassment_recall_enabled': True})
        request = self.request(12)
        request['message_times'][0] = time.time() - impressions.HARASSMENT_SCAN_WINDOW_SECONDS - 1
        request['message_times'][2] = time.time() - impressions.HARASSMENT_SCAN_WINDOW_SECONDS - 1
        output = json.dumps({'member_impressions': [], 'sexual_harassment_indices': [1, 3, 11, 12]})
        with patch.object(answers, '_model_call', return_value=output):
            result = self.call('POST', 'group-impressions', request)
        self.assertEqual(result['harassment_message_ids'], ['message-11', 'message-12'])

    def test_future_messages_are_not_scanned_and_unrepresentable_timestamps_are_rejected(self):
        self.configure()
        request = self.request(1)
        request['message_times'][0] = time.time() + 20
        output = json.dumps({'member_impressions': [], 'sexual_harassment_indices': [1]})
        with patch.object(answers, '_model_call', return_value=output):
            result = self.call('POST', 'group-impressions', request)
        self.assertEqual(result['harassment_scan_count'], 0)
        self.assertEqual(result['harassment_flagged'], 0)
        request['batch_id'] = 'b' * 64
        request['message_times'][0] = 10 ** 1000
        with self.assertRaises(test_answers.app.Problem):
            self.call('POST', 'group-impressions', request)

    def test_truncated_response_keeps_provider_usage_in_trace(self):
        self.configure()
        def truncated(cfg, *args):
            cfg['_usage'].update(completion_tokens=8192, prompt_tokens=5000)
            raise answers.ModelError('output_truncated')
        with patch.object(answers, '_model_call', side_effect=truncated):
            result = self.call('POST', 'group-impressions', self.request())
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['details']['model_calls'][0]['usage']['completion_tokens'], 8192)
