"""Channel: 每个企微机器人一个独立 Channel — chatid 路由 + 流式分段"""
import asyncio, json, logging, os, uuid

from ws_client import WsClient
from session import ProcessPool

log = logging.getLogger(__name__)

DEFAULT_WELCOME = "👋 你好！我是 Kiro AI 助手，有什么可以帮你的？"
WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
STREAM_SEGMENT_LIMIT = 1500
FLUSH_INTERVAL = 0.3  # 攒 0.3s 再发一次，避免企微版本冲突


class StreamSegmenter:
    """流式输出分段：带缓冲节流，累计超阈值时 finish 当前 stream 开新 stream"""

    def __init__(self, ws: WsClient, req_id: str, stream_id: str,
                 limit: int = STREAM_SEGMENT_LIMIT, flush_interval: float = FLUSH_INTERVAL):
        self._ws = ws
        self._req_id = req_id
        self._stream_id = stream_id
        self._limit = limit
        self._flush_interval = flush_interval
        self._seg_len = 0
        self._buf = ""
        self._finished = False
        self._flush_task: asyncio.Task | None = None

    async def feed(self, delta: str):
        self._buf += delta
        # 缓冲区超过阈值剩余空间时立刻 flush，否则等定时器
        if self._seg_len + len(self._buf) >= self._limit:
            await self._flush()
        elif not self._flush_task or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self):
        await asyncio.sleep(self._flush_interval)
        if self._buf:
            await self._flush()

    async def _flush(self):
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        while self._buf:
            space = self._limit - self._seg_len
            if len(self._buf) <= space:
                await self._ws.send_stream(self._req_id, self._stream_id, self._buf, finish=False)
                self._seg_len += len(self._buf)
                self._buf = ""
            else:
                cut = space
                nl = self._buf.rfind("\n", 0, space)
                if nl > 0:
                    cut = nl + 1
                part = self._buf[:cut]
                if part:
                    await self._ws.send_stream(self._req_id, self._stream_id, part, finish=False)
                await self._ws.send_stream(self._req_id, self._stream_id, "", finish=True)
                self._stream_id = uuid.uuid4().hex[:16]
                self._seg_len = 0
                self._buf = self._buf[cut:]

    async def finish(self):
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self._buf:
            await self._flush()
        if not self._finished and self._seg_len > 0:
            await self._ws.send_stream(self._req_id, self._stream_id, "", finish=True)
            self._finished = True


class Channel:
    def __init__(self, config: dict):
        self.bot_id = config["bot_id"]
        self.welcome_msg = config.get("welcome_msg", DEFAULT_WELCOME)
        self.ws = WsClient(self.bot_id, config["secret"], self._on_message, self._on_event)
        self.pool = ProcessPool()
        self._chats = self._parse_chats(config)

    def _parse_chats(self, config: dict) -> dict:
        if "chats" in config:
            return config["chats"]
        return {"default": {
            "agent": config.get("agent"),
            "cwd": WORK_DIR,
        }}

    def _get_chat_config(self, chatid: str) -> dict:
        return self._chats.get(chatid, self._chats.get("default", {}))

    async def start(self):
        log.info("Channel [%s] 启动", self.bot_id[:8])
        await self.ws.start()

    # ---- 消息回调 ----

    async def _on_message(self, req_id: str, body: dict):
        chatid = body.get("chatid", "")
        userid = body.get("from", {}).get("userid", "unknown")
        msgtype = body.get("msgtype", "")
        if not chatid:
            chatid = f"dm_{userid}"  # 单聊用 userid 做隔离 key
        log.info("收到消息 req=%s chatid=%s userid=%s type=%s", req_id, chatid, userid, msgtype)

        if msgtype != "text":
            await self.ws.send_stream(req_id, uuid.uuid4().hex[:16], "暂不支持该消息类型，请发送文本消息。", finish=True)
            return

        text = body.get("text", {}).get("content", "").strip()
        if not text:
            return

        if text.startswith("@"):
            parts = text.split(None, 1)
            text = parts[1] if len(parts) > 1 else text

        text = f"[{userid}]: {text}"  # FR-5: 携带用户标识

        stream_id = uuid.uuid4().hex[:16]
        asyncio.create_task(self._process_and_reply(req_id, stream_id, chatid, text))

    async def _process_and_reply(self, req_id: str, stream_id: str, chatid: str, text: str):
        chat_cfg = self._get_chat_config(chatid)
        agent = chat_cfg.get("agent")
        cwd = chat_cfg.get("cwd", WORK_DIR)
        seg = StreamSegmenter(self.ws, req_id, stream_id)
        try:
            proc = await self.pool.get_or_create(chatid, agent=agent, cwd=cwd)
            await proc.send(text, on_chunk=seg.feed)
            await seg.finish()
        except Exception as e:
            log.error("chat 异常 req=%s: %s", req_id, e)
            await self.ws.send_stream(req_id, stream_id, f"❌ 处理异常: {e}", finish=True)

    # ---- 事件回调 ----

    async def _on_event(self, req_id: str, body: dict):
        event_type = body.get("event", {}).get("eventtype", "")
        if event_type == "enter_chat":
            await self.ws.send_welcome(req_id, self.welcome_msg)
        elif event_type == "disconnected_event":
            log.warning("Channel [%s] 收到断开事件", self.bot_id[:8])


class ChannelManager:
    def __init__(self):
        self.channels: list[Channel] = []

    def load(self, config_path: str = "channels.json"):
        with open(config_path) as f:
            items = json.load(f)
        for item in items:
            self.channels.append(Channel(item))
        log.info("加载 %d 个 Channel", len(self.channels))

    async def start_all(self):
        return [asyncio.create_task(ch.start()) for ch in self.channels]
