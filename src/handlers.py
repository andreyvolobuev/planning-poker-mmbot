"""WebSocket `posted` event handling for planning poker."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

from mattermostdriver import Driver

from src import parsing
from src.sessions import PlanningSession, SessionStore, median_ceil

log = logging.getLogger(__name__)

_TEAM_CHANNEL_TYPES = frozenset({"O", "P", "G"})


@dataclass
class BotContext:
    driver: Driver
    bot_id: str
    site_url: str
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


def _thread_permalink(ctx: BotContext, team_id: str, planning_root_post_id: str) -> str:
    try:
        team = ctx.driver.teams.get_team(team_id)
        slug = (team.get("name") or team_id).strip()
    except Exception:
        log.exception("get_team failed team_id=%s", team_id)
        slug = team_id
    base = ctx.site_url.rstrip("/")
    return f"{base}/{quote(slug, safe='')}/pl/{planning_root_post_id}"


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


def _post_dm_top_level(driver: Driver, channel_id: str, text: str) -> dict[str, Any]:
    return driver.posts.create_post(
        {
            "channel_id": channel_id,
            "message": text,
            "root_id": "",
        }
    )


def _dm_reply_in_thread(
    driver: Driver,
    channel_id: str,
    thread_root_id: str,
    text: str,
) -> None:
    driver.posts.create_post(
        {
            "channel_id": channel_id,
            "message": text,
            "root_id": thread_root_id,
        }
    )


def _send_dm_invites(ctx: BotContext, session: PlanningSession) -> None:
    failed: list[str] = []
    permalink = _thread_permalink(ctx, session.team_id, session.root_post_id)
    dm_text = (
        f"‼️ [Тут]({permalink}) нужна оценка по {session.jira_url}.\n\n"
        f"Ответь **одним целым числом** (например `1`).\n\n"
        "--------------------------------"
    )
    for uid in session.voter_ids:
        try:
            dm_ch = ctx.driver.channels.create_direct_message_channel([ctx.bot_id, uid])
            cid = dm_ch.get("id")
            if not cid:
                raise RuntimeError("no channel id in response")
            created = _post_dm_top_level(ctx.driver, cid, dm_text)
            pid = created.get("id")
            if not pid:
                raise RuntimeError("create_post returned no id")
            session.dm_invite_root_by_user[uid] = pid
            log.info(
                "ЛС запрос оценки → участник=@%s | задача=%s | тред_канала=%s | id_приглашения_лс=%s",
                session.username_by_id.get(uid, uid),
                session.jira_url,
                permalink,
                pid,
            )
        except Exception:
            log.warning(
                "Не удалось отправить ЛС пользователю %s",
                session.username_by_id.get(uid, uid),
                exc_info=True,
            )
            failed.append(_mention_label(session.username_by_id, uid))
    if failed:
        _post_in_thread(
            ctx,
            session.root_post_id,
            session.channel_id,
            "Не удалось написать в личку: "
            + ", ".join(failed)
            + ". Проверьте настройки приватности / кто может вам писать.",
        )


def handle_channel_root_post(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    channel_id = post.get("channel_id") or data.get("channel_id")

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
        organizer_user_id=user_id or "",
        voter_ids=voter_ids,
        username_by_id=username_by_id,
    )
    if not ok:
        _post_in_thread(ctx, post_id, channel_id, err or "Не удалось начать раунд.")
        return

    session = ctx.session_store.get_by_root(post_id)
    if not session:
        log.error("session missing right after try_start root=%s", post_id)
        return

    thread_link = _thread_permalink(ctx, session.team_id, session.root_post_id)
    participants = ", ".join(_mention_label(username_by_id, uid) for uid in voter_ids)
    log.info(
        "Старт оценки: задача=%s | тред=%s | channel_id=%s | участники: %s",
        jira_url,
        thread_link,
        channel_id,
        participants,
    )

    _send_dm_invites(ctx, session)

    mentions = " ".join(_mention_label(username_by_id, uid) for uid in voter_ids)
    welcome = (
        f"{mentions} жду оценок по тикету {jira_url}. "
        "**В ЛС от бота** — ссылка на этот тред и инструкция; оценку пришлите **ответом в треде на сообщение бота** (не в корень ЛС). "
        "Голос в канале в тред не присылайте — не засчитывается.\n"
        "Досрочно подвести итог по уже пришедшим голосам может **автор этого поста**, написав в этом треде `/finish`."
    )
    _post_in_thread(ctx, post_id, channel_id, welcome)


def _finalize_session(
    ctx: BotContext,
    session: PlanningSession,
    *,
    forced: bool = False,
) -> None:
    if ctx.session_store.get_by_root(session.root_post_id) is None:
        return

    done_link = _thread_permalink(ctx, session.team_id, session.root_post_id)
    lines: list[str] = []
    for uid in session.voter_ids:
        un = session.username_by_id.get(uid) or uid
        if uid in session.votes:
            lines.append(f"@{un} {session.votes[uid]}")
        else:
            lines.append(f"@{un} —")
    body = "\n".join(lines)
    values = [session.votes[uid] for uid in session.voter_ids if uid in session.votes]

    if not values:
        total_line = "Итог: голосов нет, медиану не считаем."
        total_log: str | int = "—"
    else:
        total = median_ceil(values)
        total_line = (
            f"Итог: {total} (медиана по {len(values)} из {len(session.voter_ids)} голосов)"
            if forced and len(values) < len(session.voter_ids)
            else f"Итог: {total}"
        )
        total_log = total

    if forced:
        header = "Раунд завершён досрочно (`/finish`). Результаты по **имеющимся** голосам:"
        log.info(
            "Раунд завершён досрочно (/finish) | задача=%s | тред=%s | итог=%s | голосов=%s/%s",
            session.jira_url,
            done_link,
            total_log,
            len(values),
            len(session.voter_ids),
        )
    else:
        header = "Все оценки получены. Результаты:"
        log.info(
            "Раунд завершён: все проголосовали | задача=%s | тред=%s | итоговая_оценка=%s",
            session.jira_url,
            done_link,
            total_log,
        )

    extra = ""
    if forced:
        absent = [
            _mention_label(session.username_by_id, uid)
            for uid in session.voter_ids
            if uid not in session.votes
        ]
        if absent:
            extra = "\n\nНе проголосовали: " + ", ".join(absent)

    msg = f"{header}\n```\n{body}\n\n{total_line}\n```{extra}"
    _post_in_thread(ctx, session.root_post_id, session.channel_id, msg)
    ctx.session_store.finalize(session)


def handle_channel_finish_command(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    root_id = (post.get("root_id") or "").strip()
    if not root_id:
        return
    if (post.get("message") or "").strip().lower() != "/finish":
        return

    channel_id = post.get("channel_id") or data.get("channel_id")
    if not channel_id:
        return

    user_id = post.get("user_id")
    session = ctx.session_store.get_by_root(root_id)
    if not session:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "По этому треду нет активного голосования.",
        )
        return

    if user_id != session.organizer_user_id:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Команду `/finish` может отправить только автор поста, с которого запущено голосование.",
        )
        return

    if ctx.session_store.all_voted(session):
        _finalize_session(ctx, session, forced=False)
    else:
        _finalize_session(ctx, session, forced=True)


def handle_dm_post(ctx: BotContext, post: dict[str, Any]) -> None:
    user_id = post.get("user_id")
    if not user_id or user_id == ctx.bot_id:
        return

    channel_id = post.get("channel_id")
    if not channel_id:
        return

    actual_root = (post.get("root_id") or "").strip()

    if not actual_root:
        if ctx.session_store.user_has_pending_dm_invite(user_id):
            _post_dm_top_level(
                ctx.driver,
                channel_id,
                "Для каждой задачи у меня отдельное сообщение-приглашение. "
                "Открой **тред** (Reply) у нужного приглашения и пришли там **одно целое число**. "
                "В корень чата число не засчитывается.",
            )
        return

    session = ctx.session_store.session_for_dm_invite_thread(user_id, actual_root)
    if not session:
        _dm_reply_in_thread(
            ctx.driver,
            channel_id,
            actual_root,
            "По этому треду нет активного раунда (уже завершён или это старое приглашение).",
        )
        return

    raw = (post.get("message") or "").strip()
    value = parsing.parse_int_vote(raw)
    if value is None:
        _dm_reply_in_thread(
            ctx.driver,
            channel_id,
            actual_root,
            "Нужно **одно целое число** в этом треде (например `5`), без текста.",
        )
        return

    is_new_vote = user_id not in session.votes
    updated = ctx.session_store.record_vote(session, user_id, value)

    planning_link = _thread_permalink(ctx, updated.team_id, updated.root_post_id)
    log.info(
        "Оценка получена: участник=@%s | значение=%s | задача=%s | тред=%s | root_треда_лс=%s%s",
        updated.username_by_id.get(user_id, user_id),
        value,
        updated.jira_url,
        planning_link,
        actual_root,
        "" if is_new_vote else " (обновление)",
    )

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
        _finalize_session(ctx, updated, forced=False)


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
        elif channel_type in _TEAM_CHANNEL_TYPES:
            if (post.get("root_id") or "").strip():
                handle_channel_finish_command(ctx, post, data)
            else:
                handle_channel_root_post(ctx, post, data)
    except Exception:
        log.exception("handler error post_id=%s", post.get("id"))


async def websocket_event_handler(ctx: BotContext, message: str) -> None:
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8", errors="replace")
    handle_posted_message(ctx, message)
