"""调度层：单 worker 串行合成，排队过久直接丢弃，单次合成超时放弃。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

SynthFunc = Callable[[str, str], Awaitable[str]]


class SynthExpired(Exception):
    """排队时间超过 queue_expire，回复已经过时。"""


class SynthTimeout(Exception):
    """单次合成超过 synth_timeout。"""


class SchedulerStopped(Exception):
    """插件正在卸载。"""


@dataclass
class _Job:
    provider_id: str
    text: str
    enqueued_at: float
    future: asyncio.Future = field(repr=False)


class SynthScheduler:
    def __init__(self, synth: SynthFunc, *, expire: float = 10.0, timeout: float = 15.0):
        self._synth = synth
        self.expire = expire
        self.timeout = timeout
        self._queue: asyncio.Queue[_Job] | None = None
        self._worker: asyncio.Task | None = None
        self._stopped = False
        self.busy = False
        self.stats = {"done": 0, "failed": 0, "expired": 0, "timeout": 0}

    @property
    def pending(self) -> int:
        return self._queue.qsize() if self._queue else 0

    def _ensure_worker(self) -> asyncio.Queue[_Job]:
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())
        return self._queue

    async def submit(self, provider_id: str, text: str) -> str:
        """排队合成，返回音频文件路径；失败时抛出异常。"""
        if self._stopped:
            raise SchedulerStopped("插件正在卸载")
        queue = self._ensure_worker()
        future = asyncio.get_running_loop().create_future()
        queue.put_nowait(_Job(provider_id, text, time.monotonic(), future))
        return await future

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                if job.future.done():
                    continue
                waited = time.monotonic() - job.enqueued_at
                if waited > self.expire:
                    self.stats["expired"] += 1
                    job.future.set_exception(
                        SynthExpired(f"排队 {waited:.1f} 秒，超过 {self.expire:g} 秒已丢弃")
                    )
                    continue
                self.busy = True
                try:
                    path = await asyncio.wait_for(
                        self._synth(job.provider_id, job.text), self.timeout
                    )
                except asyncio.TimeoutError:
                    self.stats["timeout"] += 1
                    if not job.future.done():
                        job.future.set_exception(
                            SynthTimeout(f"合成超过 {self.timeout:g} 秒，已放弃")
                        )
                except asyncio.CancelledError:
                    if not job.future.done():
                        job.future.set_exception(SchedulerStopped("插件正在卸载"))
                    raise
                except Exception as exc:  # TTS 挂了、OOM 等都走这里
                    self.stats["failed"] += 1
                    if not job.future.done():
                        # 去掉回溯再交出去：回溯里有 worker 自己的协程帧，
                        # 调用方若清理这些帧（3.11 的 traceback.clear_frames 会终结挂起的协程），
                        # worker 会被直接结束，之后的任务永远等不到结果。
                        job.future.set_exception(exc.with_traceback(None))
                else:
                    self.stats["done"] += 1
                    if not job.future.done():
                        job.future.set_result(path)
                finally:
                    self.busy = False
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        self._stopped = True
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._queue:
            while not self._queue.empty():
                job = self._queue.get_nowait()
                if not job.future.done():
                    job.future.set_exception(SchedulerStopped("插件正在卸载"))
