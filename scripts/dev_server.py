"""脱离 AstrBot 预览 WebUI：用假的 TTS 服务跑真实的页面接口。

仅供开发调试，监听 127.0.0.1。

    py -3 scripts/dev_server.py --port 8765 --demo

--demo 会预先跑几条示例回复，让总览页有数据可看。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import struct
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.engine import Engine  # noqa: E402
from core.settings import Settings  # noqa: E402
from core.webapi import dispatch  # noqa: E402

PAGE_DIR = ROOT / "pages" / "freezia"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


class FakeHost:
    """假的宿主：两个假 TTS 服务，生成一段提示音代替语音。"""

    def __init__(self, audio_dir: Path, *, builtin_tts: bool, streaming: bool):
        self.audio_dir = audio_dir
        self.builtin_tts = builtin_tts
        self.streaming = streaming

    def list_tts_providers(self):
        return [
            {"id": "fake_genie", "type": "dev"},
            {"id": "fake_genie_happy", "type": "dev"},
            {"id": "fake_genie_sad", "type": "dev"},
            {"id": "fake_genie_ja", "type": "dev"},
            {"id": "fake_slow", "type": "dev"},
        ]

    def list_chat_providers(self):
        return [{"id": "fake_chat", "type": "dev"}]

    async def chat(self, provider_id: str, system_prompt: str, prompt: str, umo: str = "") -> str:
        """假的对话模型：翻译时在前面标上目标语言，清洗时原样返回；要求标情绪时按语气词粗判。"""
        await asyncio.sleep(0.3)
        match = re.search(r"翻译成(\S+?)。", system_prompt)
        shorten = re.search(r"不超过 (\d+) 字", system_prompt)
        if shorten:  # 假装缩写：取开头一段
            prompt = prompt[: max(4, int(shorten.group(1)) - 6)] + "……"
        text = f"〔{match.group(1)}〕{prompt}" if match else prompt
        if "情绪代码" in system_prompt:
            mood = "sad" if "呜" in prompt else "happy" if re.search(r"[！!哈]", prompt) else "neutral"
            text = f"[{mood}]{text}"
        return text

    async def synthesize(self, provider_id: str, text: str) -> str:
        if provider_id == "fake_slow":
            await asyncio.sleep(60)  # 用来观察超时降级
        await asyncio.sleep(min(0.2 + len(text) * 0.02, 2.0))
        path = self.audio_dir / f"{time.time_ns()}.wav"
        _write_tone(path, text)
        return str(path)

    def diagnostics(self):
        return {
            "builtin_tts_enabled": self.builtin_tts,
            "streaming_enabled": self.streaming,
            "callback_api_base": False,
            "page_api": True,
        }


def _write_tone(path: Path, text: str) -> None:
    rate = 16000
    per_char = int(rate * 0.09)
    frames = bytearray()
    for index, char in enumerate(text[:60]):
        freq = 330 + (ord(char) % 12) * 30
        for n in range(per_char):
            fade = min(1.0, n / 300, (per_char - n) / 300)
            frames += struct.pack("<h", int(6000 * fade * math.sin(2 * math.pi * freq * n / rate)))
        if index % 6 == 5:
            frames += b"\x00\x00" * int(rate * 0.05)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))


async def seed_demo(engine: Engine) -> None:
    if not engine.settings.voice_languages:
        data = engine.settings.to_dict()
        data["voice_languages"] = [{"code": "ja", "name": "日语", "provider_id": "fake_genie_ja"}]
        data["pronunciations"] = [{"from": "Freezia", "to": "芙蕾齐娅", "lang": ""}]
        default = data["profiles"][data["default_profile"]]
        default["emotion_voices"] = [
            {"emotion": "happy", "provider_id": "fake_genie_happy", "markers": ["哈哈", "嘿嘿", "好耶"]},
            {"emotion": "sad", "provider_id": "fake_genie_sad", "markers": ["呜", "难过", "哭"]},
        ]
        engine.save_settings(Settings.from_dict(data))
    request = engine.request_voice(umo="aiocqhttp:FriendMessage:10006", user_text="晚安", language="ja")
    plan = engine.plan(umo="aiocqhttp:FriendMessage:10006", reply_text="（小声）晚安，做个好梦～",
                       user_text="晚安", is_group=False, voice_request=request if request["ok"] else None)
    try:
        await engine.synthesize(plan)
        engine.mark_sent(plan)
    except Exception:
        pass
    voice = engine.plan(umo="aiocqhttp:FriendMessage:10007", reply_text="听到你的声音啦", user_text="",
                        is_group=False, user_voice=True)
    try:
        await engine.synthesize(voice)
        engine.mark_sent(voice)
    except Exception:
        pass
    samples = [
        ("aiocqhttp:FriendMessage:10001", "*探头* 你回来啦～", "", False, 0.05),
        ("aiocqhttp:FriendMessage:10001", "好的～", "", False, 0.9),
        ("aiocqhttp:GroupMessage:20002", "大家晚上好呀！", "", True, 0.01),
        ("aiocqhttp:FriendMessage:10001", "今天的天气" * 30, "", False, 0.01),
        ("aiocqhttp:FriendMessage:10002", "（歪头）嗯？要我念给你听吗♡", "说给我听", False, 0.9),
        ("aiocqhttp:FriendMessage:10003", "收到，我打字回你～", "别出声", False, 0.01),
        ("aiocqhttp:FriendMessage:10004", "🎉🎉", "", False, 0.01),
        ("aiocqhttp:FriendMessage:10005", "早安，今天也要元气满满哦！", "", False, 0.02),
        ("aiocqhttp:FriendMessage:10008", "（小声哭）呜……你终于回来了", "", False, 0.01),
        ("aiocqhttp:FriendMessage:10009", "嘿嘿，抓到你偷吃零食了", "", False, 0.01),
    ]
    for umo, reply, user_text, is_group, rand in samples:
        plan = engine.plan(umo=umo, reply_text=reply, user_text=user_text, is_group=is_group, rand=rand)
        if plan.decision.speak:
            try:
                await engine.synthesize(plan)
                engine.mark_sent(plan)
            except Exception:
                pass


def make_handler(engine: Engine, loop: asyncio.AbstractEventLoop):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FreeziaTTSDev"

        def log_message(self, *_args):
            pass

        def _send(self, status: int, body: bytes, mime: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _api(self, method: str) -> None:
            suffix = self.path.split("?", 1)[0][len("/api/page/"):]
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"status": "error", "message": "请求不是有效 JSON"})
            future = asyncio.run_coroutine_threadsafe(dispatch(engine, method, suffix, body), loop)
            try:
                self._json(200, {"status": "ok", "data": future.result(timeout=120)})
            except LookupError as exc:
                self._json(404, {"status": "error", "message": str(exc)})
            except Exception as exc:
                self._json(400, {"status": "error", "message": str(exc) or type(exc).__name__})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/page/"):
                return self._api("GET")
            if path in STATIC:
                name, mime = STATIC[path]
                return self._send(200, (PAGE_DIR / name).read_bytes(), mime)
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            if self.path.startswith("/api/page/"):
                return self._api("POST")
            self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=Path(tempfile.gettempdir()) / "freezia_tts_dev")
    parser.add_argument("--demo", action="store_true", help="预先跑几条示例回复")
    parser.add_argument("--no-builtin-warning", action="store_true", help="模拟已关闭 AstrBot 自带 TTS")
    parser.add_argument("--streaming", action="store_true", help="模拟开启了流式输出")
    args = parser.parse_args()

    audio_dir = args.data / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    host = FakeHost(audio_dir, builtin_tts=not args.no_builtin_warning, streaming=args.streaming)
    engine = Engine(host, args.data)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    if args.demo:
        asyncio.run_coroutine_threadsafe(seed_demo(engine), loop).result(timeout=60)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine, loop))
    print(f"Freezia TTS Speech 开发预览：http://127.0.0.1:{args.port}  数据目录：{args.data}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        asyncio.run_coroutine_threadsafe(engine.stop(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
