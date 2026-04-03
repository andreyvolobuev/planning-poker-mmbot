"""Обновление Story Points и time tracking в Jira через REST API."""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import requests

from src.config import JiraIntegration

log = logging.getLogger(__name__)


def _work_hours_to_jira_duration(hours: Decimal) -> str:
    """Часы работы → строка вида 18h или 3h 30m (для timetracking)."""
    if hours <= 0:
        return "0m"
    total_minutes = int((hours * Decimal(60)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if total_minutes == 0:
        return "0m"
    d, rem = divmod(total_minutes, 24 * 60)
    h, m = divmod(rem, 60)
    parts: list[str] = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) if parts else "0m"


def _requests_verify(j: JiraIntegration) -> bool | str:
    if not j.ssl_verify:
        return False
    if j.ssl_ca_file:
        return j.ssl_ca_file
    return True


def sync_story_points_and_estimates(
    j: JiraIntegration,
    issue_key: str,
    story_points: Decimal,
) -> tuple[bool, str]:
    """
    PUT issue: customfield story points + timetracking (original + remaining).
    1 SP = j.hours_per_sp часов для estimate.
    """
    work_hours = story_points * j.hours_per_sp
    duration = _work_hours_to_jira_duration(work_hours)

    # Jira принимает story points как число (float для JSON)
    sp_payload: float | int
    if story_points == story_points.to_integral():
        sp_payload = int(story_points)
    else:
        sp_payload = float(story_points)

    url = f"{j.base_url}/rest/api/2/issue/{issue_key}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {j.token}",
    }
    payload: dict[str, Any] = {
        "fields": {
            j.story_points_field: sp_payload,
            "timetracking": {
                "originalEstimate": duration,
                "remainingEstimate": duration,
            },
        }
    }

    verify = _requests_verify(j)
    try:
        r = requests.put(url, json=payload, headers=headers, verify=verify, timeout=60)
    except requests.RequestException as e:
        log.exception("Jira request failed")
        return False, str(e)

    if r.status_code in (200, 204):
        log.info(
            "Jira обновлена: %s | SP=%s | estimate=%s",
            issue_key,
            story_points,
            duration,
        )
        return True, ""

    try:
        detail = r.json()
    except ValueError:
        detail = r.text
    msg = f"HTTP {r.status_code}: {detail}"
    log.warning("Jira error: %s", msg)
    return False, msg
