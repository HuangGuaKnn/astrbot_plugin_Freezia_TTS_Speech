import unittest

from core.decide import decide, effective_send_mode
from core.settings import Profile


def run(profile=None, **kwargs):
    params = {"text": "主人好呀～", "user_text": "", "is_group": False, "rand": 0.0}
    params.update(kwargs)
    return decide(profile or Profile(probability=0.5), **params)


class DecideTest(unittest.TestCase):
    def test_mute_phrase_wins_over_force_substring(self):
        # 「出声」是「别出声」的子串，必须判为不出声
        result = run(user_text="别出声，打字回我")
        self.assertFalse(result.speak)
        self.assertEqual(result.code, "mute_phrase")

    def test_force_ignores_length_cooldown_probability(self):
        profile = Profile(probability=0.0, max_chars=5, cooldown=60)
        result = run(profile, text="这是一段明显超过五个字的回复", user_text="说给我听嘛",
                     now=100.0, last_voice_at=90.0, rand=0.99)
        self.assertTrue(result.speak)
        self.assertTrue(result.forced)
        self.assertEqual(result.code, "force_phrase")

    def test_force_phrase_with_spaces(self):
        self.assertTrue(run(user_text="说 给 我 听").forced)

    def test_force_on_empty_text_does_not_speak(self):
        result = run(text="", user_text="念给我听")
        self.assertFalse(result.speak)
        self.assertEqual(result.code, "empty")

    def test_switches_beat_force(self):
        self.assertEqual(run(plugin_enabled=False, user_text="出声").code, "plugin_off")
        self.assertEqual(run(Profile(enabled=False), user_text="出声").code, "profile_off")
        self.assertEqual(run(session_muted=True, user_text="出声").code, "session_muted")
        self.assertEqual(run(Profile(send_mode="text_only"), user_text="出声").code, "text_only")

    def test_group_and_private_scope(self):
        self.assertEqual(run(Profile(allow_group=False), is_group=True).code, "group_blocked")
        self.assertEqual(run(Profile(allow_private=False), is_group=False).code, "private_blocked")
        self.assertTrue(run(Profile(allow_group=False), is_group=True, user_text="出声").speak)

    def test_passive_length_limit(self):
        profile = Profile(probability=1.0, max_chars=5)
        self.assertTrue(run(profile, text="一二三四五").speak)
        result = run(profile, text="一二三四五六")
        self.assertEqual(result.code, "too_long")

    def test_cooldown(self):
        profile = Profile(probability=1.0, cooldown=30)
        self.assertEqual(run(profile, now=100.0, last_voice_at=80.0).code, "cooldown")
        self.assertTrue(run(profile, now=100.0, last_voice_at=70.0).speak)
        self.assertTrue(run(Profile(probability=1.0, cooldown=0), now=100.0, last_voice_at=99.0).speak)

    def test_probability(self):
        profile = Profile(probability=0.2)
        self.assertEqual(run(profile, rand=0.1).code, "probability_hit")
        self.assertEqual(run(profile, rand=0.5).code, "probability_miss")
        self.assertTrue(run(Profile(probability=1.0), rand=0.999).speak)
        self.assertFalse(run(Profile(probability=0.0), rand=0.0).speak)

    def test_tool_and_voice_requests_are_forced(self):
        profile = Profile(probability=0.0, max_chars=3, allow_group=False)
        tool = run(profile, text="这句超过了三个字", tool_request=True, is_group=True, rand=0.99)
        self.assertEqual((tool.speak, tool.code, tool.forced), (True, "tool_request", True))
        voice = run(profile, text="这句超过了三个字", user_voice=True, rand=0.99)
        self.assertEqual(voice.code, "voice_message")
        off = run(Profile(probability=0.0, voice_to_voice=False), user_voice=True, rand=0.99)
        self.assertEqual(off.code, "probability_miss")

    def test_tool_still_respects_mute_and_switches(self):
        self.assertEqual(run(user_text="别出声", tool_request=True).code, "mute_phrase")
        self.assertEqual(run(session_muted=True, tool_request=True).code, "session_muted")
        self.assertEqual(run(text="", tool_request=True).code, "empty")

    def test_emotion_boost(self):
        profile = Profile(probability=0.4, emotion_boost=2.0)
        result = run(profile, rand=0.7, emotion_hit="♡")
        self.assertEqual(result.code, "probability_hit")
        self.assertIn("40% × 2 = 80%", result.reason)
        self.assertEqual(run(profile, rand=0.7).code, "probability_miss")
        capped = run(Profile(probability=0.8, emotion_boost=5.0), rand=0.99, emotion_hit="！")
        self.assertIn("= 100%", capped.reason)

    def test_find_emotion(self):
        from core.decide import find_emotion

        self.assertEqual(find_emotion(Profile(), "好耶！"), "！")
        self.assertIsNone(find_emotion(Profile(), "好的"))
        self.assertIsNone(find_emotion(Profile(emotion_boost=1.0), "好耶！"))

    def test_trace_covers_every_step(self):
        from core.decide import STEPS

        for kwargs in ({}, {"user_text": "别出声"}, {"user_text": "说给我听"}, {"rand": 0.99}, {"text": ""}):
            with self.subTest(**kwargs):
                trace = run(**kwargs).trace
                self.assertEqual([item["key"] for item in trace], [key for key, _ in STEPS])
                deciding = [item for item in trace if item["state"] in ("stop", "speak")]
                self.assertEqual(len(deciding), 1)

    def test_trace_states(self):
        forced = {item["key"]: item["state"] for item in run(user_text="说给我听").trace}
        self.assertEqual(forced["force"], "speak")
        self.assertEqual(forced["probability"], "bypass")
        muted = {item["key"]: item["state"] for item in run(user_text="别出声").trace}
        self.assertEqual(muted["mute"], "stop")
        self.assertEqual(muted["content"], "unreached")
        self.assertEqual(muted["plugin"], "pass")

    def test_effective_send_mode(self):
        profile = Profile(send_mode="voice_only", group_force_both=True)
        self.assertEqual(effective_send_mode(profile, is_group=True), "both")
        self.assertEqual(effective_send_mode(profile, is_group=False), "voice_only")
        relaxed = Profile(send_mode="voice_only", group_force_both=False)
        self.assertEqual(effective_send_mode(relaxed, is_group=True), "voice_only")


if __name__ == "__main__":
    unittest.main()
