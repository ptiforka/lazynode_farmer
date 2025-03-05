import asyncio
import base64
import json
import time
import uuid
import random
import ssl
import aiohttp
from loguru import logger
from websockets_proxy import Proxy, proxy_connect
from urllib.parse import urlparse

# Constants
EXTENSION_VERSION = "5.1.1"   # or whatever the extension version is
EXTENSION_ID = "lkbnfiajjmbhnfledhphioinpickokdi"
DIRECTOR_SERVER = "https://director.getgrass.io"

PING_INTERVAL = 60
FETCH_TIMEOUT = 10
RECONNECT_DELAY = 250         # Short reconnect delay for quick retries
HEARTBEAT_TIMEOUT = 130     # Reconnect if no messages within this time

# 402 handling configuration
MAX_402_ERRORS = 50
HOUR_DELAY = 3600  # 1 hour

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

global_402_count = 0
stop_all_connections_event = asyncio.Event()

async def checkin(user_id, browser_id):
    """
    Perform a checkin to the director server and return JSON data with
    destinations + token. We handle any 2xx status code as success,
    parse as JSON if possible.
    """
    payload = {
        "browserId": browser_id,
        "userId": user_id,
        "version": EXTENSION_VERSION,
        "extensionId": EXTENSION_ID,
        "userAgent": BROWSER_HEADERS["User-Agent"],
        "deviceType": "extension"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DIRECTOR_SERVER}/checkin",
                json=payload,
                timeout=FETCH_TIMEOUT
            ) as response:

                # Treat any 2xx code as success
                if 200 <= response.status < 300:
                    text = await response.text()
                    # Try parsing the text as JSON
                    try:
                        data = json.loads(text)
                        logger.info(f"Checkin successful: {data}")
                        return data
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Checkin returned non-JSON data (status {response.status}): {text}"
                        )
                        return {}
                else:
                    logger.error(f"Checkin failed with status {response.status}")
                    return {}
    except Exception as e:
        logger.error(f"Checkin exception: {e}")
        return {}

