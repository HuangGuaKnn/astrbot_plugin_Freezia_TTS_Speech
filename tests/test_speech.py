import unittest

from core.speech import (
    apply_pronunciations,
    build_prompt,
    clean_output,
    emotion_by_markers,
    entries_for,
    merged_pronunciations,
    normalize_emotion,
    speech_length,
    split_emotion,
    truncate_sentences,
)


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
        self.assertNotIn("情绪代码", system)

    def test_emotion_prompt_lists_options(self):
        system, _ = build_prompt("晚安", None, [], ["happy", "sad"])
        self.assertIn("情绪代码", system)
        self.assertIn("happy（开心）、sad（难过）、neutral", system)
        self.assertIn("[happy]台词", system)
        self.assertIn("除了开头的情绪标签", system)


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


class ShortenTest(unittest.TestCase):
    def test_prompt_asks_for_short_version(self):
        system, _ = build_prompt("很长的台词", None, [], None, 60)
        self.assertIn("不超过 60 字", system)
        self.assertNotIn("不增加、不删减信息", system)
        system, _ = build_prompt("很长的台词", "日语", [], None, 60)
        self.assertIn("翻译成日语", system)
        self.assertIn("不超过 60 字", system)

    def test_output_check_uses_limit_not_ratio(self):
        source = "今天" * 100
        self.assertEqual(clean_output("博士，晚上见", source, False, limit=20), "博士，晚上见")
        with self.assertRaises(ValueError):
            clean_output("字" * 40, source, False, limit=20)  # 超过 20 × 1.5 + 6
        self.assertEqual(len(clean_output("a" * 80, source, True, limit=20)), 80)  # 外语放宽到 4 倍

    def test_truncate_sentences(self):
        self.assertEqual(truncate_sentences("短句。", 10), "短句。")
        self.assertEqual(truncate_sentences("不限制" * 10, 0), "不限制" * 10)
        self.assertEqual(truncate_sentences("早上好。今天要去罗德岛的甲板上散步，还要整理文件。", 10), "早上好。")
        self.assertEqual(truncate_sentences("今天要去罗德岛的甲板上散步，还要整理文件和报告", 18), "今天要去罗德岛的甲板上散步")
        self.assertEqual(truncate_sentences("一二三四五六七八九十", 5), "一二三四五")
        # 句号太靠前（只剩一个字）时不在那里断
        self.assertEqual(truncate_sentences("嗯。今天要去罗德岛的甲板上散步", 10), "嗯。今天要去罗德岛的")

    def test_speech_length(self):
        self.assertEqual(speech_length("你好。"), 3)
        self.assertEqual(speech_length("ドクター！"), 5)
        self.assertEqual(speech_length("Hello world"), 5)  # 两个单词各 2，加一个空格
        self.assertEqual(speech_length("OK了"), 3)

    def test_truncate_foreign_text(self):
        ja = "ドクター！やっと戻られたんですね！今日の作戦は本当に順調でした。今夜は食堂で祝賀会が開かれます。"
        self.assertEqual(truncate_sentences(ja, 35), "ドクター！やっと戻られたんですね！今日の作戦は本当に順調でした。")
        en = "Doctor! You are finally back. Today the operation went really well, thanks to everyone."
        self.assertEqual(truncate_sentences(en, 20), "Doctor! You are finally back.")
        self.assertEqual(truncate_sentences("Hello there my dear friend how are you", 9), "Hello there my")  # 整词截

    def test_translation_prompt_gets_budget(self):
        system, _ = build_prompt("晚安", "日语", [], None, 0, 80)
        self.assertIn("译文要适合朗读，总长度不超过 80 个字符", system)
        self.assertIn("先精简内容再翻译", system)
        system, _ = build_prompt("晚安" * 60, "日语", [], None, 80, 80)
        self.assertIn("改写成不超过 80 字", system)
        self.assertNotIn("译文要适合朗读", system)  # 已经要求缩写时不重复
        system, _ = build_prompt("晚安", None, [], None, 0, 80)
        self.assertNotIn("译文要适合朗读", system)


class EmotionTest(unittest.TestCase):
    def test_normalize(self):
        for value, expected in [("", ""), ("auto", ""), ("happy", "happy"), (" HAPPY ", "happy"), ("开心", "happy"),
                                ("平静", "neutral"), ("neutral", "neutral"), ("excited", None)]:
            self.assertEqual(normalize_emotion(value), expected, value)

    def test_markers_count_and_order(self):
        entries = [
            {"emotion": "happy", "markers": ["哈哈", "嘿嘿"]},
            {"emotion": "sad", "markers": ["呜", "哭"]},
        ]
        self.assertEqual(emotion_by_markers(entries, "（小声哭）呜呜……"), ("sad", "呜"))
        self.assertEqual(emotion_by_markers(entries, "（哭）呜……"), ("sad", "呜"))  # 次数相同取列表靠前的符号
        self.assertEqual(emotion_by_markers(entries, "哈哈，呜"), ("happy", "哈哈"))  # 平手按列表顺序
        self.assertEqual(emotion_by_markers(entries, "呜，哈哈哈哈"), ("happy", "哈哈"))
        self.assertIsNone(emotion_by_markers(entries, "好的"))
        self.assertIsNone(emotion_by_markers([{"emotion": "happy", "markers": []}], "哈哈"))

    def test_split_tag(self):
        self.assertEqual(split_emotion("[happy]今天真开心"), ("happy", "今天真开心"))
        self.assertEqual(split_emotion(" 【sad】 呜……"), ("sad", "呜……"))
        self.assertEqual(split_emotion("好耶 [happy]"), ("happy", "好耶"))
        self.assertEqual(split_emotion("台词：[shy]才没有"), ("shy", "才没有"))
        self.assertEqual(split_emotion("[开心]走吧"), ("happy", "走吧"))
        self.assertEqual(split_emotion("[neutral]好的"), ("neutral", "好的"))
        self.assertEqual(split_emotion("[笑]哈哈"), (None, "[笑]哈哈"))  # 认不出的方括号不动
        self.assertEqual(split_emotion("没有标签"), (None, "没有标签"))


if __name__ == "__main__":
    unittest.main()
