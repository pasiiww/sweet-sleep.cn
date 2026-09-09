import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import answers
import server as app


class QATests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        app.DATA=Path(self.temp.name)
        app.initialize()
        self.kb=self.api('POST','bases',{'name':'QA 测试'})['id']
        self.path=f'bases/{self.kb}/qa'
        self.api('PUT',f'bases/{self.kb}/entities',{'items':[{'name':'凯伊','aliases':['kei']}]})

    def tearDown(self): self.temp.cleanup()

    def api(self, method, path, data=None, params=None):
        return app.api(method,'/knowledge/api/'+path,data or {},params or {})

    def retrieve(self, **data):
        return self.api('POST','retrieve',{'kb_id':self.kb,**data})

    def test_only_question_is_indexed_and_crud_updates_index(self):
        item=self.api('POST',self.path,{'question':'凯伊价格','answer':'答案专有词 answeronlytoken'})
        self.assertFalse(self.retrieve(query='answeronlytoken')['results'])
        self.assertFalse(self.retrieve(query_groups=[['凯伊','answeronlytoken']])['results'])
        result=self.retrieve(query_groups=[['kei','价格']])['results']
        self.assertEqual(result[0]['source_type'],'qa')
        self.assertEqual(result[0]['content'],'答案专有词 answeronlytoken')
        self.assertEqual(result[0]['question'],'凯伊价格')
        updated=self.api('PUT',f'qa/{item["id"]}',{'question':'配送规则','answer':'updatedanswer','revision':item['revision']})
        self.assertFalse(self.retrieve(query_groups=[['凯伊','价格']])['results'])
        self.assertTrue(self.retrieve(query='配送规则')['results'])
        self.assertFalse(self.retrieve(query='updatedanswer')['results'])
        with self.assertRaises(app.Problem):
            self.api('PUT',f'qa/{item["id"]}',{'question':'旧版本','answer':'旧版本','revision':item['revision']})
        self.api('DELETE',f'qa/{item["id"]}',{'revision':updated['revision']})
        self.assertFalse(self.retrieve(query='配送规则')['results'])

    def test_mixed_retrieval_and_model_reference_contract(self):
        self.api('POST',self.path,{'question':'凯伊价格定金','answer':'定金20元。'})
        self.api('POST',f'bases/{self.kb}/documents',{'title':'凯伊','content':'价格100元。'})
        self.api('PUT','answer-settings',{'enabled':True,'model':'deepseek-v4-flash','api_key':'test-key','system_prompt':answers.DEFAULT_PROMPT})
        with patch.object(answers,'_model_call',side_effect=['{"query_groups":[["凯伊","价格"]]}','总价100元，定金20元。']) as model:
            result=self.api('POST','answer',{'kb_id':self.kb,'query':'kei价格'})
        self.assertEqual({row['source_type'] for row in result['results']},{'qa','document'})
        payload=json.loads(model.call_args_list[1].args[1][-1]['content'])
        self.assertEqual(payload['retrieved_qa'],[{'question':'凯伊价格定金','answer':'定金20元。'}])
        self.assertEqual(payload['retrieved_documents'],[{'title':'凯伊','content':'价格100元。'}])
        self.assertNotIn('来源',result['answer'])
        trace=self.api('GET','traces/'+result['trace_id'])
        self.assertEqual({r['source_type'] for r in trace['details']['retrievals'][0]['results']},{'qa','document'})

    def test_isolation_and_question_only_admin_search(self):
        item=self.api('POST',self.path,{'question':'凯伊价格','answer':'answeronlytoken'})
        other=self.api('POST','bases',{'name':'另一库'})['id']
        self.assertFalse(self.api('POST','retrieve',{'kb_id':other,'query':'凯伊价格'})['results'])
        self.assertEqual(self.api('GET',self.path,params={'q':['answeronlytoken']})['total'],0)
        self.api('DELETE','bases/'+self.kb)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM qa_entries').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM qa_fts').fetchone()[0],0)
        with self.assertRaises(app.Problem):self.api('GET',f'qa/{item["id"]}')

    def test_fallback_has_no_source_and_handoff_uses_name(self):
        self.api('POST',self.path,{'question':'凯伊价格','answer':'价格100元。'})
        result=self.api('POST','answer',{'kb_id':self.kb,'query':'凯伊价格'})
        self.assertEqual(result['answer'],'价格100元。')
        self.assertEqual(result['mode'],'document')
        result=self.api('POST','answer',{'kb_id':self.kb,'query':'unknownnothing'})
        self.assertIn('落落',result['answer'])
        self.assertNotIn('471718054',result['answer'])
        self.assertNotIn('知识库里没有足够',result['answer'])
        self.assertIn('♡',result['answer'])
        cfg=answers.defaults() | {'handoff_groups':{'group':['openid']}}
        self.assertEqual(answers.handoff(cfg,'group','no_results')['mention_openids'],['openid'])

    def test_pagination_and_limits(self):
        for i in range(22):self.api('POST',self.path,{'question':f'问题{i}','answer':'答案'})
        self.assertEqual(len(self.api('GET',self.path)['items']),20)
        self.assertEqual(len(self.api('GET',self.path,params={'offset':['20']})['items']),2)
        for body in ({'question':'','answer':'A'},{'question':'Q','answer':''},{'question':'Q'*1001,'answer':'A'}):
            with self.assertRaises(app.Problem):self.api('POST',self.path,body)


if __name__=='__main__':unittest.main()
