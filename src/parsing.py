"""Extract Jira links and @mentions from Mattermost post text."""

from __future__ import annotations

import re
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


def parse_int_vote(message: str) -> Optional[int]:
    text = (message or "").strip()
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None
