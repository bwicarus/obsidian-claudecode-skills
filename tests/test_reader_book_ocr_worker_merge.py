"""unidic 短単位 → 长単位合并(2026-09-07 用户实锤 心|疾患、感染|症、おけ|る、思|う)。不依赖 fugashi:用假 token。"""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "_server_deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

import reader_book_ocr_worker as worker  # noqa: E402


def tok(surface, pos1, pos2="*", cform="*"):
    return SimpleNamespace(surface=surface, feature=SimpleNamespace(pos1=pos1, pos2=pos2, cForm=cform))


class MergeShortUnitsTests(unittest.TestCase):
    def test_prefix_and_suffix_join_nouns(self):
        tokens = [tok("2", "名詞", "数詞"), tok("位", "接尾辞", "名詞的"), tok("心", "接頭辞"), tok("疾患", "名詞", "普通名詞")]
        self.assertEqual(worker._merge_short_units(tokens), ["2", "位", "心疾患"])
        tokens = [tok("感染", "名詞", "普通名詞"), tok("症", "接尾辞", "名詞的"), tok("の", "助詞", "格助詞")]
        self.assertEqual(worker._merge_short_units(tokens), ["感染症", "の"])
        tokens = [tok("廃棄", "名詞", "普通名詞"), tok("物", "接尾辞", "名詞的"), tok("処理", "名詞", "普通名詞")]
        self.assertEqual(worker._merge_short_units(tokens), ["廃棄物", "処理"])
        tokens = [tok("科学", "名詞", "普通名詞"), tok("的", "接尾辞", "形状詞的")]
        self.assertEqual(worker._merge_short_units(tokens), ["科学的"])

    def test_verb_chains_join_but_final_forms_stay(self):
        tokens = [tok("に", "助詞", "格助詞"), tok("おけ", "動詞", "一般", "命令形"), tok("る", "助動詞", "*", "連体形-一般"), tok("人口", "名詞", "普通名詞")]
        self.assertEqual(worker._merge_short_units(tokens, frozenset()), ["に", "おける", "人口"])   # 只测词性规则;有词典表时 における 整体成词(见下)
        tokens = [tok("含ま", "動詞", "一般", "未然形-一般"), tok("れる", "助動詞", "*", "連体形-一般"), tok("病気", "名詞", "普通名詞")]
        self.assertEqual(worker._merge_short_units(tokens), ["含まれる", "病気"])
        tokens = [tok("出し", "動詞", "非自立可能", "連用形-一般"), tok("て", "助詞", "接続助詞"), tok("いる", "動詞", "非自立可能", "終止形-一般")]
        self.assertEqual(worker._merge_short_units(tokens), ["出している"])
        tokens = [tok("見", "動詞", "非自立可能", "連用形-一般"), tok("た", "助動詞", "*", "終止形-一般"), tok("。", "補助記号", "句点")]
        self.assertEqual(worker._merge_short_units(tokens), ["見た", "。"])
        # 终止形不并:OCR 多识别出一个 う 时,思う + う 不能变成 思うう
        tokens = [tok("思う", "動詞", "一般", "終止形-一般"), tok("う", "助動詞", "*", "終止形-一般"), tok("けど", "助詞", "接続助詞")]
        self.assertEqual(worker._merge_short_units(tokens), ["思う", "う", "けど"])
        # 连用形 + て 后面跟的是一般动词(不是非自立):只并到 て
        tokens = [tok("行っ", "動詞", "一般", "連用形-促音便"), tok("て", "助詞", "接続助詞"), tok("買う", "動詞", "一般", "終止形-一般")]
        self.assertEqual(worker._merge_short_units(tokens), ["行って", "買う"])

    def test_nouns_do_not_join_each_other(self):
        tokens = [tok("脳", "名詞", "普通名詞"), tok("血管", "名詞", "普通名詞"), tok("疾患", "名詞", "普通名詞")]
        self.assertEqual(worker._merge_short_units(tokens), ["脳", "血管", "疾患"])

    def test_schema_bumped(self):
        self.assertGreaterEqual(worker._TOKENIZE_SCHEMA, 5)

    def test_dictionary_expressions_join_function_word_runs(self):
        exprs = frozenset({"なんだか", "もしかしたら", "かもしれない", "には", "ことに", "上に", "として"})
        # なん(代名詞)+だ+か → なんだか;もし+か+し+たら → もしかしたら(4 词元,最长优先)
        tokens = [tok("なん", "代名詞"), tok("だ", "助動詞", "*", "終止形-一般"), tok("か", "助詞", "副助詞"), tok("頭", "名詞", "普通名詞")]
        self.assertEqual(worker._merge_short_units(tokens, exprs), ["なんだか", "頭"])
        tokens = [tok("もし", "副詞"), tok("か", "助詞", "副助詞"), tok("し", "動詞", "非自立可能", "連用形-一般"), tok("たら", "助動詞", "*", "仮定形-一般"), tok("病気", "名詞", "普通名詞"), tok("かしら", "助詞", "終助詞")]
        self.assertEqual(worker._merge_short_units(tokens, exprs), ["もしかしたら", "病気", "かしら"])
        tokens = [tok("病気", "名詞", "普通名詞"), tok("か", "助詞", "副助詞"), tok("も", "助詞", "係助詞"), tok("しれ", "動詞", "一般", "未然形-一般"), tok("ない", "助動詞", "*", "終止形-一般"), tok("から", "助詞", "接続助詞")]
        self.assertEqual(worker._merge_short_units(tokens, exprs), ["病気", "かもしれない", "から"])
        tokens = [tok("彼", "代名詞"), tok("に", "助詞", "格助詞"), tok("は", "助詞", "係助詞")]
        self.assertEqual(worker._merge_short_units(tokens, exprs), ["彼", "には"])
        tokens = [tok("に", "助詞", "格助詞"), tok("おけ", "動詞", "一般", "命令形"), tok("る", "助動詞", "*", "連体形-一般"), tok("人口", "名詞", "普通名詞")]
        self.assertEqual(worker._merge_short_units(tokens, frozenset(exprs | {"における"})), ["における", "人口"], "JMdict 整条 に於ける 优先于词性规则")
        # 守卫:跨度里有普通名词就不并 —— こと|に 不能变 殊に,机の 上|に 不能变 上に
        tokens = [tok("その", "連体詞"), tok("こと", "名詞", "普通名詞"), tok("に", "助詞", "格助詞")]
        self.assertEqual(worker._merge_short_units(tokens, exprs), ["その", "こと", "に"])
        tokens = [tok("机", "名詞", "普通名詞"), tok("の", "助詞", "格助詞"), tok("上", "名詞", "普通名詞", "*"), tok("に", "助詞", "格助詞")]
        self.assertEqual(worker._merge_short_units(tokens, exprs), ["机", "の", "上", "に"])
        # 表达段之后词性规则照常:と+し+て → として(在表里),行っ+て 仍按规则 ③
        tokens = [tok("と", "助詞", "格助詞"), tok("し", "動詞", "非自立可能", "連用形-一般"), tok("て", "助詞", "接続助詞"), tok("行っ", "動詞", "一般", "連用形-促音便"), tok("て", "助詞", "接続助詞")]
        self.assertEqual(worker._merge_short_units(tokens, exprs), ["として", "行って"])
        # 空表 = 关掉词典段,只剩词性规则
        tokens = [tok("なん", "代名詞"), tok("だ", "助動詞", "*", "終止形-一般"), tok("か", "助詞", "副助詞")]
        self.assertEqual(worker._merge_short_units(tokens, frozenset()), ["なん", "だ", "か"])

    def test_expression_file_ships_with_the_worker(self):
        exprs = worker._load_expressions()
        self.assertTrue(exprs, "_server_deploy/data/jp_expressions.txt 缺失或为空")
        for probe in ("なんだか", "もしかしたら", "かもしれない", "には", "として"):
            self.assertIn(probe, exprs)


if __name__ == "__main__":
    unittest.main()
