import unittest
from unittest.mock import Mock

from botpy.connection import ConnectionState
from botpy.flags import Intents

import compat


class CompatibilityTests(unittest.TestCase):
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
