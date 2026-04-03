"""Mattermost Driver factory."""

from __future__ import annotations

from mattermostdriver import Driver

from src.config import Settings


def _requests_verify_arg(settings: Settings) -> bool | str:
    if not settings.ssl_verify:
        return False
    if settings.ssl_ca_file:
        return settings.ssl_ca_file
    return True


def build_driver(settings: Settings) -> Driver:
    return Driver(
        {
            "url": settings.mattermost_host,
            "port": settings.mattermost_port,
            "scheme": settings.mattermost_scheme,
            "basepath": "/api/v4",
            "token": settings.bot_token,
            "verify": _requests_verify_arg(settings),
        }
    )
