import unittest

from core.speech import apply_pronunciations, build_prompt, clean_output, entries_for, merged_pronunciations


def entry(source, to, lang=""):
    return {"from": source, "to": to, "lang": lang}


class PronunciationTest(unittest.TestCase):
    def test_longest_first_and_delete(self):
        entries = [entry("Free", "自由"), entry("Freezia", "芙蕾齐娅"), entry("♡", "")]
        self.assertEqual(apply_pronunciations("Freezia 喜欢 Free 吗♡", entries), "芙蕾齐娅 喜欢 自由 吗")

    def test_language_specific_entries(self):
        entries = [entry("AI", "人工智能"), entry("AI", "エーアイ", "ja"), entry("ok", "オーケー", "ja")]
        self.assertEqual(apply_pronunciations("AI ok", entries, ""), "人工智能 ok")
        self.assertEqual(apply_pronunciations("AI ok", entries, "ja"), "エーアイ オーケー")
        self.assertEqual([e["to"] for e in entries_for(entries, "ja")], ["エーアイ", "オーケー"])

    def test_merge_profile_first(self):
        merged = merged_pronunciations([entry("A", "1")], [entry("A", "2"), entry("B", "3"), entry("A", "4", "ja")])
        self.assertEqual([(e["from"], e["to"]) for e in merged], [("A", "1"), ("B", "3"), ("A", "4")])

    def test_no_entries(self):
        self.assertEqual(apply_pronunciations("原样", []), "原样")


class PromptTest(unittest.TestCase):
    def test_translate_prompt_has_language_and_glossary(self):
        system, prompt = build_prompt("晚安", "日语", [entry("Freezia", "フリージア"), entry("～", "")])
        self.assertIn("翻译成日语", system)
        self.assertIn("Freezia → フリージア", system)
        self.assertNotIn("～ →", system)  # 读法为空的条目不放进对照表
        self.assertEqual(prompt, "晚安")

    def test_clean_prompt_keeps_language(self):
        system, _ = build_prompt("晚安", None, [])
        self.assertIn("不要翻译", system)
        self.assertNotIn("对照表", system)


class OutputTest(unittest.TestCase):
    def test_strips_prefix_and_quotes(self):
        self.assertEqual(clean_output("翻译：「おやすみ」", "晚安", True), "おやすみ")
        self.assertEqual(clean_output('"Good night"\n', "晚安", True), "Good night")
        self.assertEqual(clean_output("第一句\n第二句", "第一句第二句", False), "第一句 第二句")

    def test_keeps_inner_quotes(self):
        self.assertEqual(clean_output("「好」她说「走吧」", "「好」她说「走吧」", False), "「好」她说「走吧」")

    def test_short_source_has_slack(self):
        self.assertEqual(clean_output("Good morning!", "早安", True), "Good morning!")

    def test_rejects_empty_and_runaway(self):
        with self.assertRaises(ValueError):
            clean_output("  ", "晚安", False)
        with self.assertRaises(ValueError):
            clean_output("好的，下面是整理后的台词，请查收。这里是一段明显过长的废话。", "晚安", False)


if __name__ == "__main__":
    unittest.main()
