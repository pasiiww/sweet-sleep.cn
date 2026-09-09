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
 def test_upload_and_new_marker(self):
  import base64
  raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')
  # Generate a structurally valid PNG with verified chunk CRCs.
  import struct,zlib
  def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
  raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\xff\x00\x00'))+chunk(b'IEND',b'')
  file=stickers.save_upload(app.DATA/'stickers',raw)
  self.assertEqual(file.read_bytes(),raw)
  self.assertRegex(file.name,r'^[a-f0-9]{32}\.png$')
  for bad in (b'<svg/>',b'bad',raw[:-5],b'x'*(stickers.MAX_UPLOAD+1)):
   with self.assertRaises(ValueError):stickers.save_upload(app.DATA/'stickers',bad)
  cfg={'system_prompt':'客服','stickers':[{'name':'玲纱-开心'},{'name':'收到'}]}
  with patch.object(answers,'model_call',return_value='好呀[玲纱-开心][收到]') as model:
   self.assertEqual(answers.complete(cfg,'你好',[]),{'supported':True,'answer':'好呀','sticker_name':'玲纱-开心'})
   self.assertIn('[玲纱-开心]',model.call_args.args[1][0]['content'])
  with patch.object(answers,'model_call',return_value='好呀'):
   self.assertNotIn('sticker_name',answers.complete(cfg,'你好',[]))
 def test_upload_http_auth_public_download_and_cleanup(self):
  import threading,struct,zlib
  from http.server import ThreadingHTTPServer
  from urllib.request import Request,urlopen
  from urllib.error import HTTPError
  import json
  def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
  raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\xff\x00\x00'))+chunk(b'IEND',b'')
  http=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
  thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
  endpoint='http://127.0.0.1:'+str(http.server_port)
  try:
   with patch.object(app,'ADMIN_TOKEN','upload-admin'),patch.object(app,'READ_TOKEN','upload-reader'):
    for token in ('','upload-reader'):
     with self.assertRaises(HTTPError) as error:urlopen(Request(endpoint+'/knowledge/api/stickers/upload?name=test',data=raw,headers={'Authorization':'Bearer '+token}))
     self.assertIn(error.exception.code,(401,403))
    request=Request(endpoint+'/knowledge/api/stickers/upload?name=test',data=raw,headers={'Authorization':'Bearer upload-admin','Content-Type':'image/png'})
    with urlopen(request) as response:row=json.load(response)
    with urlopen(endpoint+row['path']) as response:
     self.assertEqual(response.headers['Content-Type'],'image/png');self.assertEqual(response.read(),raw)
    with self.assertRaises(HTTPError) as error:urlopen(request)
    self.assertEqual(error.exception.code,409)
    self.assertEqual(len(list((app.DATA/'stickers').iterdir())),1)
    with self.assertRaises(HTTPError):urlopen(endpoint+'/knowledge/sticker-files/../../knowledge.db')
  finally:http.shutdown();http.server_close();thread.join()
