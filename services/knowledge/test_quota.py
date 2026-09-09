import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import answers
import server as app

class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.initialize()
        self.kb=app.api('POST','/knowledge/api/bases',{'name':'quota'}, {})['id']
        self.guard=patch.object(answers,'_model_call',side_effect=AssertionError('mock only'));self.guard.start()
    def tearDown(self):self.guard.stop();self.temp.cleanup()
    def ask(self,origin='qq_group',user='same-user',group='one'):
        return app.respond({'kb_id':self.kb,'query':'测试','origin':origin,'user_id':user,'group_id':group})
    def test_twenty_shared_across_channels_and_no_pipeline_on_twenty_first(self):
        with patch.object(app,'respond_pipeline',return_value={'mode':'model','reason':'ok','answer':'mock'}) as pipeline:
            for i in range(20):
                r=self.ask('qq_private' if i%2 else 'qq_group',group=str(i))
                self.assertEqual(r['quota']['used'],i+1)
            r=self.ask('qq_private');self.assertEqual(r['mode'],'quota');self.assertEqual(pipeline.call_count,20)
            with app.db() as c:
                d=json.loads(c.execute('SELECT details FROM answer_traces WHERE id=?',(r['trace_id'],)).fetchone()[0])
                self.assertEqual(d['model_calls'],[]);self.assertEqual(d['retrievals'],[])
            self.assertEqual(self.ask(user='other')['mode'],'model')
            app.initialize();self.assertEqual(self.ask()['mode'],'quota')
    def test_concurrency_atomic_and_beijing_midnight_reset(self):
        # 2026-09-09 23:59:59 Beijing, one second before the daily reset.
        from datetime import datetime
        at=datetime.fromisoformat('2026-09-09T23:59:59+08:00').timestamp()
        with patch.object(app.time,'time',return_value=at):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(lambda _:app.reserve_daily_query('one'),range(30)))
            self.assertEqual(sum(r['allowed'] for r in results),20)
        with patch.object(app.time,'time',return_value=at+1):
            r=app.reserve_daily_query('one');self.assertEqual(r['used'],1);self.assertEqual(r['day'],'2026-09-10')
    def test_missing_qq_identity_rejected(self):
        with self.assertRaises(app.Problem):self.ask(user='')

class CacheLayoutTests(unittest.TestCase):
    def test_dynamic_alias_and_question_leave_prefix_identical_in_each_stage(self):
        history=[{'role':'user','content':'kei多少钱'},{'role':'assistant','content':'100元'}]
        cfg=answers.defaults()|{'conversation_history':history,'alias_context':'kei是凯伊的别名'}
        with patch.object(answers,'model_call',return_value='{"query_groups":[["凯伊","定金"]]}') as model:
            answers.keywords(cfg,'那定金呢')
            first=model.call_args.args[1]
            answers.keywords(cfg|{'alias_context':'不同的别名'},'那尾款呢')
            second=model.call_args.args[1]
            self.assertEqual(first[:-1],second[:-1]);self.assertEqual(first[1:-1],history)
        with patch.object(answers,'model_call',return_value='20元') as model:
            answers.complete(cfg,'那定金呢',[]);first=model.call_args.args[1]
            answers.complete(cfg|{'alias_context':'新的别名'},'那尾款呢',[]);second=model.call_args.args[1]
            self.assertEqual(first[:-1],second[:-1]);self.assertEqual(first[1:-1],history)
