import asyncio
import json
import os
import uuid
import base64
import random
from typing import Optional

import aiohttp
from aiohttp import WSMsgType, ClientTimeout
from fake_useragent import UserAgent
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_random, retry_if_exception_type

# === Configuration Constants ===
EXTENSION_ID = "lkbnfiajjmbhnfledhphioinpickokdi"
EXTENSION_VERSION = "5.1.1"
DIRECTOR_SERVER = "https://director.getgrass.io"
PING_INTERVAL = 120  # seconds
RECONNECT_DELAY = 100  # seconds

# === Helper Functions ===
def array_buffer_to_base64(data: bytes) -> str:
    """Encode bytes into a base64 string."""
    return base64.b64encode(data).decode("utf-8")


# === GrassWs Class ===
class GrassWs:
    def __init__(self, user_id: str, proxy: Optional[str] = None):
        self.user_id = user_id
        self.proxy = proxy
        self.browser_id = str(uuid.uuid4())
        self.user_agent = str(UserAgent().random)
        self.session = None
        self.websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self.destination: Optional[str] = None
        self.token: Optional[str] = None
        self.alive = True

    async def create_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    async def get_checkin(self):
        await self.create_session()
        payload = {
            "browserId": self.browser_id,
            "userId": self.user_id,
            "version": EXTENSION_VERSION,
            "extensionId": EXTENSION_ID,
            "userAgent": self.user_agent,
            "deviceType": "extension",
        }
        headers = {
            "Connection": "keep-alive",
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": f"chrome-extension://{EXTENSION_ID}",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
        }
        logger.info(f"[{self.proxy}] Doing checkin...")
        async with self.session.post(
            f"{DIRECTOR_SERVER}/checkin",
            json=payload,
            headers=headers,
            proxy=self.proxy,
        ) as resp:
            text = await resp.text()
            if resp.status != 201:
                raise Exception(f"Checkin failed: {resp.status}")
            data = json.loads(text)
            self.destination = data["destinations"][0]
            self.token = data["token"]
            logger.success(f"[{self.proxy}] Checkin success. Token refreshed.")

    async def connect_websocket(self):
        await self.create_session()
        if not (self.destination and self.token):
            raise Exception("Missing destination or token.")

        uri = f"wss://{self.destination}/?token={self.token}"
        headers = {
            "Host": self.destination,
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": self.user_agent,
            "Upgrade": "websocket",
            "Origin": f"chrome-extension://{EXTENSION_ID}",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(),
            "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
            "Accept": "*/*",
        }
        logger.info(f"[{self.proxy}] Connecting WebSocket...")
        self.websocket = await self.session.ws_connect(
            uri,
            headers=headers,
            proxy=self.proxy,
            timeout=ClientTimeout(ws_close=30)
        )
        logger.success(f"[{self.proxy}] WebSocket connected.")

    async def send_message(self, message: dict):
        await self.websocket.send_str(json.dumps(message, separators=(",", ":")))

    async def send_ping(self):
        ping_msg = {
            "id": str(uuid.uuid4()),
            "version": "1.0.0",
            "action": "PING",
            "data": {},
        }
        logger.debug(f"[{self.proxy}] Sending PING...")
        await self.send_message(ping_msg)

    async def perform_http_request(self, params: dict) -> dict:
        url = params.get("url")
        method = params.get("method", "GET")
        headers = params.get("headers", {})
        body = params.get("body")

        request_kwargs = {
            "method": method,
            "url": url,
            "headers": headers,
            "ssl": False,
        }
        if body:
            request_kwargs["data"] = base64.b64decode(body)

        async with self.session.request(**request_kwargs, proxy=self.proxy) as resp:
            content = await resp.read()
            resp_headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-encoding"}
            return {
                "url": str(resp.url),
                "status": resp.status,
                "status_text": resp.reason,
                "headers": resp_headers,
                "body": array_buffer_to_base64(content),
            }

    async def handle_messages(self):
        async for msg in self.websocket:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    action = data.get("action")
                    req_id = data.get("id")

                    if action == "PING":
                        await self.send_message({"id": req_id, "origin_action": "PONG", "result": {}})
                    elif action == "PONG":
                        pass
                    elif action == "HTTP_REQUEST":
                        params = data.get("data")
                        result = await self.perform_http_request(params)
                        await self.send_message({"id": req_id, "origin_action": "HTTP_REQUEST", "result": result})
                    elif action == "AUTH":
                        auth_payload = {
                            "browser_id": self.browser_id,
                            "user_id": self.user_id,
                            "user_agent": self.user_agent,
                            "timestamp": int(asyncio.get_event_loop().time()),
                            "device_type": "extension",
                            "version": EXTENSION_VERSION,
                            "extension_id": EXTENSION_ID,
                        }
                        await self.send_message({"id": req_id, "origin_action": "AUTH", "result": auth_payload})
                    else:
                        logger.warning(f"[{self.proxy}] Unknown action: {action}")

                except Exception as e:
                    logger.error(f"[{self.proxy}] Message handling error: {e}")

            elif msg.type == WSMsgType.CLOSED:
                logger.warning(f"[{self.proxy}] WebSocket closed.")
                break
            elif msg.type == WSMsgType.ERROR:
                logger.error(f"[{self.proxy}] WebSocket error.")
                break

    async def ping_loop(self):
        while self.alive:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await self.send_ping()
            except Exception as e:
                logger.error(f"[{self.proxy}] Ping failed: {e}")
                break

    @retry(stop=stop_after_attempt(3), wait=wait_random(3, 5))
    async def connection_handler(self):
        await self.get_checkin()
        await self.connect_websocket()

    async def run(self):
        while self.alive:
            try:
                await self.connection_handler()
                await asyncio.gather(self.handle_messages(), self.ping_loop())
            except Exception as e:
                logger.error(f"[{self.proxy}] Exception occurred: {e}")
                await asyncio.sleep(RECONNECT_DELAY)
            finally:
                try:
                    if self.websocket:
                        await self.websocket.close()
                    if self.session:
                        await self.session.close()
                except Exception as e:
                    logger.error(f"[{self.proxy}] Cleanup error: {e}")

# === Load Proxies ===
def load_proxies(file_path: str = "local_proxies.txt") -> list:
    try:
        with open(file_path, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.warning("local_proxies.txt not found.")
        return []

# === Start Clients ===
async def start_all_connections(user_id: str):
    proxies = load_proxies()
    clients = [GrassWs(user_id=user_id, proxy=p) for p in proxies] if proxies else [GrassWs(user_id=user_id)]

    await asyncio.gather(*(client.run() for client in clients))

async def main():
    user_id = input("Enter your user_id: ").strip()
    await start_all_connections(user_id)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program interrupted. Exiting...")
