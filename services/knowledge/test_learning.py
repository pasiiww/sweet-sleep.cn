import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import answers
import learning
import server as app


class LearningTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(answers,'_model_call',side_effect=AssertionError('Regression must mock model calls'));guard.start();self.addCleanup(guard.stop)
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.initialize()
        self.kb=self.api('POST','bases',{'name':'学习测试'})['id'];self.at=time.time()-500
        self.cfg=learning.defaults()|{'threshold':3,'bindings':[{'qq':'1229837719','group_id':'group001','member_id':'owner001'},{'qq':'471718054','group_id':'group001','member_id':'admin001'}]}
        self.api('PUT',f'bases/{self.kb}/learning',self.cfg)
        self.api('PUT','answer-settings',{'enabled':True,'model':'deepseek-v4-flash','api_key':'test-key','system_prompt':answers.DEFAULT_PROMPT})
    def tearDown(self):self.temp.cleanup()
    def api(self,m,p,d=None):return app.api(m,'/knowledge/api/'+p,d or {},{})
    def event(self,mid,content='闲聊',member='owner001',at=None,group='group001',**extra):
        return self.api('POST','learning/events',{'kb_id':self.kb,'group_id':group,'member_id':member,'message_id':mid,'content':content,'at':at if at is not None else self.at,**extra})
    def batch(self,prefix,at=None):
        self.event(prefix+'1','凯伊售价100元',at=at)
        self.event(prefix+'2','哈哈',member='admin001',at=at)
        return self.event(prefix+'3','今天很开心',at=at)['job_id']
    def fact(self,mid,answer='凯伊售价100元。',**more):
        return {'subject':'凯伊','attribute':'价格','scope':'','question':'凯伊多少钱？','answer':answer,'source_id':mid,'quote':'凯伊售价100元','existing_qa_id':None,'confidence':95,**more}
    def run_job(self,job,facts):
        with patch.object(answers,'_model_call',return_value=json.dumps({'facts':facts})):self.assertTrue(learning.run_once(app))
        return self.api('GET','learning/jobs/'+job)
    def test_threshold_group_identity_and_dedup(self):
        self.assertTrue(self.event('othergroup',group='group002')['accepted'])
        for n in range(5):self.event('guest'+str(n),member='visitor1')
        self.event('a');self.assertFalse(self.event('a')['accepted']);self.event('b',member='admin001')
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM learning_jobs').fetchone()[0],0)
        job=self.event('c')['job_id'];row=self.run_job(job,[])
        self.assertEqual(row['status'],'completed');self.assertEqual(row['details']['changes'],[])
        self.assertEqual(len(row['details']['batch_source_ids']),3)
    def test_new_qa_immediate_retrieval_and_provenance(self):
        job=self.batch('new');row=self.run_job(job,[self.fact('new1')]);self.assertEqual(row['status'],'completed')
        qa=row['details']['changes'][0]['after'];self.assertEqual(qa['updated_at'],learning.utc(self.at))
        result=self.api('POST','retrieve',{'kb_id':self.kb,'query':'凯伊多少钱'})
        self.assertEqual(result['results'][0]['content'],'凯伊售价100元。')
        self.assertEqual(result['results'][0]['updated_at'],qa['updated_at'])
        self.assertEqual(row['details']['changes'][0]['qq'],'1229837719')
        self.assertEqual(row['details']['model_calls'][0]['stage'],'learning')
    def test_newer_updates_old_arrival_cannot_overwrite(self):
        first=self.batch('a');r=self.run_job(first,[self.fact('a1')]);qid=r['details']['changes'][0]['qa_id']
        newer=self.batch('b',self.at+20);self.run_job(newer,[self.fact('b1',answer='凯伊售价200元。')])
        older=self.batch('c',self.at+10);r=self.run_job(older,[self.fact('c1',answer='凯伊售价150元。')])
        self.assertEqual(r['details']['changes'][0]['action'],'skipped_newer')
        old=self.api('GET','qa/'+str(qid));self.assertEqual(old['answer'],'凯伊售价100元。')
        self.assertEqual(self.api('GET','qa/'+str(old['superseded_by']))['answer'],'凯伊售价200元。')
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM qa_entries').fetchone()[0],2)
    def test_separate_scope_does_not_overwrite(self):
        self.run_job(self.batch('a'),[self.fact('a1',scope='活动一')])
        self.run_job(self.batch('b',self.at+10),[self.fact('b1',scope='活动二',question='活动二凯伊多少钱？')])
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM qa_entries').fetchone()[0],2)
    def test_untrusted_or_old_context_is_not_evidence(self):
        self.event('guest','凯伊售价100元',member='visitor1');job=self.batch('a')
        r=self.run_job(job,[self.fact('guest')]);self.assertEqual(r['error'],'invalid_learning_output')
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM qa_entries').fetchone()[0],0)
    def test_invalid_quote_atomic_and_retry(self):
        job=self.batch('a');r=self.run_job(job,[self.fact('a1'),self.fact('a1',quote='不存在的原话')])
        self.assertEqual(r['status'],'pending');self.assertEqual(r['attempts'],1)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM qa_entries').fetchone()[0],0)
            c.execute('UPDATE learning_jobs SET next_try=0 WHERE id=?',(job,))
        self.assertEqual(self.run_job(job,[])['status'],'completed')
    def test_config_change_cancels_pending(self):
        job=self.batch('a');self.api('PUT',f'bases/{self.kb}/learning',self.cfg|{'enabled':False})
        self.assertEqual(self.api('GET','learning/jobs/'+job)['status'],'cancelled')
        self.assertFalse(learning.run_once(app))
    def test_manual_newer_edit_wins(self):
        row=self.run_job(self.batch('a'),[self.fact('a1')]);qid=row['details']['changes'][0]['qa_id']
        self.api('PUT',f'qa/{qid}',{'question':'凯伊多少钱？','answer':'人工确认300元。'})
        row=self.run_job(self.batch('b',self.at+10),[self.fact('b1')])
        self.assertEqual(row['details']['changes'][0]['action'],'skipped_newer')
        self.assertEqual(self.api('GET',f'qa/{qid}')['answer'],'人工确认300元。')
    def test_invalid_identity_and_expired_messages(self):
        with self.assertRaises(app.Problem):self.api('PUT',f'bases/{self.kb}/learning',self.cfg|{'bindings':[{'qq':'12345','group_id':'group001','member_id':'evil0001'}]})
        self.assertFalse(self.event('old',at=time.time()-1801)['accepted'])
        self.assertFalse(self.event('command','/身份')['accepted'])
        self.assertFalse(self.event('nan',at=float('nan'))['accepted'])
    def test_latest_conflict_instruction_and_dates_reach_pe2(self):
        results=[{'source_type':'qa','question':'凯伊多少钱','title':'凯伊多少钱','content':'100元','updated_at':'2026-09-09T12:00:00Z'},
                 {'title':'旧文档','content':'80元','updated_at':'2026-09-08T12:00:00Z'}]
        with patch.object(answers,'model_call',return_value='100元') as call:answers.complete(answers.defaults(),'凯伊多少钱',results)
        self.assertIn('更新日期较新的为准',call.call_args.args[1][0]['content'])
        payload=json.loads(call.call_args.args[1][-1]['content'])
        self.assertEqual(payload['retrieved_qa'][0]['updated_at'],results[0]['updated_at'])
        self.assertEqual(payload['retrieved_documents'][0]['updated_at'],results[1]['updated_at'])

    def test_quoted_reply_single_trigger_and_local_reference(self):
        self.event('question','凯伊价格是多少？',member='visitor1',msg_idx='ref-001')
        result=self.event('reply','100元',is_reply=True,reference={'msg_idx':'ref-001'})
        row=self.api('GET','learning/jobs/'+result['job_id']);d=row['details']
        self.assertEqual(d['trigger'],'quoted_reply')
        self.assertEqual(d['batch_source_ids'],['reply'])
        source=next(m for m in d['context'] if m['message_id']=='reply')
        self.assertEqual(source['reference']['quotes'][0]['content'],'凯伊价格是多少？')
        row=self.run_job(result['job_id'],[self.fact('reply',quote='100元')])
        self.assertEqual(row['status'],'completed')
        self.assertTrue(row['details']['classification']['relevant'])
        self.assertEqual(row['details']['changes'][0]['after']['answer'],'凯伊售价100元。')
    def test_guest_reply_does_not_trigger(self):
        result=self.event('guest','闲聊',member='visitor1',is_reply=True,reference={'quotes':[{'content':'今天吃什么'}]})
        self.assertNotIn('job_id',result)
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM learning_jobs').fetchone()[0],0)
    def test_per_message_preceding_ten_and_union_dedup(self):
        for i in range(12):self.event('g'+str(i),'访客聊天',member='visitor1',at=self.at+i)
        self.event('a1','凯伊售价100元',at=self.at+20)
        self.event('g12','其他问题',member='visitor2',at=self.at+21)
        self.event('a2','哈哈',at=self.at+22)
        job=self.event('a3','好呀',at=self.at+23)['job_id']
        d=self.api('GET','learning/jobs/'+job)['details']
        self.assertEqual(d['context_by_source']['a1'],['g'+str(i) for i in range(2,12)])
        self.assertEqual(d['context_by_source']['a2'],['g'+str(i) for i in range(3,13)])
        self.assertEqual(d['context_by_source']['a3'],d['context_by_source']['a2'])
        self.assertEqual(len(d['context']),14)
        self.assertEqual(len({m['message_id'] for m in d['context']}),14)
    def test_idle_trigger_after_ten_minutes_and_no_duplicates(self):
        self.event('lonely','凯伊售价100元',at=self.at)
        with patch.object(learning.time,'time',return_value=self.at+599),app.db() as c:
            learning.schedule_idle(c);self.assertEqual(c.execute('SELECT count(*) FROM learning_jobs').fetchone()[0],0)
        with patch.object(learning.time,'time',return_value=self.at+600),app.db() as c:
            learning.schedule_idle(c);learning.schedule_idle(c)
            rows=c.execute('SELECT details FROM learning_jobs').fetchall()
            self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0][0])['trigger'],'idle_timeout')
    def test_admin_reply_resets_idle_but_guest_does_not(self):
        self.event('normal',at=self.at)
        self.event('quoted',at=self.at+300,member='admin001',is_reply=True)
        self.event('guest',at=self.at+450,member='visitor1')
        with patch.object(learning.time,'time',return_value=self.at+899),app.db() as c:
            learning.schedule_idle(c);self.assertEqual(c.execute('SELECT count(*) FROM learning_jobs').fetchone()[0],1)
        with patch.object(learning.time,'time',return_value=self.at+900),app.db() as c:
            learning.schedule_idle(c);self.assertEqual(c.execute('SELECT count(*) FROM learning_jobs').fetchone()[0],2)
    def test_quote_content_cannot_supply_source_evidence(self):
        job=self.event('reply','不知道',is_reply=True,reference={'quotes':[{'content':'凯伊售价100元'}]})['job_id']
        row=self.run_job(job,[self.fact('reply')]);self.assertEqual(row['error'],'invalid_learning_output')

    def test_default_four_counts_both_admins_but_not_immediate_jobs(self):
        self.assertEqual(learning.defaults()['threshold'],4)
        self.api('PUT',f'bases/{self.kb}/learning',self.cfg|{'threshold':4})
        self.event('a');self.event('b',member='admin001')
        self.event('mention','100元',mentions=['visitor1'])
        self.assertNotIn('job_id',self.event('c'))
        job=self.event('d',member='admin001')['job_id']
        d=self.api('GET','learning/jobs/'+job)['details']
        self.assertEqual(d['batch_source_ids'],['a','b','c','d'])

    def test_mention_targets_last_two_each_dedup_and_quote_together(self):
        for i in range(3):self.event('g'+str(i),'凯伊价格？',member='visitor1',at=self.at+i)
        self.event('h','定金多少？',member='visitor2',at=self.at+3)
        self.event('future','不要读未来',member='visitor1',at=self.at+20)
        self.event('noise','无关聊天',member='visitor3',at=self.at+4)
        job=self.event('reply','凯伊售价100元',at=self.at+5,mentions=['visitor1','visitor1','visitor2','missing1'],is_reply=True,reference={'message_id':'g0'})['job_id']
        d=self.api('GET','learning/jobs/'+job)['details']
        self.assertEqual(d['context_by_source']['reply'],['g1','g2','h'])
        self.assertEqual(d['mention_context_by_source']['reply'],{'visitor1':['g1','g2'],'visitor2':['h'],'missing1':[]})
        self.assertEqual([r['message_id'] for r in d['context']],['g1','g2','h','reply'])
        self.assertEqual(d['context'][-1]['reference']['quotes'][0]['message_id'],'g0')
        row=self.run_job(job,[self.fact('reply')]);self.assertEqual(row['status'],'completed')
        payload=json.loads(row['details']['model_calls'][0]['messages'][-1]['content'])
        self.assertEqual(payload['mention_context_by_source'],d['mention_context_by_source'])

    def test_guest_mention_no_trigger_and_admin_mention_resets_silence(self):
        self.event('normal')
        self.assertNotIn('job_id',self.event('guest',member='visitor1',mentions=['owner001']))
        job=self.event('mention',at=self.at+300,mentions=['visitor1'])['job_id']
        self.assertEqual(self.api('GET','learning/jobs/'+job)['details']['trigger'],'member_mention')
        with patch.object(learning.time,'time',return_value=self.at+899),app.db() as c:
            learning.schedule_idle(c);self.assertEqual(c.execute('SELECT count(*) FROM learning_jobs').fetchone()[0],1)
        with patch.object(learning.time,'time',return_value=self.at+900),app.db() as c:
            learning.schedule_idle(c);self.assertEqual(c.execute('SELECT count(*) FROM learning_jobs').fetchone()[0],2)

    def test_owner_auto_listening_and_bot_ignored(self):
        self.api('PUT',f'bases/{self.kb}/learning',self.cfg|{'bindings':[]})
        self.event('prior','凯伊价格？',member='guest001',group='newgroup')
        result=self.event('owner','凯伊售价100元',member='unknownowner',group='newgroup',member_role='owner',is_reply=True,reference={'message_id':'prior'})
        row=self.run_job(result['job_id'],[self.fact('owner')])
        self.assertEqual(row['status'],'completed');self.assertEqual(row['details']['context'][-1]['qq'],'owner')
        self.assertFalse(self.event('robot',member='otherbot',group='newgroup',member_role='owner',author_bot=True,is_reply=True)['accepted'])
        self.assertNotIn('job_id',self.event('admin','凯伊售价100元',member='notbound',member_role='admin',is_reply=True))

    def test_global_binding_accepts_other_group_keeps_context_local(self):
        self.event('same-id','凯伊价格？',member='guest001',group='newgroup')
        self.event('same-id','不要混入其他群',member='guest001')
        job=self.event('owner','凯伊售价100元',group='newgroup',is_reply=True,reference={'message_id':'same-id'})['job_id']
        d=self.api('GET','learning/jobs/'+job)['details']
        self.assertEqual(d['context'][-1]['reference']['quotes'][0]['content'],'凯伊价格？')

    def test_documents_and_qa_reach_model_unchanged_skips_insert(self):
        self.api('POST',f'bases/{self.kb}/documents',{'title':'凯伊价格','content':'凯伊售价100元'})
        self.api('POST',f'bases/{self.kb}/qa',{'question':'凯伊定金','answer':'20元'})
        job=self.event('reply','凯伊售价100元',is_reply=True)['job_id']
        row=self.run_job(job,[])
        payload=json.loads(row['details']['model_calls'][0]['messages'][-1]['content'])
        self.assertTrue(payload['existing_documents']);self.assertTrue(payload['existing_qa'])
        self.assertEqual(row['details']['changes'],[])
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM qa_entries').fetchone()[0],1)

    def test_append_update_retains_manual_record_provenance_and_latest_retrieval(self):
        original=self.api('POST',f'bases/{self.kb}/qa',{'question':'凯伊多少钱？','answer':'凯伊售价80元。'})
        with app.db() as c:c.execute('UPDATE qa_entries SET updated_at=? WHERE id=?',(learning.utc(self.at-10),original['id']))
        job=self.event('reply','凯伊售价100元',is_reply=True,raw_content='<@guest001> 凯伊售价100元')['job_id']
        row=self.run_job(job,[self.fact('reply',existing_qa_id=original['id'])]);created=row['details']['changes'][0]['after']
        self.assertNotEqual(original['id'],created['id']);self.assertEqual(created['origin'],'model')
        self.assertIn('<@guest001>',json.loads(created['source_context'])['content'])
        old=self.api('GET','qa/'+str(original['id']));self.assertEqual(old['origin'],'manual');self.assertEqual(old['answer'],'凯伊售价80元。')
        self.assertEqual(old['superseded_by'],created['id'])
        results=self.api('POST','retrieve',{'kb_id':self.kb,'query':'凯伊多少钱'})['results']
        self.assertEqual([r['qa_id'] for r in results],[created['id']])
        self.api('PUT','qa/'+str(created['id']),{'question':created['question'],'answer':'人工校正'})
        updated=self.api('GET','qa/'+str(created['id']));self.assertEqual(updated['updated_by'],'manual');self.assertEqual(updated['source_context'],created['source_context'])
        with app.db() as c:
            c.execute('DELETE FROM learning_jobs');c.execute('DELETE FROM learning_events')
        self.assertEqual(self.api('GET','qa/'+str(created['id']))['source_context'],created['source_context'])

    def test_confidence_threshold_and_review(self):
        for confidence,status in [(80,'active'),(79.9,'pending')]:
            job=self.event('confidence'+str(confidence),'凯伊售价100元',is_reply=True)['job_id']
            row=self.run_job(job,[self.fact('confidence'+str(confidence),scope=str(confidence),question='凯伊价格'+str(confidence),confidence=confidence)])
            qa=row['details']['changes'][0]['after'];self.assertEqual(qa['publication'],status)
            if status=='pending':
                qid=qa['id']
                hits=self.api('POST','retrieve',{'kb_id':self.kb,'query':'凯伊价格'})['results'];self.assertNotIn(qid,[h.get('qa_id') for h in hits])
                self.api('POST',f'learning/reviews/{qid}/approve')
                self.assertEqual(self.api('GET',f'qa/{qid}')['publication'],'active')
                with self.assertRaises(app.Problem):self.api('POST',f'learning/reviews/{qid}/approve')

    def test_pending_update_does_not_replace_until_approval_and_stale_block(self):
        active=self.run_job(self.event('active','凯伊售价100元',is_reply=True)['job_id'],[self.fact('active')])['details']['changes'][0]['after']
        pending=self.run_job(self.event('pending','凯伊售价200元',at=self.at+5,is_reply=True)['job_id'],[self.fact('pending',answer='凯伊售价200元',quote='凯伊售价200元',confidence=70)])['details']['changes'][0]['after']
        self.assertIsNone(self.api('GET','qa/'+str(active['id']))['superseded_by'])
        self.api('PUT','qa/'+str(active['id']),{'question':active['question'],'answer':'人工确认的新价格'})
        with self.assertRaises(app.Problem):self.api('POST',f"learning/reviews/{pending['id']}/approve")
        self.api('POST',f"learning/reviews/{pending['id']}/reject")
        self.assertEqual(self.api('GET','qa/'+str(pending['id']))['publication'],'rejected')

    def test_missing_confidence_defaults_to_review(self):
        fact=self.fact('unknown');fact.pop('confidence')
        row=self.run_job(self.event('unknown','凯伊售价100元',is_reply=True)['job_id'],[fact])
        self.assertEqual(row['details']['changes'][0]['after']['publication'],'pending')
