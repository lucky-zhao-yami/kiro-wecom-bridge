"""企微智能机器人 WebSocket 长连接客户端"""
import asyncio, base64, json, logging, time, uuid

import websockets

log = logging.getLogger(__name__)

WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 30
PONG_TIMEOUT = HEARTBEAT_INTERVAL * 3  # 90s
MAX_BACKOFF = 60


def _req_id():
    return uuid.uuid4().hex[:16]


class WsClient:
    def __init__(self, bot_id: str, secret: str, on_message, on_event):
        self._bot_id = bot_id
        self._secret = secret
        self._on_message = on_message  # async (req_id, body)
        self._on_event = on_event      # async (req_id, body)
        self._ws = None
        self._running = False
        self._last_pong: float = 0
        self._auth_failures = 0
        self._send_lock = asyncio.Lock()
        self._msg_rate_lock = asyncio.Lock()
        self._last_send_msg: float = 0
        self._media_waiters: dict[str, asyncio.Future] = {}  # req_id → Future[bytes|None]

    # ---- 连接生命周期 ----

    async def start(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None) as ws:
                    self._ws = ws
                    await self._subscribe()
                    self._auth_failures = 0
                    self._last_pong = time.monotonic()
                    backoff = 1
                    log.info("[%s] WS 已连接并认证", self._bot_id[:8])
                    await self._recv_loop()
            except RuntimeError as e:
                if "认证失败" in str(e):
                    self._auth_failures += 1
                    if self._auth_failures >= 5:
                        log.error("[%s] 认证连续失败 %d 次，停止重连", self._bot_id[:8], self._auth_failures)
                        return
                log.error("[%s] WS 断线: %s, %ds 后重连", self._bot_id[:8], e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception as e:
                log.error("[%s] WS 断线: %s, %ds 后重连", self._bot_id[:8], e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            finally:
                self._ws = None

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    # ---- 认证 ----

    async def _subscribe(self):
        await self._send_raw({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": _req_id()},
            "body": {"bot_id": self._bot_id, "secret": self._secret},
        })
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
        resp = json.loads(raw)
        if resp.get("errcode", -1) != 0:
            raise RuntimeError(f"认证失败: {resp}")

    # ---- 接收循环 ----

    async def _recv_loop(self):
        hb = asyncio.create_task(self._heartbeat())
        try:
            async for raw in self._ws:
                # aibot_get_media 可能返回二进制
                if isinstance(raw, bytes):
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # 纯二进制媒体数据 — 分发给第一个等待中的 media waiter
                        for rid, fut in list(self._media_waiters.items()):
                            if not fut.done():
                                fut.set_result(raw)
                                break
                        continue
                else:
                    msg = json.loads(raw)

                cmd = msg.get("cmd", "")
                req_id = msg.get("headers", {}).get("req_id", "")
                body = msg.get("body", {})

                # 检查是否是 media 响应（JSON 格式的成功/失败）
                if req_id in self._media_waiters:
                    fut = self._media_waiters[req_id]
                    if not fut.done():
                        if msg.get("errcode", 0) != 0:
                            fut.set_result(None)
                        else:
                            data = body.get("data", "")
                            fut.set_result(base64.b64decode(data) if data else None)
                    continue

                if cmd == "aibot_msg_callback":
                    asyncio.create_task(self._on_message(req_id, body))
                elif cmd == "aibot_event_callback":
                    asyncio.create_task(self._on_event(req_id, body))
                elif cmd == "pong" or (not cmd and msg.get("errcode") == 0):
                    self._last_pong = time.monotonic()
                else:
                    log.info("[%s] 收到未知 cmd: %s body=%s", self._bot_id[:8], cmd, str(msg)[:200])
        finally:
            hb.cancel()
            # 清理所有等待中的 media waiter
            for fut in self._media_waiters.values():
                if not fut.done():
                    fut.set_result(None)
            self._media_waiters.clear()

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                if time.monotonic() - self._last_pong > PONG_TIMEOUT:
                    log.error("[%s] 超过 %ds 未收到 pong，主动断开", self._bot_id[:8], PONG_TIMEOUT)
                    await self._ws.close()
                    return
                await self._send_raw({"cmd": "ping", "headers": {"req_id": _req_id()}})
            except Exception:
                return

    # ---- 发送方法 ----

    async def _send_raw(self, payload: dict):
        if self._ws:
            async with self._send_lock:
                await self._ws.send(json.dumps(payload))

    async def send_stream(self, req_id: str, stream_id: str, content: str, finish=False):
        """流式回复 aibot_respond_msg"""
        await self._send_raw({
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": finish, "content": content},
            },
        })

    async def send_welcome(self, req_id: str, text: str):
        """回复欢迎语 aibot_respond_welcome_msg"""
        await self._send_raw({
            "cmd": "aibot_respond_welcome_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "text", "text": {"content": text}},
        })

    async def get_media(self, media_id: str, timeout: float = 30) -> bytes | None:
        """通过 aibot_get_media 下载媒体文件，返回原始字节"""
        rid = _req_id()
        fut = asyncio.get_running_loop().create_future()
        self._media_waiters[rid] = fut
        try:
            await self._send_raw({
                "cmd": "aibot_get_media",
                "headers": {"req_id": rid},
                "body": {"media_id": media_id},
            })
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.error("get_media 超时 media_id=%s", media_id)
            return None
        finally:
            self._media_waiters.pop(rid, None)

    async def send_msg(self, chatid: str, chat_type: int, content: str):
        """主动推送 aibot_send_msg (markdown)
        统一频率控制：两次推送间隔至少 1s，防止企微 45009
        """
        async with self._msg_rate_lock:
            gap = time.monotonic() - self._last_send_msg
            if gap < 1:
                await asyncio.sleep(1 - gap)
            self._last_send_msg = time.monotonic()
            actual_id = chatid.removeprefix("dm_") if chat_type == 1 else chatid
            await self._send_raw({
                "cmd": "aibot_send_msg",
                "headers": {"req_id": _req_id()},
                "body": {
                    "chatid": actual_id,
                    "chat_type": chat_type,
                    "msgtype": "markdown",
                    "markdown": {"content": content},
                },
            })
