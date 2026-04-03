"""Extract Jira links, @mentions, and vote values from Mattermost post text."""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

# HTTP(S) URL ending with /browse/PROJECT-123 (Jira-style)
JIRA_BROWSE_URL_RE = re.compile(
    r"(https?://[^\s\]>\"']+/browse/[A-Za-z][A-Za-z0-9]*-\d+)",
    re.IGNORECASE,
)

# Mattermost usernames: @user.name, @user_name, etc.
MENTION_USERNAME_RE = re.compile(
    r"(?<![A-Za-z0-9_.])@([a-z0-9][a-z0-9._-]*)",
    re.IGNORECASE,
)

# Допустимая сетка оценок: кратно 0.05 (сотые только 00 или 05)
VOTE_STEP = Decimal("0.05")


def extract_jira_url(message: str) -> Optional[str]:
    m = JIRA_BROWSE_URL_RE.search(message or "")
    return m.group(1) if m else None


def extract_usernames_from_message(message: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in MENTION_USERNAME_RE.finditer(message or ""):
        name = m.group(1).lower()
        if name not in seen:
            seen.add(name)
            out.append(m.group(1))
    return out


def mention_user_ids_from_post_props(post: dict) -> list[str]:
    """Collect user ids from Mattermost post props/metadata if present."""
    ids: list[str] = []

    props = post.get("props") or {}
    if isinstance(props, dict):
        raw = props.get("mentions")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item:
                    ids.append(item)

    metadata = post.get("metadata") or {}
    if isinstance(metadata, dict):
        mentions = metadata.get("mentions")
        if isinstance(mentions, list):
            for m in mentions:
                if isinstance(m, dict):
                    uid = m.get("user_id")
                    if isinstance(uid, str) and uid:
                        ids.append(uid)
                elif isinstance(m, str) and m:
                    ids.append(m)

    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for uid in ids:
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def _normalize_decimal_separators(raw: str) -> str:
    """Поддержка `3.14` и `3,14`; при обоих разделителях десятичный — правый (1.234,56 / 1,234.56)."""
    s = raw.strip().replace(" ", "")
    if not s:
        return ""
    sign = ""
    if s[0] in "+-":
        sign = s[0]
        s = s[1:]
    if not s:
        return ""
    nc = s.count(",")
    nd = s.count(".")
    if nc == 0 and nd == 0:
        return sign + s
    if nc == 1 and nd == 0:
        return sign + s.replace(",", ".")
    if nd == 1 and nc == 0:
        return sign + s
    li = max(s.rfind(","), s.rfind("."))
    intpart = s[:li].replace(".", "").replace(",", "")
    frac = s[li + 1 :].replace(".", "").replace(",", "")
    if not intpart and frac:
        intpart = "0"
    if not frac:
        return sign + intpart
    return sign + intpart + "." + frac


def quantize_vote_up(d: Decimal) -> Decimal:
    """Округление вверх до шага 0.05 (сотые только 00 или 05)."""
    q = (d / VOTE_STEP).to_integral_value(rounding=ROUND_CEILING)
    return (q * VOTE_STEP).quantize(VOTE_STEP)


def parse_vote(message: str) -> Optional[Decimal]:
    """
    Одно число в сообщении; запятая или точка как десятичный разделитель.
    Любая дробная часть приводится вверх к ближайшему кратному 0.05.
    """
    norm = _normalize_decimal_separators(message or "")
    if not norm or norm in "+-":
        return None
    try:
        d = Decimal(norm)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return quantize_vote_up(d)


def format_vote(d: Decimal) -> str:
    """Краткая строка для отображения (5, 2.5, 2.95)."""
    d = d.quantize(VOTE_STEP)
    if d == d.to_integral():
        return str(int(d))
    s = format(d, "f").rstrip("0").rstrip(".")
    return s


def format_arithmetic_mean(values: list[Decimal]) -> str:
    """Среднее арифметическое для итога; до сотых, без лишних нулей."""
    if not values:
        return ""
    total = sum(values, Decimal(0))
    mean = total / Decimal(len(values))
    q = mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = format(q, "f").rstrip("0").rstrip(".")
    return s


def parse_int_vote(message: str) -> Optional[int]:
    """Только целые; для обратной совместимости тестов."""
    d = parse_vote(message)
    if d is None:
        return None
    if d != d.to_integral():
        return None
    return int(d)
