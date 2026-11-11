import unittest

from core.text_filter import clean_for_tts, normalize_only
from tests.corpus import FILTER_CASES


class TextFilterTest(unittest.TestCase):
    def test_corpus(self):
        for label, source, expected in FILTER_CASES:
            with self.subTest(label=label, source=source):
                self.assertEqual(clean_for_tts(source), expected)

    def test_idempotent(self):
        for label, source, _expected in FILTER_CASES:
            with self.subTest(label=label):
                once = clean_for_tts(source)
                self.assertEqual(clean_for_tts(once), once)

    def test_normalize_only_keeps_content(self):
        self.assertEqual(normalize_only("*动作* 你好\n世界"), "*动作*你好，世界")
        self.assertEqual(normalize_only("🎉"), "")


if __name__ == "__main__":
    unittest.main()
