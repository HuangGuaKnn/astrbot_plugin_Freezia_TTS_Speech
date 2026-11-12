"""用桩模块代替 AstrBot，检查 main.py 的钩子、发送和指令逻辑。

这不是真实 AstrBot 联调：只验证插件自己的分支是否按设计走。
"""

import asyncio
import enum
import importlib
import logging
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


# ---------- AstrBot 桩 ----------
class _Comp:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Plain(_Comp):
    def __init__(self, text):
        super().__init__(text=text)


class Image(_Comp):
    pass


class Record(_Comp):
    pass


@dataclass
class MessageChain:
    chain: list = field(default_factory=list)


class ResultContentType(enum.Enum):
    LLM_RESULT = 1
    GENERAL_RESULT = 2
    STREAMING_RESULT = 3
    STREAMING_FINISH = 4


class FakeResult:
    def __init__(self, chain, content_type=ResultContentType.LLM_RESULT):
        self.chain = chain
        self.result_content_type = content_type

    def is_llm_result(self):
        return self.result_content_type == ResultContentType.LLM_RESULT


class FakeEvent:
    def __init__(self, result, message="", umo="aiocqhttp:FriendMessage:1", private=True, components=None):
        self._result = result
        self.message_str = message
        self.unified_msg_origin = umo
        self._private = private
        self._extras = {}
        self._components = components if components is not None else [Plain(message)]

    def get_result(self):
        return self._result

    def get_messages(self):
        return self._components

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        return self._extras.get(key, default)

    def is_private_chat(self):
        return self._private

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


class FakeProvider:
    def __init__(self, root, pid="genie", fail=False):
        self.provider_config = {"id": pid, "type": "genie_tts"}
        self.root = root
        self.fail = fail
        self.texts = []

    async def get_audio(self, text):
        self.texts.append(text)
        if self.fail:
            raise ConnectionError("TTS 服务未启动")
        path = Path(self.root) / f"{self.provider_config['id']}-{len(self.texts)}.wav"
        path.write_bytes(b"RIFF")
        return str(path)


class FakeContext:
    def __init__(self, root):
        self.provider = FakeProvider(root)
        self.extra_tts = [FakeProvider(root, pid="genie_ja")]
        self.config = {
            "provider_tts_settings": {"enable": False},
            "provider_settings": {"streaming_response": False},
            "callback_api_base": "",
        }
        self.sent = []
        self.send_ok = True
        self.routes = []
        self.llm_calls = []
        self.llm_reply = ""
        self.llm_fail = False

    def get_all_tts_providers(self):
        return self.tts_providers()

    def get_provider_by_id(self, pid):
        return next((p for p in self.tts_providers() if p.provider_config["id"] == pid), None)

    def tts_providers(self):
        return [self.provider, *self.extra_tts]

    def get_all_providers(self):
        return [types.SimpleNamespace(provider_config={"id": "chat_main", "type": "openai_chat_completion"})]

    async def get_current_chat_provider_id(self, umo):
        return "chat_main"

    async def llm_generate(self, *, chat_provider_id, prompt=None, system_prompt=None, **kwargs):
        self.llm_calls.append({"provider": chat_provider_id, "prompt": prompt, "system": system_prompt})
        if self.llm_fail:
            raise TimeoutError("模型超时")
        return types.SimpleNamespace(completion_text=self.llm_reply or prompt)

    def get_config(self, umo=None):
        return self.config

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return self.send_ok

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, tuple(methods)))


def _install_stubs(data_dir: Path):
    def passthrough(*_args, **_kwargs):
        return lambda func: func

    class Group:
        def command(self, *_args, **_kwargs):
            return lambda func: func

    def command_group(*_args, **_kwargs):
        return lambda func: Group()

    filter_ns = types.SimpleNamespace(
        on_decorating_result=passthrough,
        command_group=command_group,
        permission_type=passthrough,
        llm_tool=passthrough,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    )

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(_name):
            return data_dir

    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.api.message_components": types.ModuleType("astrbot.api.message_components"),
    }
    modules["astrbot.api"].logger = logging.getLogger("freezia_tts_test")
    modules["astrbot.api.event"].AstrMessageEvent = FakeEvent
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = filter_ns
    modules["astrbot.api.star"].Context = FakeContext
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].StarTools = StarTools
    modules["astrbot.api.star"].register = passthrough
    components = modules["astrbot.api.message_components"]
    components.Plain, components.Image, components.Record = Plain, Image, Record
    modules["astrbot"].api = modules["astrbot.api"]
    modules["astrbot.api"].message_components = components
    sys.modules.update(modules)


