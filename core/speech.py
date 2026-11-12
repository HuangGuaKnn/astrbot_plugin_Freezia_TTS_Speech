"""朗读文本的后处理：LLM 口语化清洗 / 翻译的提示词与结果校验、发音词典。"""

from __future__ import annotations

import re

MAX_LLM_INPUT = 1000

_PREFIX = re.compile(
    r"^\s*(?:改写后|清洗后|翻译|译文|结果|输出|台词|最终台词|Translation|Output|Result)\s*[:：]\s*",
    re.I,
)
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'"))


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


def build_prompt(text: str, language_name: str | None, glossary: list[dict]) -> tuple[str, str]:
    """返回 (system_prompt, prompt)。language_name 为空表示只做口语化清洗、不翻译。"""
    rules = [
        "你是语音合成前的台词整理器。",
        f"任务：把输入的角色台词整理成适合朗读的口语{f'，并翻译成{language_name}' if language_name else ''}。",
        "规则：",
        "1. 删除动作描写、旁白、括号注释、markdown、链接、表情和颜文字，只保留角色说出口的话。",
        "2. 保留原有语气和情绪，不增加、不删减信息，不解释。",
    ]
    if language_name:
        rules.append(f"3. 用自然的{language_name}口语翻译，语气词换成{language_name}里对应的说法。")
    else:
        rules.append("3. 保持原来的语言，不要翻译。")
    names = [item for item in glossary if item.get("to")]
    if names:
        pairs = "；".join(f"{item['from']} → {item['to']}" for item in names[:30])
        rules.append(f"4. 专有名词按对照表处理：{pairs}。")
    rules.append("只输出整理后的台词本身，不要引号、前缀或任何说明。")
    return "\n".join(rules), text[:MAX_LLM_INPUT]


def clean_output(output: str, source: str, translating: bool) -> str:
    """校验 LLM 输出。明显不对时抛出 ValueError，由调用方回退。"""
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
    # 比例加上一点绝对余量：很短的原文（如「早安」→ Good morning!）按比例算会误判
    low, high, slack = (0.2, 4.0, 12) if translating else (0.3, 1.5, 6)
    if len(text) < len(source) * low - 2 or len(text) > len(source) * high + slack:
        raise ValueError(f"LLM 返回长度异常（{len(text)} 字，原文 {len(source)} 字）")
    return text
