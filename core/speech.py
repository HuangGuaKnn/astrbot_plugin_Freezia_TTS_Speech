"""朗读文本的后处理：LLM 口语化清洗 / 翻译的提示词与结果校验、发音词典。"""

from __future__ import annotations

import re

from .settings import EMOTIONS, NEUTRAL

MAX_LLM_INPUT = 1000

_PREFIX = re.compile(
    r"^\s*(?:改写后|清洗后|翻译|译文|结果|输出|台词|最终台词|Translation|Output|Result)\s*[:：]\s*",
    re.I,
)
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'"))
_LATIN_WORD = re.compile(r"[A-Za-z0-9À-ɏ'’]+")
# 句末：中日文标点，或后面跟空格 / 结尾的英文句点
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?…～~」』”]+|\.(?=\s|$)")
_PAUSE_BOUNDARY = re.compile(r"[，,、；;：:]")
_TOKEN = re.compile(r"[A-Za-z0-9À-ɏ'’]+|\s+|.", re.S)
_AUTO_EMOTION = {"", "auto", "自动"}
_NEUTRAL_WORDS = {NEUTRAL, "平静", "默认", "普通", "normal", "calm", "default"}
_TAG = r"[\[【]\s*([A-Za-z]{2,12}|[一-鿿]{1,4})\s*[\]】]"
_LEADING_TAG = re.compile(rf"^\s*{_TAG}\s*")
_TRAILING_TAG = re.compile(rf"\s*{_TAG}\s*$")


def normalize_emotion(value: str) -> str | None:
    """把工具或页面传来的情绪（代码或中文名）规整成代码。

    空 / auto 返回 ""（自动判断）；平静一类返回 neutral（用默认声音）；不认识返回 None。
    """
    value = (value or "").strip().lower()
    if value in _AUTO_EMOTION:
        return ""
    if value in _NEUTRAL_WORDS:
        return NEUTRAL
    for code, name in EMOTIONS.items():
        if value in (code, name):
            return code
    return None


def emotion_by_markers(entries: list[dict], text: str) -> tuple[str, str] | None:
    """按情绪音色里的符号粗判情绪：命中次数最多的情绪胜出，平手按列表顺序。返回 (情绪, 命中的符号)。"""
    best: tuple[int, str, str] | None = None
    for entry in entries:
        hits = [(text.count(marker), marker) for marker in entry.get("markers") or [] if marker in text]
        if not hits:
            continue
        count = sum(n for n, _ in hits)
        if best is None or count > best[0]:
            # 报告出现次数最多的符号，平手取列表里靠前的
            best = (count, entry["emotion"], max(hits, key=lambda hit: hit[0])[1])
    return (best[1], best[2]) if best else None


def split_emotion(output: str) -> tuple[str | None, str]:
    """取出 LLM 在台词前（或后）标的情绪标签。返回 (情绪代码或 None, 去掉标签的文字)。

    认不出的方括号内容不动，交给后面的长度校验。
    """
    text = _PREFIX.sub("", (output or "").strip())
    for pattern in (_LEADING_TAG, _TRAILING_TAG):
        match = pattern.search(text)
        if match:
            code = normalize_emotion(match.group(1))
            if code:
                return code, (text[: match.start()] + text[match.end():]).strip()
    return None, text


def merged_pronunciations(profile_entries: list[dict], global_entries: list[dict]) -> list[dict]:
    """档案词典优先；同一原文 + 语言的全局条目被档案条目覆盖。"""
    seen = {(item["from"], item.get("lang", "")) for item in profile_entries}
    return list(profile_entries) + [
        item for item in global_entries if (item["from"], item.get("lang", "")) not in seen
    ]


def entries_for(entries: list[dict], language: str) -> list[dict]:
    """取适用于这个语言的条目，同一原文只留一条。

    优先级：指定为该语言的条目 > 不限语言的条目；同一层里保持传入顺序（档案在前）。
    language 为空表示和文字相同，只用不限语言的条目。
    """
    specific = [item for item in entries if language and item.get("lang", "") == language]
    generic = [item for item in entries if not item.get("lang", "")]
    result, seen = [], set()
    for item in specific + generic:
        if item["from"] not in seen:
            seen.add(item["from"])
            result.append(item)
    return result


def apply_pronunciations(text: str, entries: list[dict], language: str = "") -> str:
    """按原文从长到短替换，避免短词先替换破坏长词。读法可以为空（表示删掉）。"""
    applicable = entries_for(entries, language)
    if not applicable:
        return text
    mapping = {item["from"]: item["to"] for item in applicable}
    ordered = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(source) for source in ordered))
    replaced = pattern.sub(lambda match: mapping[match.group(0)], text)
    return re.sub(r"[ \t]{2,}", " ", replaced).strip()


