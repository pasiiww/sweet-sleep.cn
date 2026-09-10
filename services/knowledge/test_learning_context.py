import unittest
from learning_context import prepare, clean_text


class ContextTests(unittest.TestCase):
    def test_noise_and_facts(self):
        for text in ['', '   ', '<faceType=6,faceId="0",ext="abc">', '哈哈哈哈', '！！！', '😀😀']:
            self.assertEqual(clean_text(text), '')
        for text in ['有啊', '不是', '改成20元', '9月12日凯伊下单 https://example.com/p', '哈哈，定金20元']:
            self.assertEqual(clean_text(text), text)

    def test_short_ids_references_and_trust(self):
        def row(mid, member, text, trusted=False, **kw):
            return dict(message_id=mid,member_id=member,content=text,qq='owner' if trusted else '',
                        at=100,member_role='owner' if trusted else 'member',reference={},mentions=[],**kw)
        a=row('long-question','long-guest','定金20元')
        b=row('long-answer','long-admin','定金20元',True)
        c=row('long-repeat','long-admin','定金20元',True)
        d=row('long-confirm','long-admin','不是，改成30元',True,is_reply=True)
        d['reference']={'message_id':'long-question','msg_idx':'opaque-index','quotes':[{'content':'定金20元','member_id':'long-guest','message_id':'long-question'}]}
        d['mentions']=['long-guest']
        payload,debug=prepare({'context':[a,b,c,d],'batch_source_ids':['long-answer','long-repeat','long-confirm'],
                               'context_by_source':{'long-confirm':['long-question','long-repeat']}})
        self.assertEqual([r['message_id'] for r in payload['context']],['m1','m2','m4'])
        self.assertEqual(payload['context'][-1]['reference']['message_id'],'m1')
        self.assertEqual(payload['context'][-1]['reference']['quotes'][0]['member_id'],'u1')
        self.assertEqual(payload['batch_source_ids'],['m2','m4'])
        self.assertEqual(payload['context_by_source']['m4'],['m1'])
        self.assertEqual(debug['message_ids']['m4'],'long-confirm')
        self.assertNotIn('opaque-index',str(payload))
        self.assertNotIn('long-admin',str(payload))
