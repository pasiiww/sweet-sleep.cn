import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import answers
import server as app

class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.initialize()
        self.kb=app.api('POST','/knowledge/api/bases',{'name':'retry'}, {})['id']
        app.api('POST',f'/knowledge/api/bases/{self.kb}/qa',{'question':'凯伊定金是多少','answer':'20元'}, {})
        with app.db() as c:
            cfg=app.answer_config(c)|{'api_key':'mock-key'};c.execute("UPDATE app_settings SET value=? WHERE name='answer'",(json.dumps(cfg),))
    def tearDown(self):self.temp.cleanup()
    def test_retry_empty_case_qa_hints_and_same_date_for_both_stages(self):
        with patch.object(answers,'_model_call',side_effect=['{"query_groups":[["不存在的词"]]}','{"query_groups":[["凯伊","定金"]]}','定金20元']) as model:
            r=app.respond({'kb_id':self.kb,'query':'付款途径'})
        self.assertEqual(r['mode'],'model');self.assertEqual(model.call_count,3)
        payloads=[json.loads(call.args[1][-1]['content']) for call in model.call_args_list]
        self.assertEqual(payloads[0]['qa_hints'],['凯伊定金是多少'])
        self.assertEqual(payloads[1]['empty_retrieval']['failed_query_groups'],[['不存在的词']])
        self.assertEqual(payloads[0]['current_date'],payloads[2]['current_date']);self.assertIn('星期',payloads[0]['current_date'])
        d=app.api('GET','/knowledge/api/traces/'+r['trace_id'],{}, {})['details']
        self.assertEqual(len(d['retrievals']),2);self.assertEqual(d['model_calls'][1]['stage'],'keywords_retry')
    def test_no_retry_when_hit_and_retry_capped_on_empty(self):
        for group,count in [('凯伊',2),('不存在',3)]:
            outputs=['{"query_groups":[["'+group+'"]]}']*(count-1)+['不知道 [[HANDOFF]]']
            with patch.object(answers,'_model_call',side_effect=outputs) as model:
                app.respond({'kb_id':self.kb,'query':'问题'})
            self.assertEqual(model.call_count,count)
    def test_date_uses_beijing_weekday(self):
        from datetime import datetime
        with patch.object(answers.time,'time',return_value=datetime.fromisoformat('2026-09-10T00:01:00+08:00').timestamp()):
            self.assertEqual(answers.current_date(),'2026年09月10日 星期四（北京时间）')
