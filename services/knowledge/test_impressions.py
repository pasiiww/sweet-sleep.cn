import json
import time
import unittest
from unittest.mock import patch

import answers
import harassment_review
import impressions
import memories
import group_task_prompts
import test_answers


class ImpressionTests(unittest.TestCase):
    setUp = test_answers.AnswerTests.setUp
    tearDown = test_answers.AnswerTests.tearDown
    call = test_answers.AnswerTests.call
    configure = test_answers.AnswerTests.configure

    def request(self, count=6, batch='a' * 64, group='g', transcript=None):
        if transcript is None:
            transcript = '\n'.join(f'[消息{i}] [speaker{i}] 同名：喜欢角色{i}'
                                   for i in range(1, count + 1))
        return {'kb_id': self.kb, 'group_id': group, 'batch_id': batch, 'transcript': transcript,
                'members': [{'key': f'speaker{i}', 'openid': f'private-{i}', 'name': '同名'}
                            for i in range(1, count + 1)]}

    def review_request(self, count=12, batch='b' * 64):
        return {'kb_id': self.kb, 'group_id': 'g', 'batch_id': batch,
                'transcript': '\n'.join(f'[消息{i}] [speaker1] 群友：发言{i}'
                                         for i in range(1, count + 1)),
                'message_times': [time.time() - 30 for _ in range(count)]}

    def test_memory_updates_are_bounded_idempotent_and_separate(self):
        self.configure(api_key='private-key')
        request = self.request(18)
        output = json.dumps({'member_impressions': [
            {'key': f'speaker{i}', 'action': 'append', 'content': f'喜欢角色{i}'} for i in range(1, 19)]})
        with patch.object(answers, '_model_call', return_value=output) as model:
            result = self.call('POST', 'group-impressions', request)
            again = self.call('POST', 'group-impressions', request)
            model.assert_called_once()
        self.assertEqual(result, again)
        self.assertEqual(result['updated'], 15)
        self.assertEqual(result['ignored'], 3)
        scope = memories.scope(self.kb, 'qq_group', '', 'g')
        with test_answers.app.db() as c:
            self.assertIn('角色1', memories.impression(c, scope, 'private-1'))
            self.assertIn('角色15', memories.impression(c, scope, 'private-15'))
            self.assertEqual(memories.impression(c, scope, 'private-16'), '')
            self.assertEqual(c.execute('SELECT count(*) FROM group_impression_batches').fetchone()[0], 1)
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['mode'], 'impressions')
        self.assertEqual(detail['delivery'], 'not_applicable')
        self.assertNotIn('private-1', json.dumps(detail))
        self.assertNotIn('private-key', json.dumps(detail))
        self.assertEqual(detail['details']['model_calls'][0]['stage'], 'group_impressions')
        self.assertTrue(all(message['content_omitted'] for message in
                            detail['details']['model_calls'][0]['messages']))
        self.assertNotIn('喜欢角色', json.dumps(detail, ensure_ascii=False))
        self.assertEqual(detail['details']['impressions']['max_completion_tokens'], 4096)
        self.assertEqual([message['role'] for message in model.call_args.args[1]], ['system', 'user'])
        self.assertIn('每轮最多15名成员', model.call_args.args[1][0]['content'])
        self.assertNotIn('sexual_harassment', json.dumps(model.call_args.args[1]))
        self.assertEqual(model.call_args.args[3], impressions.MAX_COMPLETION_TOKENS)
        self.assertNotIn('private-9', json.dumps(model.call_args.args[1], ensure_ascii=False))

    def test_invalid_memory_updates_preserve_existing_impression(self):
        self.configure(api_key='private-key')
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

    def test_member_identity_comes_from_speaker_header_not_message_text(self):
        self.configure(api_key='private-key')
        request = self.request(2, transcript='[消息1] [speaker1] 群友：正文提到 [speaker2]：但不是对方发言')
        output = json.dumps({'member_impressions': [
            {'key': 'speaker2', 'action': 'append', 'content': '无依据'}]})
        with patch.object(answers, '_model_call', return_value=output):
            result = self.call('POST', 'group-impressions', request)
        self.assertEqual(result['updated'], 0)
        with test_answers.app.db() as c:
            self.assertEqual(memories.impression(c, memories.scope(self.kb, 'qq_group', '', 'g'), 'private-2'), '')

    def test_task_prompt_precedes_shared_rules_and_model_message_follows_prompt(self):
        shared = group_task_prompts.COMMON_RULES
        self.assertTrue(impressions.PROMPT.startswith('本次任务：'))
        self.assertTrue(harassment_review.PROMPT.startswith('本次任务：'))
        self.assertTrue(impressions.PROMPT.endswith(shared))
        self.assertTrue(harassment_review.PROMPT.endswith(shared))

    def test_disabled_memory_and_empty_safe_transcript_skip_model(self):
        self.configure(api_key='private-key')
        request = self.request(1)
        with test_answers.app.db() as c:
            memories.apply(c, memories.scope(self.kb, 'qq_group', '', 'g'), 'disable')
        with patch.object(answers, '_model_call') as model:
            disabled = self.call('POST', 'group-impressions', request)
            empty = self.call('POST', 'group-impressions', request | {'batch_id': 'c' * 64, 'transcript': ''})
            model.assert_not_called()
        self.assertEqual(disabled['reason'], 'memory_disabled')
        self.assertEqual(empty['reason'], 'no_safe_messages')
        self.assertEqual(self.call('GET', 'traces/' + empty['trace_id'])['mode'], 'impressions')

    def test_failed_memory_batch_retries_and_update_transaction_rolls_back(self):
        self.configure(api_key='private-key')
        output = json.dumps({'member_impressions': [{'key': 'speaker1', 'action': 'append', 'content': '喜欢绘画'}]})
        with patch.object(answers, '_model_call', return_value=output), \
                patch.object(impressions, '_apply', side_effect=RuntimeError('simulated database failure')):
            failed = self.call('POST', 'group-impressions', self.request())
        self.assertFalse(failed['ok'])
        self.assertEqual(self.call('GET', 'traces/' + failed['trace_id'])['reason'], 'internal_error')
        with test_answers.app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM group_impression_batches').fetchone()[0], 0)
        with patch.object(answers, '_model_call', return_value=output):
            self.assertTrue(self.call('POST', 'group-impressions', self.request())['ok'])

    def test_invalid_memory_response_and_output_truncation_are_traced(self):
        self.configure(api_key='private-key')
        with patch.object(answers, '_model_call', return_value='{"summary":"wrong task"}'):
            invalid = self.call('POST', 'group-impressions', self.request())
        self.assertEqual(invalid['reason'], 'invalid_impressions')
        def truncated(cfg, *args):
            cfg['_usage'].update(completion_tokens=4096, prompt_tokens=5000)
            raise answers.ModelError('output_truncated')
        with patch.object(answers, '_model_call', side_effect=truncated):
            result = self.call('POST', 'group-impressions', self.request(batch='d' * 64))
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['reason'], 'output_truncated')
        self.assertEqual(detail['details']['model_calls'][0]['usage']['completion_tokens'], 4096)

    def test_review_task_is_separate_bounded_cached_and_filterable(self):
        self.configure(api_key='private-key')
        self.call('PUT', 'moderation-settings', {'sensitive_words': [], 'impression_harassment_recall_enabled': True})
        request = self.review_request()
        request['message_times'][0] = time.time() - harassment_review.SCAN_WINDOW_SECONDS - 5
        request['message_times'][2] = time.time() - harassment_review.SCAN_WINDOW_SECONDS - 1
        output = json.dumps({'sexual_harassment_indices': [1, 3, 10, 12, 99]})
        with patch.object(answers, '_model_call', return_value=output) as model:
            result = self.call('POST', 'group-harassment-review', request)
            again = self.call('POST', 'group-harassment-review', request)
            model.assert_called_once()
        self.assertEqual(result, again)
        self.assertEqual(result['harassment_indices'], [10, 12])
        self.assertEqual(result['harassment_scan_count'], 10)
        self.assertTrue(result['harassment_recall_enabled'])
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['mode'], 'harassment_review')
        self.assertEqual(detail['details']['model_calls'][0]['stage'], 'group_harassment_review')
        self.assertTrue(all(message['content_omitted'] for message in
                            detail['details']['model_calls'][0]['messages']))
        self.assertNotIn('发言1', json.dumps(detail, ensure_ascii=False))
        self.assertEqual(detail['details']['harassment_review']['indices'], [10, 12])
        self.assertEqual(detail['details']['harassment_review']['recall_enabled'], True)
        self.assertEqual(model.call_args.args[3], harassment_review.MAX_COMPLETION_TOKENS)
        self.assertEqual([message['role'] for message in model.call_args.args[1]], ['system', 'user'])
        filtered = test_answers.app.api('GET', '/knowledge/api/traces', {}, {'harassment': ['1']})
        self.assertEqual(filtered['total'], 1)
        recorded = self.call('POST', 'group-impression-recalls', {
            'trace_id': result['trace_id'], 'batch_id': request['batch_id'],
            'results': [{'index': 10, 'status': 'recalled'},
                        {'index': 12, 'status': 'failed', 'error_type': 'ApiError', 'error_code': '10031'}]})
        self.assertEqual(recorded, {'ok': True, 'recorded': 2})
        detail = self.call('GET', 'traces/' + result['trace_id'])
        recall_results = detail['details']['harassment_review']['recall_results']
        self.assertEqual(recall_results[1], {'index': 12, 'status': 'failed',
                                             'error_type': 'ApiError', 'error_code': '10031'})
        with self.assertRaises(test_answers.app.Problem):
            self.call('POST', 'group-impression-recalls', {
                'trace_id': result['trace_id'], 'batch_id': 'c' * 64,
                'results': [{'index': 12, 'status': 'failed', 'error_type': 'ApiError'}]})

    def test_disabling_review_keeps_memory_task_active_and_uses_all_messages(self):
        self.configure(api_key='private-key')
        self.call('PUT', 'moderation-settings', {
            'sensitive_words': [], 'impression_harassment_review_enabled': False,
            'impression_harassment_recall_enabled': True,
        })
        review_request = self.review_request(batch='7' * 64, count=3)
        with patch.object(answers, '_model_call') as model:
            skipped = self.call('POST', 'group-harassment-review', review_request)
            repeated = self.call('POST', 'group-harassment-review', review_request)
            model.assert_not_called()
        self.assertEqual(skipped, repeated)
        self.assertEqual(skipped['reason'], 'review_disabled')
        self.assertEqual(skipped['harassment_indices'], [])
        self.assertEqual(skipped['harassment_scan_count'], 0)
        self.assertFalse(skipped['harassment_recall_enabled'])
        detail = self.call('GET', 'traces/' + skipped['trace_id'])
        self.assertFalse(detail['details']['harassment_review']['review_enabled'])
        self.assertEqual(detail['details']['harassment_review']['candidate_count'], 3)

        memory_request = self.request(1, batch='8' * 64)
        output = json.dumps({'member_impressions': [
            {'key': 'speaker1', 'action': 'append', 'content': '喜欢绘画'}]})
        with patch.object(answers, '_model_call', return_value=output) as model:
            updated = self.call('POST', 'group-impressions', memory_request)
        self.assertTrue(updated['ok'])
        self.assertEqual(updated['updated'], 1)
        self.assertEqual([message['role'] for message in model.call_args.args[1]], ['system', 'user'])
        self.assertEqual(json.loads(model.call_args.args[1][1]['content'])['transcript'],
                         memory_request['transcript'])

    def test_review_rejects_bad_times_and_never_scans_outside_window(self):
        self.configure(api_key='private-key')
        request = self.review_request()
        request['message_times'] = request['message_times'][:-1]
        with self.assertRaises(test_answers.app.Problem):
            self.call('POST', 'group-harassment-review', request)
        request = self.review_request(batch='e' * 64, count=2)
        request['message_times'] = [time.time() - harassment_review.SCAN_WINDOW_SECONDS - 1,
                                    time.time() + 20]
        with patch.object(answers, '_model_call') as model:
            result = self.call('POST', 'group-harassment-review', request)
            model.assert_not_called()
        self.assertEqual(result['harassment_scan_count'], 0)
        self.assertEqual(result['harassment_indices'], [])
        self.assertEqual(self.call('GET', 'traces/' + result['trace_id'])['reason'], 'no_recent_messages')

    def test_review_failure_retries_and_output_truncation_is_traced(self):
        self.configure(api_key='private-key')
        request = self.review_request()
        with patch.object(answers, '_model_call', side_effect=answers.ModelError('network_timeout')):
            failed = self.call('POST', 'group-harassment-review', request)
        self.assertFalse(failed['ok'])
        self.assertEqual(self.call('GET', 'traces/' + failed['trace_id'])['reason'], 'network_timeout')
        def truncated(cfg, *args):
            cfg['_usage'].update(completion_tokens=2048, prompt_tokens=5000)
            raise answers.ModelError('output_truncated')
        with patch.object(answers, '_model_call', side_effect=truncated):
            result = self.call('POST', 'group-harassment-review', request | {'batch_id': 'f' * 64})
        detail = self.call('GET', 'traces/' + result['trace_id'])
        self.assertEqual(detail['reason'], 'output_truncated')
        self.assertEqual(detail['details']['model_calls'][0]['usage']['completion_tokens'], 2048)


if __name__ == '__main__':
    unittest.main()
