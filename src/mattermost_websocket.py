"""
mattermostdriver использует ssl.Purpose.CLIENT_AUTH для WSS — это контекст для
*серверного* сокета; для клиента нужен SERVER_AUTH. На актуальных Python/OpenSSL
это даёт: "Cannot create a client socket with a PROTOCOL_TLS_SERVER context".
"""

from __future__ import annotations

import ssl

from mattermostdriver.websocket import Websocket


class ServerAuthSSLWebsocket(Websocket):
    """Тот же Websocket из драйвера, но с корректным SSL для исходящего wss://."""

    async def connect(self, event_handler):
        import asyncio
        import json

        import websockets
        from mattermostdriver.websocket import log

        context: ssl.SSLContext | None
        if self.options["scheme"] == "https":
            verify = self.options["verify"]
            context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
            if verify is False:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            elif isinstance(verify, str):
                context.load_verify_locations(cafile=verify)
        else:
            context = None

        scheme = "wss://" if self.options["scheme"] == "https" else "ws://"

        url = "{scheme:s}{url:s}:{port:s}{basepath:s}/websocket".format(
            scheme=scheme,
            url=self.options["url"],
            port=str(self.options["port"]),
            basepath=self.options["basepath"],
        )

        self._alive = True

        while True:
            try:
                kw_args = {}
                if self.options["websocket_kw_args"] is not None:
                    kw_args = self.options["websocket_kw_args"]
                websocket = await websockets.connect(
                    url,
                    ssl=context,
                    **kw_args,
                )
                await self._authenticate_websocket(websocket, event_handler)
                while self._alive:
                    try:
                        await self._start_loop(websocket, event_handler)
                    except websockets.ConnectionClosedError:
                        break
                if (not self.options["keepalive"]) or (not self._alive):
                    break
            except Exception as e:
                log.warning("Failed to establish websocket connection: %s", e)
                await asyncio.sleep(self.options["keepalive_delay"])
