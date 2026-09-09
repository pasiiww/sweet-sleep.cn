import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import answers
import server as app
import traces


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.kb = self.api('POST', 'bases', {'name':'Trace 测试'})['id']
        self.api('POST', f'bases/{self.kb}/documents', {'title':'凯伊','content':'价格100元，定金20元。'})

    def tearDown(self):
        self.temp.cleanup()

    def api(self, method, path, body=None, params=None):
        return app.api(method, '/knowledge/api/'+path, body or {}, params or {})

    def ask(self, **extra):
        return self.api('POST', 'answer', {'kb_id':self.kb,'query':'凯伊价格', **extra})

    def test_stages_snapshot_and_redaction(self):
        self.api('PUT','answer-settings', {'enabled':True,'model':'deepseek-v4-flash','api_key':'private-api-secret', 'system_prompt':'请简洁回答 private-api-secret'})
        with patch.object(answers, '_model_call', side_effect=['{"query_groups":[["凯伊","价格"]]}','100元。']):
            result = self.ask(origin='preview')
        detail = self.api('GET','traces/'+result['trace_id'])
        self.assertEqual(detail['answer'],'100元。')
        self.assertEqual(detail['delivery'],'not_applicable')
        self.assertEqual([call['stage'] for call in detail['details']['model_calls']], ['keywords','answer'])
        self.assertEqual(detail['details']['retrievals'][0]['results'][0]['content'],'价格100元，定金20元。')
        self.assertEqual(detail['details']['retrievals'][0]['searches'][0]['query'],'凯伊价格')
        self.assertEqual(detail['details']['retrievals'][0]['searches'][0]['kind'],'original')
        self.assertEqual(detail['details']['retrievals'][0]['searches'][1]['query'],['凯伊','价格'])
        self.assertNotIn('private-api-secret',json.dumps(detail))
        self.assertNotIn('receipt_hash',detail)
        self.assertEqual(result['trace_receipt'],'')

    def test_handoff_and_errors_keep_diagnostics(self):
        self.api('PUT','answer-settings', {'enabled':True,'model':'deepseek-v4-flash','api_key':'secret-key','system_prompt':'请简洁回答'})
        with patch.object(answers, '_model_call', side_effect=['{"query_groups":[["凯伊","价格"]]}','[[HANDOFF]]']):
            result = self.ask()
        detail = self.api('GET','traces/'+result['trace_id'])
        self.assertEqual(detail['mode'],'handoff')
        self.assertTrue(detail['details']['retrievals'][0]['results'])
        with patch.object(answers, '_model_call', side_effect=answers.ModelError('insufficient_balance')):
            result = self.ask()
        detail = self.api('GET','traces/'+result['trace_id'])
        self.assertEqual(detail['reason'],'insufficient_balance')
        self.assertEqual(detail['details']['model_calls'][0]['error'],'insufficient_balance')
        with patch.object(app,'respond_pipeline',side_effect=RuntimeError('private error body')):
            with self.assertRaises(RuntimeError): self.ask(query='异常测试')
        rows = self.api('GET','traces',params={'mode':['error']})['items']
        self.assertEqual(len(rows),1)
        self.assertNotIn('private error body',json.dumps(self.api('GET','traces/'+rows[0]['id'])))

    def test_delivery_receipt_and_filtering(self):
        result = self.ask(origin='qq_group',user_id='member1',group_id='group1',session_id='session1')
        payload = {'trace_id':result['trace_id'],'receipt':'wrong','status':'delivered','content':'实际回复'}
        with self.assertRaises(app.Problem): self.api('POST','trace-delivery',payload)
        payload['receipt'] = result['trace_receipt']
        self.api('POST','trace-delivery',payload)
        detail = self.api('GET','traces/'+result['trace_id'])
        self.assertEqual(detail['delivery'],'delivered')
        self.assertEqual(detail['details']['delivery']['content'],'实际回复')
        self.assertNotIn(result['trace_receipt'],json.dumps(detail))
        for key,value in [('user_id','member1'),('group_id','group1'),('session_id','session1'),('q','凯伊'),('origin','qq_group')]:
            self.assertEqual(self.api('GET','traces',params={key:[value]})['total'],1)
        self.assertEqual(self.api('GET','traces',params={'user_id':['another']})['total'],0)

    def test_retention_pagination_and_cleanup(self):
        with app.db() as c:
            for i in range(32):
                trace_id,_ = traces.create(c,self.kb,str(i),{'origin':'api','user_id':'','group_id':'','session_id':''})
            c.execute('UPDATE answer_traces SET created=? WHERE id=?',(time.time()-traces.RETENTION-1,trace_id))
        self.assertEqual(self.api('GET','traces')['total'],31)
        self.assertEqual(len(self.api('GET','traces')['items']),30)
        self.assertEqual(len(self.api('GET','traces',params={'offset':['30']})['items']),1)
        with self.assertRaises(app.Problem): self.api('GET','traces/'+trace_id)
        with app.db() as c:
            traces.cleanup(c)
            self.assertEqual(c.execute('SELECT count(*) FROM answer_traces').fetchone()[0],31)
        self.assertEqual(self.api('GET','traces',params={'start':[str(time.time()+60)]})['total'],0)
        with self.assertRaises(app.Problem): self.api('GET','traces',params={'start':['nan']})


if __name__ == '__main__': unittest.main()
