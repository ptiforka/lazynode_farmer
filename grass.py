import asyncio
import base64
import json
import time
import uuid
import re
import ssl
from loguru import logger

import aiohttp
from websocket import create_connection, WebSocketTimeoutException
import ssl

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
WEBSOCKET_URLS = [
    "wss://proxy2.wynd.network:4650",
    "wss://proxy2.wynd.network:4444",
]

PING_INTERVAL = 120
RECONNECT_DELAY = 5
FETCH_TIMEOUT = 10

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

last_live_timestamp = {}

def parse_proxy_url(proxy_url):
    pattern = re.compile(r'^http://([^:]+):([^@]+)@([^:]+):(\d+)$')
    match = pattern.match(proxy_url)
    if not match:
        raise ValueError(f"Invalid proxy URL: {proxy_url}")
    user, password, host, port = match.groups()
    return host, int(port), (user, password)

async def perform_http_request(data, proxy):
    request_id = str(uuid.uuid4())
    method = data.get("method", "GET").upper()
    url = data.get("url")
    headers = data.get("headers", {}).copy()
    final_headers = BROWSER_HEADERS.copy()
    final_headers.update(headers)

    body_b64 = data.get("body")
    request_body = base64.b64decode(body_b64) if body_b64 else None

    logger.info(f"[HTTP_REQUEST-{request_id}] {method} {url} via {proxy}")

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
                proxy=proxy
            ) as response:
                status = response.status
                reason = response.reason
                raw_headers = response.headers
                content = await response.read()
                b64_body = base64.b64encode(content).decode("utf-8")

                logger.info(f"[HTTP_REQUEST-{request_id}] Response: {status} {reason}")
                return {
                    "url": str(response.url),
                    "status": status,
                    "status_text": reason,
                    "headers": dict(raw_headers),
                    "body": b64_body
                }
        except Exception as e:
            logger.error(f"[HTTP_REQUEST-{request_id}] Request failed: {e}")
            return {
                "url": url,
                "status": 400,
                "status_text": str(e),
                "headers": {},
                "body": ""
            }

async def authenticate(_data, user_id):
    logger.info("[AUTH] Authentication requested.")
    browser_id = str(uuid.uuid4())
    return {
        "browser_id": browser_id,
        "user_id": user_id,
        "user_agent": BROWSER_HEADERS["User-Agent"],
        "timestamp": int(time.time()),
        "device_type": "extension",
        "version": "4.26.2",
        "extension_id": "ilehaonighjijnmpnagapkhpcdbhclfg"
    }

async def handle_pong(_data):
    logger.debug("[PONG] Received PONG. No action needed.")

async def send_ping(ws):
    msg = {
        "id": str(uuid.uuid4()),
        "version": "1.0.0",
        "action": "PING",
        "data": {}
    }
    await asyncio.to_thread(ws.send, json.dumps(msg))
    logger.debug("[WebSocket] Sent PING")

async def handle_incoming_message(ws, raw_message, connection_id, proxy, user_id):
    last_live_timestamp[connection_id] = int(time.time())
    logger.debug(f"[WebSocket-{connection_id}] Received: {raw_message}")

    try:
        message = json.loads(raw_message)
    except Exception as e:
        logger.error(f"[WebSocket-{connection_id}] Invalid JSON: {e}")
        return

    action = message.get("action")
    msg_id = message.get("id")
    logger.info(f"[WebSocket-{connection_id}] Handling action: {action}, msg_id={msg_id}")

    # Define RPC_CALL_TABLE here so we can pass user_id to AUTH
    RPC_CALL_TABLE = {
        "HTTP_REQUEST": perform_http_request,
        "AUTH": lambda data: authenticate(data, user_id),
        "PONG": handle_pong
    }

    if action == "PING":
        response = {
            "id": msg_id,
            "origin_action": "PONG",
            "result": {}
        }
        await asyncio.to_thread(ws.send, json.dumps(response))
        logger.debug("[WebSocket] Responded with PONG")
        return

    if action in RPC_CALL_TABLE:
        rpc_func = RPC_CALL_TABLE[action]
        try:
            if action == "HTTP_REQUEST":
                result = await rpc_func(message.get("data", {}), proxy)
            else:
                result = await rpc_func(message.get("data", {}))

            response = {
                "id": msg_id,
                "origin_action": action,
                "result": result
            }
            await asyncio.to_thread(ws.send, json.dumps(response))
            logger.info(f"[WebSocket-{connection_id}] Sent response for {action}, msg_id={msg_id}")
        except Exception as e:
            logger.exception(f"[WebSocket-{connection_id}] Error handling {action}: {e}")
    else:
        logger.warning(f"[WebSocket-{connection_id}] Unknown action: {action}")

