import asyncio
import base64
import json
import time
import uuid
import random
import ssl
from loguru import logger
import aiohttp
from websockets_proxy import Proxy, proxy_connect

# Configuration
WEBSOCKET_URLS = [
    "wss://proxy2.wynd.network:4650",
    "wss://proxy2.wynd.network:4444",
]

PING_INTERVAL = 60
FETCH_TIMEOUT = 10
RECONNECT_DELAY = 5
HEARTBEAT_TIMEOUT = 130  # If no messages received within this time, reconnect

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

class ConnectionHandler:
    def __init__(self, proxy, connection_id, user_id):
        self.proxy = proxy
        self.connection_id = connection_id
        self.user_id = user_id
        self.last_live_ts = time.time()
        self.stop_event = asyncio.Event()
        self.ws = None
        self.reconnect_task = None
        self.ping_task = None

    async def perform_http_request(self, data):
        request_id = str(uuid.uuid4())
        method = data.get("method", "GET").upper()
        url = data.get("url")
        headers = data.get("headers", {}).copy()
        final_headers = BROWSER_HEADERS.copy()
        final_headers.update(headers)

        body_b64 = data.get("body")
        request_body = base64.b64decode(body_b64) if body_b64 else None

        logger.info(f"[{self.connection_id}-HTTP_REQUEST-{request_id}] {method} {url} via {self.proxy}")

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.request(
                    method=method,
                    url=url,
                    headers=final_headers,
                    data=request_body,
                    timeout=FETCH_TIMEOUT,
                    allow_redirects=False,
                    proxy=self.proxy
                ) as response:
                    status = response.status
                    reason = response.reason
                    raw_headers = response.headers
                    content = await response.read()
                    b64_body = base64.b64encode(content).decode("utf-8")

                    logger.info(f"[{self.connection_id}-HTTP_REQUEST-{request_id}] Response: {status} {reason}")
                    return {
                        "url": str(response.url),
                        "status": status,
                        "status_text": reason,
                        "headers": dict(raw_headers),
                        "body": b64_body
                    }
            except Exception as e:
                logger.error(f"[{self.connection_id}-HTTP_REQUEST-{request_id}] Request failed: {e}")
                return {
                    "url": url,
                    "status": 400,
                    "status_text": str(e),
                    "headers": {},
                    "body": ""
                }

    async def authenticate(self, _data):
        logger.info(f"[{self.connection_id}-AUTH] Authentication requested.")
        browser_id = str(uuid.uuid4())
        return {
            "browser_id": browser_id,
            "user_id": self.user_id,
            "user_agent": BROWSER_HEADERS["User-Agent"],
            "timestamp": int(time.time()),
            "device_type": "extension",
            "version": "4.26.2",
            "extension_id": "lkbnfiajjmbhnfledhphioinpickokdi"
        }

    async def handle_pong(self, _data):
        logger.debug(f"[{self.connection_id}-PONG] Received PONG.")

    async def handle_incoming_message(self, raw_message):
        self.last_live_ts = time.time()
        logger.debug(f"[{self.connection_id}] Received: {raw_message}")

        try:
            message = json.loads(raw_message)
        except Exception as e:
            logger.error(f"[{self.connection_id}] Invalid JSON: {e}")
            return

        action = message.get("action")
        msg_id = message.get("id")
        logger.info(f"[{self.connection_id}] Handling action: {action}, msg_id={msg_id}")

        RPC_CALL_TABLE = {
            "HTTP_REQUEST": self.perform_http_request,
            "AUTH": self.authenticate,
            "PONG": self.handle_pong
        }

        if action == "PING":
            response = {
                "id": msg_id,
                "origin_action": "PONG",
                "result": {}
            }
            await self.send(response)
            logger.debug(f"[{self.connection_id}] Responded with PONG")
            return

        if action in RPC_CALL_TABLE:
            rpc_func = RPC_CALL_TABLE[action]
            try:
                data = message.get("data", {})
                result = await rpc_func(data)
                response = {
                    "id": msg_id,
                    "origin_action": action,
                    "result": result
                }
                await self.send(response)
                logger.info(f"[{self.connection_id}] Sent response for {action}, msg_id={msg_id}")
            except Exception as e:
                logger.exception(f"[{self.connection_id}] Error handling {action}: {e}")
        else:
            logger.warning(f"[{self.connection_id}] Unknown action: {action}")

    async def send_ping(self):
        msg = {
            "id": str(uuid.uuid4()),
            "version": "1.0.0",
            "action": "PING",
            "data": {}
        }
        await self.send(msg)
        logger.debug(f"[{self.connection_id}] Sent PING")

    async def send(self, message):
        if self.ws and self.ws.open:
            await self.ws.send(json.dumps(message))

    async def ping_loop(self):
        while not self.stop_event.is_set():
            if self.ws and self.ws.open:
                await self.send_ping()
            await asyncio.sleep(PING_INTERVAL)

    async def heartbeat_check(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(HEARTBEAT_TIMEOUT)
            if (time.time() - self.last_live_ts) > HEARTBEAT_TIMEOUT:
                logger.warning(f"[{self.connection_id}] No recent activity, reconnecting...")
                await self.close_ws()
                break

    async def close_ws(self):
        if self.ws:
            await self.ws.close()
        self.ws = None

    async def run(self):
        proxy_obj = Proxy.from_url(self.proxy)
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        while not self.stop_event.is_set():
            uri = random.choice(WEBSOCKET_URLS)
            logger.info(f"[{self.connection_id}] Connecting to {uri} via {self.proxy}")

            try:
                async with proxy_connect(uri, proxy=proxy_obj, ssl=ssl_context, server_hostname="proxy2.wynd.network") as websocket:
                    self.ws = websocket
                    self.last_live_ts = time.time()
                    logger.success(f"[{self.connection_id}] Connected to {uri} via proxy")

                    # Start ping and heartbeat checks
                    ping_task = asyncio.create_task(self.ping_loop())
                    heartbeat_task = asyncio.create_task(self.heartbeat_check())

                    try:
                        async for msg in websocket:
                            await self.handle_incoming_message(msg)
                    except Exception as e:
                        logger.error(f"[{self.connection_id}] WebSocket error: {e}")

                    ping_task.cancel()
                    heartbeat_task.cancel()

            except Exception as e:
                logger.error(f"[{self.connection_id}] Connection error: {e}")

            logger.info(f"[{self.connection_id}] Reconnecting in {RECONNECT_DELAY} seconds...")
            await asyncio.sleep(RECONNECT_DELAY)

    async def stop(self):
        self.stop_event.set()
        await self.close_ws()

async def start_all_connections(user_id):
    logger.info("[Main] Loading proxies from local_proxies.txt...")
    with open("local_proxies.txt", "r") as f:
        proxies = [line.strip() for line in f if line.strip()]

    logger.info(f"[Main] Starting tasks for {len(proxies)} proxies...")
    handlers = []
    for i, proxy in enumerate(proxies):
        ch = ConnectionHandler(proxy, i, user_id)
        handlers.append(ch)
        asyncio.create_task(ch.run())

    return handlers

async def main():
    user_id = input("Please enter user_id: ").strip()
    await start_all_connections(user_id)
    # Run indefinitely
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
