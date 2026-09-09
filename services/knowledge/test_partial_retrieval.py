import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import server as app
import answers

class PartialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.initialize()
        self.kb=app.api('POST','/knowledge/api/bases',{'name':'partial'}, {})['id']
    def tearDown(self):self.temp.cleanup()
    def add(self,kind,**data):return app.api('POST',f'/knowledge/api/bases/{self.kb}/{kind}',data,{})
    def test_partial_qa_and_documents_full_match_ranked_first(self):
        single=self.add('documents',title='下单指南',content='请在店铺购买')
        double=self.add('documents',title='凯伊',content='下单请咨询客服')
        full=self.add('qa',question='凯伊娃娃如何下单',answer='联系客服即可')
        self.add('qa',question='没有关键词',answer='凯伊娃娃下单')
        result=app.retrieve({'kb_id':self.kb,'query_groups':[['凯伊','娃娃','下单']],'top_k':10})['results']
        self.assertEqual(result[0]['qa_id'],full['id'])
        self.assertEqual(result[1]['document_id'],double['id'])
        self.assertEqual(result[2]['document_id'],single['id']);self.assertEqual(len(result),3)
    def test_original_query_retrieves_content_missing_generated_terms(self):
        target=self.add('documents',title='毛绒下单说明',content='通过店铺首页选购')
        result=app.search_terms(self.kb,[['模型漏掉关键词']],original_query='毛绒怎么下单啊？')
        self.assertTrue(any(r['document_id']==target['id'] for r in result['results']))
        self.assertEqual(result['searches'][0]['kind'],'original')
    def test_up_to_six_keywords_and_deduplicated_scoring(self):
        group=['凯伊','娃娃','毛绒','下单','购买','订购']
        self.assertEqual(answers.normalize_query_groups([group]),[group])
        self.add('documents',title='凯伊',content='娃娃下单')
        a=app.retrieve({'kb_id':self.kb,'query_groups':[['凯伊','下单']]})
        b=app.retrieve({'kb_id':self.kb,'query_groups':[['凯伊','下单'],['凯伊']]})
        self.assertEqual(a['results'][0]['score'],b['results'][0]['score'])
