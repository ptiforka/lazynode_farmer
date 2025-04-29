import asyncio
import json
import os
import uuid
import base64
import random
import re
from typing import Optional

import aiohttp
from aiohttp import WSMsgType
from fake_useragent import UserAgent
from loguru import logger

# === Config ===
EXTENSION_ID    = "lkbnfiajjmbhnfledhphioinpickokdi"
EXTENSION_VER   = "5.1.1"
DIRECTOR_SERVER = "https://director.getgrass.io"
PING_INTERVAL   = 120         # seconds
BASE_BACKOFF    = 100          # seconds
JITTER_FACTOR   = 0.8         # fraction of BASE_BACKOFF


def jitter_delay(base: float) -> float:
    return base + random.uniform(0, base * JITTER_FACTOR)


def array_buffer_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


class GrassClient:
    def __init__(self, user_id: str, proxy: Optional[str] = None):
        self.user_id    = user_id
        self.proxy      = proxy
        self.browser_id = str(uuid.uuid4())
        self.user_agent = UserAgent().random
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.dest: Optional[str] = None
        self.token: Optional[str] = None

    async def ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    async def checkin(self):
        await self.ensure_session()
        payload = {
            "browserId":   self.browser_id,
            "userId":      self.user_id,
            "version":     EXTENSION_VER,
            "extensionId": EXTENSION_ID,
            "userAgent":   self.user_agent,
            "deviceType":  "extension",
        }
        headers = {
            "User-Agent":   self.user_agent,
            "Content-Type": "application/json",
            "Origin":       f"chrome-extension://{EXTENSION_ID}",
        }

        logger.info(f"[{self.proxy}] CHECKIN → POST {DIRECTOR_SERVER}/checkin")
        async with self.session.post(
            f"{DIRECTOR_SERVER}/checkin",
            json=payload,
            headers=headers,
            proxy=self.proxy,
        ) as resp:
            text   = await resp.text()
            status = resp.status

            if status in (429, 503):
                retry_after = resp.headers.get("Retry-After", "").strip()
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 60.0
                raise RuntimeError(f"CHECKIN {status} ({text.strip()}), retry after {delay}")

            if status != 201:
                raise RuntimeError(f"CHECKIN failed {status}: {text!r}")

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"CHECKIN: invalid JSON: {text!r}")

            self.dest  = data["destinations"][0]
            self.token = data["token"]
            logger.success(f"[{self.proxy}] CHECKIN OK → dest={self.dest}")

    async def connect_ws(self):
        if not self.dest or not self.token:
            raise RuntimeError("missing dest/token")
        uri = f"wss://{self.dest}/?token={self.token}"
        headers = {
            "User-Agent": self.user_agent,
            "Origin":     f"chrome-extension://{EXTENSION_ID}",
        }
        logger.info(f"[{self.proxy}] WS → CONNECT {uri}")
        self.ws = await self.session.ws_connect(uri, headers=headers, proxy=self.proxy)
        logger.success(f"[{self.proxy}] WS ← OPEN")

    async def send_json(self, msg: dict):
        await self.ws.send_str(json.dumps(msg, separators=(",", ":")))

    async def send_ping(self):
        ping = {
            "id":      str(uuid.uuid4()),
            "version": "1.0.0",
            "action":  "PING",
            "data":    {},
        }
        await self.send_json(ping)
        logger.debug(f"[{self.proxy}] WS → PING")

    async def http_request(self, params: dict) -> dict:
        kwargs = {
            "method":  params.get("method", "GET"),
            "url":     params["url"],
            "headers": params.get("headers", {}),
            "ssl":     False,
            "proxy":   self.proxy,
        }
        if body := params.get("body"):
            kwargs["data"] = base64.b64decode(body)
        async with self.session.request(**kwargs) as resp:
            content = await resp.read()
            hdrs = {k: v for k, v in resp.headers.items() if k.lower() != "content-encoding"}
            return {
                "url":         str(resp.url),
                "status":      resp.status,
                "status_text": resp.reason,
                "headers":     hdrs,
                "body":        array_buffer_to_base64(content),
            }

    async def handle_ws(self):
        async for msg in self.ws:
            if msg.type == WSMsgType.TEXT:
                data   = json.loads(msg.data)
                action = data.get("action")
                req_id = data.get("id")

                if action == "PING":
                    # reply server PING with PONG
                    await self.send_json({
                        "id":             req_id,
                        "origin_action":  "PONG",
                        "result":         {}
                    })
                elif action == "PONG":
                    # server heartbeat acknowledgement, ignore
                    continue
                elif action == "HTTP_REQUEST":
                    result = await self.http_request(data["data"])
                    await self.send_json({
                        "id":             req_id,
                        "origin_action":  "HTTP_REQUEST",
                        "result":         result
                    })
                elif action == "AUTH":
                    auth = {
                        "browser_id":   self.browser_id,
                        "user_id":      self.user_id,
                        "user_agent":   self.user_agent,
                        "timestamp":    int(asyncio.get_event_loop().time()),
                        "device_type":  "extension",
                        "version":      EXTENSION_VER,
                        "extension_id": EXTENSION_ID,
                    }
                    await self.send_json({
                        "id":             req_id,
                        "origin_action":  "AUTH",
                        "result":         auth
                    })
                else:
                    logger.warning(f"[{self.proxy}] WS ← Unknown action {action!r}")

            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                logger.warning(f"[{self.proxy}] WS ← {msg.type.name}, exiting")
                break

    async def ping_loop(self):
        while self.ws and not self.ws.closed:
            await asyncio.sleep(PING_INTERVAL + random.uniform(-5, 5))
            try:
                await self.send_ping()
            except Exception:
                break

    async def run(self):
        await self.ensure_session()
        backoff = BASE_BACKOFF
        while True:
            # only checkin/connect if WS is not open
            if not self.ws or self.ws.closed:
                try:
                    await self.checkin()
                    await self.connect_ws()
                except RuntimeError as e:
                    m = re.search(r"retry after ([\d\.]+)", str(e))
                    backoff = float(m.group(1)) if m else BASE_BACKOFF
                    logger.error(f"[{self.proxy}] {e}; retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                    continue
                except Exception as e:
                    logger.error(f"[{self.proxy}] UNEXPECTED: {e}; retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                    continue

            # steady-state: WS is open, run handlers
            try:
                await asyncio.gather(self.handle_ws(), self.ping_loop())
            except Exception as e:
                logger.warning(f"[{self.proxy}] WS died: {e}; reconnecting in {BASE_BACKOFF}s")
            finally:
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                await asyncio.sleep(BASE_BACKOFF)


def load_proxies(path="local_proxies.txt") -> list[str]:
    try:
        return [line.strip() for line in open(path) if line.strip()]
    except FileNotFoundError:
        logger.warning("no proxy file, using direct connection")
        return []

async def main():
    user_id = input("Enter user_id: ").strip()
    proxies = load_proxies()
    clients = [GrassClient(user_id, p) for p in proxies] or [GrassClient(user_id)]
    await asyncio.gather(*(c.run() for c in clients))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exiting…")
