"""WebUI 页面接口。只做分发和校验，不依赖 AstrBot 的 Web 框架。"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Any

from . import VERSION
from .decide import decide, effective_probability, effective_send_mode, find_emotion
from .engine import Engine, describe_error, filter_text
from .settings import Profile, Settings

GET_ROUTES = ("state",)
POST_ROUTES = ("settings", "profiles/save", "profiles/delete", "preview", "synthesize")

MAX_TEST_CHARS = 500
MAX_AUDIO_BYTES = 10 * 1024 * 1024
_GLOBAL_KEYS = (
    "enabled",
    "tts_provider_id",
    "default_profile",
    "synth_timeout",
    "queue_expire",
    "use_file_service",
    "debug",
    "session_profiles",
    "muted_sessions",
    "voice_languages",
    "llm_provider_id",
    "llm_timeout",
    "translate_fail",
    "pronunciations",
)
FOLLOW_PROFILE = "@profile"
_AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".opus": "audio/ogg"}


async def dispatch(engine: Engine, method: str, path: str, body: dict | None = None) -> Any:
    body = body or {}
    if method == "GET" and path == "state":
        return state(engine)
    if method == "POST":
        if path == "settings":
            return save_settings(engine, body)
        if path == "profiles/save":
            return save_profile(engine, body)
        if path == "profiles/delete":
            return delete_profile(engine, body)
        if path == "preview":
            return preview(engine, body)
        if path == "synthesize":
            return await synthesize(engine, body)
    raise LookupError(f"未知接口：{method} {path}")


def state(engine: Engine) -> dict[str, Any]:
    settings = engine.settings
    try:
        providers = engine.host.list_tts_providers()
    except Exception as exc:
        providers = []
        provider_error = describe_error(exc)
    else:
        provider_error = ""
    try:
        diagnostics = engine.host.diagnostics()
    except Exception as exc:
        diagnostics = {"error": describe_error(exc)}
    try:
        chat_providers = engine.host.list_chat_providers()
    except Exception:
        chat_providers = []
    default_profile = settings.profiles[settings.default_profile]
    return {
        "version": VERSION,
        "settings": settings.to_dict(),
        "providers": providers,
        "chat_providers": chat_providers,
        "provider_error": provider_error,
        "resolved_provider": engine.resolve_provider_id(default_profile),
        "diagnostics": diagnostics,
        "runtime": engine.runtime_status(),
    }


def save_settings(engine: Engine, body: dict) -> dict[str, Any]:
    data = engine.settings.to_dict()
    for key in _GLOBAL_KEYS:
        if key in body:
            data[key] = body[key]
    engine.save_settings(Settings.from_dict(data))
    return state(engine)


def save_profile(engine: Engine, body: dict) -> dict[str, Any]:
    original = body.get("original") or None
    profile = Profile.from_dict(body.get("profile"))
    data = engine.settings.to_dict()
    profiles: dict[str, Any] = data["profiles"]
    if original and original != profile.name:
        if original not in profiles:
            raise LookupError(f"档案「{original}」不存在")
        if profile.name in profiles:
            raise ValueError(f"已有名为「{profile.name}」的档案")
        profiles = {
            (profile.name if name == original else name): value
            for name, value in profiles.items()
        }
        if data["default_profile"] == original:
            data["default_profile"] = profile.name
        data["session_profiles"] = {
            umo: (profile.name if name == original else name)
            for umo, name in data["session_profiles"].items()
        }
    elif not original and profile.name in profiles:
        raise ValueError(f"已有名为「{profile.name}」的档案")
    profiles[profile.name] = profile.to_dict()
    data["profiles"] = profiles
    engine.save_settings(Settings.from_dict(data))
    return state(engine)


def delete_profile(engine: Engine, body: dict) -> dict[str, Any]:
    name = body.get("name")
    data = engine.settings.to_dict()
    if name not in data["profiles"]:
        raise LookupError(f"档案「{name}」不存在")
    if name == data["default_profile"]:
        raise ValueError("不能删除当前的默认档案，请先把别的档案设为默认")
    del data["profiles"][name]
    data["session_profiles"] = {
        umo: profile for umo, profile in data["session_profiles"].items() if profile != name
    }
    engine.save_settings(Settings.from_dict(data))
    return state(engine)


def _pick_profile(engine: Engine, name: Any) -> Profile:
    settings = engine.settings
    return settings.profiles.get(name) or settings.profiles[settings.default_profile]


def _pick_language(engine: Engine, profile: Profile, value: Any) -> str:
    """页面传来的语言：@profile 跟随档案；空表示和文字相同；其余必须是已配置的语言。"""
    if value is None or value == FOLLOW_PROFILE:
        return profile.voice_language
    code = engine.normalize_language(str(value))
    if code is None:
        raise ValueError(f"没有配置语言「{value}」")
    return code


def preview(engine: Engine, body: dict) -> dict[str, Any]:
    """试算过滤结果和判定路径。不计冷却，概率只说明会不会进入概率判定；不调用 LLM。"""
    profile = _pick_profile(engine, body.get("profile"))
    text = str(body.get("text") or "")[:5000]
    filtered = filter_text(profile, text)
    is_group = bool(body.get("is_group"))
    emotion = find_emotion(profile, text)
    decision = decide(
        profile,
        text=filtered,
        user_text=str(body.get("user_text") or ""),
        is_group=is_group,
        plugin_enabled=engine.settings.enabled,
        rand=0.0,
        tool_request=bool(body.get("simulate_tool")),
        user_voice=bool(body.get("simulate_voice")),
        emotion_hit=emotion,
    )
    result = decision.to_dict()
    probability, label = effective_probability(profile, emotion)
    if decision.code == "probability_hit":
        result["reason"] = f"满足被动触发条件，每次有 {probability:.0%} 的机会出声"
        result["trace"][-1].update(state="chance", note=f"{label} 机会")
    elif decision.code == "probability_miss":
        result["reason"] = "概率为 0，被动触发不会出声"
    language = _pick_language(engine, profile, body.get("language"))
    lang = engine.settings.language(language) if language else None
    return {
        "profile": profile.name,
        "filtered": filtered,
        "chars": len(filtered),
        "max_chars": profile.max_chars,
        "send_mode": effective_send_mode(profile, is_group),
        "language": language,
        "language_name": lang["name"] if lang else "",
        "uses_llm": bool(lang) or profile.filter_mode == "llm",
        "decision": result,
    }


async def synthesize(engine: Engine, body: dict) -> dict[str, Any]:
    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("请输入要合成的文字")
    if len(text) > MAX_TEST_CHARS:
        raise ValueError(f"试听文字最多 {MAX_TEST_CHARS} 字")
    profile = _pick_profile(engine, body.get("profile"))
    if body.get("filter", True):
        text = filter_text(profile, text)
        if not text:
            raise ValueError("过滤后没有可朗读的内容")
    language = _pick_language(engine, profile, body.get("language"))
    try:
        spoken, audio_path, elapsed = await engine.speak_text(profile, text, language)
    except Exception as exc:
        raise RuntimeError(f"合成失败：{describe_error(exc)}") from exc
    data, mime = await asyncio.to_thread(_read_audio, Path(audio_path))
    lang = engine.settings.language(spoken.language) if spoken.language else None
    return {
        "text": spoken.text,
        "language": spoken.language,
        "language_name": lang["name"] if lang else "",
        "llm_used": spoken.llm_used,
        "llm_ms": spoken.llm_ms,
        "note": spoken.note,
        "provider_id": spoken.provider_id,
        "elapsed_ms": elapsed,
        "bytes": len(data),
        "audio": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
    }


def _read_audio(path: Path) -> tuple[bytes, str]:
    size = path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        raise ValueError(f"音频文件过大（{size // 1024} KB），无法在页面试听")
    mime = _AUDIO_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    return path.read_bytes(), mime or "application/octet-stream"


__all__ = ["GET_ROUTES", "POST_ROUTES", "dispatch"]