async def ping_loop(ws, connection_id):
    while True:
        if not ws.connected:
            break
        current_time = int(time.time())
        if (current_time - last_live_timestamp.get(connection_id, current_time)) > 129:
            logger.warning(f"[WebSocket-{connection_id}] No recent activity, closing...")
            await asyncio.to_thread(ws.close)
            break
        await send_ping(ws)
        await asyncio.sleep(PING_INTERVAL)

def connect_websocket(ws_url, proxy_host, proxy_port, proxy_auth, timeout=10):
    # Create a custom SSL context
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    logger.info(f"Connecting to {ws_url} via proxy {proxy_host}:{proxy_port}")
    ws = create_connection(
        ws_url,
        http_proxy_host=proxy_host,
        http_proxy_port=proxy_port,
        http_proxy_auth=proxy_auth,
        timeout=timeout,
        sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False, "ssl_context": ssl_context}
    )
    logger.success(f"Connected to {ws_url} via proxy!")
    return ws

async def run_connection(proxy, connection_id, user_id):
    proxy_host, proxy_port, proxy_auth = parse_proxy_url(proxy)
    proxy_auth = tuple(proxy_auth) if proxy_auth else None
    http_proxy = proxy

    while True:
        ws_url = WEBSOCKET_URLS[connection_id % len(WEBSOCKET_URLS)]
        try:
            ws = await asyncio.to_thread(connect_websocket, ws_url, proxy_host, proxy_port, proxy_auth)
            last_live_timestamp[connection_id] = int(time.time())

            ping_task = asyncio.create_task(ping_loop(ws, connection_id))

            while True:
                if not ws.connected:
                    logger.warning(f"[WebSocket-{connection_id}] Disconnected.")
                    break
                try:
                    raw_msg = await asyncio.to_thread(ws.recv)
                    if raw_msg is None:
                        logger.warning(f"[WebSocket-{connection_id}] No message (disconnected).")
                        break
                    await handle_incoming_message(ws, raw_msg, connection_id, http_proxy, user_id)
                except WebSocketTimeoutException:
                    pass
                except Exception as e:
                    logger.error(f"[WebSocket-{connection_id}] Error receiving message: {e}")
                    break

            await asyncio.to_thread(ws.close)
            ping_task.cancel()

        except Exception as e:
            logger.error(f"[WebSocket-{connection_id}] Connection error: {e}")

        logger.info(f"[WebSocket-{connection_id}] Reconnecting in {RECONNECT_DELAY} seconds...")
        await asyncio.sleep(RECONNECT_DELAY)

async def main():
    # Ask the user for user_id at the start
    user_id = input("Please enter user_id: ").strip()

    logger.info("[Main] Loading proxies from local_proxies.txt...")
    with open("local_proxies.txt", "r") as f:
        proxies = [line.strip() for line in f if line.strip()]

    logger.info(f"[Main] Starting tasks for {len(proxies)} proxies...")
    tasks = []
    for i, proxy in enumerate(proxies):
        tasks.append(asyncio.create_task(run_connection(proxy, i, user_id)))
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