class ConnectionHandler:
    def __init__(self, proxy, connection_id, user_id):
        self.proxy = proxy
        self.connection_id = connection_id
        self.user_id = user_id
        # Each connection uses a "browser_id". Could persist it if you like.
        self.browser_id = str(uuid.uuid4())
        self.last_live_ts = time.time()
        self.stop_event = asyncio.Event()
        self.ws = None

    async def perform_http_request(self, data):
        request_id = str(uuid.uuid4())
        method = data.get("method", "GET").upper()
        url = data.get("url")
        headers = data.get("headers", {}).copy()
        final_headers = BROWSER_HEADERS.copy()
        final_headers.update(headers)

        body_b64 = data.get("body")
        request_body = base64.b64decode(body_b64) if body_b64 else None

        logger.info(
            f"[{self.connection_id}-HTTP_REQUEST-{request_id}] {method} {url} via {self.proxy}"
        )
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
                    logger.info(
                        f"[{self.connection_id}-HTTP_REQUEST-{request_id}] "
                        f"Response: {status} {reason}"
                    )
                    return {
                        "url": str(response.url),
                        "status": status,
                        "status_text": reason,
                        "headers": dict(raw_headers),
                        "body": b64_body
                    }
            except Exception as e:
                logger.error(
                    f"[{self.connection_id}-HTTP_REQUEST-{request_id}] Request failed: {e}"
                )
                return {
                    "url": url,
                    "status": 400,
                    "status_text": str(e),
                    "headers": {},
                    "body": ""
                }

    async def authenticate(self, _data):
        logger.info(f"[{self.connection_id}-AUTH] Authentication requested.")
        return {
            "browser_id": self.browser_id,
            "user_id": self.user_id,
            "user_agent": BROWSER_HEADERS["User-Agent"],
            "timestamp": int(time.time()),
            "device_type": "extension",
            "version": EXTENSION_VERSION,
            "extension_id": EXTENSION_ID
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
        logger.info(
            f"[{self.connection_id}] Handling action: {action}, msg_id={msg_id}"
        )

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
                logger.info(
                    f"[{self.connection_id}] Sent response for {action}, msg_id={msg_id}"
                )
            except Exception as e:
                logger.exception(
                    f"[{self.connection_id}] Error handling {action}: {e}"
                )
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
        while not self.stop_event.is_set() and not stop_all_connections_event.is_set():
            if self.ws and self.ws.open:
                await self.send_ping()
            await asyncio.sleep(PING_INTERVAL)

    async def heartbeat_check(self):
        while not self.stop_event.is_set() and not stop_all_connections_event.is_set():
            await asyncio.sleep(HEARTBEAT_TIMEOUT)
            if (time.time() - self.last_live_ts) > HEARTBEAT_TIMEOUT:
                logger.warning(
                    f"[{self.connection_id}] No recent activity, reconnecting..."
                )
                await self.close_ws()
                break

    async def close_ws(self):
        if self.ws:
            await self.ws.close()
        self.ws = None

    async def run(self):
        """
        Continuously:
          - Checkin to get destinations + token
          - Pick a destination
          - Connect via websockets-proxy
          - On error or stop, wait and retry
        """
        global global_402_count
        proxy_obj = Proxy.from_url(self.proxy)

        # Create an SSL context for wss connections
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        while not self.stop_event.is_set() and not stop_all_connections_event.is_set():
            # 1) Perform checkin to get the latest destinations + token
            checkin_data = await checkin(self.user_id, self.browser_id)
            if not checkin_data:
                logger.error(
                    f"[{self.connection_id}] Checkin failed. "
                    f"Retrying in {RECONNECT_DELAY} seconds..."
                )
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            destinations = checkin_data.get("destinations", [])
            token = checkin_data.get("token", "")
            if not destinations or not token:
                logger.error(
                    f"[{self.connection_id}] Invalid checkin data: {checkin_data}. "
                    f"Retrying in {RECONNECT_DELAY} seconds..."
                )
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            # 2) Ensure each destination has a scheme
            #    If the server only returns IP/host, prepend ws://
            #    (If the server supports wss://, you might want "wss://" instead)
            destinations = [
                d if d.startswith("ws://") or d.startswith("wss://")
                else "ws://" + d
                for d in destinations
            ]

            uri = random.choice(destinations) + f"?token={token}"
            logger.info(f"[{self.connection_id}] Connecting to {uri} via {self.proxy}")

            # 3) If it's wss://, pass ssl_context. If ws://, pass None
            parsed = urlparse(uri)
            if parsed.scheme == "wss":
                chosen_ssl = ssl_context
                hostname = "proxy2.wynd.network"
            else:
                chosen_ssl = None
                hostname = None

            try:
                async with proxy_connect(
                    uri,
                    proxy=proxy_obj,
                    ssl=chosen_ssl,
                    server_hostname=hostname
                ) as websocket:
                    self.ws = websocket
                    self.last_live_ts = time.time()
                    logger.success(
                        f"[{self.connection_id}] Connected to {uri} via proxy"
                    )

                    # Start ping and heartbeat tasks
                    ping_task = asyncio.create_task(self.ping_loop())
                    heartbeat_task = asyncio.create_task(self.heartbeat_check())

                    try:
                        async for msg in websocket:
                            if stop_all_connections_event.is_set():
                                break
                            await self.handle_incoming_message(msg)
                    except Exception as e:
                        logger.error(f"[{self.connection_id}] WebSocket error: {e}")

                    ping_task.cancel()
                    heartbeat_task.cancel()

            except Exception as e:
                logger.error(f"[{self.connection_id}] Connection error: {e}")
                # If we see "402" in the error, handle the 402 logic
                error_str = str(e)
                if "402" in error_str:
                    global_402_count += 1
                    logger.error(
                        f"[{self.connection_id}] Encountered 402 error. "
                        f"Count: {global_402_count}"
                    )
                    if global_402_count > MAX_402_ERRORS:
                        logger.error(
                            "Too many 402 errors encountered. Disabling connections "
                            "and retrying in one hour..."
                        )
                        await stop_all_connections()
                        await asyncio.sleep(HOUR_DELAY)
                        global_402_count = 0
                        await start_all_connections(self.user_id)
                        return

            if not stop_all_connections_event.is_set():
                logger.info(
                    f"[{self.connection_id}] Reconnecting in {RECONNECT_DELAY} seconds..."
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def stop(self):
        self.stop_event.set()
        await self.close_ws()


async def stop_all_connections():
    stop_all_connections_event.set()
    logger.info("[Main] Stopping all connections due to excessive 402 errors.")


async def start_all_connections(user_id):
    stop_all_connections_event.clear()
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
    # Keep running indefinitely
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
