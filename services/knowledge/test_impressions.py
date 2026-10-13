import json
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
                'transcript': '\n'.join(f'同名 [speaker{i}]：喜欢角色{i}' for i in range(1, count + 1)),
                'members': [{'key': f'speaker{i}', 'openid': f'private-{i}', 'name': '同名'} for i in range(1, count + 1)]}

    def test_bounded_updates_and_atomic_idempotent_batch(self):
        self.configure(api_key='private-key')
        output = json.dumps({'member_impressions': [
            {'key': f'speaker{i}', 'action': 'append', 'content': f'喜欢角色{i}'} for i in range(1, 7)]})
        with patch.object(answers, '_model_call', return_value=output) as model:
            result = self.call('POST', 'group-impressions', self.request())
            again = self.call('POST', 'group-impressions', self.request())
            model.assert_called_once()
        self.assertEqual(result, again)
        self.assertEqual(result['updated'], 5)
        self.assertEqual(result['ignored'], 1)
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with test_answers.app.db() as c:
            self.assertIn('角色1', memories.impression(c, scope, 'private-1'))
            self.assertEqual(memories.impression(c, scope, 'private-6'), '')
            self.assertEqual(c.execute('SELECT count(*) FROM group_impression_batches').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM daily_queries').fetchone()[0], 0)
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['mode'], 'impressions')
        self.assertEqual(detail['delivery'], 'not_applicable')
        self.assertNotIn('private-1', json.dumps(detail))
        self.assertNotIn('private-key', json.dumps(detail))
        self.assertEqual(detail['details']['model_calls'][0]['stage'], 'group_impressions')
        self.assertIn('每轮最多5名成员', model.call_args.args[1][0]['content'])

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
        with patch.object(answers, '_model_call', return_value=json.dumps({'member_impressions': updates})):
            result = self.call('POST', 'group-impressions', self.request(3))
        self.assertEqual(result['updated'], 2)
        self.assertEqual(result['ignored'], 4)
        with test_answers.app.db() as c:
            self.assertEqual(memories.impression(c, scope, 'private-1'), '群友印象：已有印象')
            self.assertEqual(memories.impression(c, scope, 'private-2'), '群友印象：' + '短' * 80)

    def test_failed_batch_retries_and_disabled_memory_is_acknowledged_without_model(self):
        self.configure()
        with patch.object(answers, '_model_call', side_effect=answers.ModelError('network_timeout')):
            failed = self.call('POST', 'group-impressions', self.request())
        self.assertFalse(failed['ok'])
        self.assertEqual(self.call('GET', 'traces/' + failed['trace_id'])['reason'], 'network_timeout')
        with test_answers.app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM group_impression_batches').fetchone()[0], 0)
            memories.apply(c, memories.scope(self.kb, 'qq_group', '', 'g'), 'disable')
        with patch.object(answers, '_model_call') as model:
            result = self.call('POST', 'group-impressions', self.request())
            model.assert_not_called()
        self.assertTrue(result['ok'])
        self.assertEqual(result['reason'], 'memory_disabled')

    def test_update_transaction_rolls_back_and_same_batch_can_retry(self):
        self.configure()
        output = json.dumps({'member_impressions': [{'key': 'speaker1', 'action': 'append', 'content': '喜欢绘画'}]})
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

    def test_truncated_response_keeps_provider_usage_in_trace(self):
        self.configure()
        def truncated(cfg, *args):
            cfg['_usage'].update(completion_tokens=8192, prompt_tokens=5000)
            raise answers.ModelError('output_truncated')
        with patch.object(answers, '_model_call', side_effect=truncated):
            result = self.call('POST', 'group-impressions', self.request())
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['details']['model_calls'][0]['usage']['completion_tokens'], 8192)
