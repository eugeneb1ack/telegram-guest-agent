import unittest

from rich_renderer import parse_inline, render_blocks
from test_guest_gateway_rich import FakeGateway


class RichRendererTests(unittest.TestCase):
    def test_table_preserves_code_pipes_escapes_empty_cells_and_alignment(self):
        table = render_blocks('| A | B | C |\n|:---|:---:|---:|\n| `a|b` | x\\|y | |')[0]
        self.assertEqual([c['align'] for c in table['cells'][1]], ['left', 'center', 'right'])
        self.assertEqual([c['text'] for c in table['cells'][1]], [
            {'type': 'code', 'text': 'a|b'}, 'x|y', '',
        ])
        self.assertTrue(all(c['is_header'] for c in table['cells'][0]))
        self.assertTrue(table['is_compact'])
        escaped_code = render_blocks('| A | B |\n|---|---|\n| `a\\|b` | 2 |')[0]
        self.assertEqual(escaped_code['cells'][1][0]['text'], {'type': 'code', 'text': 'a|b'})

    def test_table_stops_at_next_structural_block_without_blank_line(self):
        for suffix, kind in [('> A | B', 'blockquote'), ('- A | B', 'list'),
                             ('## A | B', 'heading'), ('```text\na | b\n```', 'pre')]:
            blocks = render_blocks('| A | B |\n|---|---|\n| 1 | 2 |\n' + suffix)
            self.assertEqual([b['type'] for b in blocks], ['table', kind])
            self.assertEqual(len(blocks[0]['cells']), 2)

    def test_wide_table_falls_back_without_losing_columns(self):
        source = '|' + 'x|' * 21 + '\n|' + '---|' * 21 + '\n|' + 'y|' * 21
        self.assertEqual(render_blocks(source), [{'type': 'pre', 'text': source}])

    def test_mismatched_header_separator_is_readable_text(self):
        self.assertEqual(render_blocks('| A | B |\n|---|')[0]['type'], 'paragraph')

    def test_heading_levels_and_distinct_paragraphs(self):
        blocks = render_blocks('# Title\n\nFirst.\n\nSecond.\n\n### Section')
        self.assertEqual([b['type'] for b in blocks], ['heading', 'paragraph', 'paragraph', 'heading'])
        self.assertEqual([blocks[0]['size'], blocks[-1]['size']], [1, 3])

    def test_details_keep_state_children_and_following_paragraph(self):
        blocks = render_blocks('<details open><summary>More</summary>\n\nFirst.\n\n'
                               '<details>\n<summary>Nested</summary>\nSecond.\n</details>\n'
                               '</details>\n\nAfter.')
        self.assertEqual([b['type'] for b in blocks], ['details', 'paragraph'])
        outer = blocks[0]
        self.assertTrue(outer['is_open'])
        self.assertEqual(outer['summary'], 'More')
        self.assertEqual(outer['blocks'][0]['text'], 'First.')
        inner = outer['blocks'][1]
        self.assertEqual(inner['summary'], 'Nested')
        self.assertNotIn('is_open', inner)
        self.assertEqual(inner['blocks'][0]['text'], 'Second.')
        self.assertEqual(blocks[1]['text'], 'After.')

    def test_inline_details_open_is_preserved(self):
        self.assertTrue(render_blocks('<details open><summary>More</summary>Body</details>')[0]['is_open'])

    def test_literal_escapes_code_and_inline_styles(self):
        self.assertEqual(parse_inline(r'\*literal\*'), '*literal*')
        self.assertEqual(parse_inline('``a`b|c``'), {'type': 'code', 'text': 'a`b|c'})
        self.assertEqual(parse_inline('`**literal** & <b>x</b>`'),
                         {'type': 'code', 'text': '**literal** & <b>x</b>'})
        for source, kind in [('**bold**', 'bold'), ('*italic*', 'italic'),
                             ('~~old~~', 'strikethrough'), ('==key==', 'marked'),
                             ('||hidden||', 'spoiler'), ('<u>u</u>', 'underline'),
                             ('<sub>2</sub>', 'subscript'), ('<sup>2</sup>', 'superscript')]:
            self.assertEqual(parse_inline(source)['type'], kind)

    def test_formula_currency_code_and_links_are_distinct(self):
        blocks = render_blocks('Price $5 and $10\n\n$$E=mc^2$$\n\n```python\nx = "<b>"\n```')
        self.assertEqual(blocks, [{'type': 'paragraph', 'text': 'Price $5 and $10'},
                                 {'type': 'mathematical_expression', 'expression': 'E=mc^2'},
                                 {'type': 'pre', 'text': 'x = "<b>"', 'language': 'python'}])
        self.assertEqual(parse_inline('[Docs](https://core.telegram.org/bots/api)')['url'],
                         'https://core.telegram.org/bots/api')

    def test_native_lists_quotes_footnotes_and_media(self):
        blocks = render_blocks('- [x] Done\n  - Child\n- [ ] Later\n\n> Quote\n\n'
                               '<blockquote expandable>\nLong quote\n</blockquote>\n\n'
                               'Source[^n].\n\n[^n]: [Docs](https://core.telegram.org/bots/api)\n\n'
                               '![Photo](https://telegram.org/example/photo.jpg "Caption")')
        self.assertEqual([b['type'] for b in blocks],
                         ['list', 'blockquote', 'expandable_blockquote', 'paragraph', 'footer', 'photo'])
        self.assertTrue(blocks[0]['items'][0]['is_checked'])
        self.assertEqual(blocks[0]['items'][0]['blocks'][1]['type'], 'list')
        self.assertNotIn('is_checked', blocks[0]['items'][1])

    def test_table_rows_and_list_items_consume_block_budget(self):
        table = render_blocks('| A | B |\n|---|---|\n' + '|1|2|\n' * 10, maximum=4)[0]
        self.assertEqual(len(table['cells']), 3)  # Table + three rows = four.
        items = render_blocks('- A\n- B\n- C', maximum=5)[0]['items']
        self.assertEqual(len(items), 2)  # List + two items + two paragraphs.

    def test_media_types_captions_groups_and_maps(self):
        for extension, kind in [('png', 'photo'), ('gif', 'animation'), ('mp4', 'video'),
                                ('mp3', 'audio'), ('ogg', 'voice_note'), ('pdf', 'document'),
                                ('ZIP', 'document')]:
            url = f'https://example.com/file.{extension}?download=1'
            block = render_blocks(f'![Alt]({url} "**Caption**")')[0]
            self.assertEqual(block['type'], kind)
            self.assertEqual(block[kind]['media'], url)
            self.assertEqual(block['caption']['text'], {'type': 'bold', 'text': 'Caption'})
        for kind in ['collage', 'slideshow']:
            block = render_blocks(f'<tg-{kind}>\n![](https://example.com/1.png)\n'
                                  f'![](https://example.com/2.mp4)\n</tg-{kind}>')[0]
            self.assertEqual(block['type'], kind)
            self.assertEqual([b['type'] for b in block['blocks']], ['photo', 'video'])
        block = render_blocks('<tg-map lat="41.9" long="12.5" zoom="14"/>')[0]
        self.assertEqual(block['location'], {'latitude': 41.9, 'longitude': 12.5})

    def test_transport_policy_is_explicit_and_capability_aware(self):
        gateway = FakeGateway()
        policy = gateway._hermes_instructions()
        for phrase in ['native Telegram Rich Message', '2-4 short columns', '20 columns',
                       'Only inline formatting', 'Do not use\nUserbot to post a second copy',
                       'essential caveats outside', 'Inline $math$ is not supported']:
            self.assertIn(phrase, policy)
        gateway.cfg.rich_messages_enabled = False
        policy = gateway._hermes_instructions()
        self.assertIn('rich delivery is disabled', policy)
        self.assertNotIn('including real tables', policy)
