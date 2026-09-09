import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import request, error

import server as app


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()
        self.kb = self.call('POST', 'bases', {'name': '产品文档'})['id']

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, body=None, params=None):
        return app.api(method, '/knowledge/api/' + path, body or {}, params or {})

    def doc(self, content='退款申请请联系客服，提供订单号。', title='退款政策', kb=None):
        return self.call('POST', f'bases/{kb or self.kb}/documents', {'title': title, 'content': content})['id']

    def search(self, query='退款', **extra):
        return self.call('POST', 'retrieve', {'kb_id': self.kb, 'query': query, **extra})

    def test_chinese_crud_index_and_isolation(self):
        doc = self.doc()
        other = self.call('POST', 'bases', {'name': '其他知识库'})['id']
        self.doc('退款专属秘密记录', kb=other)
        found = self.search()
        self.assertEqual([r['document_id'] for r in found['results']], [doc])
        self.assertIn('订单号', found['context'])
        self.call('PUT', 'documents/' + doc, {'title': '账户操作', 'content': '登录账户请使用邮箱验证码'})
        self.assertEqual(self.search()['results'], [])
        self.assertEqual(len(self.search('验证码')['results']), 1)
        self.call('DELETE', 'documents/' + doc)
        self.assertEqual(self.search('验证码')['results'], [])

    def test_base_delete_cascades_fts(self):
        self.doc()
        self.call('DELETE', 'bases/' + self.kb)
        with app.db() as c:
            for table in ('documents', 'chunks', 'chunk_fts'):
                self.assertEqual(c.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_persistence_and_document_search(self):
        doc = self.doc()
        app.initialize()
        self.assertEqual(self.call('GET', 'documents/' + doc)['title'], '退款政策')
        self.assertEqual(len(self.call('GET', f'bases/{self.kb}/documents', params={'q': ['订单']})['items']), 1)

    def test_context_budget_and_safe_query(self):
        self.doc('退款政策。' * 500)
        result = self.search(max_context_chars=100)
        self.assertLessEqual(len(result['context']), 100)
        self.assertTrue(result['results'][0]['truncated'])
        self.assertEqual(self.search('" OR * - :')['results'], [])

    def test_invalid_values_and_missing_model(self):
        for body in ({'name': 'x', 'chunk_size': 100, 'overlap': 50}, {'name': 'x', 'top_k': True}):
            with self.assertRaises(app.Problem):
                self.call('POST', 'bases', body)
        self.doc()
        with self.assertRaises(app.Problem) as ctx:
            self.search(mode='vector')
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(app.Problem):
            self.search(top_k=100)

    def set_model(self, **extra):
        with patch.object(app, 'validate_url'):
            return self.call('PUT', 'settings', {'base_url': 'https://example.com/v1', 'model': 'test', 'api_key': 'secret', **extra})

    def test_vectors_hybrid_staleness_and_config_secret(self):
        self.doc()
        self.assertNotIn('api_key', self.set_model())
        with patch.object(app, 'embed', side_effect=lambda texts, cfg: [[1.0, 0.0] for _ in texts]):
            self.call('POST', f'bases/{self.kb}/embed')
            self.assertEqual(len(self.search(mode='vector')['results']), 1)
            self.assertEqual(self.search(mode='hybrid')['score_type'], 'rrf')
            self.set_model(model='new-model')
            with self.assertRaises(app.Problem):
                self.search(mode='vector')
        with patch.object(app, 'validate_url'):
            self.call('PUT', 'settings', {'base_url': 'https://new.example.com/v1', 'model': 'test'})
        with app.db() as c:
            self.assertEqual(app.config(c)['api_key'], '')

    def test_chunking_covers_original_and_update_invalidates_vectors(self):
        text = ''.join(str(i % 10) for i in range(5000))
        parts = app.split_text(text, 600, 80)
        self.assertTrue(all(len(p) <= 600 for p in parts))
        self.assertEqual(parts[0] + ''.join(p[80:] for p in parts[1:]), text)
        doc = self.doc(text)
        self.set_model()
        with patch.object(app, 'embed', side_effect=lambda texts, cfg: [[1.0, 0.0] for _ in texts]):
            self.call('POST', f'bases/{self.kb}/embed')
        self.call('PUT', 'bases/' + self.kb, {'name': 'new', 'chunk_size': 300, 'overlap': 40})
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(self.call('GET', 'documents/' + doc)['content'], text)

    def test_embedding_response_contract(self):
        cfg = {'base_url': 'https://example.com/v1', 'model': 'test', 'api_key': ''}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return json.dumps({'data': [{'index': 1, 'embedding': [0, 3]}, {'index': 0, 'embedding': [2, 0]}]}).encode()
        with patch.object(app, 'validate_url'), patch.object(app.request, 'build_opener') as opener:
            opener.return_value.open.return_value = Response()
            self.assertEqual(app.embed(['a', 'b'], cfg), [[1, 0], [0, 1]])
            sent = opener.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(sent.data)['input'], ['a', 'b'])

    def test_http_auth_and_static_allowlist(self):
        self.doc()
        app.ADMIN_TOKEN, app.READ_TOKEN, app.LEARN_TOKEN = 'a' * 32, 'r' * 32, 'l' * 32
        http = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        def req(path, token='', body=None):
            payload = json.dumps(body).encode() if body is not None else None
            r = request.Request(f'http://127.0.0.1:{http.server_port}/knowledge/' + path, data=payload,
                                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            try:
                with request.urlopen(r) as response: return response.status, response.read()
            except error.HTTPError as exc:
                return exc.code, exc.read()
        try:
            self.assertEqual(req('api/bases')[0], 401)
            self.assertEqual(req('api/learning/events',app.READ_TOKEN,{'kb_id':self.kb})[0],403)
            self.assertEqual(req('api/learning/events',app.LEARN_TOKEN,{'kb_id':self.kb})[0],200)
            self.assertEqual(req('api/bases',app.LEARN_TOKEN)[0],403)
            self.assertEqual(req('api/learning/jobs?kb_id='+self.kb,app.READ_TOKEN)[0],403)
            self.assertEqual(req('api/bases', app.READ_TOKEN)[0], 403)
            self.assertEqual(req('api/answer-settings', app.READ_TOKEN)[0], 403)
            self.assertEqual(req('api/traces', app.READ_TOKEN)[0], 403)
            self.assertEqual(req(f'api/bases/{self.kb}/qa', app.READ_TOKEN)[0], 403)
            self.assertEqual(req('api/qa/1', app.READ_TOKEN)[0], 403)
            self.assertEqual(req('api/traces/example', app.READ_TOKEN)[0], 403)
            self.assertEqual(req('api/traces', app.ADMIN_TOKEN)[0], 200)
            self.assertEqual(req('api/trace-delivery', app.READ_TOKEN, {'trace_id':'unknown','receipt':'bad','status':'delivered'})[0], 403)
            self.assertEqual(req('api/answer-settings')[0], 401)
            self.assertEqual(req('api/bases', app.ADMIN_TOKEN)[0], 200)
            self.assertEqual(req('api/retrieve', app.READ_TOKEN, {'kb_id': self.kb, 'query': '退款'})[0], 200)
            status, body = req('api/answer', app.READ_TOKEN, {'kb_id': self.kb, 'query': '退款'})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['reason'], 'missing_key')
            self.assertEqual(req('../services/knowledge/server.py')[0], 404)
            self.assertEqual(req('knowledge.db')[0], 404)
            self.assertEqual(req('')[0], 200)
        finally:
            http.shutdown(); http.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
