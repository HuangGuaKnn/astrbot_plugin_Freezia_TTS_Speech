"""配置：全局设置 + 档案（profile），保存在插件数据目录的 settings.json。"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SEND_MODES = ("both", "voice_only", "text_only")
# regex：只用正则剥离；llm：正则剥离后再让 LLM 口语化清洗；off：不过滤
FILTER_MODES = ("regex", "llm", "off")
TRANSLATE_FAIL = ("skip", "original")
DEFAULT_PROFILE = "default"

DEFAULT_FORCE_PHRASES = ["说给我听", "念给我听", "用语音说", "出声"]
DEFAULT_MUTE_PHRASES = ["别出声", "不要出声", "打字就好", "别用语音"]
DEFAULT_EMOTION_MARKERS = ["！", "!", "♡", "❤", "哈哈"]
# 情绪音色可选的情绪（固定一组，方便写进工具说明和提示词）；neutral 表示用默认声音
EMOTIONS = {
    "happy": "开心",
    "sad": "难过",
    "angry": "生气",
    "surprised": "惊讶",
    "shy": "害羞",
    "gentle": "温柔",
}
NEUTRAL = "neutral"
NEUTRAL_NAME = "平静"

_PROFILE_NAME = re.compile(r"^[\w\-一-鿿]{1,32}$")
_LANGUAGE_CODE = re.compile(r"^[A-Za-z][\w\-]{0,15}$")
MAX_PHRASES = 50
MAX_PHRASE_LEN = 20
MAX_SESSIONS = 500
MAX_LANGUAGES = 20
MAX_PRONUNCIATIONS = 200
MAX_PRONUNCIATION_LEN = 40


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(max(number, low), high)


def _int(value: Any, default: int, low: int, high: int) -> int:
    return int(_number(value, default, low, high))


def _choice(value: Any, options: tuple[str, ...], default: str) -> str:
    return value if isinstance(value, str) and value in options else default


def _phrases(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\n,，]", value)
    if not isinstance(value, list):
        return list(default)
    result: list[str] = []
    for item in value:
        text = str(item).strip()[:MAX_PHRASE_LEN]
        if text and text not in result:
            result.append(text)
    return result[:MAX_PHRASES]


def valid_profile_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_PROFILE_NAME.fullmatch(name))


def valid_language_code(code: Any) -> bool:
    return isinstance(code, str) and bool(_LANGUAGE_CODE.fullmatch(code))


def _pronunciations(value: Any) -> list[dict[str, str]]:
    """发音词典条目：{"from": 原文, "to": 读法（可为空，表示删除）, "lang": 语言代码或空}。"""
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or "")[:MAX_PRONUNCIATION_LEN]
        if not source.strip():
            continue
        lang = str(item.get("lang") or "").strip()
        if lang and not valid_language_code(lang):
            lang = ""
        key = (source, lang)
        if key in seen:
            continue
        seen.add(key)
        result.append({"from": source, "to": str(item.get("to") or "")[:MAX_PRONUNCIATION_LEN], "lang": lang})
        if len(result) >= MAX_PRONUNCIATIONS:
            break
    return result


def _emotion_voices(value: Any) -> list[dict[str, Any]]:
    """情绪音色：{"emotion": 情绪代码, "provider_id": 该情绪用的 TTS 服务, "markers": 判断用的符号}。"""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        emotion = str(item.get("emotion") or "").strip()
        if emotion not in EMOTIONS or any(entry["emotion"] == emotion for entry in result):
            continue
        result.append({
            "emotion": emotion,
            "provider_id": str(item.get("provider_id") or "").strip()[:128],
            "markers": _phrases(item.get("markers"), []),
        })
    return result


def _languages(value: Any) -> list[dict[str, str]]:
    """语音语言对照：{"code": 语言代码, "name": 显示名, "provider_id": 该语言用的 TTS 服务}。"""
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not valid_language_code(code) or any(lang["code"] == code for lang in result):
            continue
        result.append({
            "code": code,
            "name": str(item.get("name") or code).strip()[:16] or code,
            "provider_id": str(item.get("provider_id") or "").strip()[:128],
        })
        if len(result) >= MAX_LANGUAGES:
            break
    return result


@dataclass
class Profile:
    """一套发声规则。全局默认用一套，也可以给某个会话单独指定。"""

    name: str = DEFAULT_PROFILE
    enabled: bool = True
    probability: float = 0.2
    max_chars: int = 80
    # 朗读字数上限：过滤后超过时由 LLM 另写一段不超过这个字数的短版来念（文字照发全文）；0 表示不限制
    speak_max_chars: int = 80
    cooldown: int = 30
    send_mode: str = "both"
    group_force_both: bool = True
    allow_group: bool = False
    allow_private: bool = True
    filter_mode: str = "regex"
    tts_provider_id: str = ""
    force_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_FORCE_PHRASES))
    mute_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_MUTE_PHRASES))
    # 语音语言：空表示和文字相同；填语言代码时先翻译再用该语言的 TTS 服务合成
    voice_language: str = ""
    # 角色可以调用语音工具主动开口；同一会话两次调用的最短间隔（秒）
    tool_enabled: bool = True
    tool_interval: int = 60
    # 用户发来语音消息时，这一轮用语音回复
    voice_to_voice: bool = True
    # 回复里出现这些符号时，被动触发概率乘以系数（1 表示不联动）
    emotion_boost: float = 1.5
    emotion_markers: list[str] = field(default_factory=lambda: list(DEFAULT_EMOTION_MARKERS))
    # 档案自己的发音词典，与全局词典合并，同一原文以档案为准
    pronunciations: list[dict[str, str]] = field(default_factory=list)
    # 情绪 → TTS 服务（同一角色不同参考音频的条目）。只在念和文字相同的语言时切换
    emotion_voices: list[dict[str, Any]] = field(default_factory=list)

    def emotion_voice(self, emotion: str) -> dict[str, Any] | None:
        return next((entry for entry in self.emotion_voices if entry["emotion"] == emotion), None)

    @classmethod
    def from_dict(cls, data: Any, name: str | None = None) -> Profile:
        data = data if isinstance(data, dict) else {}
        base = cls()
        profile_name = name if name is not None else data.get("name", base.name)
        if not valid_profile_name(profile_name):
            raise ValueError("档案名只能包含中文、字母、数字、下划线和短横线，长度 1~32")
        return cls(
            name=profile_name,
            enabled=_bool(data.get("enabled"), base.enabled),
            probability=_number(data.get("probability"), base.probability, 0.0, 1.0),
            max_chars=_int(data.get("max_chars"), base.max_chars, 1, 2000),
            speak_max_chars=_int(data.get("speak_max_chars"), base.speak_max_chars, 0, 2000),
            cooldown=_int(data.get("cooldown"), base.cooldown, 0, 86400),
            send_mode=_choice(data.get("send_mode"), SEND_MODES, base.send_mode),
            group_force_both=_bool(data.get("group_force_both"), base.group_force_both),
            allow_group=_bool(data.get("allow_group"), base.allow_group),
            allow_private=_bool(data.get("allow_private"), base.allow_private),
            filter_mode=_choice(data.get("filter_mode"), FILTER_MODES, base.filter_mode),
            tts_provider_id=str(data.get("tts_provider_id") or "").strip()[:128],
            force_phrases=_phrases(data.get("force_phrases"), base.force_phrases),
            mute_phrases=_phrases(data.get("mute_phrases"), base.mute_phrases),
            voice_language=(
                code if valid_language_code(code := str(data.get("voice_language") or "").strip()) else ""
            ),
            tool_enabled=_bool(data.get("tool_enabled"), base.tool_enabled),
            tool_interval=_int(data.get("tool_interval"), base.tool_interval, 0, 86400),
            voice_to_voice=_bool(data.get("voice_to_voice"), base.voice_to_voice),
            emotion_boost=_number(data.get("emotion_boost"), base.emotion_boost, 1.0, 5.0),
            emotion_markers=_phrases(data.get("emotion_markers"), base.emotion_markers),
            pronunciations=_pronunciations(data.get("pronunciations")),
            emotion_voices=_emotion_voices(data.get("emotion_voices")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Settings:
    enabled: bool = True
    tts_provider_id: str = ""
    default_profile: str = DEFAULT_PROFILE
    synth_timeout: float = 15.0
    queue_expire: float = 10.0
    use_file_service: bool = False
    debug: bool = False
    # 语言 → TTS 服务。AstrBot 的 TTS 服务把语言写在各自配置里，每种语言单独建一个条目
    voice_languages: list[dict[str, str]] = field(default_factory=list)
    # LLM 清洗 / 翻译用的对话模型，空表示用会话当前的模型
    llm_provider_id: str = ""
    llm_timeout: float = 8.0
    # 翻译失败时：skip 不发语音；original 念原文
    translate_fail: str = "skip"
    pronunciations: list[dict[str, str]] = field(default_factory=list)
    session_profiles: dict[str, str] = field(default_factory=dict)
    muted_sessions: list[str] = field(default_factory=list)
    profiles: dict[str, Profile] = field(
        default_factory=lambda: {DEFAULT_PROFILE: Profile()}
    )

    @classmethod
    def from_dict(cls, data: Any) -> Settings:
        data = data if isinstance(data, dict) else {}
        base = cls()
        profiles: dict[str, Profile] = {}
        raw_profiles = data.get("profiles")
        if isinstance(raw_profiles, dict):
            for key, value in raw_profiles.items():
                try:
                    profiles[key] = Profile.from_dict(value, name=key)
                except ValueError:
                    continue
        if not profiles:
            profiles = {DEFAULT_PROFILE: Profile()}
        default_profile = data.get("default_profile")
        if default_profile not in profiles:
            default_profile = DEFAULT_PROFILE if DEFAULT_PROFILE in profiles else next(iter(profiles))

        session_profiles: dict[str, str] = {}
        raw_map = data.get("session_profiles")
        if isinstance(raw_map, list):  # WebUI 以列表形式提交
            raw_map = {
                str(item.get("umo", "")): item.get("profile")
                for item in raw_map
                if isinstance(item, dict)
            }
        if isinstance(raw_map, dict):
            for umo, profile_name in raw_map.items():
                umo = str(umo).strip()
                if umo and profile_name in profiles and len(session_profiles) < MAX_SESSIONS:
                    session_profiles[umo] = profile_name

        muted: list[str] = []
        raw_muted = data.get("muted_sessions")
        if isinstance(raw_muted, list):
            for umo in raw_muted:
                umo = str(umo).strip()
                if umo and umo not in muted and len(muted) < MAX_SESSIONS:
                    muted.append(umo)

        return cls(
            enabled=_bool(data.get("enabled"), base.enabled),
            tts_provider_id=str(data.get("tts_provider_id") or "").strip()[:128],
            default_profile=default_profile,
            synth_timeout=_number(data.get("synth_timeout"), base.synth_timeout, 2.0, 120.0),
            queue_expire=_number(data.get("queue_expire"), base.queue_expire, 1.0, 120.0),
            use_file_service=_bool(data.get("use_file_service"), base.use_file_service),
            debug=_bool(data.get("debug"), base.debug),
            voice_languages=_languages(data.get("voice_languages")),
            llm_provider_id=str(data.get("llm_provider_id") or "").strip()[:128],
            llm_timeout=_number(data.get("llm_timeout"), base.llm_timeout, 2.0, 60.0),
            translate_fail=_choice(data.get("translate_fail"), TRANSLATE_FAIL, base.translate_fail),
            pronunciations=_pronunciations(data.get("pronunciations")),
            session_profiles=session_profiles,
            muted_sessions=muted,
            profiles=profiles,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["profiles"] = {name: profile.to_dict() for name, profile in self.profiles.items()}
        return data

    def resolve_profile(self, umo: str) -> Profile:
        """会话指定的档案 → 全局默认档案。"""
        name = self.session_profiles.get(umo) or self.default_profile
        return self.profiles.get(name) or self.profiles.get(self.default_profile) or Profile()

    def is_muted(self, umo: str) -> bool:
        return umo in self.muted_sessions

    def language(self, code: str) -> dict[str, str] | None:
        return next((lang for lang in self.voice_languages if lang["code"] == code), None)


class SettingsStore:
    """settings.json 的读写。写入走临时文件 + 替换，避免写一半损坏。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.settings = self._load()

    def _load(self) -> Settings:
        if not self.path.is_file():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            backup = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return Settings()
        return Settings.from_dict(data)

    def save(self, settings: Settings | None = None) -> Settings:
        if settings is not None:
            self.settings = settings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(self.settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        return self.settings
