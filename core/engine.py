"""把配置、过滤、决策、清洗 / 翻译、调度串起来。不依赖 AstrBot，宿主能力通过 Host 注入。"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .decide import Decision, contains_any, decide, effective_send_mode, find_emotion
from .scheduler import SynthExpired, SynthScheduler, SynthTimeout
from .settings import EMOTIONS, NEUTRAL, Profile, Settings, SettingsStore
from .speech import (
    MAX_LLM_INPUT,
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
from .text_filter import clean_for_tts, normalize_only

MAX_EVENTS = 100
PREVIEW_CHARS = 60
SOURCE_BY_CODE = {"force_phrase": "user", "tool_request": "tool", "voice_message": "voice"}
_SAME_LANGUAGE = {"", "auto", "same", "default", "原文", "跟随"}
# 情绪来源：tool 角色调用工具时指定；manual 试听时手动选；llm 清洗时模型判断；marker 按符号粗判
FIXED_EMOTION_SOURCES = ("tool", "manual")


class Host(Protocol):
    """宿主（AstrBot 或开发用的假宿主）需要提供的能力。"""

    def list_tts_providers(self) -> list[dict[str, Any]]: ...

    def list_chat_providers(self) -> list[dict[str, Any]]: ...

    async def synthesize(self, provider_id: str, text: str) -> str: ...

    async def chat(self, provider_id: str, system_prompt: str, prompt: str, umo: str = "") -> str: ...

    def diagnostics(self) -> dict[str, Any]: ...


class SpeechError(Exception):
    """准备朗读文本失败，例如翻译失败且设置为不发语音。"""


@dataclass
class Spoken:
    """最终交给 TTS 的内容。"""

    text: str
    language: str  # 空表示和文字相同
    provider_id: str
    llm_used: bool = False
    llm_ms: int | None = None
    note: str = ""
    emotion: str = ""  # 空表示没有判断情绪（档案没配情绪音色或念外语）
    emotion_source: str = ""
    shortened: bool = False  # 念的是缩写或截取后的内容，不是全文


@dataclass
class Plan:
    event_id: int
    umo: str
    profile: Profile
    text: str
    decision: Decision
    send_mode: str
    language: str = ""
    source: str = "passive"
    previous_voice_at: float | None = None
    emotion: str = ""
    emotion_source: str = ""
    shortened: bool = False  # 合成后填写：语音不是全文


def filter_text(profile: Profile, text: str) -> str:
    return normalize_only(text) if profile.filter_mode == "off" else clean_for_tts(text)


def needs_shortening(profile: Profile, text: str) -> bool:
    """过滤后超过朗读字数上限：要另写一段短版来念。"""
    return bool(profile.speak_max_chars) and len(text) > profile.speak_max_chars


def guess_emotion(profile: Profile, raw_text: str) -> tuple[str, str, str]:
    """按符号粗判情绪（用过滤前的原文，括号里的「哭」「笑」也算）。返回 (情绪, 来源, 命中的符号)。

    档案没配情绪音色时不判断，全部为空。
    """
    if not profile.emotion_voices:
        return "", "", ""
    hit = emotion_by_markers(profile.emotion_voices, raw_text)
    return (hit[0], "marker", hit[1]) if hit else ("", "", "")


def emotion_label(code: str) -> str:
    return EMOTIONS.get(code) or ("平静" if code == NEUTRAL else code)


def _short(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class Engine:
    def __init__(self, host: Host, data_dir: Path):
        self.host = host
        self.store = SettingsStore(Path(data_dir) / "settings.json")
        self.scheduler = SynthScheduler(self.host.synthesize)
        self._apply_runtime_settings()
        self.last_voice_at: dict[str, float] = {}
        self.last_tool_at: dict[str, float] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._event_ids = itertools.count(1)
        self.counters = {
            "decided": 0, "speak": 0, "sent": 0, "failed": 0,
            "tool": 0, "translated": 0, "shortened": 0, "llm_failed": 0,
        }

    # ---------- 配置 ----------
    @property
    def settings(self) -> Settings:
        return self.store.settings

    def save_settings(self, settings: Settings) -> Settings:
        self.store.save(settings)
        self._apply_runtime_settings()
        return settings

    def _apply_runtime_settings(self) -> None:
        self.scheduler.expire = self.settings.queue_expire
        self.scheduler.timeout = self.settings.synth_timeout

    def set_muted(self, umo: str, muted: bool) -> None:
        data = self.settings.to_dict()
        sessions = [s for s in data["muted_sessions"] if s != umo]
        if muted:
            sessions.append(umo)
        data["muted_sessions"] = sessions
        self.save_settings(Settings.from_dict(data))

    def resolve_provider_id(self, profile: Profile) -> str:
        if profile.tts_provider_id:
            return profile.tts_provider_id
        if self.settings.tts_provider_id:
            return self.settings.tts_provider_id
        providers = self.host.list_tts_providers()
        return str(providers[0]["id"]) if providers else ""

    def provider_for(self, profile: Profile, language: str, emotion: str = "") -> str:
        """外语用语言对照表里的服务；和文字相同的语言先看情绪音色，再用档案 / 全局的服务。"""
        if not language:
            entry = profile.emotion_voice(emotion) if emotion else None
            if entry and entry["provider_id"]:
                return entry["provider_id"]
            return self.resolve_provider_id(profile)
        lang = self.settings.language(language)
        if not lang:
            raise LookupError(f"没有配置语言「{language}」")
        if not lang["provider_id"]:
            raise LookupError(f"没有给「{lang['name']}」指定 TTS 服务")
        return lang["provider_id"]

    def normalize_language(self, value: str) -> str | None:
        """把工具或页面传来的语言（代码或显示名）规整成已配置的代码；空表示和文字相同；不认识返回 None。"""
        value = (value or "").strip()
        if value.lower() in _SAME_LANGUAGE:
            return ""
        for lang in self.settings.voice_languages:
            if value.lower() in (lang["code"].lower(), lang["name"].lower()):
                return lang["code"]
        return None

    # ---------- 角色的语音工具 ----------
    def request_voice(
        self, *, umo: str, user_text: str, language: str = "", emotion: str = "", now: float | None = None
    ) -> dict:
        """角色调用语音工具时的检查。返回给 LLM 的结果；ok 为真时调用方给事件打上标记。

        情绪写错不拒绝，改为自动判断。
        """
        settings = self.settings
        profile = settings.resolve_profile(umo)
        now = time.monotonic() if now is None else now

        def refuse(reason: str) -> dict:
            return {"ok": False, "reason": reason}

        if not settings.enabled:
            return refuse("语音功能已关闭")
        if not profile.enabled or profile.send_mode == "text_only":
            return refuse("这个会话不发送语音")
        if not profile.tool_enabled:
            return refuse("这个会话不允许主动使用语音")
        if settings.is_muted(umo):
            return refuse("这个会话被设为静音")
        mute_hit = contains_any(user_text, profile.mute_phrases)
        if mute_hit:
            return refuse(f"用户说了「{mute_hit}」，这次只打字")
        if (language or "").strip():
            code = self.normalize_language(language)
            if code is None:
                available = "、".join(f"{lang['code']}（{lang['name']}）" for lang in settings.voice_languages)
                return refuse(f"没有配置这个语言。可用：{available or '只能用和文字相同的语言'}")
        else:
            code = profile.voice_language
        last = self.last_tool_at.get(umo)
        if last is not None and profile.tool_interval and now - last < profile.tool_interval:
            return refuse(f"刚用过语音，{profile.tool_interval - (now - last):.0f} 秒后才能再用")
        mood = normalize_emotion(emotion)
        note = "本轮回复会以语音发送。正常写出要说的话，不要提到工具或语音合成。"
        if mood is None:
            mood = ""
            note += f"（没有「{emotion.strip()}」这种情绪，将自动判断）"
        self.last_tool_at[umo] = now
        self.counters["tool"] += 1
        return {"ok": True, "language": code, "emotion": mood, "note": note}

    # ---------- 决策 ----------
    def plan(
        self,
        *,
        umo: str,
        reply_text: str,
        user_text: str,
        is_group: bool,
        voice_request: dict | None = None,
        user_voice: bool = False,
        now: float | None = None,
        rand: float | None = None,
    ) -> Plan:
        settings = self.settings
        profile = settings.resolve_profile(umo)
        text = filter_text(profile, reply_text)
        now = time.monotonic() if now is None else now
        previous = self.last_voice_at.get(umo)
        decision = decide(
            profile,
            text=text,
            user_text=user_text,
            is_group=is_group,
            plugin_enabled=settings.enabled,
            session_muted=settings.is_muted(umo),
            now=now,
            last_voice_at=previous,
            rand=random.random() if rand is None else rand,
            tool_request=bool(voice_request),
            user_voice=user_voice,
            emotion_hit=find_emotion(profile, reply_text),
        )
        language = voice_request.get("language", profile.voice_language) if voice_request else profile.voice_language
        source = SOURCE_BY_CODE.get(decision.code, "passive")
        emotion, emotion_source, _ = guess_emotion(profile, reply_text)
        requested = (voice_request or {}).get("emotion") or ""
        if requested and profile.emotion_voices:
            emotion, emotion_source = requested, "tool"
        send_mode = effective_send_mode(profile, is_group)
        if send_mode == "voice_only" and needs_shortening(profile, text):
            send_mode = "both"  # 念的是缩写版，全文还要用文字发出去
        plan = Plan(
            event_id=next(self._event_ids),
            umo=umo,
            profile=profile,
            text=text,
            decision=decision,
            send_mode=send_mode,
            language=language,
            source=source,
            previous_voice_at=previous,
            emotion=emotion,
            emotion_source=emotion_source,
        )
        self.counters["decided"] += 1
        if decision.speak:
            self.counters["speak"] += 1
            # 先占住冷却，避免合成期间下一条回复也触发；失败时再还原
            self.last_voice_at[umo] = now
        self.events.appendleft(
            {
                "id": plan.event_id,
                "time": time.time(),
                "umo": umo,
                "profile": profile.name,
                "group": is_group,
                "text": _short(text),
                "chars": len(text),
                "code": decision.code,
                "stage": next(
                    (item["key"] for item in decision.trace if item["state"] in ("stop", "speak")),
                    "",
                ),
                "reason": decision.reason,
                "forced": decision.forced,
                "source": source if decision.speak else "",
                "language": language if decision.speak else "",
                "emotion": "",
                "emotion_source": "",
                "provider_id": "",
                "spoken": "",
                "mode": plan.send_mode if decision.speak else "",
                "status": "pending" if decision.speak else "skipped",
                "detail": "",
                "elapsed_ms": None,
                "llm_ms": None,
            }
        )
        return plan

    def _update_event(self, event_id: int, **changes: Any) -> None:
        for event in self.events:
            if event["id"] == event_id:
                event.update(changes)
                return

    # ---------- 朗读文本 ----------
    async def prepare(
        self,
        profile: Profile,
        text: str,
        language: str,
        umo: str = "",
        emotion: str = "",
        emotion_source: str = "",
    ) -> Spoken:
        """LLM 清洗 / 翻译（按需，顺便判断情绪）→ 发音词典 → 按语言和情绪选 TTS 服务。

        emotion / emotion_source 是事先得到的情绪（角色指定、手动选择或符号粗判）；
        角色和手动指定的不再让模型判断，符号粗判的会被模型的判断覆盖。
        """
        settings = self.settings
        lang = settings.language(language) if language else None
        if language and not lang:
            raise SpeechError(f"没有配置语言「{language}」")
        entries = merged_pronunciations(profile.pronunciations, settings.pronunciations)
        spoken, llm_ms, used, notes = text, None, False, []
        translating = lang is not None
        cleaning = profile.filter_mode == "llm" and len(text) <= MAX_LLM_INPUT
        # 情绪音色只在念和文字相同的语言时生效
        voiced = [entry["emotion"] for entry in profile.emotion_voices if entry["provider_id"]]
        ask_emotion = bool(voiced) and not translating and emotion_source not in FIXED_EMOTION_SOURCES
        # 太长的回复由 LLM 另写一段短版来念；和清洗 / 翻译 / 情绪判断合并为一次调用。
        # 原文不长但要翻译时，译文可能变长（中译日约 1.6 倍），把上限作为译文的长度预算交给模型
        cap = profile.speak_max_chars
        limit = cap if needs_shortening(profile, text) else 0
        budget = cap if translating and not limit else 0
        shortened = False
        if translating or cleaning or limit:
            started = time.monotonic()
            try:
                system, prompt = build_prompt(
                    text, lang["name"] if lang else None, entries_for(entries, language),
                    voiced if ask_emotion else None, limit, budget,
                )
                raw = await asyncio.wait_for(
                    self.host.chat(settings.llm_provider_id, system, prompt, umo), settings.llm_timeout
                )
                mood = None
                if ask_emotion:
                    mood, raw = split_emotion(raw)
                spoken = clean_output(raw, text, translating=translating, limit=limit)
                before = speech_length(spoken)
                cut = bool(cap and (limit or translating)) and before > cap
                if cut:
                    spoken = truncate_sentences(spoken, cap)  # 模型没守住上限时截到句末
                if limit:
                    shortened = True
                    notes.append(f"回复 {len(text)} 字，由 LLM 缩写为 {speech_length(spoken)} 字")
                elif cut:
                    shortened = True
                    notes.append(f"译文约 {before} 字，超过朗读上限 {cap} 字，只念前 {speech_length(spoken)} 字")
                if shortened:
                    self.counters["shortened"] += 1
                if mood:
                    emotion, emotion_source = mood, "llm"
                used = True
                if translating:
                    self.counters["translated"] += 1
            except Exception as exc:
                self.counters["llm_failed"] += 1
                reason = f"超过 {settings.llm_timeout:g} 秒" if isinstance(exc, asyncio.TimeoutError) else describe_error(exc)
                if translating and settings.translate_fail != "original":
                    raise SpeechError(f"翻译失败：{reason}") from None
                if limit:
                    spoken = truncate_sentences(text, limit)
                    shortened = True
                if translating:
                    language, lang = "", None
                    notes.append(f"翻译失败（{reason}），改念原文" + (f"前 {len(spoken)} 字" if limit else ""))
                elif limit:
                    notes.append(f"LLM 缩写失败（{reason}），改念前 {len(spoken)} 字")
                else:
                    notes.append(f"LLM 清洗失败（{reason}），使用正则结果")
            llm_ms = _ms(started)
        if language or not profile.emotion_voices:
            emotion, emotion_source = "", ""
        spoken = apply_pronunciations(spoken, entries, language)
        if not spoken:
            raise SpeechError("按发音词典处理后没有可朗读的内容")
        provider_id = self.provider_for(profile, language, emotion)
        entry = profile.emotion_voice(emotion) if emotion and not language else None
        if entry and entry["provider_id"] and not self._provider_available(entry["provider_id"]):
            notes.append(f"{emotion_label(emotion)}音色「{entry['provider_id']}」不可用，改用默认声音")
            provider_id = self.resolve_provider_id(profile)
        return Spoken(
            spoken, language, provider_id, used, llm_ms, "；".join(notes), emotion, emotion_source, shortened
        )

    def _provider_available(self, provider_id: str) -> bool:
        try:
            return any(item["id"] == provider_id for item in self.host.list_tts_providers())
        except Exception:
            return True  # 查不到列表时照常尝试，失败由合成环节报告

    # ---------- 合成 ----------
    async def synthesize(self, plan: Plan) -> str:
        """为决策通过的回复准备文本并合成语音；失败时还原冷却并把原因记进事件。"""
        started = time.monotonic()
        try:
            spoken = await self.prepare(
                plan.profile, plan.text, plan.language, plan.umo, plan.emotion, plan.emotion_source
            )
            plan.shortened = spoken.shortened
            if spoken.shortened and plan.send_mode == "voice_only":
                plan.send_mode = "both"  # 语音不是全文（例如译文太长被截短），文字还要发
            self._update_event(
                plan.event_id, spoken=_short(spoken.text), language=spoken.language,
                llm_ms=spoken.llm_ms, detail=spoken.note, emotion=spoken.emotion,
                emotion_source=spoken.emotion_source, provider_id=spoken.provider_id, mode=plan.send_mode,
            )
            if not spoken.provider_id:
                raise LookupError("没有可用的 TTS 服务，请先在 AstrBot 添加并在插件里选择")
            path = await self.scheduler.submit(spoken.provider_id, spoken.text)
        except Exception as exc:
            self.mark_failed(plan, describe_error(exc), started)
            raise
        self._update_event(plan.event_id, elapsed_ms=_ms(started))
        return path

    def mark_sent(self, plan: Plan) -> None:
        self.counters["sent"] += 1
        self._update_event(plan.event_id, status="sent")

    def mark_failed(self, plan: Plan, detail: str, started: float | None = None) -> None:
        self.counters["failed"] += 1
        if plan.previous_voice_at is None:
            self.last_voice_at.pop(plan.umo, None)
        else:
            self.last_voice_at[plan.umo] = plan.previous_voice_at
        changes: dict[str, Any] = {"status": "failed", "detail": detail}
        if started is not None:
            changes["elapsed_ms"] = _ms(started)
        self._update_event(plan.event_id, **changes)

    async def speak_text(
        self,
        profile: Profile,
        text: str,
        language: str,
        umo: str = "",
        emotion: str = "",
        emotion_source: str = "",
    ) -> tuple[Spoken, str, int]:
        """试听和 /ftts say 用：不经过决策，走清洗 / 翻译 / 词典后直接合成。返回 (内容, 音频路径, 总耗时毫秒)。"""
        started = time.monotonic()
        spoken = await self.prepare(profile, text, language, umo, emotion, emotion_source)
        if not spoken.provider_id:
            raise LookupError("没有可用的 TTS 服务，请先在 AstrBot 添加并在插件里选择")
        path = await self.scheduler.submit(spoken.provider_id, spoken.text)
        return spoken, path, _ms(started)

    def runtime_status(self) -> dict[str, Any]:
        return {
            "queue": self.scheduler.pending,
            "busy": self.scheduler.busy,
            "scheduler": dict(self.scheduler.stats),
            "counters": dict(self.counters),
            "events": list(self.events),
        }

    async def stop(self) -> None:
        await self.scheduler.stop()


def describe_error(exc: Exception) -> str:
    if isinstance(exc, (SynthExpired, SynthTimeout, LookupError, SpeechError)):
        return str(exc)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
