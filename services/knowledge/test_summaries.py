import unittest
from unittest.mock import patch
import summaries
import answers
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
            self.assertIn('明天发货',messages[1]['content'])
            self.assertNotIn('api_key',messages[1]['content'])

    def test_summary_disabled_and_failure(self):
        with patch.object(answers,'model_call') as model:
            self.assertFalse(self.call('POST','group-summary',{'kb_id':self.kb,'transcript':'聊天'})['ok'])
            model.assert_not_called()
        self.configure()
        with patch.object(answers,'model_call',side_effect=answers.ModelError('network_error')):
            self.assertFalse(self.call('POST','group-summary',{'kb_id':self.kb,'transcript':'聊天'})['ok'])
