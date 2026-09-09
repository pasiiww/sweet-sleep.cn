import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import server as app
import answers
import stickers

class StickerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.initialize()
 def tearDown(self):self.temp.cleanup()
 def api(self,m,p='stickers',d=None):return app.api(m,'/knowledge/api/'+p,d or {},{})
 def test_crud_disabled_and_revision(self):
  row=self.api('POST',d={'name':'开心','path':'/menu/assets/kei.jpg'})
  self.assertEqual(row['url'],'https://sweet-sleep.cn/menu/assets/kei.jpg')
  with app.db() as c:self.assertEqual(len(stickers.available(c)),1)
  updated=self.api('PUT','stickers/'+str(row['id']),dict(row,enabled=False))
  with app.db() as c:self.assertEqual(stickers.available(c),[])
  with self.assertRaises(Exception):self.api('PUT','stickers/'+str(row['id']),dict(row,enabled=True))
  self.api('DELETE','stickers/'+str(row['id']));self.assertEqual(self.api('GET')['items'],[])
 def test_paths(self):
  for bad in ['http://example.com/a.jpg','https://127.0.0.1/a.jpg','https://localhost/a.jpg','/../a.png','//example.com/a.jpg','/a.svg','https://a.com:80/a.jpg','https://u:p@a.com/a.jpg']:
   with self.subTest(bad=bad),self.assertRaises(ValueError):stickers.image_url(bad)
  self.assertEqual(stickers.image_url('/www/wwwroot/myweb/a.jpg'),'https://sweet-sleep.cn/a.jpg')
  self.assertIn('%',stickers.image_url('/开心.png'))
 def test_model_whitelist_and_text(self):
  cfg={'system_prompt':'客服','stickers':[{'name':'开心'}]}
  with patch.object(answers,'model_call',return_value='好呀 [[STICKER:未知]] [[STICKER:开心]]'):
   self.assertEqual(answers.complete(cfg,'你好',[]),{'supported':True,'answer':'好呀','sticker_name':'开心'})
  with patch.object(answers,'model_call',return_value='好呀 [[STICKER:未知]]'):
   self.assertEqual(answers.complete(cfg,'你好',[]),{'supported':True,'answer':'好呀'})
 def test_pipeline_descriptor(self):
  row=self.api('POST',d={'name':'开心','path':'/a.jpg'})
  kb=self.api('POST','bases',{'name':'测试'})['id']
  self.api('POST',f'bases/{kb}/documents',{'title':'营业','content':'营业时间十点至八点'})
  self.api('PUT','answer-settings',{'enabled':True,'api_key':'mock-key','model':'mock-model','system_prompt':'客服'})
  with patch.object(answers,'keywords',return_value=[['营业']]),patch.object(answers,'complete',return_value={'supported':True,'answer':'十点营业呀','sticker_name':'开心'}):
   reply=self.api('POST','answer',{'kb_id':kb,'query':'营业时间'})
  self.assertEqual(reply['sticker']['url'],row['url']);self.assertNotIn('sticker_name',reply)
