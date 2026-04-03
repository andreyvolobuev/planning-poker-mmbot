"""Planning poker bot: WebSocket + REST (mattermostdriver)."""

from __future__ import annotations

import logging
import sys

import urllib3
from mattermostdriver import Driver
from urllib3.exceptions import InsecureRequestWarning

from src.config import load_settings
from src.handlers import BotContext, websocket_event_handler
from src.mattermost_client import build_driver, resolve_planning_channel
from src.mattermost_websocket import ServerAuthSSLWebsocket
from src.sessions import SessionStore


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("poker_bot")

    settings = load_settings()
    if not settings.ssl_verify:
        log.warning("MATTERMOST_SSL_VERIFY=false: проверка TLS отключена (только для доверенной сети).")
        urllib3.disable_warnings(InsecureRequestWarning)
    driver: Driver = build_driver(settings)
    driver.login()

    me = driver.users.get_user("me")
    bot_user_id = me["id"]
    if settings.bot_id and bot_user_id != settings.bot_id:
        log.warning(
            "BOT_ID в .env (%s) не совпадает с id из API (%s); используется id из API.",
            settings.bot_id,
            bot_user_id,
        )

    channel = resolve_planning_channel(driver, settings.planning_channel, bot_user_id)
    planning_channel_id = channel["id"]
    log.info(
        "Подключено: канал #%s (%s), bot user=%s",
        channel.get("name"),
        planning_channel_id,
        me.get("username"),
    )

    store = SessionStore()
    ctx = BotContext(
        driver=driver,
        bot_id=bot_user_id,
        planning_channel_id=planning_channel_id,
        session_store=store,
    )

    async def on_event(message: str) -> None:
        await websocket_event_handler(ctx, message)

    log.info("WebSocket: ожидание событий…")
    driver.init_websocket(on_event, websocket_cls=ServerAuthSSLWebsocket)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
