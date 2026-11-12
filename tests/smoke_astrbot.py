"""用真实的 AstrBot SDK 做离线联调。

不读取现有 AstrBot 配置、账号、插件或数据库，不启动平台适配器，不请求外部模型或 TTS：
ASTRBOT_ROOT 指向插件 .qa/ 下新建的临时目录，模型、TTS 和平台都是只在内存中记录的本地替身。

    <AstrBot 自带的 python> -B tests/smoke_astrbot.py --astrbot-path <包含 astrbot 包的目录>

桌面版 Windows 示例：
    & 'D:\\AstrBot\\backend\\python\\python.exe' -B tests/smoke_astrbot.py --astrbot-path 'D:\\AstrBot\\backend\\app'
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib
import json
import math
import os
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
NAME = ROOT.name


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--astrbot-path", required=True, help="包含 astrbot 包的 AstrBot 源码目录")
    parser.add_argument("--site-packages", action="append", default=[], help="额外的依赖目录，可重复")
    return parser.parse_args()


def write_tone(path: Path, seconds: float = 0.4) -> None:
    rate = 16000
    frames = b"".join(
        struct.pack("<h", int(4000 * math.sin(2 * math.pi * 440 * n / rate))) for n in range(int(rate * seconds))
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)


async def run_smoke(isolation: Path) -> None:
    # 必须在 ASTRBOT_ROOT 隔离之后再导入 AstrBot
    import astrbot
    from astrbot.api.web import PluginRequest, bind_request_context
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.message.components import Image, Plain, Record
    from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
    from astrbot.core.persona_mgr import PersonaManager
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.context_utils import call_event_hook
    from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
    from astrbot.core.pipeline.process_stage.stage import StarRequestSubStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.manager import PlatformManager
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform import Platform
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import LLMResponse, ProviderMeta
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.provider.provider import Provider, TTSProvider
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.context import Context
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, star_handlers_registry
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.umop_config_router import UmopConfigRouter
    from astrbot.core.utils.astrbot_path import get_astrbot_root
    from astrbot.dashboard.services.plugin_page_service import PluginPageService
    from starlette.requests import Request

    main_mod = importlib.import_module(f"{NAME}.main")
    assert Path(get_astrbot_root()).resolve() == isolation.resolve(), "AstrBot 数据目录没有隔离"
    assert ".qa" in isolation.parts
    checks: list[str] = []

    class OfflineTTS(TTSProvider):
        """只在隔离目录里写一段提示音的 TTS 替身。"""

        def __init__(self, provider_id: str):
            super().__init__({"id": provider_id, "type": "offline_tts"}, {})
            self.texts: list[str] = []
            self.fail = False

        def meta(self):
            return ProviderMeta(id=self.provider_config["id"], model="offline", type="offline_tts")

        async def get_audio(self, text: str) -> str:
            self.texts.append(text)
            if self.fail:
                raise ConnectionError("离线 TTS 替身被设置为失败")
            path = isolation / f"{self.provider_config['id']}-{time.time_ns()}.wav"
            write_tone(path)
            return str(path)

    class OfflineChat(Provider):
        """对话模型替身：记录请求，按约定返回固定内容，不联网。"""

        def __init__(self, provider_id: str):
            super().__init__({"id": provider_id, "type": "offline_chat"}, {})
            self.model_name = "offline-chat"
            self.calls: list[dict] = []
            self.reply = "好的～"

        def meta(self):
            return ProviderMeta(id=self.provider_config["id"], model=self.get_model(), type="offline_chat")

        def get_current_key(self):
            return "fixture-secret"

        def set_key(self, key):
            raise AssertionError("插件不应修改模型密钥")

        async def get_models(self):
            return ["offline-chat"]

        async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
            self.calls.append({"prompt": prompt, "system_prompt": system_prompt, "kwargs": sorted(kwargs)})
            return LLMResponse(role="assistant", completion_text=self.reply)

    class RecordingPlatform(Platform):
        def __init__(self, event_queue):
            super().__init__({"id": "offline_smoke"}, event_queue)
            self.sent: list[tuple[str, object]] = []

        def meta(self):
            return PlatformMetadata(name="offline_smoke", id="offline_smoke", description="内存记录型适配器")

        async def run(self):
            raise AssertionError("冒烟测试不应启动适配器")

        async def send_by_session(self, session, message_chain):
            self.sent.append((str(session), message_chain))

    await db_helper.initialize()
    router = UmopConfigRouter(sp)
    await router.initialize()
    acm = AstrBotConfigManager(astrbot_config, router, sp)
    await acm.initialize()
    personas = PersonaManager(db_helper, acm)
    await personas.initialize()
    providers = ProviderManager(acm, db_helper, personas)
    tts_main, tts_ja = OfflineTTS("offline_genie_zh"), OfflineTTS("offline_genie_ja")
    chat = OfflineChat("offline_chat")
    providers.tts_provider_insts.extend([tts_main, tts_ja])
    providers.provider_insts.append(chat)
    providers.inst_map.update({p.meta().id: p for p in (tts_main, tts_ja, chat)})
    event_queue = asyncio.Queue()
    platforms = PlatformManager(astrbot_config, event_queue)
    recording = RecordingPlatform(event_queue)
    platforms.platform_insts.append(recording)
    context = Context(
        event_queue, astrbot_config, db_helper, providers, platforms,
        ConversationManager(db_helper), None, personas, acm, None, None,
    )
    context.registered_web_apis = []
    plugin = main_mod.FreeziaTTS(context)
    module = type(plugin).__module__

    def make_event(text: str, *, group: bool = False, sender: str = "smoke-user", components=None):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        session = "smoke-group" if group else sender
        message.self_id, message.session_id, message.message_id = "smoke-bot", session, str(time.time_ns())
        message.sender = MessageMember(user_id=sender, nickname="Smoke")
        if group:
            message.group_id = session
        message.message = components if components is not None else [Plain(text)]
        message.message_str, message.raw_message = text, {}
        return AstrMessageEvent(text, message, recording.meta(), session)

    async def request_api(suffix: str, method: str = "GET", payload: dict | None = None):
        route = next(r for r in context.registered_web_apis if r[0] == f"/{NAME}/page/{suffix}" and method in r[2])
        body = json.dumps(payload or {}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
            "server": ("127.0.0.1", 0), "client": ("127.0.0.1", 0), "path": route[0],
            "query_string": urlencode({}).encode(), "headers": [(b"content-type", b"application/json")],
        }
        with bind_request_context(PluginRequest(Request(scope, receive), plugin_name=NAME, username="smoke")):
            response = await route[1]()
        data = json.loads(response.body)
        assert response.status_code == 200, f"{suffix} 返回 HTTP {response.status_code}: {data}"
        return data.get("data", data) if isinstance(data, dict) and data.get("status") == "ok" else data

    async def drain():
        while plugin._tasks:
            await asyncio.gather(*list(plugin._tasks), return_exceptions=True)

    try:
        # ---- 元数据与插件页面 ----
        metadata = PluginManager._load_plugin_metadata(str(ROOT))
        assert metadata and metadata.name == NAME == main_mod.PLUGIN_NAME
        metadata.root_dir_name = ROOT.name
        page_manager = SimpleNamespace(
            context=SimpleNamespace(get_all_stars=lambda: [metadata]),
            plugin_store_path=str(ROOT.parent), reserved_plugin_path=str(isolation / "unused"),
        )
        page_service = PluginPageService(page_manager)
        pages = await page_service.discover_plugin_pages(metadata)
        assert [page.name for page in pages] == ["freezia"]
        assert metadata.i18n["zh-CN"]["pages"]["freezia"]["title"] == "Freezia TTS Speech"
        checks.append("真实 PluginManager 读取 metadata；PluginPageService 发现 pages/freezia 与 i18n 标题")

        # ---- 处理函数注册 ----
        star_map[module].star_cls = plugin
        star_map[module].activated = True
        handlers = star_handlers_registry.get_handlers_by_module_name(module)
        for handler in handlers:
            handler.handler = getattr(plugin, handler.handler_name)
        names = {h.handler_name for h in handlers}
        decorating = [h for h in handlers if h.event_type == EventType.OnDecoratingResultEvent]
        assert [h.handler_name for h in decorating] == ["on_decorating_result"]
        after_sent = [h for h in handlers if h.event_type == EventType.OnAfterMessageSentEvent]
        assert [h.handler_name for h in after_sent] == ["after_message_sent"]
        assert {"ftts_status", "ftts_on", "ftts_off", "ftts_say"} <= names
        checks.append("on_decorating_result 钩子与 /ftts 指令组注册到真实 star_handlers_registry")

        # ---- 页面接口 ----
        await plugin.initialize()
        routes = {(r[0], tuple(r[2])) for r in context.registered_web_apis}
        assert (f"/{NAME}/page/state", ("GET",)) in routes and (f"/{NAME}/page/synthesize", ("POST",)) in routes
        state = await request_api("state")
        assert [p["id"] for p in state["providers"]] == ["offline_genie_zh", "offline_genie_ja"]
        assert state["diagnostics"]["builtin_tts_enabled"] is False
        preview = await request_api("preview", "POST", {"text": "*挥手* 你好呀～", "user_text": "说给我听"})
        assert preview["filtered"] == "你好呀～" and preview["decision"]["forced"]
        synth = await request_api("synthesize", "POST", {"text": "（歪头）早上好"})
        assert synth["text"] == "早上好" and synth["audio"].startswith("data:audio/wav;base64,")
        await request_api("settings", "POST", {"tts_provider_id": "offline_genie_zh"})
        checks.append("register_web_api 路由经真实 PluginRequest 调用：state / preview / synthesize / settings")

        # ---- 真实回复装饰阶段 ----
        config = copy.deepcopy(dict(astrbot_config))
        config["wake_prefix"] = ["/"]
        config["admins_id"] = ["smoke-admin"]
        # 装饰阶段通过 plugin_manager.context 查询自带 TTS，这里只提供 context
        pipeline = PipelineContext(config, SimpleNamespace(context=context), "offline-smoke", db_helper=None)
        decorate = ResultDecorateStage()
        await decorate.initialize(pipeline)

        async def decorate_reply(text, user_text="", *, group=False, kind=ResultContentType.LLM_RESULT, extra=None):
            event = make_event(user_text, group=group)
            result = MessageEventResult()
            for comp in extra or []:
                result.chain.append(comp)
            result.message(text).set_result_content_type(kind)
            event.set_result(result)
            async for _ in decorate.process(event):
                pass
            await drain()
            return event

        def set_profile(**changes):
            payload = dict(plugin.engine.settings.profiles["default"].to_dict(), **changes)
            return request_api("profiles/save", "POST", {"original": "default", "profile": payload})

        await set_profile(probability=1.0, cooldown=0, send_mode="both")
        event = await decorate_reply("*轻轻挥手* 你回来啦～")
        assert [c.text for c in event.get_result().chain if isinstance(c, Plain)] == ["*轻轻挥手* 你回来啦～"]
        assert tts_main.texts[-1] == "你回来啦～"
        umo, chain = recording.sent[-1]
        assert umo == event.unified_msg_origin and isinstance(chain.chain[0], Record)
        assert Path(chain.chain[0].file).is_file()
        checks.append("文字 + 语音：真实 ResultDecorateStage 保留文字，语音经 Context.send_message 由适配器替身收到")

        await set_profile(send_mode="voice_only")
        image = Image(file="https://example.invalid/x.png")
        sent_before = len(recording.sent)
        event = await decorate_reply("早安～", extra=[image])
        chain = event.get_result().chain
        assert isinstance(chain[0], Image) and isinstance(chain[1], Record) and len(chain) == 2
        assert len(recording.sent) == sent_before
        checks.append("仅语音：钩子把文字替换为真实 Record 组件，图片保留，不另发消息")

        tts_main.fail = True
        event = await decorate_reply("合成会失败")
        assert [c.text for c in event.get_result().chain if isinstance(c, Plain)] == ["合成会失败"]
        tts_main.fail = False
        checks.append("TTS 失败时仍发文字")

        calls_before = len(tts_main.texts)
        await decorate_reply("流式回复", kind=ResultContentType.STREAMING_FINISH)
        await decorate_reply("指令回复", kind=ResultContentType.GENERAL_RESULT)
        assert len(tts_main.texts) == calls_before
        checks.append("流式结果与非 LLM 结果被跳过")

        # ---- 指令：真实唤醒与分发阶段 ----
        waking, dispatch = WakingCheckStage(), StarRequestSubStage()
        await waking.initialize(pipeline)
        await dispatch.initialize(pipeline)

        async def run_command(text, sender="smoke-user"):
            event = make_event(text, sender=sender)
            await waking.process(event)
            results = []
            if event.is_stopped():  # 权限不足等情况，真实流水线到此为止
                return event, results
            async for _ in dispatch.process(event):
                if event.get_result():
                    results.append(event.get_result())
            return event, results

        _, results = await run_command("/ftts status")
        assert any("会话 ID：offline_smoke:FriendMessage:smoke-user" in r.get_plain_text() for r in results)
        denied, results = await run_command("/ftts off")
        assert not plugin.engine.settings.is_muted(denied.unified_msg_origin)
        _, results = await run_command("/ftts off", sender="smoke-admin")
        assert plugin.engine.settings.is_muted("offline_smoke:FriendMessage:smoke-admin")
        await run_command("/ftts on", sender="smoke-admin")
        _, results = await run_command("/ftts say （鞠躬）大家好", sender="smoke-admin")
        assert any(isinstance(c, Record) for r in results for c in r.chain)
        assert tts_main.texts[-1] == "大家好"
        checks.append("/ftts 指令经真实 WakingCheckStage + StarRequestSubStage 执行；非管理员不能静音，管理员可以")

        # ---- 角色可调用的工具 ----
        tool = next(t for t in llm_tools.func_list if t.name == "freezia_voice_reply")
        assert getattr(tool.handler, "__module__", "") == module  # handler_module_path 由插件管理器加载时补上
        properties = tool.parameters["properties"]
        assert properties["language"]["type"] == "string" and "ja" in properties["language"]["description"]
        assert properties["emotion"]["type"] == "string" and "happy" in properties["emotion"]["description"]
        assert not {"language", "emotion"} & set(tool.parameters.get("required", []))
        assert "普通闲聊不要调用" in tool.description
        checks.append("llm_tool 注册到真实 FunctionToolManager，文档字符串解析出 language、emotion 两个可选 string 参数")

        await request_api("settings", "POST", {
            "voice_languages": [{"code": "ja", "name": "日语", "provider_id": "offline_genie_ja"}],
            "llm_provider_id": "offline_chat",
        })
        await set_profile(send_mode="both", probability=0.0, tool_interval=0)
        chat.reply = "おやすみ～"
        event = make_event("晚安")
        reply = json.loads(await plugin.freezia_voice_reply(event, language="日语"))
        assert reply["ok"] and event.get_extra(main_mod.VOICE_REQUEST_KEY) == {"language": "ja", "emotion": ""}
        result = MessageEventResult()
        result.message("（小声）晚安～").set_result_content_type(ResultContentType.LLM_RESULT)
        event.set_result(result)
        async for _ in decorate.process(event):
            pass
        await drain()
        assert tts_ja.texts[-1] == "おやすみ～", tts_ja.texts
        assert "翻译成日语" in chat.calls[-1]["system_prompt"] and chat.calls[-1]["prompt"] == "晚安～"
        assert isinstance(recording.sent[-1][1].chain[0], Record)
        assert plugin.engine.events[0]["source"] == "tool" and plugin.engine.events[0]["status"] == "sent"
        checks.append("工具标记经真实事件 extra 传到装饰阶段；Context.llm_generate 调用对话模型替身翻译，日语 TTS 合成并发送")

        muted_umo = "offline_smoke:FriendMessage:muted-user"
        plugin.engine.set_muted(muted_umo, True)
        refused = json.loads(await plugin.freezia_voice_reply(make_event("你好", sender="muted-user")))
        assert not refused["ok"] and "静音" in refused["reason"]
        plugin.engine.set_muted(muted_umo, False)
        checks.append("静音会话里工具返回拒绝原因，不打标记")

        # ---- 语音对语音 ----
        tts_count = len(tts_main.texts)
        voice_event = make_event("", components=[Record(file=str(isolation / "incoming.wav"))])
        result = MessageEventResult()
        result.message("听到你的声音啦").set_result_content_type(ResultContentType.LLM_RESULT)
        voice_event.set_result(result)
        async for _ in decorate.process(voice_event):
            pass
        await drain()
        assert len(tts_main.texts) == tts_count + 1 and tts_main.texts[-1] == "听到你的声音啦"
        assert plugin.engine.events[0]["source"] == "voice"
        checks.append("用户消息里的真实 Record 组件触发语音回复")

        # ---- 开启语音转文字：预处理阶段先把语音换成文字 ----
        class OfflineSTT:
            def __init__(self):
                self.paths = []

            async def get_text(self, audio_url: str) -> str:
                self.paths.append(audio_url)
                return "你好呀"

        stt = OfflineSTT()

        async def using_stt(_umo):
            return stt

        stt_config = dict(config, provider_stt_settings={"enable": True, "provider_id": "offline_stt"})
        preprocess = PreProcessStage()
        await preprocess.initialize(PipelineContext(
            stt_config, SimpleNamespace(context=SimpleNamespace(get_using_stt_provider_async=using_stt)),
            "offline-smoke", db_helper=None,
        ))
        incoming = isolation / "incoming_stt.wav"
        write_tone(incoming)
        stt_event = make_event("", sender="stt-user", components=[Record(file=str(incoming))])
        # OneBot（aiocqhttp）数组格式的原始消息
        stt_event.message_obj.raw_message = {"message": [{"type": "record", "data": {"file": str(incoming)}}]}
        await preprocess.process(stt_event)
        assert stt.paths and not any(isinstance(c, Record) for c in stt_event.get_messages())
        assert stt_event.message_str == "你好呀"
        result = MessageEventResult()
        result.message("嗯嗯，我在听").set_result_content_type(ResultContentType.LLM_RESULT)
        stt_event.set_result(result)
        async for _ in decorate.process(stt_event):
            pass
        await drain()
        assert tts_main.texts[-1] == "嗯嗯，我在听" and plugin.engine.events[0]["source"] == "voice"
        checks.append("开启语音转文字时，真实 PreProcessStage 把消息链里的语音换成文字；插件按平台原始消息仍识别出语音并用语音回复")

        # ---- 情绪音色 ----
        tts_sad = OfflineTTS("offline_genie_sad")
        providers.tts_provider_insts.append(tts_sad)
        providers.inst_map[tts_sad.meta().id] = tts_sad
        await set_profile(emotion_voices=[{"emotion": "sad", "provider_id": "offline_genie_sad", "markers": ["呜"]}])
        event = make_event("我今天好难过", sender="sad-user")
        reply = json.loads(await plugin.freezia_voice_reply(event, emotion="难过"))
        assert reply["ok"] and reply["emotion"] == "sad", reply
        result = MessageEventResult()
        result.message("没关系，我一直在").set_result_content_type(ResultContentType.LLM_RESULT)
        event.set_result(result)
        async for _ in decorate.process(event):
            pass
        await drain()
        assert tts_sad.texts == ["没关系，我一直在"], tts_sad.texts
        record = plugin.engine.events[0]
        assert (record["emotion"], record["emotion_source"], record["provider_id"]) == ("sad", "tool", "offline_genie_sad")
        await set_profile(probability=1.0)
        await decorate_reply("（小声哭）呜……你终于回来了")
        assert tts_sad.texts[-1] == "呜……你终于回来了", (tts_sad.texts, plugin.engine.events[0])
        assert plugin.engine.events[0]["emotion_source"] == "marker"
        checks.append("工具指定「难过」与回复里的情绪符号都会改用难过音色的 TTS 条目合成")

        # ---- 长回复缩写 ----
        await set_profile(send_mode="voice_only", speak_max_chars=12, emotion_voices=[], probability=0.0)
        chat.reply = "博士，作战顺利，晚上见～"
        long_reply = "博士，今天的作战非常顺利，大家都平安回来了。晚上食堂有庆功宴，记得早点过来，我会给你留位置的。"
        sent_before = len(recording.sent)
        event = await decorate_reply(long_reply, "说给我听")
        assert [c.text for c in event.get_result().chain if isinstance(c, Plain)] == [long_reply]
        assert tts_main.texts[-1] == "博士，作战顺利，晚上见～", tts_main.texts[-1]
        assert "不超过 12 字" in chat.calls[-1]["system_prompt"]
        assert len(recording.sent) == sent_before + 1 and isinstance(recording.sent[-1][1].chain[0], Record)
        assert plugin.engine.events[0]["mode"] == "both"
        checks.append("「仅语音」遇到超长回复：文字照发全文，Context.llm_generate 写的短版另行合成发送")

        # ---- 译文超过朗读上限：「仅语音」也先发文字，再由真实的发送后钩子补发语音 ----
        await set_profile(send_mode="voice_only", speak_max_chars=10, voice_language="ja")
        chat.reply = "ドクター！やっと戻られたんですね！今日の作戦は本当に順調でした。"
        sent_before = len(recording.sent)
        event = await decorate_reply("博士，欢迎回来。", "说给我听")
        assert [c.text for c in event.get_result().chain if isinstance(c, Plain)] == ["博士，欢迎回来。"]
        assert len(recording.sent) == sent_before, "文字发出之前不应先发语音"
        assert "长度不超过 10 个字" in chat.calls[-1]["system_prompt"]
        await call_event_hook(event, EventType.OnAfterMessageSentEvent)
        assert len(recording.sent) == sent_before + 1 and isinstance(recording.sent[-1][1].chain[0], Record)
        assert tts_ja.texts[-1] == "ドクター！", tts_ja.texts[-1]
        checks.append("译文超过朗读上限时截到句末；「仅语音」保留文字，语音经真实 OnAfterMessageSentEvent 钩子在文字之后补发")
    finally:
        await plugin.terminate()
        await db_helper.engine.dispose()

    print(json.dumps({
        "ok": True,
        "astrbot_version": astrbot.__version__,
        "mode": "offline-real-sdk",
        "external_model_verified": False,
        "external_tts_verified": False,
        "external_platform_delivery_verified": False,
        "checks": checks,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    source = Path(args.astrbot_path).expanduser().resolve()
    if not (source / "astrbot" / "__init__.py").is_file():
        raise SystemExit("--astrbot-path 必须包含 astrbot 包")
    sys.dont_write_bytecode = True
    for directory in args.site_packages:
        sys.path.append(str(Path(directory).expanduser().resolve()))
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(ROOT.parent))
    qa = (ROOT / ".qa").resolve()
    qa.mkdir(exist_ok=True)
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="astrbot-smoke-", dir=qa) as temporary:
        isolation = Path(temporary).resolve()
        if isolation.parent != qa:
            raise SystemExit("拒绝在 .qa 之外创建冒烟数据目录")
        os.environ["ASTRBOT_ROOT"] = str(isolation)
        os.chdir(isolation)
        try:
            asyncio.run(run_smoke(isolation))
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    main()
