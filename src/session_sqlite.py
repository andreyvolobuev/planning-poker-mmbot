"""SQLite persistence for active planning sessions (stdlib sqlite3, WAL)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from decimal import Decimal
from typing import Any

from src.sessions import PlanningSession, SessionStore

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"

UPSERT_SQL = """
INSERT INTO planning_sessions (
  root_post_id, channel_id, team_id, jira_url, organizer_user_id,
  voter_ids_json, username_by_id_json, votes_json, dm_invite_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(root_post_id) DO UPDATE SET
  channel_id = excluded.channel_id,
  team_id = excluded.team_id,
  jira_url = excluded.jira_url,
  organizer_user_id = excluded.organizer_user_id,
  voter_ids_json = excluded.voter_ids_json,
  username_by_id_json = excluded.username_by_id_json,
  votes_json = excluded.votes_json,
  dm_invite_json = excluded.dm_invite_json
"""


def open_connection(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
          key TEXT PRIMARY KEY NOT NULL,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS planning_sessions (
          root_post_id TEXT PRIMARY KEY NOT NULL,
          channel_id TEXT NOT NULL,
          team_id TEXT NOT NULL,
          jira_url TEXT NOT NULL,
          organizer_user_id TEXT NOT NULL,
          voter_ids_json TEXT NOT NULL,
          username_by_id_json TEXT NOT NULL,
          votes_json TEXT NOT NULL,
          dm_invite_json TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SCHEMA_VERSION,),
    )
    conn.commit()


def _session_to_params(session: PlanningSession) -> tuple[Any, ...]:
    votes_obj = {uid: str(val) for uid, val in session.votes.items()}
    return (
        session.root_post_id,
        session.channel_id,
        session.team_id,
        session.jira_url,
        session.organizer_user_id,
        json.dumps(session.voter_ids),
        json.dumps(session.username_by_id),
        json.dumps(votes_obj),
        json.dumps(session.dm_invite_root_by_user),
    )


def upsert_session(conn: sqlite3.Connection, session: PlanningSession) -> None:
    conn.execute(UPSERT_SQL, _session_to_params(session))
    conn.commit()


def delete_session(conn: sqlite3.Connection, root_post_id: str) -> None:
    conn.execute("DELETE FROM planning_sessions WHERE root_post_id = ?", (root_post_id,))
    conn.commit()


def _row_to_session(row: sqlite3.Row) -> PlanningSession:
    voter_ids: list[str] = json.loads(row["voter_ids_json"])
    username_by_id: dict[str, str] = json.loads(row["username_by_id_json"])
    votes_raw: dict[str, str] = json.loads(row["votes_json"])
    votes: dict[str, Decimal] = {uid: Decimal(s) for uid, s in votes_raw.items()}
    dm_invite: dict[str, str] = json.loads(row["dm_invite_json"])
    return PlanningSession(
        root_post_id=row["root_post_id"],
        channel_id=row["channel_id"],
        team_id=row["team_id"],
        jira_url=row["jira_url"],
        organizer_user_id=row["organizer_user_id"],
        voter_ids=voter_ids,
        username_by_id=username_by_id,
        votes=votes,
        finalized=False,
        dm_invite_root_by_user=dm_invite,
    )


def load_all_sessions(conn: sqlite3.Connection) -> list[PlanningSession]:
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM planning_sessions")
        rows = cur.fetchall()
    finally:
        conn.row_factory = None
    return [_row_to_session(r) for r in rows]


class SqliteBackedSessionStore(SessionStore):
    """SessionStore с записью в SQLite после рассылки ЛС, голосов и удалением при finalize."""

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._conn = open_connection(db_path)
        init_schema(self._conn)
        loaded = load_all_sessions(self._conn)
        for s in loaded:
            self._sessions[s.root_post_id] = s
        if loaded:
            log.info("Загружено активных раундов из SQLite: %s", len(loaded))

    def persist_session(self, root_post_id: str) -> None:
        s = self.get_by_root(root_post_id)
        if not s:
            return
        try:
            upsert_session(self._conn, s)
        except Exception:
            log.exception("SQLite upsert failed root=%s", root_post_id)

    def record_vote(
        self, session: PlanningSession, user_id: str, value: Decimal
    ) -> PlanningSession:
        out = super().record_vote(session, user_id, value)
        try:
            upsert_session(self._conn, out)
        except Exception:
            log.exception("SQLite upsert after vote failed root=%s", out.root_post_id)
        return out

    def finalize(self, session: PlanningSession) -> None:
        rid = session.root_post_id
        super().finalize(session)
        try:
            delete_session(self._conn, rid)
        except Exception:
            log.exception("SQLite delete after finalize failed root=%s", rid)
