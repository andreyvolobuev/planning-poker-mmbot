"""Load settings from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


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


def _parse_ssl_verify() -> bool:
    raw = os.environ.get("MATTERMOST_SSL_VERIFY", "").strip().lower()
    if not raw:
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ValueError(f"MATTERMOST_SSL_VERIFY must be true/false (got {raw!r})")


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

    ssl_verify = _parse_ssl_verify()
    ca_file = os.environ.get("MATTERMOST_SSL_CA_FILE", "").strip() or None

    return Settings(
        mattermost_url=url.rstrip("/"),
        mattermost_host=host,
        mattermost_scheme=scheme,
        mattermost_port=port,
        bot_token=token,
        bot_id=bot_id,
        ssl_verify=ssl_verify,
        ssl_ca_file=ca_file,
    )
