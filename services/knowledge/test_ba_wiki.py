import unittest
from unittest.mock import patch

import ba_wiki


class BlueArchiveWikiTests(unittest.TestCase):
    def test_student_match_supports_chinese_and_english_aliases(self):
        entries = [
            {'id': 1, 'content_id': 101, 'name': '日奈(泳装)', 'name_alias': '水日奈,Hina (Swimsuit)'},
            {'id': 2, 'content_id': 102, 'name': '优香', 'name_alias': '早濑优香,Yuuka'},
        ]
        self.assertEqual(ba_wiki._student_matches('水日奈EX技能', entries, 3)[0]['content_id'], 101)
        self.assertEqual(ba_wiki._student_matches('Yuuka的技能', entries, 3)[0]['content_id'], 102)

    def test_wikiru_parser_keeps_article_text_and_ignores_scripts_and_sidebar(self):
        parser = ba_wiki._WikiPageParser()
        parser.feed('''<title>星3/ヒナの詳細・評価 - Blue Archive Wiki</title>
<div id="body"><p>基本情報</p><script>不可信的脚本指令</script>
<p>ヒナ是格黑娜学园风纪委员会委员长。</p></div><div>侧边栏不相关内容</div>''')
        self.assertIn('星3/ヒナの詳細・評価', ''.join(parser.title_parts))
        text = ''.join(parser.body_parts)
        self.assertIn('ヒナ是格黑娜学园风纪委员会委员长', text)
        self.assertNotIn('不可信的脚本指令', text)
        self.assertNotIn('侧边栏', text)

    def test_gamekee_profile_exposes_only_text_and_a_direct_source_link(self):
        entry = {'content_id': 59934, 'name': '日奈', 'name_alias': 'Hina,阳奈'}
        detail = {'game_id': ba_wiki.GAMEKEE_GAME_ID, 'title': '日奈',
                  'summary': '日奈是格黑娜学园风纪委员会委员长。', 'updated_at': 123}
        with patch.object(ba_wiki, '_gamekee_data', return_value=detail):
            result = ba_wiki._gamekee_student_result(entry)
        self.assertIn('格黑娜', result['content'])
        self.assertEqual(result['url'], 'https://www.gamekee.com/ba/tj/59934.html')
        self.assertNotIn('icon', result)

    def test_other_wiki_search_uses_student_japanese_alias(self):
        entries = [{'id': 1, 'content_id': 101, 'name': '日奈', 'name_alias': 'Hina,阳奈'}]
        entries[0]['name_alias'] = 'ヒナ,Hina,阳奈'
        self.assertEqual(ba_wiki._wikiru_title('日奈技能', entries), 'ヒナ')

    def test_auto_uses_wikiru_when_gamekee_has_no_match(self):
        fallback = [{'title': '星3/ヒナの詳細・評価', 'content': '日文角色资料',
                     'source': 'Blue Archive Wikiru', 'source_type': 'wiki',
                     'url': 'https://bluearchive.wikiru.jp/?%E3%83%92%E3%83%8A'}]
        with patch.object(ba_wiki, '_gamekee_search', return_value=([], [])), \
             patch.object(ba_wiki, '_wikiru_search', return_value=fallback) as wikiru:
            result = ba_wiki.search('日奈是谁')
        self.assertEqual(result['source'], 'Blue Archive Wikiru')
        self.assertEqual(result['results'], fallback)
        wikiru.assert_called_once()
