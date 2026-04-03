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


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, PlanningSession] = {}
        self._user_session: dict[str, str] = {}

    def busy_users(self, user_ids: list[str]) -> list[str]:
        busy: list[str] = []
        for uid in user_ids:
            sid = self._user_session.get(uid)
            if sid and sid in self._sessions and not self._sessions[sid].finalized:
                busy.append(uid)
        return busy

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
        error_message is human-readable (Markdown) for the thread.
        """
        unique_voters: list[str] = []
        seen: set[str] = set()
        for uid in voter_ids:
            if uid not in seen:
                seen.add(uid)
                unique_voters.append(uid)

        if not unique_voters:
            return False, "Не указаны участники для оценки (упоминания `@username`)."

        conflict = self.busy_users(unique_voters)
        if conflict:
            labels = []
            for uid in conflict:
                un = username_by_id.get(uid) or uid
                labels.append(f"@{un}")
            return False, (
                "Нельзя начать раунд: эти участники уже в активной оценке: "
                + ", ".join(labels)
            )

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
        for uid in unique_voters:
            self._user_session[uid] = root_post_id
        return True, None

    def get_by_root(self, root_post_id: str) -> Optional[PlanningSession]:
        return self._sessions.get(root_post_id)

    def session_for_voter(self, user_id: str) -> Optional[PlanningSession]:
        sid = self._user_session.get(user_id)
        if not sid:
            return None
        s = self._sessions.get(sid)
        if not s or s.finalized:
            return None
        if user_id not in set(s.voter_ids):
            return None
        return s

    def record_vote(self, user_id: str, value: int) -> Optional[PlanningSession]:
        session = self.session_for_voter(user_id)
        if not session:
            return None
        session.votes[user_id] = value
        return session

    def all_voted(self, session: PlanningSession) -> bool:
        need = set(session.voter_ids)
        return need.issubset(session.votes.keys())

    def finalize(self, session: PlanningSession) -> None:
        session.finalized = True
        for uid in session.voter_ids:
            if self._user_session.get(uid) == session.root_post_id:
                del self._user_session[uid]
        # keep session in _sessions for debugging; optional removal
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
