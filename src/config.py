"""Load settings from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class JiraIntegration:
    token: str
    base_url: str
    story_points_field: str
    hours_per_sp: Decimal
    ssl_verify: bool
    ssl_ca_file: str | None


@dataclass(frozen=True)
class Settings:
    mattermost_url: str
    mattermost_host: str
    mattermost_scheme: str
    mattermost_port: int
    bot_token: str
    bot_id: str
    ssl_verify: bool
    ssl_ca_file: str | None
    jira: JiraIntegration | None


def _normalize_url(url: str) -> tuple[str, str, str]:
    raw = url.strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise ValueError(f"Invalid MATTERMOST_URL: {url!r}")
    scheme = parsed.scheme or "https"
    host = parsed.hostname
    return raw, host, scheme


def _parse_ssl_verify(env_name: str) -> bool:
    raw = os.environ.get(env_name, "").strip().lower()
    if not raw:
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ValueError(f"{env_name} must be true/false (got {raw!r})")


def _load_jira_integration() -> JiraIntegration | None:
    token = os.environ.get("JIRA_TOKEN", "").strip()
    if not token:
        return None
    base = os.environ.get("JIRA_BASE_URL", "https://jira.2gis.ru").strip().rstrip("/")
    field = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10080").strip()
    hp_raw = os.environ.get("JIRA_HOURS_PER_SP", "6").strip()
    try:
        hours_per_sp = Decimal(hp_raw)
    except Exception as e:
        raise ValueError(f"JIRA_HOURS_PER_SP must be a number (got {hp_raw!r})") from e
    if hours_per_sp <= 0:
        raise ValueError("JIRA_HOURS_PER_SP must be positive")
    jira_ssl = _parse_ssl_verify("JIRA_SSL_VERIFY")
    jira_ca = os.environ.get("JIRA_SSL_CA_FILE", "").strip() or None
    return JiraIntegration(
        token=token,
        base_url=base,
        story_points_field=field,
        hours_per_sp=hours_per_sp,
        ssl_verify=jira_ssl,
        ssl_ca_file=jira_ca,
    )


def load_settings() -> Settings:
    url = os.environ.get("MATTERMOST_URL", "").strip()
    token = os.environ.get("BOT_TOKEN", os.environ.get("MATTERMOST_BOT_TOKEN", "")).strip()
    bot_id = os.environ.get("BOT_ID", "").strip()

    if not url:
        raise ValueError("MATTERMOST_URL is required")
    if not token:
        raise ValueError("BOT_TOKEN is required")
    _, host, scheme = _normalize_url(url)
    port_raw = os.environ.get("MATTERMOST_PORT", "").strip()
    if port_raw:
        port = int(port_raw)
    else:
        port = 443 if scheme == "https" else 8065

    ssl_verify = _parse_ssl_verify("MATTERMOST_SSL_VERIFY")
    ca_file = os.environ.get("MATTERMOST_SSL_CA_FILE", "").strip() or None

    jira = _load_jira_integration()

    return Settings(
        mattermost_url=url.rstrip("/"),
        mattermost_host=host,
        mattermost_scheme=scheme,
        mattermost_port=port,
        bot_token=token,
        bot_id=bot_id,
        ssl_verify=ssl_verify,
        ssl_ca_file=ca_file,
        jira=jira,
    )
