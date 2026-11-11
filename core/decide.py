"""决策层：这条回复要不要念出来。纯函数，便于测试。

除了结论，还记录每一步检查的结果（trace），供 WebUI 画出判定路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .settings import Profile

MODE_LABELS = {"both": "文字 + 语音", "voice_only": "仅语音", "text_only": "仅文字"}

# (键, 名称)，按判定顺序排列
STEPS = (
    ("plugin", "插件开关"),
    ("profile", "档案开关"),
    ("session", "会话静音"),
    ("send_mode", "发送方式"),
    ("mute", "不出声短语"),
    ("content", "可朗读内容"),
    ("force", "强制出声短语"),
    ("scope", "聊天范围"),
    ("length", "被动字数上限"),
    ("cooldown", "冷却"),
    ("probability", "触发概率"),
)
_LABELS = dict(STEPS)

# trace 中每一步的状态：
#   pass 通过、继续往下判；stop 在这里被拦下；speak 在这里决定出声；
#   bypass 被强制出声跳过；unreached 前面已有结论，没有走到
TRACE_STATES = ("pass", "stop", "speak", "bypass", "unreached")


@dataclass(frozen=True)
class Decision:
    speak: bool
    code: str
    reason: str
    forced: bool = False
    trace: tuple = field(default=(), compare=False)

    def to_dict(self) -> dict:
        return {
            "speak": self.speak,
            "code": self.code,
            "reason": self.reason,
            "forced": self.forced,
            "trace": [dict(item) for item in self.trace],
        }


class _Trace:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, key: str, state: str, note: str = "") -> None:
        self.items.append({"key": key, "label": _LABELS[key], "state": state, "note": note})

    def close(self, rest: str) -> tuple:
        seen = {item["key"] for item in self.items}
        for key, _label in STEPS:
            if key not in seen:
                self.add(key, rest)
        return tuple(self.items)


def _contains_any(text: str, phrases: list[str]) -> str | None:
    compact = "".join(text.split())
    for phrase in phrases:
        needle = "".join(phrase.split())
        if needle and needle in compact:
            return phrase
    return None


def effective_send_mode(profile: Profile, is_group: bool) -> str:
    """群聊里只发语音容易让人以为没回应，按档案设置改成文字+语音。"""
    if is_group and profile.group_force_both and profile.send_mode == "voice_only":
        return "both"
    return profile.send_mode


def decide(
    profile: Profile,
    *,
    text: str,
    user_text: str,
    is_group: bool,
    plugin_enabled: bool = True,
    session_muted: bool = False,
    now: float = 0.0,
    last_voice_at: float | None = None,
    rand: float = 0.0,
) -> Decision:
    """按优先级判定，命中即停。

    text 是过滤后的回复文本，user_text 是用户这条消息（指令短语只在这里找）。
    rand 由调用方传入 [0, 1) 的随机数，测试时可固定。
    """
    trace = _Trace()

    def done(speak: bool, code: str, reason: str, forced: bool = False, rest: str = "unreached"):
        return Decision(speak, code, reason, forced, trace.close(rest))

    if not plugin_enabled:
        trace.add("plugin", "stop", "已关闭")
        return done(False, "plugin_off", "插件已全局关闭")
    trace.add("plugin", "pass", "已开启")

    if not profile.enabled:
        trace.add("profile", "stop", f"「{profile.name}」已关闭")
        return done(False, "profile_off", f"档案「{profile.name}」已关闭")
    trace.add("profile", "pass", profile.name)

    if session_muted:
        trace.add("session", "stop", "已静音")
        return done(False, "session_muted", "这个会话被设为静音")
    trace.add("session", "pass", "未静音")

    if profile.send_mode == "text_only":
        trace.add("send_mode", "stop", "仅文字")
        return done(False, "text_only", "档案的发送方式是仅文字")
    trace.add("send_mode", "pass", MODE_LABELS.get(profile.send_mode, profile.send_mode))

    # 先判「不出声」：否则「出声」会命中「别出声」
    mute_hit = _contains_any(user_text, profile.mute_phrases)
    if mute_hit:
        trace.add("mute", "stop", f"含「{mute_hit}」")
        return done(False, "mute_phrase", f"用户消息含「{mute_hit}」")
    trace.add("mute", "pass", "未出现")

    force_hit = _contains_any(user_text, profile.force_phrases)
    if not text:
        trace.add("content", "stop", "过滤后为空")
        return done(False, "empty", "过滤后没有可朗读的内容", bool(force_hit))
    trace.add("content", "pass", f"{len(text)} 字")

    if force_hit:
        trace.add("force", "speak", f"含「{force_hit}」")
        return done(True, "force_phrase", f"用户消息含「{force_hit}」，强制出声", True, rest="bypass")
    trace.add("force", "pass", "未出现")

    if is_group and not profile.allow_group:
        trace.add("scope", "stop", "群聊未允许")
        return done(False, "group_blocked", "档案不允许在群聊发声")
    if not is_group and not profile.allow_private:
        trace.add("scope", "stop", "私聊未允许")
        return done(False, "private_blocked", "档案不允许在私聊发声")
    trace.add("scope", "pass", "群聊" if is_group else "私聊")

    if len(text) > profile.max_chars:
        trace.add("length", "stop", f"{len(text)} / {profile.max_chars} 字")
        return done(
            False,
            "too_long",
            f"过滤后 {len(text)} 字，超过被动触发上限 {profile.max_chars} 字",
        )
    trace.add("length", "pass", f"{len(text)} / {profile.max_chars} 字")

    if last_voice_at is not None and profile.cooldown > 0:
        remaining = profile.cooldown - (now - last_voice_at)
        if remaining > 0:
            trace.add("cooldown", "stop", f"还剩 {remaining:.0f} 秒")
            return done(False, "cooldown", f"冷却中，还剩 {remaining:.0f} 秒")
    trace.add("cooldown", "pass", "不限制" if profile.cooldown <= 0 else "已就绪")

    if rand < profile.probability:
        trace.add("probability", "speak", f"{profile.probability:.0%}")
        return done(True, "probability_hit", f"概率命中（{profile.probability:.0%}）")
    trace.add("probability", "stop", f"{profile.probability:.0%}")
    return done(False, "probability_miss", f"概率未命中（{profile.probability:.0%}）")
