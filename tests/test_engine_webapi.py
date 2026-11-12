import json
import tempfile
import unittest
from pathlib import Path

from core.engine import Engine, SpeechError
from core.settings import Profile, Settings, SettingsStore
from core.webapi import dispatch


class FakeHost:
    def __init__(self, root: Path, providers=("tts_a", "tts_b", "tts_ja")):
        self.root = root
        self.providers = list(providers)
        self.fail = False
        self.calls = []
        self.chats = []
        self.chat_reply = None  # None 表示原样返回
        self.chat_error = None

    def list_tts_providers(self):
        return [{"id": pid, "type": "fake"} for pid in self.providers]

    def list_chat_providers(self):
        return [{"id": "chat_main", "type": "fake"}]

    async def chat(self, provider_id, system_prompt, prompt, umo=""):
        self.chats.append({"provider": provider_id, "system": system_prompt, "prompt": prompt, "umo": umo})
        if self.chat_error:
            raise self.chat_error
        return prompt if self.chat_reply is None else self.chat_reply

    async def synthesize(self, provider_id, text):
        self.calls.append((provider_id, text))
        if self.fail:
            raise ConnectionError("连接被拒绝")
        path = self.root / f"{len(self.calls)}.wav"
        path.write_bytes(b"RIFF0000WAVE")
        return str(path)

    def diagnostics(self):
        return {"builtin_tts_enabled": True, "streaming_enabled": False}


class EngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = FakeHost(self.root)
        self.engine = Engine(self.host, self.root)

    async def asyncTearDown(self):
        await self.engine.stop()
        self.tmp.cleanup()

    def plan(self, **kwargs):
        params = {"umo": "qq:FriendMessage:1", "reply_text": "主人好呀～", "user_text": "",
                  "is_group": False, "now": 100.0, "rand": 0.0}
        params.update(kwargs)
        return self.engine.plan(**params)

    async def test_plan_filters_and_records_event(self):
        plan = self.plan(reply_text="*挥手* 主人好呀～")
        self.assertEqual(plan.text, "主人好呀～")
        self.assertTrue(plan.decision.speak)
        self.assertEqual(self.engine.provider_for(plan.profile, plan.language), "tts_a")  # 未指定时用第一个服务
        self.assertEqual(self.engine.events[0]["status"], "pending")
        self.assertEqual(self.engine.events[0]["source"], "passive")

    async def test_cooldown_reserved_then_restored_on_failure(self):
        plan = self.plan()
        self.assertEqual(self.plan(now=101.0).decision.code, "cooldown")
        self.host.fail = True
        with self.assertRaises(ConnectionError):
            await self.engine.synthesize(plan)
        self.assertNotIn(plan.umo, self.engine.last_voice_at)
        self.assertEqual(self.engine.events[-1]["status"], "failed")
        self.assertIn("连接被拒绝", self.engine.events[-1]["detail"])

    async def test_successful_synthesis(self):
        plan = self.plan()
        path = await self.engine.synthesize(plan)
        self.engine.mark_sent(plan)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.host.calls, [("tts_a", "主人好呀～")])
        self.assertEqual(self.engine.events[0]["status"], "sent")

    async def test_profile_provider_overrides_global(self):
        data = self.engine.settings.to_dict()
        data["tts_provider_id"] = "tts_b"
        data["profiles"]["voice2"] = Profile(name="voice2", tts_provider_id="tts_a").to_dict()
        data["session_profiles"] = {"qq:GroupMessage:9": "voice2"}
        self.engine.save_settings(Settings.from_dict(data))
        plain, mapped = self.plan(), self.plan(umo="qq:GroupMessage:9")
        self.assertEqual(self.engine.provider_for(plain.profile, plain.language), "tts_b")
        self.assertEqual(self.engine.provider_for(mapped.profile, mapped.language), "tts_a")

    async def test_mute_session(self):
        self.engine.set_muted("qq:FriendMessage:1", True)
        self.assertEqual(self.plan().decision.code, "session_muted")
        reloaded = SettingsStore(self.root / "settings.json").settings
        self.assertEqual(reloaded.muted_sessions, ["qq:FriendMessage:1"])


