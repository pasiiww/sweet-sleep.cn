import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from bot import KnowledgeBot, SeenMessages
from group_summary import GroupSummary, clean, LIMIT


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.seen = SeenMessages(':memory:')
        self.retriever = SimpleNamespace(kb_id='kb', summarize=AsyncMock(return_value={'ok':True,'answer':'讨论了活动安排。'}))
        self.bot = KnowledgeBot(self.retriever, self.seen)
        self.now = time.time()

    def tearDown(self):
        self.seen.conn.close()

    def message(self, mid, content, offset=0, group='g', name=''):
        return SimpleNamespace(id=mid, content=content, group_openid=group, sweet_sender_name=name,
            timestamp=datetime.fromtimestamp(self.now+offset, timezone.utc).isoformat(),
            author=SimpleNamespace(member_openid='u'), reply=AsyncMock(return_value={'id':'reply'}))

    async def test_command_checkpoint_success_no_model_on_empty_and_group_isolation(self):
        await self.bot.on_group_message_create(self.message('one','明天晚上八点公布结果',-3,name='超过十二字的群昵称样例长长长'))
        await self.bot.on_group_message_create(self.message('other','别的群的秘密',-2,'other'))
        query=self.message('q','/总结')
        await self.bot.on_group_message_create(query)
        self.assertIn('明天晚上八点',self.retriever.summarize.call_args.args[0])
        self.assertIn('超过十二字的群昵称样例长：',self.retriever.summarize.call_args.args[0])
        self.assertNotIn('秘密',self.retriever.summarize.call_args.args[0])
        await self.bot.on_group_at_message_create(query)
        self.assertEqual(self.retriever.summarize.await_count,1)
        await self.bot.on_group_message_create(self.message('q2','/总结',1))
        self.assertEqual(self.retriever.summarize.await_count,1)
        self.assertEqual(self.seen.conn.execute('SELECT count(*) FROM dialogue').fetchone()[0],0)

    async def test_model_or_delivery_failure_does_not_advance(self):
        self.bot.summaries.observe(self.message('one','群活动下周五举办',-1))
        self.retriever.summarize.return_value={'ok':False,'answer':'失败'}
        await self.bot.on_group_message_create(self.message('q','/总结'))
        self.assertIsNone(self.seen.conn.execute('SELECT * FROM summary_checkpoint').fetchone())
        self.retriever.summarize.return_value={'ok':True,'answer':'总结'}
        query=self.message('q2','/总结');query.reply.side_effect=RuntimeError('failed')
        await self.bot.on_group_message_create(query)
        self.assertIsNone(self.seen.conn.execute('SELECT * FROM summary_checkpoint').fetchone())

    def test_shortest_window_dedup_and_char_limit(self):
        for i in range(420):
            self.bot.summaries.observe(self.message(str(i),'内容'+str(i),-420+i))
        transcript,checkpoint,count=self.bot.summaries.snapshot('g',self.now)
        self.assertEqual(count,400)
        self.assertNotIn('：内容0\n',transcript)
        self.bot.summaries.delivered('g',(self.now-10,0))
        self.assertLessEqual(self.bot.summaries.snapshot('g',self.now)[2],10)
        self.bot.summaries.observe(self.message('long','甲'*LIMIT,1))
        text,_,_=self.bot.summaries.snapshot('g',self.now+2)
        self.assertLessEqual(len(text),LIMIT)
        self.assertNotIn('内容',text)
        self.assertEqual(self.bot.summaries.snapshot('g',self.now+36002)[2],0)

    def test_clean_and_no_raw_json_or_repeat(self):
        self.assertEqual(clean('{"text":"明天发货", "auth_token":"secret", "attachments":[]}'),'明天发货')
        self.assertEqual(clean('[图片] 😀 <face id="1"/>'),'')
        for i,text in enumerate(['明天发货！','明天发货','[表情包]','😀']):
            self.bot.summaries.observe(self.message(str(i),text,-4+i))
        self.assertEqual(self.bot.summaries.snapshot('g',self.now)[2],1)

    async def test_inflight_new_messages_survive_checkpoint(self):
        self.bot.summaries.observe(self.message('one','活动开始',-1))
        async def generate(_):
            self.bot.summaries.observe(self.message('later','活动新增事项',1))
            return {'ok':True,'answer':'活动开始'}
        self.retriever.summarize.side_effect=generate
        await self.bot.on_group_message_create(self.message('q','/总结'))
        text,_,count=self.bot.summaries.snapshot('g',self.now+2)
        self.assertEqual(count,1)
        self.assertIn('新增事项',text)

    def test_previous_summary_only_when_checkpoint_is_reached(self):
        store=self.bot.summaries
        store.delivered('g',(self.now-20,0),'上轮：发货日期尚未确定。')
        store.observe(self.message('new','现在确定周五发货',-1))
        text,_,_=store.snapshot('g',self.now)
        self.assertIn('上轮：发货日期尚未确定',text)
        self.assertIn('现在确定周五发货',text)
        self.assertIn('上轮',GroupSummary(self.seen.conn).snapshot('g',self.now)[0])
        store.delivered('g',(self.now-36001,0),'不该出现的过期总结')
        self.assertNotIn('过期总结',store.snapshot('g',self.now)[0])
        store.delivered('g',(self.now-500,0),'不该出现的超条数总结')
        for i in range(401):
            store.observe(self.message('many'+str(i),'不同发言'+str(i),-450+i))
        self.assertNotIn('超条数总结',store.snapshot('g',self.now)[0])

    def test_previous_summary_counts_toward_character_budget(self):
        store=self.bot.summaries
        store.delivered('g',(self.now-5,0),'旧总结'*300)
        store.observe(self.message('long','甲'*(LIMIT-100),-1))
        text,_,_=store.snapshot('g',self.now)
        self.assertLessEqual(len(text),LIMIT)
        self.assertNotIn('旧总结',text)

    async def test_actual_successful_reply_retained_and_failure_keeps_it(self):
        store=self.bot.summaries
        store.observe(self.message('one','第一次讨论',-2))
        query=self.message('q','/总结',-1)
        await self.bot.on_group_message_create(query)
        actual=query.reply.call_args.kwargs['content']
        self.assertTrue(actual.startswith('刚刚群里主要聊了这些～'))
        store.observe(self.message('two','第二次讨论',0))
        self.assertIn(actual,store.snapshot('g',self.now+1)[0])
        self.retriever.summarize.return_value={'ok':False,'answer':'失败'}
        await self.bot.on_group_message_create(self.message('q2','/总结',1))
        self.assertEqual(self.seen.conn.execute('SELECT summary FROM summary_checkpoint WHERE group_id=?',('g',)).fetchone()[0],actual)
