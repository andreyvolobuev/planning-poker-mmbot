"""In-memory planning poker sessions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PlanningSession:
    root_post_id: str
    channel_id: str
    team_id: str
    jira_url: str
    voter_ids: list[str] = field(default_factory=list)
    username_by_id: dict[str, str] = field(default_factory=dict)
    votes: dict[str, int] = field(default_factory=dict)
    finalized: bool = False
    # id поста-приглашения в ЛС; ответ с оценкой должен иметь root_id == этому id
    dm_invite_root_by_user: dict[str, str] = field(default_factory=dict)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, PlanningSession] = {}

    def try_start(
        self,
        root_post_id: str,
        channel_id: str,
        team_id: str,
        jira_url: str,
        voter_ids: list[str],
        username_by_id: dict[str, str],
    ) -> tuple[bool, Optional[str]]:
        """
        Register a new session. Returns (ok, error_message).
        Несколько активных раундов с одними и теми же людьми разрешены: голос сопоставляется
        по треду ЛС (root_id ответа == id приглашения по конкретной задаче).
        """
        unique_voters: list[str] = []
        seen: set[str] = set()
        for uid in voter_ids:
            if uid not in seen:
                seen.add(uid)
                unique_voters.append(uid)

        if not unique_voters:
            return False, "Не указаны участники для оценки (упоминания `@username`)."

        session = PlanningSession(
            root_post_id=root_post_id,
            channel_id=channel_id,
            team_id=team_id,
            jira_url=jira_url,
            voter_ids=unique_voters,
            username_by_id=dict(username_by_id),
            votes={},
            finalized=False,
        )
        self._sessions[root_post_id] = session
        return True, None

    def get_by_root(self, root_post_id: str) -> Optional[PlanningSession]:
        return self._sessions.get(root_post_id)

    def session_for_dm_invite_thread(
        self, user_id: str, dm_thread_root_id: str
    ) -> Optional[PlanningSession]:
        """Сессия, в которой этот пользователь должен голосовать в треде с данным root_id."""
        if not dm_thread_root_id:
            return None
        for s in self._sessions.values():
            if s.finalized:
                continue
            if s.dm_invite_root_by_user.get(user_id) == dm_thread_root_id:
                return s
        return None

    def user_has_pending_dm_invite(self, user_id: str) -> bool:
        """Есть ли незавершённый раунд, где пользователю уже отправили приглашение в ЛС."""
        for s in self._sessions.values():
            if s.finalized or user_id not in s.voter_ids:
                continue
            if s.dm_invite_root_by_user.get(user_id):
                return True
        return False

    def record_vote(self, session: PlanningSession, user_id: str, value: int) -> PlanningSession:
        session.votes[user_id] = value
        return session

    def all_voted(self, session: PlanningSession) -> bool:
        need = set(session.voter_ids)
        return need.issubset(session.votes.keys())

    def finalize(self, session: PlanningSession) -> None:
        session.finalized = True
        del self._sessions[session.root_post_id]


def median_ceil(values: list[int]) -> int:
    if not values:
        raise ValueError("empty values")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return math.ceil((s[mid - 1] + s[mid]) / 2)
