from pathlib import Path
import tempfile
import unittest

import answers
import server as app


class DrinkMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, body=None):
        return app.api(method, '/knowledge/api/drink-menu', body or {}, {})

    def test_menu_has_seed_list_and_admin_can_replace_or_clear_it(self):
        result = self.call('GET')
        self.assertEqual(result['items'], answers.DEFAULT_DRINK_MENU)
        self.assertEqual(result['defaults'], answers.DEFAULT_DRINK_MENU)

        menu = [{'brand': '午觉糖水铺', 'product': '奶茶特调', 'temperature': 'both'}]
        self.assertEqual(self.call('PUT', {'items': menu})['items'], menu)
        self.assertEqual(self.call('GET')['items'], menu)
        self.assertEqual(self.call('PUT', {'items': []})['items'], [])

    def test_menu_settings_survive_answer_settings_save(self):
        menu = [{'brand': '古茗', 'product': '生椰抹茶麻薯', 'temperature': 'both'}]
        self.call('PUT', {'items': menu})
        answer_cfg = app.api('GET', '/knowledge/api/answer-settings', {}, {})
        answer_cfg['enabled'] = not answer_cfg['enabled']
        app.api('PUT', '/knowledge/api/answer-settings', answer_cfg, {})
        self.assertEqual(self.call('GET')['items'], menu)

    def test_invalid_or_duplicate_rows_are_rejected(self):
        for items in (
            'not a list', [{'brand': '品牌', 'product': ''}],
            [{'brand': '品牌', 'product': '产品', 'temperature': 'warmish'}],
            [{'brand': '品牌', 'product': '产品'}, {'brand': ' 品牌 ', 'product': '产品 '}],
            [{'brand': '品牌', 'product': '产品\n注入'}],
            [{'brand': '品牌' * 21, 'product': '产品'}],
        ):
            with self.subTest(items=items):
                with self.assertRaises(app.Problem) as ctx:
                    self.call('PUT', {'items': items})
                self.assertEqual(ctx.exception.status, 400)

    def test_legacy_menu_rows_default_to_temperature_flexible(self):
        items = self.call('PUT', {'items': [{'brand': '品牌', 'product': '产品'}]})['items']
        self.assertEqual(items, [{'brand': '品牌', 'product': '产品', 'temperature': 'both'}])


if __name__ == '__main__':
    unittest.main()
