import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from botpy.connection import ConnectionState
from botpy.flags import Intents

import compat


class CompatibilityTests(unittest.TestCase):
    def test_strip_bot_tag_keeps_other_member_mention_for_model(self):
        message = SimpleNamespace(sweet_you_mention_ids={'bot-id'}, sweet_bot_mention_ids={'bot-id'})
        self.assertEqual(compat.strip_bot_mention_tags(
            '<@bot-id> 请看看 <@member-id> 的发言', message),
            '  请看看 <@member-id> 的发言')

    def test_mentioned_members_resolves_sdk_id_to_openid(self):
        message = SimpleNamespace(
            mentions=[SimpleNamespace(id='tag-id', username='小明')],
            sweet_mention_openids={'tag-id': 'member-openid'},
            sweet_mention_aliases={'tag-id': ('tag-id', 'member-openid')},
            sweet_you_mention_ids={'bot-id'}, sweet_bot_mention_ids={'bot-id'})
        self.assertEqual(compat.mentioned_members(message), [
            {'openid': 'member-openid', 'name': '小明'}])

    def test_group_member_event_intent_and_parser(self):
        compat.install()
        intents = Intents(public_messages=True, group_member_event=True)
        self.assertTrue(intents.public_messages)
        self.assertTrue(intents.group_member_event)
        self.assertEqual(intents.value & (1 << 24), 1 << 24)

        dispatch = Mock()
        state = ConnectionState(dispatch, None)
        self.assertIn('group_member_add', state.parsers)
        state.parsers['group_member_add']({
            'id': 'event-1',
            'd': {'group_openid': 'group-1', 'member_openid': 'member-1',
                  'user_openid': 'user-1', 'username': '新同学'},
        })
        dispatch.assert_called_once()
        name, event = dispatch.call_args.args
        self.assertEqual(name, 'group_member_add')
        self.assertEqual(event.event_id, 'event-1')
        self.assertEqual(event.group_openid, 'group-1')
        self.assertEqual(event.member_openid, 'member-1')
        self.assertEqual(event.username, '新同学')


if __name__ == '__main__':
    unittest.main()
