import json
import tempfile
import unittest
from pathlib import Path

from core.engine import Engine
from core.settings import Profile, Settings, SettingsStore
from core.webapi import dispatch


class FakeHost:
    def __init__(self, root: Path, providers=("tts_a", "tts_b")):
        self.root = root
        self.providers = list(providers)
        self.fail = False
        self.calls = []

    def list_tts_providers(self):
        return [{"id": pid, "type": "fake"} for pid in self.providers]

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
        self.assertEqual(plan.provider_id, "tts_a")  # 未指定时用第一个服务
        self.assertEqual(self.engine.events[0]["status"], "pending")

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
        self.assertEqual(self.plan().provider_id, "tts_b")
        self.assertEqual(self.plan(umo="qq:GroupMessage:9").provider_id, "tts_a")

    async def test_mute_session(self):
        self.engine.set_muted("qq:FriendMessage:1", True)
        self.assertEqual(self.plan().decision.code, "session_muted")
        reloaded = SettingsStore(self.root / "settings.json").settings
        self.assertEqual(reloaded.muted_sessions, ["qq:FriendMessage:1"])


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
