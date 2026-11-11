import asyncio
import unittest

from core.scheduler import SchedulerStopped, SynthExpired, SynthScheduler, SynthTimeout


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_serial_in_order(self):
        running = 0
        peak = 0
        order = []

        async def synth(provider, text):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            order.append(text)
            running -= 1
            return f"{provider}/{text}.wav"

        scheduler = SynthScheduler(synth, expire=5, timeout=5)
        results = await asyncio.gather(*(scheduler.submit("p", str(i)) for i in range(3)))
        self.assertEqual(results, ["p/0.wav", "p/1.wav", "p/2.wav"])
        self.assertEqual(order, ["0", "1", "2"])
        self.assertEqual(peak, 1)
        self.assertEqual(scheduler.stats["done"], 3)
        await scheduler.stop()

    async def test_timeout(self):
        async def synth(provider, text):
            await asyncio.sleep(1)
            return "never"

        scheduler = SynthScheduler(synth, expire=5, timeout=0.05)
        with self.assertRaises(SynthTimeout):
            await scheduler.submit("p", "慢")
        self.assertEqual(scheduler.stats["timeout"], 1)
        await scheduler.stop()

    async def test_expired_jobs_are_dropped(self):
        async def synth(provider, text):
            await asyncio.sleep(0.2)
            return text

        scheduler = SynthScheduler(synth, expire=0.1, timeout=5)
        first = asyncio.create_task(scheduler.submit("p", "第一条"))
        await asyncio.sleep(0)
        second = asyncio.create_task(scheduler.submit("p", "第二条"))
        self.assertEqual(await first, "第一条")
        with self.assertRaises(SynthExpired):
            await second
        self.assertEqual(scheduler.stats["expired"], 1)
        await scheduler.stop()

    async def test_errors_propagate_and_worker_survives(self):
        calls = 0

        async def synth(provider, text):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("TTS 服务未启动")
            return "ok.wav"

        scheduler = SynthScheduler(synth, expire=5, timeout=5)
        with self.assertRaises(ConnectionError):
            await scheduler.submit("p", "一")
        self.assertEqual(await scheduler.submit("p", "二"), "ok.wav")
        self.assertEqual(scheduler.stats["failed"], 1)
        await scheduler.stop()

    async def test_stop_rejects_new_jobs(self):
        async def synth(provider, text):
            return text

        scheduler = SynthScheduler(synth)
        await scheduler.stop()
        with self.assertRaises(SchedulerStopped):
            await scheduler.submit("p", "x")


if __name__ == "__main__":
    unittest.main()
