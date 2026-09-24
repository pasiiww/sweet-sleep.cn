import sqlite3
import unittest

import memories


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        memories.initialize(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_group_memory_is_shared_but_private_memory_is_per_user(self):
        group_a = memories.scope('kb1', 'qq_group', 'member-one', 'group-one')
        group_b = memories.scope('kb1', 'qq_group', 'member-two', 'group-one')
        group_other = memories.scope('kb1', 'qq_group', 'member-one', 'group-two')
        private_a = memories.scope('kb1', 'qq_private', 'user-one')
        private_b = memories.scope('kb1', 'qq_private', 'user-two')
        self.assertEqual(group_a, group_b)
        self.assertNotEqual(group_a, group_other)
        self.assertNotEqual(private_a, private_b)
        self.assertNotEqual(group_a, private_a)
        memories.apply(self.conn, group_a, 'save', '群里偏好简洁回答')
        self.assertEqual(memories.apply(self.conn, group_b, 'list')['items'], ['群里偏好简洁回答'])
        self.assertEqual(memories.apply(self.conn, group_other, 'list')['items'], [])
        self.assertEqual(memories.apply(self.conn, private_a, 'list')['items'], [])

    def test_memory_updates_deletes_and_controls_are_bounded(self):
        scope = memories.scope('kb1', 'qq_private', 'user-one')
        self.assertTrue(memories.apply(self.conn, scope, 'save', '用户喜欢短回答')['saved'])
        self.assertTrue(memories.apply(self.conn, scope, 'save', '用户喜欢短回答')['updated'])
        self.assertEqual(len(memories.list_items(self.conn, scope)), 1)
        self.assertFalse(memories.apply(self.conn, scope, 'disable')['enabled'])
        self.assertEqual(memories.context(self.conn, scope), [])
        self.assertFalse(memories.apply(self.conn, scope, 'save', '用户喜欢短回答')['ok'])
        self.assertTrue(memories.apply(self.conn, scope, 'enable')['enabled'])
        self.assertEqual(memories.apply(self.conn, scope, 'forget', '喜欢短回答')['deleted'], 1)
        self.assertTrue(memories.apply(self.conn, scope, 'save', '另一个事实')['saved'])
        self.assertEqual(memories.apply(self.conn, scope, 'clear')['deleted'], 1)
        self.assertEqual(memories.list_items(self.conn, scope), [])

    def test_invalid_scope_and_overlong_entry_are_rejected(self):
        self.assertEqual(memories.scope('kb', 'api', 'user', 'group'), '')
        scope = memories.scope('kb', 'qq_private', 'user')
        self.assertFalse(memories.apply(self.conn, scope, 'save', 'x' * (memories.MAX_ITEM_CHARS+1))['ok'])


if __name__ == '__main__':
    unittest.main()