def build_prompt(
    text: str,
    language_name: str | None,
    glossary: list[dict],
    emotions: list[str] | None = None,
    limit: int = 0,
    budget: int = 0,
) -> tuple[str, str]:
    """返回 (system_prompt, prompt)。

    language_name 为空表示只做口语化清洗、不翻译；emotions 非空时要求模型在最前面标出情绪；
    limit 大于 0 表示原台词太长，要改写成不超过 limit 字的短版；
    budget 大于 0 表示翻译时译文的朗读长度上限（原文不长、译文可能变长时用）。
    """
    rules = [
        "你是语音合成前的台词整理器。",
        f"任务：把输入的角色台词整理成适合朗读的口语{f'，并翻译成{language_name}' if language_name else ''}。",
        "规则：",
        "1. 删除动作描写、旁白、括号注释、markdown、链接、表情和颜文字，只保留角色说出口的话。",
    ]
    if limit:
        rules.append(
            f"2. 原台词太长，不适合整段念出来：改写成不超过 {limit} 字的简短口语，保留角色的语气、称呼和最重要的意思，"
            "省略细节、列举和解释，一两句话即可；不添加原文没有的内容，不解释。"
        )
    else:
        rules.append("2. 保留原有语气和情绪，不增加、不删减信息，不解释。")
    if language_name:
        rules.append(f"3. 用自然的{language_name}口语翻译，语气词换成{language_name}里对应的说法。")
        if budget and not limit:
            rules.append(
                f"译文要适合朗读，总长度不超过 {budget} 个字符（假名、汉字、标点各算 1 个，英文等每个单词约算 2 个）。"
                "直译会超出时，先精简内容再翻译，只保留最重要的意思，宁可短一些。"
            )
    else:
        rules.append("3. 保持原来的语言，不要翻译。")
    names = [item for item in glossary if item.get("to")]
    if names:
        pairs = "；".join(f"{item['from']} → {item['to']}" for item in names[:30])
        rules.append(f"4. 专有名词按对照表处理：{pairs}。")
    if emotions:
        options = "、".join(f"{code}（{EMOTIONS[code]}）" for code in emotions if code in EMOTIONS)
        rules.append(
            f"5. 判断这句台词的情绪，在输出最前面用方括号标出一个情绪代码，可选：{options}、"
            f"{NEUTRAL}（平静或没有明显情绪）。例如：[{emotions[0]}]台词。"
        )
        rules.append("除了开头的情绪标签，只输出整理后的台词本身，不要引号、前缀或任何说明。")
    else:
        rules.append("只输出整理后的台词本身，不要引号、前缀或任何说明。")
    return "\n".join(rules), text[:MAX_LLM_INPUT]


def speech_length(text: str) -> int:
    """按念出来的长短估算字数：汉字、假名、谚文、标点各算 1；英文等拉丁字母按单词算，每个单词 2。

    依据：实测汉语和日语每秒都约念 4.5 个字符；英语每秒约 2.5 个单词。
    """
    words = _LATIN_WORD.findall(text)
    return len(text) - sum(len(word) for word in words) + 2 * len(words)


def truncate_sentences(text: str, limit: int) -> str:
    """截到念出来不超过 limit 字（按 speech_length 计）：优先停在句末，其次停在逗号，都没有就硬截。"""
    if limit <= 0 or speech_length(text) <= limit:
        return text
    floor = max(1, int(limit * 0.3))  # 断点太靠前（只剩几个字）就不用它
    for pattern, keep_mark in ((_SENTENCE_BOUNDARY, True), (_PAUSE_BOUNDARY, False)):
        best = ""
        for match in pattern.finditer(text):
            head = text[: match.end() if keep_mark else match.start()].rstrip()
            if speech_length(head) > limit:
                break
            best = head
        if best and speech_length(best) >= floor:
            return best
    cut = ""
    for token in _TOKEN.findall(text):  # 英文按整词截，不截半个单词
        if speech_length(cut + token) > limit:
            break
        cut += token
    return cut.rstrip()


def clean_output(output: str, source: str, translating: bool, limit: int = 0) -> str:
    """校验 LLM 输出。明显不对时抛出 ValueError，由调用方回退。

    limit 大于 0 时是缩写：不按原文比例校验，只要求不超出目标太多。
    """
    text = (output or "").strip()
    text = _PREFIX.sub("", text)
    for left, right in _QUOTE_PAIRS:
        # 只去掉整句外面包着的一对引号；句中还有别的引号（「好」她说「走吧」）就不动
        wrapped = len(text) >= 2 and text.startswith(left) and text.endswith(right)
        single_pair = text.count(left) == 2 if left == right else text.count(left) == 1
        if wrapped and single_pair:
            text = text[1:-1].strip()
    text = re.sub(r"\s*\n+\s*", " ", text).strip()
    if not text:
        raise ValueError("LLM 返回为空")
    if limit:
        # 外语按字符数比汉字长，翻译时放宽
        high = limit * 4 + 12 if translating else limit * 1.5 + 6
        if len(text) > high:
            raise ValueError(f"LLM 缩写后仍有 {len(text)} 字，目标 {limit} 字")
        return text
    # 比例加上一点绝对余量：很短的原文（如「早安」→ Good morning!）按比例算会误判
    low, high, slack = (0.2, 4.0, 12) if translating else (0.3, 1.5, 6)
    if len(text) < len(source) * low - 2 or len(text) > len(source) * high + slack:
        raise ValueError(f"LLM 返回长度异常（{len(text)} 字，原文 {len(source)} 字）")
    return text