class FeatureTest(unittest.IsolatedAsyncioTestCase):
    """语音工具、翻译、发音词典、情绪联动、语音对语音。"""

    UMO = "qq:FriendMessage:1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = FakeHost(self.root)
        self.engine = Engine(self.host, self.root)
        self.configure()

    async def asyncTearDown(self):
        await self.engine.stop()
        self.tmp.cleanup()

    def configure(self, profile=None, **changes):
        data = self.engine.settings.to_dict()
        data["voice_languages"] = [
            {"code": "ja", "name": "日语", "provider_id": "tts_ja"},
            {"code": "en", "name": "英语", "provider_id": ""},
        ]
        data["profiles"]["default"].update({"probability": 0.0, "cooldown": 0, "tool_interval": 60, **(profile or {})})
        data.update(changes)
        self.engine.save_settings(Settings.from_dict(data))

    def plan(self, **kwargs):
        params = {"umo": self.UMO, "reply_text": "晚安～", "user_text": "", "is_group": False, "now": 100.0, "rand": 0.99}
        params.update(kwargs)
        return self.engine.plan(**params)

    # ---- 角色工具 ----
    def test_tool_request_checks(self):
        ok = self.engine.request_voice(umo=self.UMO, user_text="", language="日语", now=100.0)
        self.assertEqual((ok["ok"], ok["language"]), (True, "ja"))
        again = self.engine.request_voice(umo=self.UMO, user_text="", now=130.0)
        self.assertFalse(again["ok"])
        self.assertIn("30 秒后", again["reason"])
        self.assertTrue(self.engine.request_voice(umo=self.UMO, user_text="", now=161.0)["ok"])
        unknown = self.engine.request_voice(umo="qq:FriendMessage:2", user_text="", language="fr")
        self.assertFalse(unknown["ok"])
        self.assertIn("ja（日语）", unknown["reason"])
        muted = self.engine.request_voice(umo="qq:FriendMessage:3", user_text="别出声")
        self.assertIn("别出声", muted["reason"])
        self.configure(profile={"tool_enabled": False})
        self.assertIn("不允许", self.engine.request_voice(umo="qq:FriendMessage:4", user_text="")["reason"])
        self.assertEqual(self.engine.counters["tool"], 2)

    def test_tool_language_defaults_to_profile(self):
        self.configure(profile={"voice_language": "ja"})
        self.assertEqual(self.engine.request_voice(umo=self.UMO, user_text="")["language"], "ja")
        self.assertEqual(self.engine.request_voice(umo="qq:FriendMessage:2", user_text="", language="原文")["language"], "")

    def test_plan_with_tool_request_and_user_voice(self):
        tool = self.plan(voice_request={"language": "ja"})
        self.assertEqual((tool.decision.code, tool.source, tool.language), ("tool_request", "tool", "ja"))
        voice = self.plan(user_voice=True, umo="qq:FriendMessage:2")
        self.assertEqual((voice.decision.code, voice.source), ("voice_message", "voice"))
        self.configure(profile={"voice_to_voice": False})
        self.assertEqual(self.plan(user_voice=True, umo="qq:FriendMessage:3").decision.code, "probability_miss")

    def test_emotion_boost(self):
        self.configure(profile={"probability": 0.2, "emotion_boost": 1.5})
        hit = self.plan(reply_text="最喜欢你了♡", rand=0.25)
        self.assertEqual(hit.decision.code, "probability_hit")
        self.assertIn("× 1.5 = 30%", hit.decision.reason)
        self.assertEqual(self.plan(reply_text="好的", rand=0.25, umo="qq:FriendMessage:2").decision.code, "probability_miss")
        self.configure(profile={"probability": 0.2, "emotion_boost": 1.0})
        self.assertEqual(self.plan(reply_text="好耶！", rand=0.25, umo="qq:FriendMessage:3").decision.code, "probability_miss")

    # ---- 清洗 / 翻译 / 词典 ----
    async def test_translation_uses_language_provider(self):
        self.host.chat_reply = "おやすみ～"
        plan = self.plan(voice_request={"language": "ja"})
        await self.engine.synthesize(plan)
        self.assertEqual(self.host.calls[-1], ("tts_ja", "おやすみ～"))
        self.assertIn("翻译成日语", self.host.chats[-1]["system"])
        self.assertEqual(self.host.chats[-1]["umo"], self.UMO)
        event = self.engine.events[0]
        self.assertEqual((event["language"], event["spoken"]), ("ja", "おやすみ～"))
        self.assertIsNotNone(event["llm_ms"])
        self.assertEqual(self.engine.counters["translated"], 1)

    async def test_translation_failure_policy(self):
        self.host.chat_error = TimeoutError("超时")
        plan = self.plan(voice_request={"language": "ja"})
        with self.assertRaises(SpeechError):
            await self.engine.synthesize(plan)
        self.assertIn("翻译失败", self.engine.events[0]["detail"])
        self.configure(translate_fail="original")
        spoken = await self.engine.prepare(plan.profile, "晚安～", "ja")
        self.assertEqual((spoken.language, spoken.provider_id, spoken.text), ("", "tts_a", "晚安～"))
        self.assertIn("改念原文", spoken.note)

    async def test_language_without_provider_fails(self):
        self.host.chat_reply = "Good night"
        with self.assertRaises(LookupError):
            await self.engine.prepare(Profile(), "晚安", "en")

    async def test_llm_cleaning_falls_back_to_regex(self):
        profile = Profile(filter_mode="llm")
        self.host.chat_reply = "晚安"
        spoken = await self.engine.prepare(profile, "晚安～", "")
        self.assertEqual((spoken.text, spoken.llm_used), ("晚安", True))
        self.assertIn("不要翻译", self.host.chats[-1]["system"])
        self.host.chat_reply = "好的，下面是整理后的台词，请查收。这里是一段明显过长的废话说明。"
        spoken = await self.engine.prepare(profile, "晚安～", "")
        self.assertEqual(spoken.text, "晚安～")
        self.assertIn("LLM 清洗失败", spoken.note)

    async def test_pronunciations_profile_overrides_global(self):
        self.configure(
            profile={"pronunciations": [{"from": "Freezia", "to": "芙蕾齐娅"}]},
            pronunciations=[{"from": "Freezia", "to": "弗里西亚"}, {"from": "～", "to": ""},
                            {"from": "Freezia", "to": "フリージア", "lang": "ja"}],
        )
        profile = self.engine.settings.profiles["default"]
        spoken = await self.engine.prepare(profile, "我是Freezia～", "")
        self.assertEqual(spoken.text, "我是芙蕾齐娅")
        self.host.chat_reply = "わたしはFreezia"
        spoken = await self.engine.prepare(profile, "我是Freezia～", "ja")
        self.assertEqual(spoken.text, "わたしはフリージア")  # 指定为日语的条目优先于不限语言的
        self.assertIn("Freezia → フリージア", self.host.chats[-1]["system"])
        self.assertNotIn("芙蕾齐娅", self.host.chats[-1]["system"])

    async def test_speak_text_for_lab(self):
        self.host.chat_reply = "おはよう"
        spoken, path, elapsed = await self.engine.speak_text(Profile(), "早上好", "ja")
        self.assertEqual((spoken.text, spoken.provider_id), ("おはよう", "tts_ja"))
        self.assertTrue(Path(path).is_file())


class WebApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = Engine(FakeHost(self.root), self.root)

    async def asyncTearDown(self):
        await self.engine.stop()
        self.tmp.cleanup()

    async def call(self, method, path, body=None):
        return await dispatch(self.engine, method, path, body)

    async def test_state_shape(self):
        state = await self.call("GET", "state")
        self.assertEqual(state["resolved_provider"], "tts_a")
        self.assertTrue(state["diagnostics"]["builtin_tts_enabled"])
        self.assertIn("default", state["settings"]["profiles"])

    async def test_profile_create_rename_delete(self):
        await self.call("POST", "profiles/save", {"profile": {"name": "群聊", "probability": 0.5}})
        await self.call("POST", "settings", {"session_profiles": [{"umo": "qq:GroupMessage:1", "profile": "群聊"}]})
        with self.assertRaises(ValueError):
            await self.call("POST", "profiles/save", {"profile": {"name": "群聊"}})
        state = await self.call("POST", "profiles/save", {"original": "群聊", "profile": {"name": "群聊2", "probability": 0.5}})
        self.assertEqual(state["settings"]["session_profiles"], {"qq:GroupMessage:1": "群聊2"})
        state = await self.call("POST", "profiles/delete", {"name": "群聊2"})
        self.assertNotIn("群聊2", state["settings"]["profiles"])
        self.assertEqual(state["settings"]["session_profiles"], {})
        with self.assertRaises(ValueError):
            await self.call("POST", "profiles/delete", {"name": "default"})

    async def test_invalid_profile_name(self):
        with self.assertRaises(ValueError):
            await self.call("POST", "profiles/save", {"profile": {"name": "带 空格"}})

    async def test_preview(self):
        result = await self.call("POST", "preview", {"text": "（歪头）好呀～", "user_text": ""})
        self.assertEqual(result["filtered"], "好呀～")
        self.assertTrue(result["decision"]["speak"])
        self.assertIn("20%", result["decision"]["reason"])
        result = await self.call("POST", "preview", {"text": "好呀", "user_text": "别出声"})
        self.assertEqual(result["decision"]["code"], "mute_phrase")

    async def test_preview_simulations_and_language(self):
        await self.call("POST", "settings", {"voice_languages": [{"code": "ja", "name": "日语", "provider_id": "tts_b"}]})
        result = await self.call("POST", "preview", {"text": "晚安", "simulate_tool": True, "language": "ja"})
        self.assertEqual(result["decision"]["code"], "tool_request")
        self.assertEqual((result["language"], result["language_name"], result["uses_llm"]), ("ja", "日语", True))
        result = await self.call("POST", "preview", {"text": "晚安", "simulate_voice": True, "language": ""})
        self.assertEqual((result["decision"]["code"], result["uses_llm"]), ("voice_message", False))
        with self.assertRaises(ValueError):
            await self.call("POST", "preview", {"text": "晚安", "language": "fr"})

    async def test_synthesize_with_translation(self):
        await self.call("POST", "settings", {"voice_languages": [{"code": "ja", "name": "日语", "provider_id": "tts_b"}]})
        self.engine.host.chat_reply = "おやすみ"
        result = await self.call("POST", "synthesize", {"text": "*挥手* 晚安", "language": "ja"})
        self.assertEqual((result["text"], result["language"], result["provider_id"]), ("おやすみ", "ja", "tts_b"))
        self.assertTrue(result["llm_used"])
        self.assertIsNotNone(result["llm_ms"])

    async def test_synthesize_returns_data_url(self):
        result = await self.call("POST", "synthesize", {"text": "*挥手* 你好"})
        self.assertEqual(result["text"], "你好")
        self.assertTrue(result["audio"].startswith("data:audio/wav;base64,"))
        with self.assertRaises(ValueError):
            await self.call("POST", "synthesize", {"text": "🎉"})

    async def test_unknown_route(self):
        with self.assertRaises(LookupError):
            await self.call("GET", "nope")


