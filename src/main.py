"""Planning poker bot: WebSocket + REST (mattermostdriver)."""

from __future__ import annotations

import logging
import sys

import urllib3
from mattermostdriver import Driver
from urllib3.exceptions import InsecureRequestWarning

from src.config import load_settings
from src.handlers import BotContext, websocket_event_handler
from src.mattermost_client import build_driver
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

    log.info(
        "Бот запущен: user=%s, слушаю все командные каналы, куда добавлен бот (Jira + @mentions в корне треда).",
        me.get("username"),
    )

    if settings.jira:
        log.info("Интеграция с Jira включена (%s).", settings.jira.base_url)
        if not settings.jira.ssl_verify:
            urllib3.disable_warnings(InsecureRequestWarning)
    store = SessionStore()
    ctx = BotContext(
        driver=driver,
        bot_id=bot_user_id,
        site_url=settings.mattermost_url,
        session_store=store,
        jira=settings.jira,
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
