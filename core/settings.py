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
FILTER_MODES = ("regex", "off")
DEFAULT_PROFILE = "default"

DEFAULT_FORCE_PHRASES = ["说给我听", "念给我听", "用语音说", "出声"]
DEFAULT_MUTE_PHRASES = ["别出声", "不要出声", "打字就好", "别用语音"]

_PROFILE_NAME = re.compile(r"^[\w\-一-鿿]{1,32}$")
MAX_PHRASES = 50
MAX_PHRASE_LEN = 20
MAX_SESSIONS = 500


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


@dataclass
class Profile:
    """一套发声规则。全局默认用一套，也可以给某个会话单独指定。"""

    name: str = DEFAULT_PROFILE
    enabled: bool = True
    probability: float = 0.2
    max_chars: int = 80
    cooldown: int = 30
    send_mode: str = "both"
    group_force_both: bool = True
    allow_group: bool = False
    allow_private: bool = True
    filter_mode: str = "regex"
    tts_provider_id: str = ""
    force_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_FORCE_PHRASES))
    mute_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_MUTE_PHRASES))

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
            cooldown=_int(data.get("cooldown"), base.cooldown, 0, 86400),
            send_mode=_choice(data.get("send_mode"), SEND_MODES, base.send_mode),
            group_force_both=_bool(data.get("group_force_both"), base.group_force_both),
            allow_group=_bool(data.get("allow_group"), base.allow_group),
            allow_private=_bool(data.get("allow_private"), base.allow_private),
            filter_mode=_choice(data.get("filter_mode"), FILTER_MODES, base.filter_mode),
            tts_provider_id=str(data.get("tts_provider_id") or "").strip()[:128],
            force_phrases=_phrases(data.get("force_phrases"), base.force_phrases),
            mute_phrases=_phrases(data.get("mute_phrases"), base.mute_phrases),
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
