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
        self.assertEqual(worker._merge_short_units(tokens), ["に", "おける", "人口"])
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
        self.assertGreaterEqual(worker._TOKENIZE_SCHEMA, 4)


if __name__ == "__main__":
    unittest.main()