def _load_main(data_dir: Path):
    _install_stubs(data_dir)
    parent = str(PLUGIN_DIR.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    name = f"{PLUGIN_DIR.name}.main"
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


class _PluginCase(unittest.IsolatedAsyncioTestCase):
    """公共环境：桩模块 + 插件实例。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.main = _load_main(self.root)
        self.context = FakeContext(self.root)
        self.plugin = self.main.FreeziaTTS(self.context)
        self.set_profile(probability=1.0, cooldown=0, allow_group=True)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    def update_settings(self, **changes):
        from importlib import import_module

        settings_mod = import_module(f"{PLUGIN_DIR.name}.core.settings")
        data = self.plugin.engine.settings.to_dict()
        profile_changes = changes.pop("profile", {})
        data["profiles"]["default"].update(profile_changes)
        data.update(changes)
        self.plugin.engine.save_settings(settings_mod.Settings.from_dict(data))

    def set_profile(self, **changes):
        self.update_settings(profile=changes)

    async def drain(self):
        while self.plugin._tasks:
            await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)


class MainHookTest(_PluginCase):
    async def test_registers_page_routes(self):
        await self.plugin.initialize()
        routes = dict(self.context.routes)
        self.assertEqual(routes[f"/{self.main.PLUGIN_NAME}/page/state"], ("GET",))
        self.assertEqual(routes[f"/{self.main.PLUGIN_NAME}/page/synthesize"], ("POST",))

    async def test_both_mode_keeps_text_and_sends_voice_later(self):
        result = FakeResult([Plain("*挥手* 主人好呀～")])
        await self.plugin.on_decorating_result(FakeEvent(result))
        self.assertEqual([c.text for c in result.chain], ["*挥手* 主人好呀～"])
        await self.drain()
        self.assertEqual(self.context.provider.texts, ["主人好呀～"])
        self.assertEqual(len(self.context.sent), 1)
        umo, chain = self.context.sent[0]
        self.assertEqual(umo, "aiocqhttp:FriendMessage:1")
        self.assertIsInstance(chain.chain[0], Record)
        self.assertEqual(self.plugin.engine.events[0]["status"], "sent")

    async def test_voice_only_replaces_plain_keeps_other_components(self):
        self.set_profile(send_mode="voice_only")
        image = Image(file="x.png")
        result = FakeResult([Plain("主人"), image, Plain("好呀～")])
        await self.plugin.on_decorating_result(FakeEvent(result))
        self.assertIsInstance(result.chain[0], Record)
        self.assertIs(result.chain[1], image)
        self.assertEqual(len(result.chain), 2)
        self.assertEqual(self.context.provider.texts, ["主人好呀～"])
        self.assertEqual(self.context.sent, [])

    async def test_voice_only_in_group_becomes_both(self):
        self.set_profile(send_mode="voice_only")
        result = FakeResult([Plain("大家好～")])
        await self.plugin.on_decorating_result(FakeEvent(result, umo="aiocqhttp:GroupMessage:9", private=False))
        self.assertIsInstance(result.chain[0], Plain)
        await self.drain()
        self.assertEqual(len(self.context.sent), 1)

    async def test_voice_only_failure_falls_back_to_text(self):
        self.set_profile(send_mode="voice_only")
        self.context.provider.fail = True
        result = FakeResult([Plain("主人好呀～")])
        await self.plugin.on_decorating_result(FakeEvent(result))
        self.assertIsInstance(result.chain[0], Plain)
        self.assertEqual(self.plugin.engine.events[0]["status"], "failed")

    async def test_send_failure_is_recorded(self):
        self.context.send_ok = False
        await self.plugin.on_decorating_result(FakeEvent(FakeResult([Plain("你好")])))
        await self.drain()
        self.assertEqual(self.plugin.engine.events[0]["status"], "failed")
        self.assertIn("找不到会话", self.plugin.engine.events[0]["detail"])

    async def test_streaming_and_non_llm_results_are_skipped(self):
        with self.assertLogs("freezia_tts_test", level="WARNING") as logs:
            for kind in (ResultContentType.STREAMING_FINISH, ResultContentType.STREAMING_RESULT):
                await self.plugin.on_decorating_result(FakeEvent(FakeResult([Plain("你好")], kind)))
        self.assertEqual(len([line for line in logs.output if "流式" in line]), 1)  # 限频，只警告一次
        await self.plugin.on_decorating_result(
            FakeEvent(FakeResult([Plain("指令回复")], ResultContentType.GENERAL_RESULT))
        )
        self.assertEqual(self.context.provider.texts, [])
        self.assertEqual(len(self.plugin.engine.events), 0)

    async def test_force_phrase_from_user_message(self):
        self.set_profile(probability=0.0, max_chars=3)
        await self.plugin.on_decorating_result(
            FakeEvent(FakeResult([Plain("这条回复超过了三个字")]), message="说给我听")
        )
        await self.drain()
        self.assertEqual(self.context.provider.texts, ["这条回复超过了三个字"])

    async def test_hook_never_raises(self):
        self.plugin.engine.plan = None  # 制造内部错误
        result = FakeResult([Plain("你好")])
        with self.assertLogs("freezia_tts_test", level="ERROR"):
            await self.plugin.on_decorating_result(FakeEvent(result))
        self.assertEqual(result.chain[0].text, "你好")

    async def test_commands(self):
        event = FakeEvent(None, message="ftts say *鞠躬* 你好 世界")
        replies = [item async for item in self.plugin.ftts_say(event)]
        self.assertEqual(replies[0][0], "chain")
        self.assertEqual(self.context.provider.texts[-1], "你好世界")  # 中文之间的空格会被去掉

        replies = [item async for item in self.plugin.ftts_off(FakeEvent(None))]
        self.assertTrue(self.plugin.engine.settings.is_muted("aiocqhttp:FriendMessage:1"))
        replies = [item async for item in self.plugin.ftts_status(FakeEvent(None))]
        self.assertIn("本会话：静音", replies[0][1])
        self.assertIn("会话 ID：aiocqhttp:FriendMessage:1", replies[0][1])
        [item async for item in self.plugin.ftts_on(FakeEvent(None))]
        self.assertFalse(self.plugin.engine.settings.is_muted("aiocqhttp:FriendMessage:1"))


class MainToolTest(_PluginCase):
    """角色工具、翻译、语音对语音。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.update_settings(
            voice_languages=[{"code": "ja", "name": "日语", "provider_id": "genie_ja"}],
            profile={"probability": 0.0, "cooldown": 0, "tool_interval": 0},
        )

    async def test_tool_marks_event_and_reply_is_spoken_in_language(self):
        import json as _json

        event = FakeEvent(FakeResult([Plain("（小声）晚安～")]), message="晚安")
        result = _json.loads(await self.plugin.freezia_voice_reply(event, language="日语"))
        self.assertTrue(result["ok"])
        self.assertEqual(event.get_extra(self.main.VOICE_REQUEST_KEY), {"language": "ja"})
        self.context.llm_reply = "おやすみ～"
        await self.plugin.on_decorating_result(event)
        await self.drain()
        ja = self.context.extra_tts[0]
        self.assertEqual(ja.texts, ["おやすみ～"])
        self.assertIn("翻译成日语", self.context.llm_calls[-1]["system"])
        self.assertEqual(self.context.llm_calls[-1]["provider"], "chat_main")
        self.assertEqual(self.plugin.engine.events[0]["source"], "tool")
        self.assertEqual(self.plugin.engine.events[0]["status"], "sent")

    async def test_tool_refused_when_muted(self):
        import json as _json

        self.plugin.engine.set_muted("aiocqhttp:FriendMessage:1", True)
        event = FakeEvent(FakeResult([Plain("你好")]))
        result = _json.loads(await self.plugin.freezia_voice_reply(event))
        self.assertFalse(result["ok"])
        self.assertIn("静音", result["reason"])
        self.assertIsNone(event.get_extra(self.main.VOICE_REQUEST_KEY))

    async def test_user_voice_message_gets_voice_reply(self):
        event = FakeEvent(FakeResult([Plain("收到你的语音啦")]), message="你好", components=[Record(file="in.amr")])
        await self.plugin.on_decorating_result(event)
        await self.drain()
        self.assertEqual(self.context.provider.texts, ["收到你的语音啦"])
        self.assertEqual(self.plugin.engine.events[0]["source"], "voice")

    async def test_translation_failure_skips_voice_by_default(self):
        self.context.llm_fail = True
        event = FakeEvent(FakeResult([Plain("晚安")]))
        await self.plugin.freezia_voice_reply(event, language="ja")
        await self.plugin.on_decorating_result(event)
        await self.drain()
        self.assertEqual(self.context.extra_tts[0].texts, [])
        self.assertEqual(self.plugin.engine.events[0]["status"], "failed")
        self.assertIn("翻译失败", self.plugin.engine.events[0]["detail"])


if __name__ == "__main__":
    unittest.main()
