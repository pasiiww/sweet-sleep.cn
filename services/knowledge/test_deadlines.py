import json
import sqlite3
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import deadlines


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.now = datetime(2026, 1, 1, 8, tzinfo=deadlines.TZ).timestamp()
        self.clock = patch('deadlines.time.time', return_value=self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.db.close()

    def create(self, scope='g1', request='msg1', rule=''):
        return deadlines.manage(self.db, 'kb', 'qq_group', scope, 'alice',
            [{'key': 'mentioned1', 'openid': 'bob', 'name': 'Bob'}], request,
            'add', title='提交申请', due='2026-01-03T18:00', owner='mentioned1', rule_json=rule)

    def test_default_schedule_and_idempotency(self):
        item = self.create()
        self.assertEqual(item['reminders'], ['2026-01-01T09:00:00+08:00', '2026-01-02T09:00:00+08:00', '2026-01-03T09:00:00+08:00', '2026-01-03T18:00:00+08:00'])
        self.assertEqual(self.create()['id'], item['id'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM ddl_items').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT owner FROM ddl_items').fetchone()[0], 'bob')

    def test_custom_weekly_and_interval(self):
        end = deadlines.parse_date('2026-01-08T10:00')
        weekly = deadlines.schedule(end, {'weekdays': [0], 'time': '09:30'}, self.now)
        self.assertEqual([datetime.fromtimestamp(t, deadlines.TZ).isoformat() for t in weekly], ['2026-01-05T09:30:00+08:00'])
        repeat = deadlines.schedule(deadlines.parse_date('2026-01-01T11:00'), {'start': '2026-01-01T09:00', 'every_minutes': 60}, self.now)
        self.assertEqual(len(repeat), 3)

    def test_backfill_existing_default_reminders_only_once(self):
        default = self.create()
        self.db.execute("UPDATE ddl_items SET rule='{}' WHERE id=?", (default['id'],))
        custom = self.create(scope='g2', rule=json.dumps({'times': ['2026-01-02T09:00']}))
        self.db.execute('DELETE FROM ddl_reminders WHERE item_id=? AND at=?',
                        (default['id'], deadlines.parse_date('2026-01-03T17:30')))
        self.db.execute("DELETE FROM ddl_migrations WHERE name='default_half_hour_v1'")
        deadlines.init(self.db)
        deadlines.init(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ddl_reminders WHERE item_id=? AND at=?',
                         (default['id'], deadlines.parse_date('2026-01-03T17:30'))).fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ddl_reminders WHERE item_id=?', (custom['id'],)).fetchone()[0], 1)

    def test_half_hour_skipped_when_already_past(self):
        due = deadlines.parse_date('2026-01-01T08:20')
        self.assertEqual(deadlines.schedule(due, deadlines.default_rule(due, self.now), self.now), [self.now+600, due])

    def test_new_default_boundaries(self):
        for seconds, first in [(1200, 600), (3599, 1799.5), (3600, 1800),
                               (86400, 84600)]:
            due = self.now + seconds
            rule = deadlines.default_rule(due, self.now)
            self.assertEqual(deadlines.schedule(due, rule, self.now), [self.now + first, due])
        due = self.now + 86401
        times = deadlines.schedule(due, deadlines.default_rule(due, self.now), self.now)
        self.assertEqual(times, [self.now + 3600, due])

    def test_owner_context_does_not_cross_user_or_conversation(self):
        self.create()
        self.create(scope='g2', request='another')
        self.assertEqual([item['title'] for item in deadlines.owner_context(self.db, 'kb', 'qq_group', 'g1', 'bob')], ['提交申请'])
        self.assertEqual(deadlines.owner_context(self.db, 'kb', 'qq_group', 'g1', 'alice'), [])
        self.assertEqual(deadlines.owner_context(self.db, 'kb', 'qq_private', 'bob', 'bob'), [])
        self.assertEqual(deadlines.owner_context(self.db, 'kb', 'qq_group', 'g3', 'bob'), [])

    def test_fallback_is_scoped_and_acknowledged(self):
        self.create(rule=json.dumps({'times': ['2026-01-01T09:00']}))
        with patch('deadlines.time.time', return_value=self.now + 3700):
            claim = deadlines.claim(self.db, 'kb', 'proactive')
            self.assertEqual(claim['items'][0]['scope'], 'g1')
            deadlines.ack(self.db, 'kb', claim['lease'], False)
            self.assertFalse(deadlines.claim(self.db, 'kb', 'fallback', 'qq_group', 'g2')['items'])
            retry = deadlines.claim(self.db, 'kb', 'fallback', 'qq_group', 'g1')
            self.assertEqual(len(retry['items']), 1)
            deadlines.ack(self.db, 'kb', retry['lease'], True)
            self.assertFalse(deadlines.claim(self.db, 'kb', 'fallback', 'qq_group', 'g1')['items'])

    def test_complete_authorization_and_cancel(self):
        item = self.create()
        denied = deadlines.manage(self.db, 'kb', 'qq_group', 'g1', 'bob', [], 'msg2', 'complete', item_id=item['id'])
        self.assertIn('error', denied)
        ok = deadlines.manage(self.db, 'kb', 'qq_group', 'g1', 'alice', [], 'msg2', 'complete', item_id=item['id'])
        self.assertTrue(ok['ok'])
        with patch('deadlines.time.time', return_value=self.now + 3700):
            self.assertFalse(deadlines.claim(self.db, 'kb', 'proactive')['items'])

    def test_expired_lease_returns_to_fallback(self):
        self.create(rule=json.dumps({'times': ['2026-01-01T09:00']}))
        with patch('deadlines.time.time', return_value=self.now + 3700):
            deadlines.claim(self.db, 'kb', 'proactive')
        with patch('deadlines.time.time', return_value=self.now + 3900):
            self.assertTrue(deadlines.claim(self.db, 'kb', 'fallback', 'qq_group', 'g1')['items'])


if __name__ == '__main__':
    unittest.main()
