"""Exercise the exact packaged selection source in a DOM-free engine.

The native viewport uses the same source through JavaScriptCore. Fixtures cover
original raw indexes after sorting, table isolation and explicit PDF selections.
"""
import json
import runpy
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NativePDFSelectionCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = runpy.run_path(str(ROOT / 'ios/BWReader/package_local_reader.py'))['native_pdf_selection_core']()
        cls.node = shutil.which('node')
        if not cls.node:
            raise RuntimeError('Node is required to verify the packaged native selection engine')

    def run_core(self, chars, calls, **extra):
        page = dict(chars=chars, page_w=300, page_h=400, source='apple', engine_revision='apple-vision-structured/2', character_geometry='exact', **extra)
        script = self.source + '\nconst core = BWNativePDFSelection(' + json.dumps(page) + ');\n'
        script += 'process.stdout.write(JSON.stringify([' + ','.join('core.' + call for call in calls) + ']));'
        result = subprocess.run([self.node], input=script, text=True, encoding='utf-8', capture_output=True, check=True)
        return json.loads(result.stdout)

    @staticmethod
    def char(c, x, y, bk=0, **extra):
        return dict(c=c, x0=x, y0=y, x1=x+8, y1=y+10, bk=bk, w=-1, sp=0, **extra)

    def test_sorted_render_positions_preserve_original_indexes_and_words(self):
        # Source order differs from visual order. The native API must accept and
        # return source indexes, never expose its sorted array indexes as anchors.
        chars = [self.char('B', 8, 0), self.char('A', 0, 0), self.char('C', 16, 0)]
        selected, hit = self.run_core(chars, ['range(1, 0)', 'hit(1, 5, -1, true)'])
        self.assertEqual(selected['text'], 'ABC')
        self.assertEqual(selected['indexes'], [1, 0, 2])
        self.assertEqual(hit, 1)

    def test_cross_line_cjk_word_stays_in_its_table_cell(self):
        chars = [self.char('環', 0, 0, 1), self.char('別', 100, 0, 1),
                 self.char('境', 0, 18, 2), self.char('欄', 100, 18, 2)]
        layout = dict(regions=[
            dict(kind='table-cell', tableId=0, row=0, column=0, ranges=[[0, 0], [2, 2]]),
            dict(kind='table-cell', tableId=0, row=0, column=1, ranges=[[1, 1], [3, 3]]),
        ])
        selected, reverse = self.run_core(chars, ['range(0, 2)', 'range(2, 0)'], layout=layout)
        self.assertEqual(selected['indexes'], [0, 2])
        self.assertEqual(selected['text'], '環境')
        self.assertEqual(selected['regionMode'], 'cell')
        self.assertEqual(reverse, selected)
        self.assertTrue(all(rect[2] < 100 for rect in selected['rects']))

    def test_exact_pdf_selection_does_not_expand_to_another_word(self):
        selected = self.run_core([self.char('A', 0, 0), self.char('B', 8, 0), self.char('C', 16, 0)], ['exact([1])'])[0]
        self.assertEqual(selected['indexes'], [1])
        self.assertEqual(selected['text'], 'B')
        self.assertEqual(selected['sentence'], 'ABC')

    def test_vertical_text_keeps_reading_order_and_column_rects(self):
        chars = [self.char('上', 80, 0, line=0, vertical=True), self.char('次', 50, 0, line=1, vertical=True),
                 self.char('下', 80, 12, line=0, vertical=True), self.char('列', 50, 12, line=1, vertical=True)]
        selected = self.run_core(chars, ['range(0, 2)'])[0]
        self.assertEqual(selected['indexes'], [0, 2])
        self.assertEqual(selected['text'], '上下')
        self.assertEqual(len(selected['rects']), 1)
        self.assertEqual(selected['rects'][0][0], 80)


if __name__ == '__main__':
    unittest.main()
