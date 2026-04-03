"""WebSocket `posted` event handling for planning poker."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from mattermostdriver import Driver

from src import parsing
from src.sessions import PlanningSession, SessionStore, median_ceil

log = logging.getLogger(__name__)


@dataclass
class BotContext:
    driver: Driver
    bot_id: str
    planning_channel_id: str
    session_store: SessionStore


def _load_posted_payload(message: str) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return None
    if payload.get("event") != "posted":
        return None
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    post_raw = data.get("post")
    if isinstance(post_raw, str):
        try:
            post = json.loads(post_raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(post_raw, dict):
        post = post_raw
    else:
        return None
    return data, post


def _mention_label(username_by_id: dict[str, str], user_id: str) -> str:
    u = username_by_id.get(user_id) or user_id
    return f"@{u}"


def _build_voter_list(
    driver: Driver,
    post: dict[str, Any],
    message: str,
    bot_id: str,
) -> tuple[list[str], dict[str, str]]:
    """Returns (ordered voter user ids, username_by_id)."""
    ids_from_props = parsing.mention_user_ids_from_post_props(post)
    usernames = parsing.extract_usernames_from_message(message)

    username_by_id: dict[str, str] = {}
    voter_ids: list[str] = []
    seen: set[str] = set()

    for uid in ids_from_props:
        if uid == bot_id or uid in seen:
            continue
        seen.add(uid)
        voter_ids.append(uid)

    if usernames:
        try:
            users = driver.users.get_users_by_usernames(usernames)
        except Exception:
            log.exception("get_users_by_usernames failed for %s", usernames)
            users = []
        if not isinstance(users, list):
            users = []
        for u in users:
            uid = u.get("id")
            un = u.get("username")
            if not uid or not un or uid == bot_id or uid in seen:
                continue
            seen.add(uid)
            voter_ids.append(uid)
            username_by_id[uid] = un

    for uid in voter_ids:
        if uid not in username_by_id:
            try:
                u = driver.users.get_user(uid)
                username_by_id[uid] = u.get("username") or uid
            except Exception:
                log.exception("get_user failed for %s", uid)
                username_by_id[uid] = uid

    return voter_ids, username_by_id


def _post_in_thread(ctx: BotContext, root_post_id: str, channel_id: str, text: str) -> None:
    ctx.driver.posts.create_post(
        {
            "channel_id": channel_id,
            "message": text,
            "root_id": root_post_id,
        }
    )


def _post_dm(driver: Driver, channel_id: str, text: str) -> None:
    driver.posts.create_post(
        {
            "channel_id": channel_id,
            "message": text,
            "root_id": "",
        }
    )


def handle_channel_root_post(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    channel_id = post.get("channel_id") or data.get("channel_id")
    if channel_id != ctx.planning_channel_id:
        return

    root_id = (post.get("root_id") or "").strip()
    if root_id:
        return

    user_id = post.get("user_id")
    if user_id == ctx.bot_id:
        return

    message = post.get("message") or ""
    jira_url = parsing.extract_jira_url(message)
    if not jira_url:
        return

    post_id = post.get("id")
    team_id = post.get("team_id") or data.get("team_id")
    if not post_id or not channel_id:
        log.warning("missing post_id or channel_id in root post")
        return

    if not team_id:
        try:
            ch = ctx.driver.channels.get_channel(channel_id)
            team_id = ch.get("team_id", "")
        except Exception:
            log.exception("get_channel failed")
            return
    if not team_id:
        log.warning("could not resolve team_id")
        return

    voter_ids, username_by_id = _build_voter_list(ctx.driver, post, message, ctx.bot_id)
    if not voter_ids:
        _post_in_thread(
            ctx,
            post_id,
            channel_id,
            "Нужна ссылка на Jira **и** упоминания участников (`@username`), которые пришлют оценку в ЛС.",
        )
        return

    ok, err = ctx.session_store.try_start(
        root_post_id=post_id,
        channel_id=channel_id,
        team_id=team_id,
        jira_url=jira_url,
        voter_ids=voter_ids,
        username_by_id=username_by_id,
    )
    if not ok:
        _post_in_thread(ctx, post_id, channel_id, err or "Не удалось начать раунд.")
        return

    mentions = " ".join(_mention_label(username_by_id, uid) for uid in voter_ids)
    welcome = (
        f"{mentions} жду ваших голосов **в личных сообщениях** с оценкой тикета {jira_url}. "
        "Пришлите **только целое число** (одно сообщение — одна оценка)."
    )
    _post_in_thread(ctx, post_id, channel_id, welcome)


def _finalize_session(ctx: BotContext, session: PlanningSession) -> None:
    lines = []
    for uid in session.voter_ids:
        un = session.username_by_id.get(uid) or uid
        v = session.votes.get(uid, 0)
        lines.append(f"@{un} {v}")
    body = "\n".join(lines)
    values = [session.votes[uid] for uid in session.voter_ids]
    total = median_ceil(values)
    msg = (
        "Все оценки получены. Результаты:\n```\n"
        f"{body}\n\nИтог: {total}\n```"
    )
    _post_in_thread(ctx, session.root_post_id, session.channel_id, msg)
    ctx.session_store.finalize(session)


def handle_dm_post(ctx: BotContext, post: dict[str, Any]) -> None:
    user_id = post.get("user_id")
    if not user_id or user_id == ctx.bot_id:
        return

    session = ctx.session_store.session_for_voter(user_id)
    if not session:
        return

    channel_id = post.get("channel_id")
    if not channel_id:
        return

    raw = (post.get("message") or "").strip()
    value = parsing.parse_int_vote(raw)
    if value is None:
        _post_dm(
            ctx.driver,
            channel_id,
            "Нужно прислать **одно целое число** (например `5`). Без текста и пробелов.",
        )
        return

    is_new_vote = user_id not in session.votes
    updated = ctx.session_store.record_vote(user_id, value)
    if not updated:
        return

    label = _mention_label(updated.username_by_id, user_id)
    thread_line = (
        f"Получил оценку от {label}."
        if is_new_vote
        else f"Обновил оценку от {label}."
    )
    _post_in_thread(
        ctx,
        updated.root_post_id,
        updated.channel_id,
        thread_line,
    )

    if ctx.session_store.all_voted(updated):
        _finalize_session(ctx, updated)


def handle_posted_message(ctx: BotContext, message: str) -> None:
    parsed = _load_posted_payload(message)
    if not parsed:
        return
    data, post = parsed

    if post.get("user_id") == ctx.bot_id:
        return
    ptype = post.get("type") or ""
    if isinstance(ptype, str) and ptype.startswith("system_"):
        return

    channel_type = data.get("channel_type") or ""
    channel_id = post.get("channel_id") or data.get("channel_id")

    try:
        if channel_type == "D":
            handle_dm_post(ctx, post)
        elif channel_id == ctx.planning_channel_id:
            handle_channel_root_post(ctx, post, data)
    except Exception:
        log.exception("handler error post_id=%s", post.get("id"))


async def websocket_event_handler(ctx: BotContext, message: str) -> None:
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8", errors="replace")
    handle_posted_message(ctx, message)
