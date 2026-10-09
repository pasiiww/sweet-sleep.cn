from pathlib import Path
import tempfile
import unittest

import answers
import moderation
import server as app


class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, body=None):
        return app.api(method, '/knowledge/api/' + path, body or {}, {})

    def configure(self, words):
        defaults = answers.defaults()
        return self.call('PUT', 'answer-settings', {
            'enabled': True, 'model': 'deepseek-flash', 'api_key': '',
            'system_prompt': defaults['system_prompt'], 'keyword_prompt': defaults['keyword_prompt'],
            'handoff_groups': {}, 'sensitive_words': words,
        })

    def test_cbz_is_default_and_admin_can_edit_or_disable_terms(self):
        self.assertEqual(self.call('GET', 'answer-settings')['sensitive_words'], answers.DEFAULT_SENSITIVE_WORDS)
        moderation_settings = self.call('GET', 'moderation-settings')
        self.assertFalse(moderation_settings['harassment_mute_enabled'])
        self.assertEqual(moderation_settings['harassment_mute_threshold'], 3)
        self.assertEqual(moderation_settings['harassment_mute_duration_minutes'], 10)
        self.configure([' cbz ', 'CBZ', 'bad phrase'])
        self.assertEqual(self.call('GET', 'moderation-settings')['sensitive_words'], ['cbz', 'bad phrase'])
        self.configure([])
        self.assertEqual(self.call('GET', 'moderation-settings')['sensitive_words'], [])

    def test_sensitive_word_alternatives_expand_cartesian_products(self):
        pattern = '(A/B/C)(D/E)(F/G)'
        expected = [first + second + third
                    for first in 'ABC' for second in 'DE' for third in 'FG']
        self.assertEqual(moderation.expand_word_pattern(pattern), expected)
        self.assertEqual(len(moderation.expand_word_pattern('(A/B)(C/D)')), 4)
        self.configure([pattern, '(A/B)(C/D)'])
        settings = self.call('GET', 'moderation-settings')
        self.assertEqual(settings['sensitive_words'], [pattern, '(A/B)(C/D)'])
        self.assertEqual(settings['sensitive_word_expansions'][0], expected)
        self.assertEqual(len(settings['sensitive_word_expansions'][1]), 4)
        self.assertEqual(moderation.expand_word_pattern('射(你/)进去'), ['射你进去', '射进去'])
        self.assertEqual(moderation.expand_word_pattern(r'路径\/名字'), ['路径/名字'])

    def test_sensitive_word_alternative_syntax_and_expansion_are_bounded(self):
        for literal in ('(A/B', 'A/B', '(A///B)', '(A/(B/C))'):
            with self.subTest(literal=literal):
                self.assertEqual(moderation.expand_word_pattern(literal), [literal])
        with self.assertRaises(app.Problem):
            self.configure(['(A/B)' * 9])

    def test_dedicated_moderation_settings_update_preserves_answer_configuration(self):
        defaults = answers.defaults()
        self.configure(['cbz'])
        result = self.call('PUT', 'moderation-settings', {
            'sensitive_words': ['(蛇/🐍)(精/米青)'], 'harassment_warning_enabled': False})
        self.assertEqual(len(result['sensitive_word_expansions'][0]), 4)
        self.assertFalse(result['harassment_warning_enabled'])
        self.assertEqual(self.call('GET', 'moderation-settings')['sensitive_words'], ['(蛇/🐍)(精/米青)'])
        self.assertTrue(self.call('GET', 'answer-settings')['enabled'])
        self.assertEqual(self.call('GET', 'answer-settings')['system_prompt'], defaults['system_prompt'])

    def test_auto_mute_settings_are_editable_and_persist_through_answer_settings(self):
        result = self.call('PUT', 'moderation-settings', {
            'sensitive_words': ['cbz'], 'harassment_mute_enabled': True,
            'harassment_mute_threshold': 5, 'harassment_mute_duration_minutes': 25,
        })
        self.assertTrue(result['harassment_mute_enabled'])
        self.assertEqual(result['harassment_mute_threshold'], 5)
        self.assertEqual(result['harassment_mute_duration_minutes'], 25)

        answer_cfg = self.call('GET', 'answer-settings')
        answer_cfg['enabled'] = not answer_cfg['enabled']
        self.call('PUT', 'answer-settings', answer_cfg)
        self.assertEqual(self.call('GET', 'moderation-settings')['harassment_mute_enabled'], True)
        self.assertEqual(self.call('GET', 'moderation-settings')['harassment_mute_threshold'], 5)
        self.assertEqual(self.call('GET', 'moderation-settings')['harassment_mute_duration_minutes'], 25)

    def test_auto_mute_settings_reject_invalid_values(self):
        base = {'sensitive_words': []}
        for field, values in (
            ('harassment_mute_enabled', [1, 'true']),
            ('harassment_mute_threshold', [True, 0, 21]),
            ('harassment_mute_duration_minutes', [True, 0, 1441]),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(app.Problem) as ctx:
                        self.call('PUT', 'moderation-settings', base | {field: value})
                    self.assertEqual(ctx.exception.status, 400)

    def test_triggered_reports_are_deduplicated_and_include_legacy_recall_counts(self):
        payload = {'event_hash': 'a' * 64, 'terms': ['cbz', 'word']}
        self.call('POST', 'moderation-recalls', payload)
        self.call('POST', 'moderation-recalls', payload)
        self.call('POST', 'moderation-recalls', {'event_hash': 'b' * 64, 'terms': ['cbz']})
        self.assertEqual(self.call('GET', 'moderation-recalls'), {
            'total': 2, 'by_word': [{'word': 'cbz', 'count': 2}, {'word': 'word', 'count': 1}],
            'pending_candidates': 0})

    def test_moderation_trigger_creates_one_admin_trace_with_message_context(self):
        kb_id = self.call('POST', 'bases', {'name': '撤回 Trace 测试'})['id']
        payload = {
            'event_hash': '9' * 64, 'terms': ['cbz'], 'candidates': ['新短语'],
            'trace_meta': {'kb_id': kb_id, 'group_id': 'group_1', 'user_id': 'user_2',
                           'message_id': 'message.3', 'content': '有人发了 cbz'},
        }
        first = self.call('POST', 'moderation-recalls', payload)
        second = self.call('POST', 'moderation-recalls', payload)
        self.assertEqual(first['trace_id'], second['trace_id'])
        listed = app.api('GET', '/knowledge/api/traces', {}, {'mode': ['moderation']})
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['items'][0]['id'], first['trace_id'])
        with app.db() as c:
            row = c.execute('SELECT * FROM answer_traces WHERE id=?', (first['trace_id'],)).fetchone()
            self.assertEqual(row['mode'], 'moderation')
            self.assertEqual(row['reason'], 'sensitive_word_triggered')
            self.assertEqual(row['origin'], 'qq_group')
            self.assertEqual(row['group_id'], 'group_1')
            self.assertEqual(row['user_id'], 'user_2')
            self.assertEqual(row['delivery'], 'not_applicable')
            self.assertEqual(row['question'], '有人发了 cbz')
            import json
            details = json.loads(row['details'])
            self.assertEqual(details['moderation']['message_id'], 'message.3')
            self.assertEqual(details['moderation']['terms'], ['cbz'])
            self.assertEqual(details['moderation']['candidates'], ['新短语'])
            self.assertEqual(c.execute("SELECT count(*) FROM answer_traces WHERE mode='moderation'").fetchone()[0], 1)

    def test_candidates_are_idempotent_and_approval_adds_sensitive_word(self):
        payload = {'event_hash': 'c' * 64, 'terms': ['和你做爱'], 'candidates': ['和你做爱']}
        self.call('POST', 'moderation-recalls', payload)
        self.call('POST', 'moderation-recalls', payload)
        self.assertEqual(self.call('GET', 'moderation-candidates')['items'][0]['hit_count'], 1)
        self.call('POST', 'moderation-candidates', {'term': '和你做爱', 'decision': 'approve'})
        self.assertIn('和你做爱', self.call('GET', 'moderation-settings')['sensitive_words'])
        self.assertEqual(self.call('GET', 'moderation-candidates')['items'], [])
        self.assertEqual(self.call('GET', 'moderation-recalls')['pending_candidates'], 0)

    def test_approving_candidate_already_covered_by_variant_does_not_duplicate_rule(self):
        self.configure(['(A/B)'])
        self.call('POST', 'moderation-recalls', {
            'event_hash': 'f' * 64, 'terms': ['B'], 'candidates': ['B']})
        self.call('POST', 'moderation-candidates', {'term': 'B', 'decision': 'approve'})
        self.assertEqual(self.call('GET', 'moderation-settings')['sensitive_words'], ['(A/B)'])

    def test_rejected_candidates_stay_out_of_sensitive_words(self):
        self.call('POST', 'moderation-recalls', {
            'event_hash': 'd' * 64, 'terms': ['乱讲'], 'candidates': ['乱讲']})
        self.call('POST', 'moderation-candidates', {'term': '乱讲', 'decision': 'reject'})
        self.assertNotIn('乱讲', self.call('GET', 'moderation-settings')['sensitive_words'])
        self.assertEqual(self.call('GET', 'moderation-candidates')['items'], [])

    def test_invalid_terms_and_event_ids_are_rejected(self):
        with self.assertRaises(app.Problem) as ctx:
            self.configure(['x' * 81])
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(app.Problem) as ctx:
            self.call('POST', 'moderation-recalls', {'event_hash': 'bad', 'terms': ['cbz']})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(app.Problem) as ctx:
            self.call('POST', 'moderation-recalls', {'event_hash': 'c' * 64, 'terms': []})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(app.Problem) as ctx:
            self.call('POST', 'moderation-recalls', {
                'event_hash': 'e' * 64, 'terms': ['ok'], 'candidates': ['x' * 81]})
        self.assertEqual(ctx.exception.status, 400)


if __name__ == '__main__':
    unittest.main()
