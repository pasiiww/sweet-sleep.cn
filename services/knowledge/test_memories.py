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

    def test_identical_personal_facts_keep_both_members_and_forget_only_self(self):
        scope = memories.scope('kb', 'qq_group', '', 'group')
        for member, nickname in [('a', '甲'), ('b', '乙')]:
            memories.remember_member(self.conn, scope, member, nickname)
            memories.apply(self.conn, scope, 'save', '我喜欢无糖奶茶', member_openid=member)
        self.assertEqual(len(memories.context(self.conn, scope)), 2)
        self.assertTrue(memories.apply(self.conn, scope, 'save', '我喜欢无糖奶茶', member_openid='b')['updated'])
        self.assertEqual(memories.apply(self.conn, scope, 'forget', '无糖奶茶', member_openid='b')['deleted'], 1)
        remaining = memories.context(self.conn, scope)
        self.assertEqual(len(remaining), 1)
        self.assertIn('首次记录昵称“甲”', remaining[0])

    def test_legacy_schema_migrates_without_losing_owner_or_timestamps(self):
        self.conn.execute('DROP TABLE conversation_memories')
        self.conn.execute('''CREATE TABLE conversation_memories (
            scope TEXT NOT NULL, normalized TEXT NOT NULL, content TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, owner_openid TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(scope,normalized))''')
        self.conn.execute('INSERT INTO conversation_memories VALUES(?,?,?,?,?,?)',
                          ('scope', memories.normalize('本人喜欢猫'), '本人喜欢猫', 10, 20, 'first'))
        memories.initialize(self.conn)
        memories.initialize(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM conversation_memories').fetchone(),
                         ('scope', memories.normalize('本人喜欢猫'), '本人喜欢猫', 10, 20, 'first'))
        memories.apply(self.conn, 'scope', 'save', '本人喜欢猫', member_openid='second')
        self.assertEqual(len(memories.context(self.conn, 'scope')), 2)

    def test_first_member_name_is_stable_and_memory_is_attributed(self):
        scope = memories.scope('kb1', 'qq_group', 'member-openid', 'group-one')
        memories.remember_member(self.conn, scope, 'member-openid', '落落的初始昵称非常非常长长')
        memories.remember_member(self.conn, scope, 'member-openid', '后来修改的昵称')
        memories.apply(self.conn, scope, 'save', '本人自称“落落”。', member_openid='member-openid')

        identity = memories.member_identity(self.conn, scope, 'member-openid')
        self.assertEqual(identity['first_nickname'], '落落的初始昵称非常非常长')
        rendered = memories.context(self.conn, scope)[0]
        self.assertIn('首次记录昵称“落落的初始昵称非常非常长”', rendered)
        self.assertIn('本人自称“落落”', rendered)
        self.assertNotIn('member-openid', rendered)
        self.assertTrue(memories.supports_identity_query(self.conn, scope, 'member-openid', '落落是谁'))

    def test_ambiguous_legacy_self_memory_is_repaired_and_linked_to_trace_member(self):
        self.conn.execute('''CREATE TABLE answer_traces(
            kb_id TEXT, origin TEXT, user_id TEXT, group_id TEXT, question TEXT, created REAL)''')
        self.conn.execute('''CREATE TABLE learning_events(
            kb_id TEXT, group_id TEXT, member_id TEXT, member_name TEXT, at REAL, id INTEGER)''')
        scope = memories.scope('kb1', 'qq_group', 'member-openid', 'group-one')
        old = '群里提到的“落落”是当前这位群友（本人自称）。'
        self.conn.execute('INSERT INTO conversation_memories(scope,normalized,content,created,updated,owner_openid) VALUES(?,?,?,?,?,?)',
                          (scope, memories.normalize(old), old, 1, 1, ''))
        self.conn.execute('INSERT INTO answer_traces VALUES(?,?,?,?,?,?)',
                          ('kb1', 'qq_group', 'member-openid', 'group-one', '落落是我', 2))
        self.conn.execute('INSERT INTO learning_events VALUES(?,?,?,?,?,?)',
                          ('kb1', 'group-one', 'member-openid', '初始昵称', 1, 1))

        memories.initialize(self.conn)

        self.assertEqual(memories.list_items(self.conn, scope), ['本人自称“落落”。'])
        identity = memories.member_identity(self.conn, scope, 'member-openid')
        self.assertEqual(identity['first_nickname'], '初始昵称')
        row = self.conn.execute('SELECT owner_openid FROM conversation_memories WHERE scope=?', (scope,)).fetchone()
        self.assertEqual(row[0], 'member-openid')



    def test_impressions_replace_per_member_and_follow_controls(self):
        scope = memories.scope('kb', 'qq_group', '', 'g')
        self.assertTrue(memories.update_impression(self.conn, scope, 'a', '喜欢日奈，偏好简洁回复'))
        self.assertTrue(memories.update_impression(self.conn, scope, 'b', '喜欢星野'))
        self.assertTrue(memories.update_impression(self.conn, scope, 'a', '喜欢日奈，也喜欢凯'))
        self.assertEqual(len(memories.list_items(self.conn, scope)), 2)
        self.assertIn('喜欢凯', memories.impression(self.conn, scope, 'a'))
        self.assertNotIn('星野', str(memories.context(self.conn, scope, 'a')))
        self.assertEqual(memories.context(self.conn, scope), [])
        self.assertEqual(memories.impression(self.conn, memories.scope('kb', 'qq_group', '', 'other'), 'a'), '')
        memories.apply(self.conn, scope, 'disable')
        self.assertEqual(memories.impression(self.conn, scope, 'a'), '')
        self.assertFalse(memories.update_impression(self.conn, scope, 'a', '不应更新'))
        memories.apply(self.conn, scope, 'enable')
        self.assertEqual(memories.apply(self.conn, scope, 'forget', '喜欢日奈', member_openid='a')['deleted'], 1)
        self.assertEqual(memories.apply(self.conn, scope, 'clear')['deleted'], 1)

    def test_impression_validation_and_independent_capacity(self):
        scope = memories.scope('kb', 'qq_group', '', 'g')
        for i in range(memories.MAX_ITEMS):
            memories.apply(self.conn, scope, 'save', '事实' + str(i))
        self.assertTrue(memories.update_impression(self.conn, scope, 'a', '喜欢日奈'))
        for content in ('', '字' * 241, 'openid为secret', '我的密码是123', None):
            self.assertFalse(memories.update_impression(self.conn, scope, 'a', content))
        self.assertIn('喜欢日奈', memories.impression(self.conn, scope, 'a'))

    def test_private_group_context_separates_own_profile_from_public_memory_search(self):
        group_a = memories.scope('kb1', 'qq_group', '', 'group-a')
        group_b = memories.scope('kb1', 'qq_group', '', 'group-b')
        for group, group_id in ((group_a, 'group-a'), (group_b, 'group-b')):
            memories.register_scope(self.conn, 'kb1', 'qq_group', '', group_id)
            memories.remember_member(self.conn, group, 'user-a', '小明')
        memories.apply(self.conn, group_a, 'save', '周末看电影', member_openid='')
        memories.apply(self.conn, group_a, 'save', '本人喜欢无糖', member_openid='user-a')
        memories.apply(self.conn, group_a, 'save', '别人喜欢辣味', member_openid='user-b')
        memories.update_impression(self.conn, group_a, 'user-a', '喜欢日奈')
        memories.update_impression(self.conn, group_a, 'user-b', '喜欢星野')
        memories.apply(self.conn, group_b, 'save', '周五晚有活动', member_openid='')
        memories.apply(self.conn, group_b, 'disable')

        context = memories.group_member_context_for_member(self.conn, 'kb1', 'user-a')
        self.assertTrue(any('本人喜欢无糖' in item for item in context['items']))
        self.assertTrue(any('喜欢日奈' in item for item in context['items']))
        self.assertEqual(context['identity']['first_nickname'], '小明')
        self.assertFalse(any('周末看电影' in item or '别人喜欢辣味' in item
                             or '喜欢星野' in item for item in context['items']))
        group_context = memories.context(self.conn, group_a, include_public=False)
        self.assertFalse(any('周末看电影' in item for item in group_context))
        self.assertEqual(memories.search_group_memories_for_member(
            self.conn, 'kb1', 'user-a', '周末电影'), ['【群聊公共记忆】周末看电影'])
        self.assertEqual(memories.search_group_memories_for_member(
            self.conn, 'kb1', 'user-a', '周五活动'), [])
        self.assertFalse(any('别人喜欢辣味' in item or '喜欢星野' in item
                             for item in memories.search_group_memories_for_member(
                                 self.conn, 'kb1', 'user-a', '喜欢')))


if __name__ == '__main__':
    unittest.main()
