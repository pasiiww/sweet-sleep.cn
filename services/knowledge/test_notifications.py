import tempfile
from pathlib import Path
import unittest
import server as app
import notifications

class NotificationsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.initialize()
  self.kb=self.api('POST','bases',{'name':'测试'})['id']
 def tearDown(self):self.temp.cleanup()
 def api(self,m,p,d=None):return app.api(m,'/knowledge/api/'+p,d or {},{})
 def test_transactional_updates_and_delivery(self):
  doc=self.api('POST',f'bases/{self.kb}/documents',{'title':'说明','content':'旧内容'})
  self.assertEqual(self.api('GET','owner-notifications')['items'],[])
  self.api('PUT','owner-notifications',{'enabled':True,'openid':'owner_c2c_openid'})
  self.api('PUT','documents/'+doc['id'],{'title':'说明','content':'新内容'})
  item=self.api('POST','owner-notifications/claim')
  self.assertEqual(item['openid'],'owner_c2c_openid');self.assertIn('新内容',item['content'])
  self.assertEqual(self.api('POST','owner-notifications/claim'),{})
  self.api('POST','owner-notifications/ack',{'receipt':item['receipt'],'status':'delivered'})
  self.assertEqual(self.api('GET','owner-notifications')['items'][0]['status'],'delivered')
 def test_pending_qa_not_notified_approval_is(self):
  self.api('PUT','owner-notifications',{'enabled':True,'openid':'owner_c2c_openid'})
  with app.db() as c:
   c.execute("INSERT INTO qa_entries(kb_id,question,answer,revision,updated_at,publication) VALUES(?,?,?,?,?,?)",(self.kb,'问题','答案','rev',app.now(),'pending'))
   self.assertEqual(c.execute('SELECT count(*) FROM owner_notifications').fetchone()[0],0)
   c.execute("UPDATE qa_entries SET publication='active' WHERE kb_id=?",(self.kb,))
  item=self.api('POST','owner-notifications/claim');self.assertIn('答案',item['content'])
  self.api('POST','owner-notifications/ack',{'receipt':item['receipt'],'status':'failed','error':'Forbidden'})
  self.assertEqual(self.api('POST','owner-notifications/claim'),{})
  self.api('POST','owner-notifications/'+str(item['ids'][0])+'/retry')
  self.assertTrue(self.api('POST','owner-notifications/claim'))
 def test_bad_binding_and_rollback(self):
  with self.assertRaises(app.Problem):self.api('PUT','owner-notifications',{'enabled':True,'openid':'471718054'})
  self.api('PUT','owner-notifications',{'enabled':True,'openid':'owner_c2c_openid'})
  try:
   with app.db() as c:
    c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)',('rollback',self.kb,'标题','正文','',app.now()))
    raise RuntimeError()
  except RuntimeError:pass
  self.assertEqual(self.api('GET','owner-notifications')['items'],[])
