from __future__ import annotations

import asyncio
import re
import time
import traceback
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import PLUGIN_NAME, VERSION
from .core.engine import Engine, Plan, describe_error, filter_text
from .core.webapi import GET_ROUTES, POST_ROUTES, dispatch

PAGE_PREFIX = f"/{PLUGIN_NAME}/page"
STREAMING_WARN_INTERVAL = 600
MODE_LABELS = {"both": "文字+语音", "voice_only": "仅语音", "text_only": "仅文字"}
_SAY_PREFIX = re.compile(r"^\s*[/!！]?ftts\s+say\b\s*", re.I)


class AstrBotHost:
    """把 AstrBot 的 TTS 服务和配置暴露给核心逻辑。"""

    def __init__(self, context: Context):
        self.context = context

    def list_tts_providers(self) -> list[dict[str, Any]]:
        result = []
        for provider in self.context.get_all_tts_providers() or []:
            config = getattr(provider, "provider_config", None) or {}
            result.append({"id": str(config.get("id", "")), "type": str(config.get("type", ""))})
        return [item for item in result if item["id"]]

    async def synthesize(self, provider_id: str, text: str) -> str:
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise LookupError(f"找不到 TTS 服务「{provider_id}」")
        if not callable(getattr(provider, "get_audio", None)):
            raise LookupError(f"「{provider_id}」不是 TTS 服务")
        path = await provider.get_audio(text)
        if not path or not Path(path).is_file():
            raise RuntimeError("TTS 服务没有返回音频文件")
        return str(path)

    def diagnostics(self) -> dict[str, Any]:
        config = self.context.get_config()
        tts = config.get("provider_tts_settings") or {}
        provider = config.get("provider_settings") or {}
        return {
            "builtin_tts_enabled": bool(tts.get("enable")),
            "streaming_enabled": bool(provider.get("streaming_response")),
            "callback_api_base": bool(str(config.get("callback_api_base") or "").strip()),
            "page_api": callable(getattr(self.context, "register_web_api", None)),
        }


def _web_adapter():
    """新版 AstrBot 提供 astrbot.api.web；旧版退回 quart。"""
    try:
        from astrbot.api.web import error_response, json_response, request

        return request, json_response, error_response, True
    except ImportError:
        from quart import jsonify, request

        def error(message: str, status_code: int = 400):
            return jsonify({"status": "error", "message": message}), status_code

        return request, jsonify, error, False


