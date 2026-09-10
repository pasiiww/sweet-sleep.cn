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
    def test_multi_character_variant_and_legacy(self):
        data=dict(kb_id=self.kb,series='蕾服',characters=['爱丽丝','凯伊'],types=[{'name':'立牌','price':'30'},{'name':'套组','price':''}],searchable=True,
                  variants=[{'character':'凯伊','type':'立牌','price':'35','status':'在售'},{'character':'爱丽丝','type':'套组','price':'','status':'不售卖'}])
        r=self.save(data)
        self.assertEqual(r['characters'],['爱丽丝','凯伊'])
        with app.db() as c:
            content=c.execute('SELECT content FROM documents WHERE id=?',(r['document_id'],)).fetchone()[0]
            self.assertIn('凯伊 / 立牌：在售；参考价35元',content)
            self.assertIn('爱丽丝 / 套组：不售卖',content)
            self.assertIn('不代表任意组合都有货',content)
        with self.assertRaises(app.Problem):self.save(data|{'variants':[{'character':'不存在','type':'立牌','price':'','status':'在售'}]})
        legacy=self.save(dict(kb_id=self.kb,character='日奈',types=[{'name':'徽章','price':'20'}]))
        self.assertEqual(legacy['characters'],['日奈'])
