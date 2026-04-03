"""Mattermost Driver factory and channel lookup."""

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


def resolve_planning_channel(driver: Driver, channel_name: str, bot_user_id: str) -> dict:
    """
    Find channel by name among teams the bot belongs to.
    Returns channel object (includes id, team_id, name, type, ...).
    """
    teams = driver.teams.get_user_teams(bot_user_id)
    last_error: str | None = None
    for team in teams:
        team_id = team["id"]
        try:
            return driver.channels.get_channel_by_name(team_id, channel_name)
        except Exception as e:  # noqa: BLE001 — try next team
            last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            continue
    raise RuntimeError(
        f"Канал {channel_name!r} не найден ни в одной команде бота. "
        f"Добавьте бота в канал. Последняя ошибка API: {last_error}"
    )
