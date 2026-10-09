import json
import hashlib
import os
import unittest
from unittest.mock import patch

import mcp_server


class McpModerationTests(unittest.TestCase):
    def test_moderation_tools_offer_warning_but_no_mute(self):
        tools={item['name']:item for item in mcp_server.TOOLS}
        self.assertTrue(tools['recall_group_message']['annotations']['destructiveHint'])
        self.assertIn('send_group_warning', tools)
        self.assertNotIn('mute_group_member', tools)
        self.assertIn('send_group_warning', mcp_server.ADMIN_BATCH_TOOLS)
        self.assertNotIn('mute_group_member', mcp_server.ADMIN_BATCH_TOOLS)

    def test_recall_tool_validates_ids_before_contacting_bot(self):
        with patch.object(mcp_server,'qq_moderation_request',return_value={'recalled':True}) as call:
            result=mcp_server.tool('recall_group_message',{'group_id':'group_123','message_id':'msg.1!!'})
            self.assertTrue(result['recalled'])
            call.assert_called_once_with('recall',{'group_id':'group_123','message_id':'msg.1!!'})
            with self.assertRaises(ValueError):
                mcp_server.tool('recall_group_message',{'group_id':'group/escape','message_id':'msg'})
            call.assert_called_once()

    def test_send_group_warning_obeys_dashboard_switch(self):
        args={'group_id':'group_123','message_id':'msg.1'}
        with patch.object(mcp_server.knowledge,'ADMIN_TOKEN','admin'), \
             patch.object(mcp_server,'api',return_value={'harassment_warning_enabled':True}) as settings, \
             patch.object(mcp_server,'qq_moderation_request',return_value={'sent':True}) as send:
            result=mcp_server.tool('send_group_warning',args)
            self.assertTrue(result['sent'])
            settings.assert_called_once_with('GET','moderation-settings')
            send.assert_called_once_with('warn',args)
        with patch.object(mcp_server.knowledge,'ADMIN_TOKEN','admin'), \
             patch.object(mcp_server,'api',return_value={'harassment_warning_enabled':False}), \
             patch.object(mcp_server,'qq_moderation_request') as send:
            result=mcp_server.tool('send_group_warning',args)
            self.assertFalse(result['sent'])
            self.assertEqual(result['reason'],'harassment_warning_disabled')
            send.assert_not_called()
        with patch.object(mcp_server.knowledge,'ADMIN_TOKEN','admin'), \
             patch.object(mcp_server,'api',return_value={'harassment_warning_enabled':True}), \
             patch.object(mcp_server,'qq_moderation_request') as send:
            with self.assertRaises(ValueError):
                mcp_server.tool('send_group_warning',{'group_id':'group/escape','message_id':'msg'})
            with self.assertRaises(ValueError):
                mcp_server.tool('send_group_warning',{'group_id':'group_123','message_id':'bad/id'})
            send.assert_not_called()

    def test_local_bot_request_requires_admin_and_private_shared_token(self):
        response=type('Response',(),{'__enter__':lambda self:self,'__exit__':lambda *args:None,
                                     'read':lambda self,*args:json.dumps({'recalled':True}).encode()})()
        with patch.object(mcp_server.knowledge,'ADMIN_TOKEN','admin'), \
             patch.object(mcp_server.knowledge,'MODERATION_TOKEN',''), \
             patch.dict(os.environ,{'KB_MODERATION_TOKEN':'private-token'}), \
             patch.object(mcp_server.urlrequest,'urlopen',return_value=response) as open_url:
            result=mcp_server.qq_moderation_request('recall',{'group_id':'group_123','message_id':'msg.1'})
        self.assertTrue(result['recalled'])
        req=open_url.call_args.args[0]
        self.assertEqual(req.full_url,'http://127.0.0.1:8766/mcp/recall')
        self.assertEqual(req.get_header('Authorization'),'Bearer private-token')
        self.assertEqual(json.loads(req.data),{'group_id':'group_123','message_id':'msg.1'})

    def test_record_harassment_count_uses_group_message_id_for_idempotency(self):
        with patch.object(mcp_server, 'api', return_value={'recorded': True}) as call:
            result=mcp_server.tool('record_harassment_count',{
                'group_id':'group_123','message_id':'msg_1','terms':['中出'],
                'candidates':['新的短语']})
        expected=hashlib.sha256('group_123\0msg_1'.encode()).hexdigest()
        self.assertTrue(result['recorded'])
        call.assert_called_once_with('POST','moderation-recalls',{
            'event_hash':expected,'terms':['中出'],'candidates':['新的短语']})

    def test_admin_batch_runs_knowledge_count_warning_and_recall_in_one_call(self):
        actions=[
            {'name':'update_qa','arguments':{'qa_id':7,'question':'问题','answer':'答案'}},
            {'name':'record_harassment_count','arguments':{
                'group_id':'group_123','message_id':'msg_1','terms':['中出']}},
            {'name':'send_group_warning','arguments':{'group_id':'group_123','message_id':'msg_1'}},
            {'name':'recall_group_message','arguments':{'group_id':'group_123','message_id':'msg_1'}},
        ]
        with patch.object(mcp_server.knowledge,'ADMIN_TOKEN','admin'), \
             patch.object(mcp_server,'api',side_effect=[{'updated':True},{'recorded':True},
                   {'harassment_warning_enabled':True}]) as api_call, \
             patch.object(mcp_server,'qq_moderation_request',side_effect=[
                 {'sent':True},{'recalled':True}]) as qq_call:
            result=mcp_server.tool('execute_admin_actions',{'actions':actions})
        self.assertEqual((result['completed'],result['total']),(4,4))
        self.assertTrue(all(row['ok'] for row in result['results']))
        self.assertEqual(api_call.call_args_list[0].args,
                         ('PUT','qa/7',{'question':'问题','answer':'答案'}))
        self.assertEqual(api_call.call_args_list[1].args[0:2],('POST','moderation-recalls'))
        self.assertEqual(qq_call.call_count,2)
        self.assertEqual([call.args[0] for call in qq_call.call_args_list],['warn','recall'])

    def test_admin_batch_caps_actions_and_reports_partial_failures(self):
        with patch.object(mcp_server.knowledge,'ADMIN_TOKEN','admin'), \
             patch.object(mcp_server,'api',side_effect=lambda method,path,*args,**kwargs:
                   {'harassment_warning_enabled':True} if path=='moderation-settings' else
                   (_ for _ in ()).throw(ValueError('write failed'))), \
             patch.object(mcp_server,'qq_moderation_request',return_value={'sent':True}):
            with self.assertRaises(ValueError):
                mcp_server.tool('execute_admin_actions',{'actions':[{'name':'delete_qa','arguments':{'qa_id':1}}]*9})
            result=mcp_server.tool('execute_admin_actions',{'actions':[
                {'name':'delete_qa','arguments':{'qa_id':1}},
                {'name':'send_group_warning','arguments':{
                    'group_id':'group_123','message_id':'msg_1'}},
                {'name':'mute_group_member','arguments':{
                    'group_id':'group_123','member_id':'member_123','duration_minutes':10}},
            ]})
        self.assertEqual((result['completed'],result['total']),(1,3),result)
        self.assertFalse(result['results'][0]['ok'])
        self.assertTrue(result['results'][1]['ok'])
        self.assertFalse(result['results'][2]['ok'])


if __name__=='__main__':
    unittest.main()
