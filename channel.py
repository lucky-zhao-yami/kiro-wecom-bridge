"""Channel: 每个企微机器人一个独立 Channel — chatid 路由 + 流式分段"""
import asyncio, base64, json, logging, os, uuid

from ws_client import WsClient
from session import ProcessPool
from guard import check_injection

log = logging.getLogger(__name__)

DEFAULT_WELCOME = "👋 你好！我是 Kiro AI 助手，有什么可以帮你的？"
WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
STREAM_SEGMENT_LIMIT = 1500
FLUSH_INTERVAL = 0.3


class StreamSegmenter:
    """流式输出分段：企微 stream 是替换式，每次发当前 segment 的累计全文"""

    def __init__(self, ws: WsClient, req_id: str, stream_id: str,
                 limit: int = STREAM_SEGMENT_LIMIT, flush_interval: float = FLUSH_INTERVAL):
        self._ws = ws
        self._req_id = req_id
        self._stream_id = stream_id
        self._limit = limit
        self._flush_interval = flush_interval
        self._seg_text = ""  # 当前 segment 已发送的累计文本
        self._buf = ""       # 待发送的增量缓冲
        self._finished = False
        self._flush_task: asyncio.Task | None = None

    async def feed(self, delta: str):
        self._buf += delta
        if len(self._seg_text) + len(self._buf) >= self._limit:
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
            space = self._limit - len(self._seg_text)
            if len(self._buf) <= space:
                # 还没超限，追加到 seg_text 并发送累计全文
                self._seg_text += self._buf
                self._buf = ""
                await self._ws.send_stream(self._req_id, self._stream_id, self._seg_text, finish=False)
            else:
                # 超限，在换行处切割，finish 当前 segment
                cut = space
                nl = self._buf.rfind("\n", 0, space)
                if nl > 0:
                    cut = nl + 1
                part = self._buf[:cut]
                self._seg_text += part
                self._buf = self._buf[cut:]
                # finish 当前 segment（发累计全文 + finish）
                await self._ws.send_stream(self._req_id, self._stream_id, self._seg_text, finish=True)
                # 开新 segment
                self._stream_id = uuid.uuid4().hex[:16]
                self._seg_text = ""

    async def finish(self):
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self._buf:
            await self._flush()
        if not self._finished and self._seg_text:
            await self._ws.send_stream(self._req_id, self._stream_id, self._seg_text, finish=True)
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
            chatid = f"dm_{userid}"
        log.info("收到消息 req=%s chatid=%s userid=%s type=%s", req_id, chatid, userid, msgtype)

        if msgtype == "image":
            media_id = body.get("image", {}).get("media_id", "")
            if not media_id:
                return
            asyncio.create_task(self._process_image(req_id, chatid, userid, media_id))
            return

        if msgtype != "text":
            await self.ws.send_stream(req_id, uuid.uuid4().hex[:16], "暂不支持该消息类型，请发送文本或图片。", finish=True)
            return

        text = body.get("text", {}).get("content", "").strip()
        if not text:
            return

        if text.startswith("@"):
            parts = text.split(None, 1)
            text = parts[1] if len(parts) > 1 else text

        # 注入检测
        hit = check_injection(text)
        if hit:
            log.warning("拦截注入 chatid=%s userid=%s pattern=%s", chatid, userid, hit)
            await self.ws.send_stream(req_id, uuid.uuid4().hex[:16], "⚠️ 检测到异常指令，已拦截。", finish=True)
            return

        text = f"[{userid}]: {text}"

        stream_id = uuid.uuid4().hex[:16]
        asyncio.create_task(self._process_and_reply(req_id, stream_id, chatid, text))

    async def _process_image(self, req_id: str, chatid: str, userid: str, media_id: str):
        """下载企微图片 → base64 → 发给 kiro ACP"""
        stream_id = uuid.uuid4().hex[:16]
        try:
            img_data = await self.ws.get_media(media_id)
            if not img_data:
                await self.ws.send_stream(req_id, stream_id, "❌ 图片下载失败，请重试。", finish=True)
                return
            img_b64 = base64.b64encode(img_data).decode()
            # 根据文件头判断 media_type
            media_type = "image/png"
            if img_data[:3] == b'\xff\xd8\xff':
                media_type = "image/jpeg"
            elif img_data[:4] == b'GIF8':
                media_type = "image/gif"
            elif img_data[:4] == b'RIFF' and img_data[8:12] == b'WEBP':
                media_type = "image/webp"

            prompt_content = [
                {"type": "text", "text": f"[{userid}]: [发送了一张图片，请描述图片内容并回答相关问题]"},
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            ]
            log.info("图片消息 chatid=%s size=%dKB type=%s", chatid, len(img_data) // 1024, media_type)

            chat_cfg = self._get_chat_config(chatid)
            agent = chat_cfg.get("agent")
            cwd = chat_cfg.get("cwd", WORK_DIR)
            mode = chat_cfg.get("mode", "full")
            seg = StreamSegmenter(self.ws, req_id, stream_id)
            proc = await self.pool.get_or_create(chatid, agent=agent, cwd=cwd, mode=mode)
            await proc.send_multimodal(prompt_content, on_chunk=seg.feed)
            await seg.finish()
        except Exception as e:
            log.error("图片处理异常 req=%s: %s", req_id, e)
            await self.ws.send_stream(req_id, stream_id, f"❌ 图片处理异常: {e}", finish=True)

    async def _process_and_reply(self, req_id: str, stream_id: str, chatid: str, text: str):
        chat_cfg = self._get_chat_config(chatid)
        agent = chat_cfg.get("agent")
        cwd = chat_cfg.get("cwd", WORK_DIR)
        mode = chat_cfg.get("mode", "full")
        seg = StreamSegmenter(self.ws, req_id, stream_id)
        try:
            proc = await self.pool.get_or_create(chatid, agent=agent, cwd=cwd, mode=mode)
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
