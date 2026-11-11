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
    def __init__(self, result, message="", umo="aiocqhttp:FriendMessage:1", private=True):
        self._result = result
        self.message_str = message
        self.unified_msg_origin = umo
        self._private = private

    def get_result(self):
        return self._result

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
        path = Path(self.root) / f"{len(self.texts)}.wav"
        path.write_bytes(b"RIFF")
        return str(path)


class FakeContext:
    def __init__(self, root):
        self.provider = FakeProvider(root)
        self.config = {
            "provider_tts_settings": {"enable": False},
            "provider_settings": {"streaming_response": False},
            "callback_api_base": "",
        }
        self.sent = []
        self.send_ok = True
        self.routes = []

    def get_all_tts_providers(self):
        return [self.provider]

    def get_provider_by_id(self, pid):
        return self.provider if pid == self.provider.provider_config["id"] else None

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


class MainHookTest(unittest.IsolatedAsyncioTestCase):
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

    def set_profile(self, **changes):
        from importlib import import_module

        settings_mod = import_module(f"{PLUGIN_DIR.name}.core.settings")
        data = self.plugin.engine.settings.to_dict()
        data["profiles"]["default"].update(changes)
        self.plugin.engine.save_settings(settings_mod.Settings.from_dict(data))

    async def drain(self):
        while self.plugin._tasks:
            await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)

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


if __name__ == "__main__":
    unittest.main()
