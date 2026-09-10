import tempfile
from pathlib import Path
import unittest
import server as app

class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=app.DATA;app.DATA=Path(self.tmp.name);app.initialize()
        self.kb=app.api('POST','/knowledge/api/bases',{'name':'制品测试'},{})['id']
    def tearDown(self):app.DATA=self.old;self.tmp.cleanup()
    def save(self,data,method='POST',suffix=''):
        return app.api(method,'/knowledge/api/products'+suffix,data,{})
    def test_crud_sync_and_revision(self):
        data=dict(kb_id=self.kb,series='',character='凯伊',types=[{'name':'立牌','price':'25.50'},{'name':'徽章','price':''}],links=[{'name':'平台','url':'https://example.com/item'}],searchable=True)
        r=self.save(data)
        with app.db() as c:
            d=c.execute('SELECT * FROM documents WHERE id=?',(r['document_id'],)).fetchone()
            self.assertIn('仅供参考',d['content']);self.assertIn('25.50元',d['content'])
        self.assertEqual(len(app.api('GET','/knowledge/api/products',{}, {'kb_id':[self.kb],'q':['立牌']})['items']),1)
        updated=self.save(data|{'revision':r['revision'],'searchable':False},'PUT','/'+r['id'])
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM documents').fetchone()[0],0)
        with self.assertRaises(app.Problem):self.save(data|{'revision':r['revision']},'PUT','/'+r['id'])
        self.save({'revision':updated['revision']},'DELETE','/'+r['id'])
        self.assertEqual(app.api('GET','/knowledge/api/products',{}, {'kb_id':[self.kb]})['items'],[])
    def test_validation(self):
        data=dict(kb_id=self.kb,character='凯伊',types=[{'name':'立牌','price':'20'}])
        for extra in [{'types':[]},{'types':[{'name':'立牌','price':'NaN'}]},{'types':[{'name':'立牌','price':'-1'}]}, {'links':[{'name':'坏链接','url':'javascript:alert(1)'}]}, {'image':'javascript:alert(1)'}, {'types':[{'name':'立牌','price':''},{'name':'立牌','price':'20'}]}]:
            with self.assertRaises(app.Problem):self.save(data|extra)
