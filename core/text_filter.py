"""第一阶段过滤：用正则剥掉不该念出来的结构性噪音。

只处理有明确标记的内容（代码、链接、markdown、*动作*、括号、表情）。
没有标记的旁白（例如「……」后面跟着的叙述）正则识别不了，留给以后的 LLM 改写阶段。
"""

from __future__ import annotations

import re

# 这些符号虽然落在 emoji/符号区段，但属于角色语气，保留在文本里。
# 它们大多不发音，最终效果以实际 TTS 听感为准。
KEEP_SYMBOLS = frozenset("♡♥～~")

_CODE_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\n]*\)")
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>\n]{0,40}>")
_URL = re.compile(
    r"(?:https?://|ftp://|www\.)"
    r"[^\s　-〿一-鿿＀-￯<>()\[\]「」『』“”‘’]+",
    re.I,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_HR = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.M)
_QUOTE = re.compile(r"^[ \t]*>+[ \t]?", re.M)
_LIST = re.compile(r"^[ \t]*(?:[-*+•·][ \t]+|\d{1,3}[.)][ \t]+|\d{1,3}、[ \t]*)", re.M)
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")

# *动作描写*：两侧不是数字（避免误伤 2*3*4），内容紧贴星号、不跨行
_ACTION = re.compile(r"(?<![0-9*])\*(?![\s*])[^*\n]+?(?<![\s*])\*(?![0-9*])")
_ACTION_FULLWIDTH = re.compile(r"＊[^＊\n]+?＊")

# 颜文字：括号内没有汉字，连同两侧的「手臂」一起去掉
_ARMS = "و۶ง٩୧୨ﾉノ丿ゞ✧☆★⌒ヽ〃♪✿ﾟ゜*"
_KAOMOJI = re.compile(
    rf"[{_ARMS}]*[(（][^()（）\n一-鿿]{{1,24}}[)）][{_ARMS}]*"
)
_ASCII_KAOMOJI = re.compile(
    r"(?<![A-Za-z])(?:QAQ|QwQ|qwq|TAT|TvT|orz|OTZ|OTL|owo|OwO|uwu|UwU)(?![A-Za-z])"
    r"|\^[_\-.ω]?\^|T[_.]T|>[_.]<|\._\."
)

# 成对括号里的说明（动作、心理、注释）。不跨行；循环几次处理嵌套。
_BRACKETS = re.compile(
    r"（[^（）\n]*）|\([^()\n]*\)|【[^【】\n]*】|\[[^\[\]\n]*\]|〔[^〔〕\n]*〕"
)
_STRAY_BRACKETS = re.compile(r"[()（）【】\[\]〔〕]")

_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoji、扑克、麻将等
    "←-⇿"  # 箭头
    "⌀-⏿"  # 技术符号（⌚⏰ 等）
    "■-◿"  # 几何形状
    "☀-➿"  # 杂项符号与装饰符号（♡♥ 在此区段，单独放行）
    "⬀-⯿"  # 星星、箭头
    "︎️‍⃣"  # 变体选择符、零宽连接符、键帽
    "\U000E0020-\U000E007F"  # 旗帜标签字符
    "]"
)

_END_PUNCT = frozenset("，。！？、；：,.!?;:…～~♡♥」』”’")
_CJK = r"　-〿一-鿿＀-￯"
_SPACE_NEAR_CJK = re.compile(rf"(?<=[{_CJK}]) +| +(?=[{_CJK}])")
_DUP_PUNCT = re.compile(r"([，,、；;：:])[，,、；;：:\s]*")
_PAUSE_BEFORE_END = re.compile(r"[，,、；;：:]+(?=[。！？!?…])")
_LEADING_PUNCT = re.compile(r"^[，,、；;：:。.！!？?\s]+")
_SPEAKABLE = re.compile(r"[0-9A-Za-z一-鿿぀-ヿ가-힯]")


def _drop_emoji(match: re.Match[str]) -> str:
    char = match.group()
    return char if char in KEEP_SYMBOLS else ""


def _strip_brackets(text: str) -> str:
    for _ in range(4):
        stripped = _BRACKETS.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text


def _tidy(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    joined = ""
    for line in lines:
        if not line:
            continue
        if joined and joined[-1] not in _END_PUNCT:
            joined += "，"
        joined += line
    joined = re.sub(r"[ \t　]+", " ", joined)
    joined = _SPACE_NEAR_CJK.sub("", joined)
    joined = _DUP_PUNCT.sub(r"\1", joined)
    joined = _PAUSE_BEFORE_END.sub("", joined)
    joined = _LEADING_PUNCT.sub("", joined)
    return joined.strip(" ，,、；;：:")


def clean_for_tts(text: str) -> str:
    """返回适合朗读的文本；剥完没有可读内容时返回空串（调用方据此不合成）。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CODE_BLOCK.sub("\n", text)
    text = _INLINE_CODE.sub("", text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = _URL.sub("", text)
    text = _EMAIL.sub("", text)
    text = _HR.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _LIST.sub("", text)
    text = _BOLD.sub(r"\2", text)
    text = _ACTION.sub("", text)
    text = _ACTION_FULLWIDTH.sub("", text)
    text = _KAOMOJI.sub("", text)
    text = _ASCII_KAOMOJI.sub("", text)
    text = _strip_brackets(text)
    text = _EMOJI.sub(_drop_emoji, text)
    text = _STRAY_BRACKETS.sub("", text)
    text = _tidy(text)
    return text if _SPEAKABLE.search(text) else ""


def normalize_only(text: str) -> str:
    """filter_mode=off 时只做空白整理，不剥任何内容。"""
    text = _tidy((text or "").replace("\r\n", "\n"))
    return text if _SPEAKABLE.search(text) else ""
