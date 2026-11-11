"""把配置、过滤、决策、调度串起来。不依赖 AstrBot，宿主能力通过 Host 注入。"""

from __future__ import annotations

import itertools
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .decide import Decision, decide, effective_send_mode
from .scheduler import SynthExpired, SynthScheduler, SynthTimeout
from .settings import Profile, Settings, SettingsStore
from .text_filter import clean_for_tts, normalize_only

MAX_EVENTS = 100
PREVIEW_CHARS = 60


class Host(Protocol):
    """宿主（AstrBot 或开发用的假宿主）需要提供的能力。"""

    def list_tts_providers(self) -> list[dict[str, Any]]: ...

    async def synthesize(self, provider_id: str, text: str) -> str: ...

    def diagnostics(self) -> dict[str, Any]: ...


@dataclass
class Plan:
    event_id: int
    umo: str
    profile: Profile
    text: str
    decision: Decision
    send_mode: str
    provider_id: str
    previous_voice_at: float | None = None


def filter_text(profile: Profile, text: str) -> str:
    return normalize_only(text) if profile.filter_mode == "off" else clean_for_tts(text)


def _short(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


class Engine:
    def __init__(self, host: Host, data_dir: Path):
        self.host = host
        self.store = SettingsStore(Path(data_dir) / "settings.json")
        self.scheduler = SynthScheduler(self.host.synthesize)
        self._apply_runtime_settings()
        self.last_voice_at: dict[str, float] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._event_ids = itertools.count(1)
        self.counters = {"decided": 0, "speak": 0, "sent": 0, "failed": 0}

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

    # ---------- 决策 ----------
    def plan(
        self,
        *,
        umo: str,
        reply_text: str,
        user_text: str,
        is_group: bool,
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
        )
        plan = Plan(
            event_id=next(self._event_ids),
            umo=umo,
            profile=profile,
            text=text,
            decision=decision,
            send_mode=effective_send_mode(profile, is_group),
            provider_id=self.resolve_provider_id(profile),
            previous_voice_at=previous,
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
                "mode": plan.send_mode if decision.speak else "",
                "status": "pending" if decision.speak else "skipped",
                "detail": "",
                "elapsed_ms": None,
            }
        )
        return plan

    def _update_event(self, event_id: int, **changes: Any) -> None:
        for event in self.events:
            if event["id"] == event_id:
                event.update(changes)
                return

    # ---------- 合成 ----------
    async def synthesize(self, plan: Plan) -> str:
        """为决策通过的回复合成语音；失败时还原冷却并把原因记进事件。"""
        started = time.monotonic()
        try:
            if not plan.provider_id:
                raise LookupError("没有可用的 TTS 服务，请先在 AstrBot 添加并在插件里选择")
            path = await self.scheduler.submit(plan.provider_id, plan.text)
        except Exception as exc:
            self.mark_failed(plan, describe_error(exc), started)
            raise
        self._update_event(plan.event_id, elapsed_ms=int((time.monotonic() - started) * 1000))
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
            changes["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        self._update_event(plan.event_id, **changes)

    async def synthesize_text(self, text: str, provider_id: str = "") -> tuple[str, int]:
        """测试用：不经过决策，直接排队合成。返回 (音频路径, 耗时毫秒)。"""
        provider_id = provider_id or self.resolve_provider_id(
            self.settings.profiles[self.settings.default_profile]
        )
        if not provider_id:
            raise LookupError("没有可用的 TTS 服务，请先在 AstrBot 添加并在插件里选择")
        started = time.monotonic()
        path = await self.scheduler.submit(provider_id, text)
        return path, int((time.monotonic() - started) * 1000)

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
    if isinstance(exc, (SynthExpired, SynthTimeout, LookupError)):
        return str(exc)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