@register(
    PLUGIN_NAME,
    "iownmmiku",
    "Freezia TTS Speech：按档案决定何时开口，清洗文本后交给 TTS 合成",
    VERSION,
    "https://github.com/HuangGuaKnn/astrbot_plugin_Freezia_TTS_Speech",
)
class FreeziaTTS(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.host = AstrBotHost(context)
        self.engine = Engine(self.host, Path(StarTools.get_data_dir(PLUGIN_NAME)))
        self._tasks: set[asyncio.Task] = set()
        self._last_streaming_warn = 0.0

    async def initialize(self) -> None:
        self._register_page_api()
        try:
            diagnostics = self.host.diagnostics()
        except Exception:
            return
        if diagnostics.get("builtin_tts_enabled"):
            logger.warning(
                "[Freezia TTS] AstrBot 自带的 TTS 仍处于开启状态，会和本插件重复合成。"
                "请在 AstrBot 配置的「AI」分组里关闭「语音回复」（provider_tts_settings.enable），TTS 服务提供商保留。"
            )
        if diagnostics.get("streaming_enabled"):
            logger.warning("[Freezia TTS] AstrBot 开启了流式输出，本插件在流式回复中不会工作。")

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.engine.stop()

    # ---------- WebUI ----------
    def _register_page_api(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            logger.warning("[Freezia TTS] 当前 AstrBot 不支持插件页面接口，WebUI 不可用。")
            return
        for suffix in GET_ROUTES:
            register_api(f"{PAGE_PREFIX}/{suffix}", self._web_handler(suffix), ["GET"], f"Freezia TTS: {suffix}")
        for suffix in POST_ROUTES:
            register_api(f"{PAGE_PREFIX}/{suffix}", self._web_handler(suffix), ["POST"], f"Freezia TTS: {suffix}")

    def _web_handler(self, suffix: str):
        async def handler(**_kwargs):
            request, jsonify, error, modern = _web_adapter()
            try:
                body = None
                if request.method == "POST":
                    body = await request.json(default={}) if modern else await request.get_json(silent=True)
                    if not isinstance(body, dict):
                        raise ValueError("请求必须是 JSON 对象")
                return jsonify(await dispatch(self.engine, request.method, suffix, body))
            except LookupError as exc:
                return error(str(exc) or "找不到对应内容", status_code=404)
            except Exception as exc:
                logger.debug(f"[Freezia TTS] 页面接口 {suffix} 出错：{traceback.format_exc()}")
                return error(str(exc) or "操作失败，请查看 AstrBot 日志", status_code=400)

        handler.__name__ = "freezia_tts_" + suffix.replace("/", "_")
        return handler

    # ---------- 回复钩子 ----------
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        try:
            await self._handle_result(event)
        except Exception:
            # 任何异常都不能影响文字回复
            logger.error(f"[Freezia TTS] 处理回复时出错，本次只发文字：{traceback.format_exc()}")

    async def _handle_result(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if result is None or not result.chain:
            return
        content_type = getattr(getattr(result, "result_content_type", None), "name", "")
        if content_type.startswith("STREAMING"):
            self._warn_streaming()
            return
        if not result.is_llm_result():
            return
        reply = "".join(comp.text for comp in result.chain if isinstance(comp, Comp.Plain))
        if not reply.strip():
            return

        plan = self.engine.plan(
            umo=event.unified_msg_origin,
            reply_text=reply,
            user_text=event.message_str or "",
            is_group=not event.is_private_chat(),
        )
        if self.engine.settings.debug:
            logger.info(f"[Freezia TTS] {plan.umo} · {plan.profile.name} · {plan.decision.reason}")
        if not plan.decision.speak:
            return

        if plan.send_mode == "voice_only":
            try:
                path = await self.engine.synthesize(plan)
                record = await self._record(path)
            except Exception as exc:
                logger.warning(f"[Freezia TTS] 合成失败，改发文字：{describe_error(exc)}")
                return
            chain, placed = [], False
            for comp in result.chain:
                if isinstance(comp, Comp.Plain):
                    if not placed:
                        chain.append(record)
                        placed = True
                    continue
                chain.append(comp)
            result.chain = chain
            self.engine.mark_sent(plan)
            return

        # both：文字照常发出，语音合成好后补发
        task = asyncio.create_task(self._send_voice_later(plan))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_voice_later(self, plan: Plan) -> None:
        try:
            path = await self.engine.synthesize(plan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[Freezia TTS] 合成失败，本次只有文字：{describe_error(exc)}")
            return
        try:
            record = await self._record(path)
            sent = await self.context.send_message(plan.umo, MessageChain(chain=[record]))
            if sent is False:
                raise RuntimeError("找不到会话对应的平台")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.engine.mark_failed(plan, f"发送失败：{describe_error(exc)}")
            logger.warning(f"[Freezia TTS] 语音发送失败：{describe_error(exc)}")
            return
        self.engine.mark_sent(plan)

    async def _record(self, path: str) -> Comp.Record:
        url = None
        if self.engine.settings.use_file_service:
            base = str(self.context.get_config().get("callback_api_base") or "").strip().rstrip("/")
            if base:
                from astrbot.core import file_token_service

                token = await file_token_service.register_file(path)
                url = f"{base}/api/file/{token}"
            else:
                logger.warning("[Freezia TTS] 已开启文件服务，但 AstrBot 没有设置 callback_api_base，改用本地路径。")
        return Comp.Record(file=url or path, url=url or path)

    def _warn_streaming(self) -> None:
        now = time.monotonic()
        if now - self._last_streaming_warn >= STREAMING_WARN_INTERVAL:
            self._last_streaming_warn = now
            logger.warning("[Freezia TTS] 检测到流式输出，本插件在流式回复中不可用，已跳过。如需语音请关闭流式输出。")

    # ---------- 指令 ----------
    @filter.command_group("ftts")
    def ftts(self):
        """Freezia TTS Speech"""

    @ftts.command("status")
    async def ftts_status(self, event: AstrMessageEvent):
        """查看本会话的语音状态"""
        umo = event.unified_msg_origin
        settings = self.engine.settings
        profile = settings.resolve_profile(umo)
        provider_id = self.engine.resolve_provider_id(profile) or "未配置"
        lines = [
            f"Freezia TTS Speech {VERSION}",
            f"插件：{'开启' if settings.enabled else '关闭'}",
            f"本会话：{'静音' if settings.is_muted(umo) else '正常'}",
            f"档案：{profile.name}（概率 {profile.probability:.0%}，被动上限 {profile.max_chars} 字，"
            f"{MODE_LABELS.get(profile.send_mode, profile.send_mode)}）",
            f"TTS 服务：{provider_id}",
            f"会话 ID：{umo}",
        ]
        try:
            diagnostics = self.host.diagnostics()
        except Exception:
            diagnostics = {}
        if diagnostics.get("builtin_tts_enabled"):
            lines.append("⚠ AstrBot 自带 TTS 仍开启，会重复合成，请关闭")
        if diagnostics.get("streaming_enabled"):
            lines.append("⚠ 流式输出已开启，本插件不会工作")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ftts.command("on")
    async def ftts_on(self, event: AstrMessageEvent):
        """恢复本会话的语音"""
        self.engine.set_muted(event.unified_msg_origin, False)
        yield event.plain_result("本会话已恢复语音。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ftts.command("off")
    async def ftts_off(self, event: AstrMessageEvent):
        """本会话静音（只发文字）"""
        self.engine.set_muted(event.unified_msg_origin, True)
        yield event.plain_result("本会话已静音，之后只发文字。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ftts.command("say")
    async def ftts_say(self, event: AstrMessageEvent):
        """直接合成一段文字：/ftts say 文字"""
        text = _SAY_PREFIX.sub("", event.message_str or "", count=1).strip()
        if not text:
            yield event.plain_result("用法：/ftts say 要念的文字")
            return
        settings = self.engine.settings
        profile = settings.resolve_profile(event.unified_msg_origin)
        cleaned = filter_text(profile, text)
        if not cleaned:
            yield event.plain_result("过滤后没有可朗读的内容。")
            return
        try:
            path, elapsed = await self.engine.synthesize_text(
                cleaned, self.engine.resolve_provider_id(profile)
            )
            record = await self._record(path)
        except Exception as exc:
            yield event.plain_result(f"合成失败：{describe_error(exc)}")
            return
        if settings.debug:
            logger.info(f"[Freezia TTS] /ftts say 合成耗时 {elapsed} ms")
        yield event.chain_result([record])
