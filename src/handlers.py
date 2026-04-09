"""WebSocket `posted` event handling for planning poker."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

from mattermostdriver import Driver

from src import parsing
from src.config import JiraIntegration
from src.jira_client import sync_story_points_and_estimates
from src.sessions import PlanningSession, SessionStore, median_ceil_vote

log = logging.getLogger(__name__)

_TEAM_CHANNEL_TYPES = frozenset({"O", "P", "G"})


@dataclass
class BotContext:
    driver: Driver
    bot_id: str
    site_url: str
    session_store: SessionStore
    jira: JiraIntegration | None


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


def _resolve_thread_planning_meta(
    ctx: BotContext,
    root_post_id: str,
) -> Optional[tuple[str, str]]:
    """
    (jira_url, organizer_user_id) для треда: активная сессия или корневой пост в Mattermost.
    Нужно после /finish, когда сессия уже удалена из памяти.
    """
    sess = ctx.session_store.get_by_root(root_post_id)
    if sess:
        return sess.jira_url, sess.organizer_user_id
    try:
        root_post = ctx.driver.posts.get_post(root_post_id)
    except Exception:
        log.exception("get_post failed root=%s", root_post_id)
        return None
    msg = root_post.get("message") or ""
    jira_url = parsing.extract_jira_url(msg)
    org = (root_post.get("user_id") or "").strip()
    if not jira_url or not org:
        return None
    return jira_url, org


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
        f"‼️ [Тут]({permalink}) запущено голосование по оценке тикета: {session.jira_url}.\n\n"
        "Ответь реплаем к этому сообщению оценкой в виде одного числа."
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


def _launch_planning_round(
    ctx: BotContext,
    *,
    root_post_id: str,
    channel_id: str,
    team_id: str,
    jira_url: str,
    organizer_user_id: str,
    voter_ids: list[str],
    username_by_id: dict[str, str],
    intro_lines: Optional[list[str]] = None,
    is_reset: bool = False,
) -> None:
    """
    Регистрирует сессию (перезаписывает существующую с тем же root_post_id), шлёт ЛС, постит приветствие в тред.
    """
    ok, err = ctx.session_store.try_start(
        root_post_id=root_post_id,
        channel_id=channel_id,
        team_id=team_id,
        jira_url=jira_url,
        organizer_user_id=organizer_user_id,
        voter_ids=voter_ids,
        username_by_id=username_by_id,
    )
    if not ok:
        _post_in_thread(ctx, root_post_id, channel_id, err or "Не удалось начать раунд.")
        return

    session = ctx.session_store.get_by_root(root_post_id)
    if not session:
        log.error("session missing right after try_start root=%s", root_post_id)
        return

    thread_link = _thread_permalink(ctx, session.team_id, session.root_post_id)
    participants = ", ".join(_mention_label(username_by_id, uid) for uid in voter_ids)
    if is_reset:
        log.info(
            "Сброс и перезапуск оценки: задача=%s | тред=%s | channel_id=%s | участники: %s",
            jira_url,
            thread_link,
            channel_id,
            participants,
        )
    else:
        log.info(
            "Старт оценки: задача=%s | тред=%s | channel_id=%s | участники: %s",
            jira_url,
            thread_link,
            channel_id,
            participants,
        )

    _send_dm_invites(ctx, session)
    ctx.session_store.persist_session(root_post_id)

    mentions = " ".join(_mention_label(username_by_id, uid) for uid in voter_ids)
    body_lines: list[str] = []
    if intro_lines:
        body_lines.extend(intro_lines)
    body_lines.append(f"{mentions} жду в личку оценок по тикету {jira_url}.")
    _post_in_thread(ctx, root_post_id, channel_id, "\n".join(body_lines))


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

    _launch_planning_round(
        ctx,
        root_post_id=post_id,
        channel_id=channel_id,
        team_id=team_id,
        jira_url=jira_url,
        organizer_user_id=user_id or "",
        voter_ids=voter_ids,
        username_by_id=username_by_id,
    )


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
            lines.append(f"@{un} {parsing.format_vote(session.votes[uid])}")
        else:
            lines.append(f"@{un} —")
    body = "\n".join(lines)
    values = [session.votes[uid] for uid in session.voter_ids if uid in session.votes]

    if not values:
        total_line = "Итог: голосов нет, медиану и среднее не считаем."
        total_log = "—"
    else:
        median_val = median_ceil_vote(values)
        median_s = parsing.format_vote(median_val)
        mean_s = parsing.format_arithmetic_mean(values)
        partial_note = (
            f" (по {len(values)} из {len(session.voter_ids)} голосов)"
            if forced and len(values) < len(session.voter_ids)
            else ""
        )
        total_line = (
            f"Итог: медиана {median_s}, среднее {mean_s}{partial_note}"
        )
        total_log = f"медиана={median_s}, среднее={mean_s}"

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


def sweep_all_voted_sessions(ctx: BotContext) -> None:
    """После рестарта: дожать итог, если все голоса уже были в SQLite до падения."""
    for session in ctx.session_store.active_sessions():
        if ctx.session_store.all_voted(session):
            _finalize_session(ctx, session, forced=False)


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

    if user_id not in (set(session.voter_ids) | {session.organizer_user_id}):
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Команду `/finish` могут отправить только участники голосования.",
        )
        return

    if ctx.session_store.all_voted(session):
        _finalize_session(ctx, session, forced=False)
    else:
        _finalize_session(ctx, session, forced=True)


def handle_channel_list_command(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    root_id = (post.get("root_id") or "").strip()
    if not root_id:
        return
    if (post.get("message") or "").strip().lower() != "/list":
        return

    channel_id = post.get("channel_id") or data.get("channel_id")
    if not channel_id:
        return

    session = ctx.session_store.get_by_root(root_id)
    if not session:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "По этому треду нет активного голосования.",
        )
        return

    voted_labels = [
        _mention_label(session.username_by_id, uid)
        for uid in session.voter_ids
        if uid in session.votes
    ]
    pending_labels = [
        _mention_label(session.username_by_id, uid)
        for uid in session.voter_ids
        if uid not in session.votes
    ]
    n = len(session.voter_ids)
    got = len(voted_labels)
    lines = [
        f"**Статус голосования** ({got}/{n}):",
        "**Уже проголосовали:** "
        + (", ".join(voted_labels) if voted_labels else "—"),
        "**Ждём:** " + (", ".join(pending_labels) if pending_labels else "никого, все внесли оценку."),
    ]
    _post_in_thread(ctx, root_id, channel_id, "\n".join(lines))


def handle_channel_reset_command(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    root_id = (post.get("root_id") or "").strip()
    if not root_id:
        return
    if (post.get("message") or "").strip().lower() != "/reset":
        return

    channel_id = post.get("channel_id") or data.get("channel_id")
    if not channel_id:
        return

    user_id = post.get("user_id")
    meta = _resolve_thread_planning_meta(ctx, root_id)
    if not meta:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Не нашёл в корне треда ссылку на Jira — `/reset` здесь недоступен.",
        )
        return

    _jira_meta, organizer_id = meta
    sess = ctx.session_store.get_by_root(root_id)
    voter_ids = sess.voter_ids if sess else []
    if user_id not in (set(voter_ids) | {organizer_id}):
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Команду `/reset` могут отправить только участники голосования.",
        )
        return

    try:
        root_post = ctx.driver.posts.get_post(root_id)
    except Exception:
        log.exception("get_post failed reset root=%s", root_id)
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Не удалось загрузить корневой пост.",
        )
        return

    message = root_post.get("message") or ""
    jira_url = parsing.extract_jira_url(message)
    if not jira_url:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "В корневом посте нет ссылки на Jira.",
        )
        return

    root_channel_id = root_post.get("channel_id") or channel_id
    team_id = root_post.get("team_id") or data.get("team_id")
    if not team_id:
        try:
            ch = ctx.driver.channels.get_channel(root_channel_id)
            team_id = ch.get("team_id", "")
        except Exception:
            log.exception("get_channel failed reset channel=%s", root_channel_id)
            _post_in_thread(
                ctx,
                root_id,
                root_channel_id,
                "Не удалось определить команду канала.",
            )
            return
    if not team_id:
        _post_in_thread(
            ctx,
            root_id,
            root_channel_id,
            "Не удалось определить команду канала.",
        )
        return

    voter_ids, username_by_id = _build_voter_list(
        ctx.driver, root_post, message, ctx.bot_id
    )
    if not voter_ids:
        _post_in_thread(
            ctx,
            root_id,
            root_channel_id,
            "В корневом посте нет упоминаний участников (`@username`). "
            "Отредактируйте пост и снова отправьте `/reset`.",
        )
        return

    _launch_planning_round(
        ctx,
        root_post_id=root_id,
        channel_id=root_channel_id,
        team_id=team_id,
        jira_url=jira_url,
        organizer_user_id=organizer_id,
        voter_ids=voter_ids,
        username_by_id=username_by_id,
        intro_lines=["**Раунд сброшен.**"],
        is_reset=True,
    )


def handle_channel_add_command(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    root_id = (post.get("root_id") or "").strip()
    if not root_id:
        return
    raw_msg = (post.get("message") or "").strip()
    if not raw_msg.lower().startswith("/add"):
        return

    tail = raw_msg[4:].strip()
    channel_id = post.get("channel_id") or data.get("channel_id")
    if not channel_id:
        return

    user_id = post.get("user_id")
    session = ctx.session_store.get_by_root(root_id)
    if not session:
        _post_in_thread(ctx, root_id, channel_id, "По этому треду нет активного голосования.")
        return

    if user_id not in (set(session.voter_ids) | {session.organizer_user_id}):
        _post_in_thread(ctx, root_id, channel_id, "Команду `/add` могут отправить только участники голосования.")
        return

    usernames = parsing.extract_usernames_from_message(tail)
    if not usernames:
        _post_in_thread(ctx, root_id, channel_id, "Укажите участника: `/add @username`.")
        return

    username = usernames[0]
    try:
        users = ctx.driver.users.get_users_by_usernames([username])
    except Exception:
        log.exception("get_users_by_usernames failed for %s", username)
        _post_in_thread(ctx, root_id, channel_id, f"Не удалось найти пользователя @{username}.")
        return

    if not isinstance(users, list) or not users:
        _post_in_thread(ctx, root_id, channel_id, f"Пользователь @{username} не найден.")
        return

    new_uid = users[0].get("id")
    new_username = users[0].get("username") or username
    if not new_uid:
        _post_in_thread(ctx, root_id, channel_id, f"Не удалось получить ID пользователя @{username}.")
        return
    if new_uid == ctx.bot_id:
        _post_in_thread(ctx, root_id, channel_id, "Нельзя добавить бота в голосование.")
        return
    if new_uid in session.voter_ids:
        _post_in_thread(ctx, root_id, channel_id, f"@{new_username} уже участвует в голосовании.")
        return

    permalink = _thread_permalink(ctx, session.team_id, session.root_post_id)
    dm_text = (
        f"‼️ [Тут]({permalink}) запущено голосование по оценке тикета: {session.jira_url}.\n\n"
        "Ответь реплаем к этому сообщению оценкой в виде одного числа."
    )
    try:
        dm_ch = ctx.driver.channels.create_direct_message_channel([ctx.bot_id, new_uid])
        cid = dm_ch.get("id")
        if not cid:
            raise RuntimeError("no channel id")
        created = _post_dm_top_level(ctx.driver, cid, dm_text)
        pid = created.get("id")
        if not pid:
            raise RuntimeError("create_post returned no id")
        session.dm_invite_root_by_user[new_uid] = pid
        log.info(
            "ЛС запрос оценки (add) → участник=@%s | задача=%s | тред_канала=%s | id_приглашения_лс=%s",
            new_username, session.jira_url, permalink, pid,
        )
    except Exception:
        log.warning("Не удалось отправить ЛС пользователю %s", new_username, exc_info=True)
        _post_in_thread(
            ctx, root_id, channel_id,
            f"Не удалось написать в личку @{new_username} — пользователь не добавлен в голосование. "
            "Проверьте настройки приватности / кто может вам писать.",
        )
        return

    session.voter_ids.append(new_uid)
    session.username_by_id[new_uid] = new_username
    ctx.session_store.persist_session(root_id)
    _post_in_thread(ctx, root_id, channel_id, f"@{new_username} добавлен в голосование.")


def handle_channel_help_command(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    root_id = (post.get("root_id") or "").strip()
    if not root_id:
        return
    if (post.get("message") or "").strip().lower() != "/help":
        return

    channel_id = post.get("channel_id") or data.get("channel_id")
    if not channel_id:
        return

    _post_in_thread(
        ctx,
        root_id,
        channel_id,
        "**Доступные команды:**\n"
        "- `/finish` — завершить голосование досрочно по имеющимся оценкам\n"
        "- `/list` — показать, кто уже проголосовал и кто ещё нет\n"
        "- `/reset` — сбросить все оценки и начать раунд заново\n"
        "- `/add @username` — добавить участника в текущее голосование\n"
        "- `/agree <оценка>` — зафиксировать итоговую оценку в Jira (например: `/agree 3` или `/agree 0,5`)\n"
        "\nКоманды отправляются реплаем в тред с голосованием.",
    )


def handle_channel_agree_command(ctx: BotContext, post: dict[str, Any], data: dict[str, Any]) -> None:
    root_id = (post.get("root_id") or "").strip()
    if not root_id:
        return
    raw_msg = (post.get("message") or "").strip()
    if not raw_msg.lower().startswith("/agree"):
        return

    tail = raw_msg[6:].strip()
    channel_id = post.get("channel_id") or data.get("channel_id")
    if not channel_id:
        return

    user_id = post.get("user_id")
    meta = _resolve_thread_planning_meta(ctx, root_id)
    if not meta:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Не нашёл в корне треда ссылку на Jira — `/agree` здесь недоступен.",
        )
        return

    jira_url, organizer_id = meta
    sess = ctx.session_store.get_by_root(root_id)
    voter_ids = sess.voter_ids if sess else []
    if user_id not in (set(voter_ids) | {organizer_id}):
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Команду `/agree` могут отправить только участники голосования.",
        )
        return

    if not tail:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Укажите оценку: `/agree 3` или `/agree 0,5` (точка или запятая как разделитель).",
        )
        return

    sp = parsing.parse_agree_story_points(tail)
    if sp is None:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "Не удалось разобрать число. Пример: `/agree 3` или `/agree 0,5`.",
        )
        return

    issue_key = parsing.extract_jira_issue_key(jira_url)
    if not issue_key:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            f"Не извлекаю ключ тикета из ссылки: {jira_url}",
        )
        return

    if ctx.jira is None:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            "**JIRA_TOKEN** не задан — в Jira ничего не записано. "
            f"Для справки согласованные SP: **{parsing.format_story_points_plain(sp)}**.",
        )
        return

    ok, err = sync_story_points_and_estimates(ctx.jira, issue_key, sp)
    work_h = sp * ctx.jira.hours_per_sp
    wh_disp = parsing.format_arithmetic_mean([work_h])
    sp_disp = parsing.format_story_points_plain(sp)
    if ok:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            f"Проставил задаче [{issue_key}]({jira_url}) оценку в {sp_disp} SP ({wh_disp} ч)."
        )
    else:
        _post_in_thread(
            ctx,
            root_id,
            channel_id,
            f"Jira не обновлена ({issue_key}): `{err}`",
        )


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
                "Открой **тред** (Reply) у нужного приглашения и пришли там оценку. "
                + " В корень чата не пиши — не засчитается.",
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
    value = parsing.parse_vote(raw)
    if value is None:
        _dm_reply_in_thread(
            ctx.driver,
            channel_id,
            actual_root,
            "Нужно одно число в этом треде."
        )
        return

    is_new_vote = user_id not in session.votes
    updated = ctx.session_store.record_vote(session, user_id, value)

    planning_link = _thread_permalink(ctx, updated.team_id, updated.root_post_id)
    log.info(
        "Оценка получена: участник=@%s | значение=%s | задача=%s | тред=%s | root_треда_лс=%s%s",
        updated.username_by_id.get(user_id, user_id),
        parsing.format_vote(value),
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
                handle_channel_reset_command(ctx, post, data)
                handle_channel_list_command(ctx, post, data)
                handle_channel_add_command(ctx, post, data)
                handle_channel_help_command(ctx, post, data)
                handle_channel_agree_command(ctx, post, data)
            else:
                handle_channel_root_post(ctx, post, data)
    except Exception:
        log.exception("handler error post_id=%s", post.get("id"))


async def websocket_event_handler(ctx: BotContext, message: str) -> None:
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8", errors="replace")
    handle_posted_message(ctx, message)
