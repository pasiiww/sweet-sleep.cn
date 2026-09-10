import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import answers
import maintenance as m
import server as app

def call(name,args):
    return {'role':'assistant','content':None,'reasoning_content':'private thinking',
            'tool_calls':[{'id':'call1','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.initialize()
        self.kb=app.api('POST','/knowledge/api/bases',{'name':'测试'},{})['id']
        with app.db() as c:
            m.settings(c,{'openids':['authorized-user']})
            c.execute("UPDATE app_settings SET value=? WHERE name='answer'",(json.dumps(answers.defaults()|{'api_key':'mock-secret'}),))
            self.qa=app.save_qa(c,self.kb,{'question':'凯伊怎么下单','answer':'旧答案'})
        self.data={'kb_id':self.kb,'user_id':'authorized-user','message_id':'msg1','query':'/modify qa 凯伊改为淘宝下单'}
    def tearDown(self):self.temp.cleanup()
    def run_cmd(self,**extra):return m.respond(app,self.data|extra)
    def test_permission_before_model(self):
        with patch.object(answers,'tool_turn') as model:
            result=self.run_cmd(user_id='unauthorized')
            self.assertFalse(result['active']);model.assert_not_called()
    def test_modify_and_idempotent_history(self):
        rid=str(self.qa['id'])
        with patch.object(answers,'tool_turn',side_effect=[call('read_record',{'id':rid}),call('update_record',{'id':rid,'question':self.qa['question'],'answer':'淘宝下单'})]) as model:
            result=self.run_cmd()
            self.assertEqual(result['reason'],'saved')
            self.assertEqual(model.call_args_list[1].args[1][-2]['reasoning_content'],'private thinking')
            again=self.run_cmd();self.assertEqual(result,again);self.assertEqual(model.call_count,2)
        with app.db() as c:
            row=c.execute('SELECT * FROM qa_entries WHERE id=?',(rid,)).fetchone()
            self.assertEqual(row['answer'],'淘宝下单');self.assertEqual(row['updated_by'],'private_admin_ai')
            trace=json.loads(c.execute('SELECT details FROM answer_traces WHERE id=?',(result['trace_id'],)).fetchone()[0])
            self.assertEqual(trace['maintenance']['operations'][-1]['result']['before']['answer'],'旧答案')
        with patch.object(answers,'tool_turn',return_value={'role':'assistant','content':'要改成什么？'}) as model:
            self.run_cmd(message_id='msg2',query='运费也改一下')
            self.assertIn('已修改',model.call_args.args[1][2]['content'])
        self.assertFalse(self.run_cmd(message_id='msg3',query='/退出')['active'])
    def test_wrong_scope_and_unread_write(self):
        with patch.object(answers,'tool_turn',return_value=call('update_record',{'id':str(self.qa['id']),'question':'x','answer':'bad'})):
            self.assertEqual(self.run_cmd()['reason'],'error')
        with app.db() as c:
            other=app.api('POST','/knowledge/api/bases',{'name':'另一个'},{})['id']
            with self.assertRaises(app.Problem):m.execute(app,c,other,'qa','read_record',{'id':str(self.qa['id'])},{})
    def test_concurrent_edit_rejected(self):
        with app.db() as c:
            read={};rid=str(self.qa['id']);m.execute(app,c,self.kb,'qa','read_record',{'id':rid},read)
            app.save_qa(c,self.kb,{'question':'新Q','answer':'人工新答案'},self.qa['id'])
            with self.assertRaises(app.Problem) as exc:m.execute(app,c,self.kb,'qa','update_record',{'id':rid,'question':'覆盖','answer':'覆盖'},read)
            self.assertEqual(exc.exception.status,409)
    def test_product_add_and_invalid_tool(self):
        product={'series':'演示','characters':['凯伊'],'image':'','notes':'','types':[{'name':'立牌','price':'20'}],'links':[],'searchable':False}
        with patch.object(answers,'tool_turn',return_value=call('add_product',product)):
            result=self.run_cmd(query='/add 商品库 凯伊立牌20元')
            self.assertEqual(result['reason'],'saved');self.run_cmd(query='/add 商品库 凯伊立牌20元')
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM circle_products').fetchone()[0],1)
            with self.assertRaises(app.Problem):m.execute(app,c,self.kb,'qa','add_product',product,{})
    def test_document_update_retains_source_and_indexes(self):
        with app.db() as c:
            doc=app.save_document(c,app.base(c,self.kb),{'title':'演示说明','content':'原文','source':'人工来源'})
        with patch.object(answers,'tool_turn',side_effect=[call('read_record',{'id':doc['id']}),call('update_record',{'id':doc['id'],'title':'演示说明','content':'新说明','source':'人工来源'})]):
            self.assertEqual(self.run_cmd(query='/modify 知识库 修改演示说明')['reason'],'saved')
        with app.db() as c:
            row=c.execute('SELECT * FROM documents WHERE id=?',(doc['id'],)).fetchone()
            self.assertEqual(row['content'],'新说明');self.assertEqual(row['source'],'人工来源')
            self.assertEqual(c.execute('SELECT content FROM chunks WHERE doc_id=?',(doc['id'],)).fetchone()[0],'新说明')

    def test_tool_protocol_reasoning_not_in_trace(self):
        response={'choices':[{'finish_reason':'tool_calls','message':call('search_records',{'query':'凯伊'})}]}
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps(response).encode()
        trace={'model_calls':[]}
        with patch.object(answers.request,'build_opener') as opener:
            opener.return_value.open.return_value=Response()
            msg=answers.tool_turn(answers.defaults()|{'api_key':'mock','_trace':trace},[{'role':'user','content':'test'}],m.tools_for('qa'))
            payload=json.loads(opener.return_value.open.call_args.args[0].data)
            self.assertFalse(payload['parallel_tool_calls']);self.assertEqual(payload['reasoning_effort'],'low')
            self.assertEqual(msg['reasoning_content'],'private thinking')
            self.assertNotIn('private thinking',json.dumps(trace))
