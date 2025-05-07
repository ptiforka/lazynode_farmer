import asyncio
import json
import os
import uuid
import base64
import random
from typing import Optional

import aiohttp
from aiohttp import WSMsgType
from fake_useragent import UserAgent
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_random, retry_if_exception_type

# === Configuration Constants ===
EXTENSION_ID = "lkbnfiajjmbhnfledhphioinpickokdi"
EXTENSION_VERSION = "5.1.1"
DIRECTOR_SERVER = "https://director.getgrass.io"
PING_INTERVAL = 120          # seconds; adjust as needed
RECONNECT_DELAY = 100         # seconds; adjust if necessary

# === Helper Functions ===
def array_buffer_to_base64(data: bytes) -> str:
    """Encode bytes into a base64 string."""
    return base64.b64encode(data).decode("utf-8")


# === GrassWs Class: Replicates the extension's behavior ===
class GrassWs:
    def __init__(self, user_id: str, proxy: Optional[str] = None):
        self.user_id = user_id
        self.proxy = proxy  # Use same proxy for all requests in this session.
        self.browser_id = str(uuid.uuid4())
        self.user_agent = str(UserAgent().random)
        # Create a session using a TCPConnector that does not validate SSL (for simplicity).
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        self.websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self.destination: Optional[str] = None
        self.token: Optional[str] = None
        self.alive = True

    async def get_checkin(self):
        """
        Perform a checkin POST request to obtain the destination and token.
        """
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
        try:
            logger.info(f"[{self.proxy}] Performing checkin with browserId={self.browser_id} userId={self.user_id}")
            async with self.session.post(
                f"{DIRECTOR_SERVER}/checkin",
                json=payload,
                headers=headers,
                proxy=self.proxy,
            ) as resp:
                text = await resp.text()
                logger.info(f"[{self.proxy}] Checkin response: status={resp.status} body: {text[:100]}...")
                if resp.status != 201:
                    raise Exception(f"Checkin failed with status code {resp.status}")
                data = json.loads(text)
                # Take the first destination; extend if you need to use multiple
                self.destination = data["destinations"][0]
                self.token = data["token"]
                logger.success(f"[{self.proxy}] Obtained destination: {self.destination} and token.")
        except Exception as e:
            logger.error(f"[{self.proxy}] Error during checkin: {e}")
            raise

    async def connect_websocket(self):
        """
        Connect via WebSocket using the obtained destination and token.
        """
        if not (self.destination and self.token):
            raise Exception("Cannot connect: destination or token not set.")

        uri = f"wss://{self.destination}/?token={self.token}"
        # Generate random key for WebSocket handshake
        rand_bytes = os.urandom(16)
        sec_ws_key = base64.b64encode(rand_bytes).decode("utf-8")
        headers = {
            "Host": self.destination,
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": self.user_agent,
            "Upgrade": "websocket",
            "Origin": f"chrome-extension://{EXTENSION_ID}",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": sec_ws_key,
            "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
            "Accept": "*/*",
        }
        try:
            logger.info(f"[{self.proxy}] Connecting to WebSocket at {uri}")
            self.websocket = await self.session.ws_connect(
                uri,
                headers=headers,
                proxy=self.proxy,
                timeout=15,
            )
            logger.success(f"[{self.proxy}] WebSocket connection established!")
        except Exception as e:
            logger.error(f"[{self.proxy}] WebSocket connection failed: {e}")
            raise

    async def send_message(self, message: dict):
        """
        Sends a JSON message over the WebSocket.
        """
        try:
            await self.websocket.send_str(json.dumps(message, separators=(",", ":")))
        except Exception as e:
            logger.error(f"[{self.proxy}] Error sending message: {e}")
            raise

    async def send_ping(self):
        """
        Sends a PING message over the WebSocket.
        """
        message = {
            "id": str(uuid.uuid4()),
            "version": "1.0.0",
            "action": "PING",
            "data": {},
        }
        logger.info(f"[{self.proxy}] Sending PING")
        await self.send_message(message)

    async def perform_http_request(self, params: dict) -> dict:
        """
        Replicates the HTTP_REQUEST action.
        Expects params with keys: url, method, headers, and optionally body (base64 encoded).
        Returns a dict with url, status, status_text, headers, and a base64 encoded body.
        """
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
            try:
                request_kwargs["data"] = base64.b64decode(body)
            except Exception as e:
                logger.error(f"[{self.proxy}] Error decoding body: {e}")

        try:
            logger.info(f"[{self.proxy}] Performing HTTP_REQUEST: {method} {url}")
            async with self.session.request(**request_kwargs, proxy=self.proxy) as resp:
                content = await resp.read()
                resp_headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-encoding"}
                result = {
                    "url": str(resp.url),
                    "status": resp.status,
                    "status_text": resp.reason,
                    "headers": resp_headers,
                    "body": array_buffer_to_base64(content),
                }
                logger.success(f"[{self.proxy}] HTTP_REQUEST successful: {method} {url} -> {resp.status}")
                return result
        except Exception as e:
            logger.error(f"[{self.proxy}] HTTP_REQUEST error: {e}")
            return {
                "url": url,
                "status": 400,
                "status_text": "Bad Request",
                "headers": {},
                "body": "",
            }

    async def handle_messages(self):
        """
        Listen to messages from the WebSocket and respond based on action.
          - For "PING": respond with "PONG".
          - For "PONG": simply log (extension does nothing).
          - For "HTTP_REQUEST": perform the HTTP request and send response.
          - For "AUTH": send back authentication data.
        """
        try:
            async for msg in self.websocket:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception as e:
                        logger.error(f"[{self.proxy}] Failed to parse message: {e}")
                        continue
                    action = data.get("action")
                    req_id = data.get("id")
                    if action == "PING":
                        # Reply with PONG as per extension logic
                        response = {"id": req_id, "origin_action": "PONG", "result": {}}
                        await self.send_message(response)
                        logger.info(f"[{self.proxy}] Received PING; responded with PONG")
                    elif action == "PONG":
                        # The extension's PONG handler is a no-op, so we just log it.
                        logger.info(f"[{self.proxy}] Received PONG")
                    elif action == "HTTP_REQUEST":
                        params = data.get("data")
                        result = await self.perform_http_request(params)
                        response = {"id": req_id, "origin_action": "HTTP_REQUEST", "result": result}
                        await self.send_message(response)
                        logger.info(f"[{self.proxy}] Processed HTTP_REQUEST")
                    elif action == "AUTH":
                        auth_result = {
                            "browser_id": self.browser_id,
                            "user_id": self.user_id,
                            "user_agent": self.user_agent,
                            "timestamp": int(asyncio.get_event_loop().time()),
                            "device_type": "extension",
                            "version": EXTENSION_VERSION,
                            "extension_id": EXTENSION_ID,
                        }
                        response = {"id": req_id, "origin_action": "AUTH", "result": auth_result}
                        await self.send_message(response)
                        logger.info(f"[{self.proxy}] Responded to AUTH")
                    else:
                        logger.warning(f"[{self.proxy}] Received unknown action: {action}")
                elif msg.type == WSMsgType.CLOSED:
                    logger.warning(f"[{self.proxy}] WebSocket connection closed")
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"[{self.proxy}] WebSocket error: {msg}")
                    break
        except Exception as e:
            logger.error(f"[{self.proxy}] Error in handle_messages: {e}")

    async def ping_loop(self):
        """
        Periodically send PING messages.
        """
        while self.alive:
            try:
                await asyncio.sleep(PING_INTERVAL)
                await self.send_ping()
            except Exception as e:
                logger.error(f"[{self.proxy}] Ping loop error: {e}")
                break

    @retry(stop=stop_after_attempt(3), wait=wait_random(3, 5), retry=retry_if_exception_type(Exception))
    async def connection_handler(self):
        """
        Connect to WebSocket after checkin. Retry a few times on failure.
        """
        await self.get_checkin()
        await self.connect_websocket()

    async def run(self):
        """
        Main loop: perform connection (with retries), then run both the message handler
        and the ping loop concurrently. On disconnect, wait and then reconnect.
        """
        while self.alive:
            try:
                logger.info(f"[{self.proxy}] Starting session for browser_id={self.browser_id}")
                await self.connection_handler()
                # Run both the WebSocket message handler and the ping loop concurrently.
                await asyncio.gather(self.handle_messages(), self.ping_loop())
            except Exception as e:
                logger.error(f"[{self.proxy}] Exception in session: {e}")
                logger.info(f"[{self.proxy}] Reconnecting after {RECONNECT_DELAY} seconds...")
                await asyncio.sleep(RECONNECT_DELAY)
            finally:
                try:
                    if self.websocket:
                        await self.websocket.close()
                except Exception as e:
                    logger.error(f"[{self.proxy}] Error closing WebSocket: {e}")

    async def close(self):
        """
        Closes the client session and stops the loop.
        """
        self.alive = False
        await self.session.close()


# === Utility to load proxies from file ===
def load_proxies(file_path: str = "local_proxies.txt") -> list:
    try:
        with open(file_path, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.error(f"Proxy file {file_path} not found.")
        return []


# === Main Entry Point ===
async def start_all_connections(user_id: str):
    """
    Loads proxies (if any) and starts a GrassWs instance for each proxy.
    If no proxy is provided, it runs without a proxy.
    """
    proxies = load_proxies()
    clients = []
    if proxies:
        for proxy in proxies:
            clients.append(GrassWs(user_id=user_id, proxy=proxy))
    else:
        # No proxies found: run with direct connection
        clients.append(GrassWs(user_id=user_id))

    tasks = [asyncio.create_task(client.run()) for client in clients]
    await asyncio.gather(*tasks)


async def main():
    try:
        with open("user_ids.txt", "r") as f:
            user_ids = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    except FileNotFoundError:
        logger.error("user_ids.txt file not found.")
        return

    if not user_ids:
        logger.warning("No valid user IDs found in user_ids.txt.")
        return

    # Create and run all connections for all user IDs concurrently
    await asyncio.gather(*(start_all_connections(user_id) for user_id in user_ids))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program interrupted; exiting...")