class SettingsTest(unittest.TestCase):
    def test_values_are_clamped(self):
        profile = Profile.from_dict({"probability": 3, "max_chars": -5, "send_mode": "x", "cooldown": "abc"})
        self.assertEqual(profile.probability, 1.0)
        self.assertEqual(profile.max_chars, 1)
        self.assertEqual(profile.send_mode, "both")
        self.assertEqual(profile.cooldown, 30)

    def test_phrases_dedup_and_split(self):
        profile = Profile.from_dict({"force_phrases": "出声\n出声， 念给我听 ,"})
        self.assertEqual(profile.force_phrases, ["出声", "念给我听"])

    def test_broken_file_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{oops", encoding="utf-8")
            store = SettingsStore(path)
            self.assertEqual(store.settings.default_profile, "default")
            self.assertTrue(any(p.name.startswith("settings.json.broken-") for p in Path(tmp).iterdir()))

    def test_mapping_to_missing_profile_dropped(self):
        settings = Settings.from_dict({"session_profiles": {"a": "default", "b": "ghost"}, "default_profile": "ghost"})
        self.assertEqual(settings.session_profiles, {"a": "default"})
        self.assertEqual(settings.default_profile, "default")

    def test_new_fields_are_sanitized(self):
        settings = Settings.from_dict({
            "voice_languages": [
                {"code": "ja", "name": "日语", "provider_id": "genie_ja"},
                {"code": "ja", "name": "重复"},
                {"code": "bad code!", "name": "非法"},
                "not a dict",
            ],
            "pronunciations": [
                {"from": "Freezia", "to": "芙蕾齐娅"},
                {"from": "Freezia", "to": "重复"},
                {"from": "Freezia", "to": "フリージア", "lang": "ja"},
                {"from": "  ", "to": "空原文"},
                {"from": "x", "to": "y", "lang": "非法 代码"},
            ],
            "translate_fail": "explode",
            "llm_timeout": 999,
            "profiles": {"default": {"voice_language": "?", "emotion_boost": 0.1, "filter_mode": "llm",
                                     "tool_interval": -5}},
        })
        self.assertEqual([lang["code"] for lang in settings.voice_languages], ["ja"])
        self.assertEqual([(p["from"], p["lang"]) for p in settings.pronunciations],
                         [("Freezia", ""), ("Freezia", "ja"), ("x", "")])
        self.assertEqual((settings.translate_fail, settings.llm_timeout), ("skip", 60.0))
        profile = settings.profiles["default"]
        self.assertEqual((profile.voice_language, profile.emotion_boost, profile.filter_mode, profile.tool_interval),
                         ("", 1.0, "llm", 0))

    def test_old_settings_get_new_defaults(self):
        settings = Settings.from_dict({"profiles": {"default": {"probability": 0.3}}})
        profile = settings.profiles["default"]
        self.assertEqual((profile.tool_enabled, profile.voice_to_voice, profile.emotion_boost), (True, True, 1.5))
        self.assertEqual((settings.voice_languages, settings.translate_fail), ([], "skip"))

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp) / "settings.json")
            store.settings.profiles["夜间"] = Profile(name="夜间", probability=0.05)
            store.save()
            data = json.loads((Path(tmp) / "settings.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(data["profiles"]["夜间"]["probability"], 0.05)
            self.assertIn("夜间", SettingsStore(Path(tmp) / "settings.json").settings.profiles)


if __name__ == "__main__":
    unittest.main()
