import asyncio
import json
import uuid
import os
import base64
import aiohttp
from aiohttp import WSMsgType
from better_proxy import Proxy
from fake_useragent import UserAgent
from loguru import logger

EXTENSION_ID = "lkbnfiajjmbhnfledhphioinpickokdi"
EXTENSION_VERSION = "5.1.1"
DIRECTOR_SERVER = "https://director.getgrass.io"
PING_INTERVAL = 120
RECONNECT_DELAY = 120  # seconds


class GrassWs:
    def __init__(self, user_id, proxy):
        self.user_id = user_id
        self.proxy = proxy
        self.browser_id = str(uuid.uuid4())
        self.user_agent = str(UserAgent().random)
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        self.websocket = None
        self.destination = None
        self.token = None
        self.alive = True

    async def get_addr(self):
        message = {
            "browserId": self.browser_id,
            "userId": self.user_id,
            "version": EXTENSION_VERSION,
            "extensionId": EXTENSION_ID,
            "userAgent": self.user_agent,
            "deviceType": "extension"
        }

        headers = {
            'Connection': 'keep-alive',
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json',
            'Accept': '*/*',
            'Origin': f'chrome-extension://{EXTENSION_ID}',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        try:
            logger.info(f"[{self.proxy}] Performing checkin request with browser_id={self.browser_id} and user_id={self.user_id}...")
            async with self.session.post(
                f'{DIRECTOR_SERVER}/checkin',
                json=message,
                headers=headers,
                proxy=self.proxy,
                ssl=False
            ) as response:
                text = await response.text()
                logger.info(f"[{self.proxy}] Checkin response status: {response.status}, body: {text[:100]}...")
                if response.status != 201:
                    raise Exception(f"Unexpected status code: {response.status}")
                data = json.loads(text)
                self.destination = data['destinations'][0]
                self.token = data['token']
                logger.success(f"[{self.proxy}] Got destination: {self.destination}")
        except Exception as e:
            logger.error(f"[{self.proxy}] Error during checkin: {e}")
            raise

    async def connect(self):
        uri = f"wss://{self.destination}/?token={self.token}"
        random_bytes = os.urandom(16)
        sec_websocket_key = base64.b64encode(random_bytes).decode('utf-8')

        headers = {
            'Host': self.destination,
            'Connection': 'Upgrade',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
            'User-Agent': self.user_agent,
            'Upgrade': 'websocket',
            'Origin': f'chrome-extension://{EXTENSION_ID}',
            'Sec-WebSocket-Version': '13',
            'Sec-WebSocket-Key': sec_websocket_key,
            'Sec-WebSocket-Extensions': 'permessage-deflate; client_max_window_bits',
            'Accept': '*/*'
        }

        try:
            logger.info(f"[{self.proxy}] Connecting to WebSocket: {uri}")
            self.websocket = await self.session.ws_connect(
                uri,
                headers=headers,
                proxy=self.proxy,
                ssl=True
            )
            logger.success(f"[{self.proxy}] WebSocket connected!")
        except Exception as e:
            logger.error(f"[{self.proxy}] Connection failed: {e}")
            raise

    async def send_ping(self):
        message = {
            "id": str(uuid.uuid4()),
            "version": "1.0.0",
            "action": "PING",
            "data": {}
        }
        await self.websocket.send_str(json.dumps(message))
        logger.info(f"[{self.proxy}] Sent PING")

    async def listen(self):
        try:
            while self.alive:
                msg = await self.websocket.receive()
                if msg.type == WSMsgType.TEXT:
                    logger.debug(f"[{self.proxy}] Received: {msg.data[:100]}...")
                    data = json.loads(msg.data)
                    action = data.get("action")
                    if action == "PING":
                        response = {
                            "id": data.get("id"),
                            "origin_action": "PONG",
                            "result": {}
                        }
                        await self.websocket.send_str(json.dumps(response))
                        logger.info(f"[{self.proxy}] Responded with PONG")
                elif msg.type == WSMsgType.CLOSED:
                    logger.warning(f"[{self.proxy}] WebSocket closed")
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"[{self.proxy}] WebSocket error: {msg}")
                    break
        except Exception as e:
            logger.error(f"[{self.proxy}] Error in listen: {e}")

    async def ping_loop(self):
        while self.alive:
            try:
                await self.send_ping()
            except Exception as e:
                logger.error(f"[{self.proxy}] Ping error: {e}")
                break
            await asyncio.sleep(PING_INTERVAL)

    async def run(self):
        while True:
            try:
                logger.info(f"[{self.proxy}] Starting new session...")
                await self.get_addr()
                await self.connect()
                logger.info(f"[{self.proxy}] Entering listen + ping loop")
                await asyncio.gather(self.listen(), self.ping_loop())
            except Exception as e:
                logger.error(f"[{self.proxy}] Fatal exception: {e}")
                logger.info(f"[{self.proxy}] Reconnecting after delay...")
                await asyncio.sleep(RECONNECT_DELAY)
            finally:
                try:
                    if self.websocket:
                        await self.websocket.close()
                except:
                    pass

    async def close(self):
        self.alive = False
        try:
            await self.session.close()
        except:
            pass


async def start_all_connections(user_id):
    logger.info("[Main] Loading proxies from local_proxies.txt...")
    with open("local_proxies.txt", "r") as f:
        proxies = [line.strip() for line in f if line.strip()]

    logger.info(f"[Main] Starting {len(proxies)} connections...")
    tasks = []
    for proxy in proxies:
        runner = GrassWs(user_id=user_id, proxy=proxy)
        tasks.append(asyncio.create_task(runner.run()))

    await asyncio.gather(*tasks)


async def main():
    user_id = input("Please enter user_id: ").strip()
    logger.info(f"[Main] Received user_id: {user_id}")
    await start_all_connections(user_id)


if __name__ == "__main__":
    asyncio.run(main())
