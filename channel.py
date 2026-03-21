"""Channel: 企微消息路由 — 根据 agent_mode 分发到不同模式"""
import asyncio, json, logging, os, uuid

from ws_client import WsClient
from stream import StreamSegmenter
from guard import check_injection
from media import download_media, save_media, is_image, aes_decrypt_image, process_voice, process_file
from agents.single.session import ProcessPool
from agents.delegate.session import DelegateSession

log = logging.getLogger(__name__)

DEFAULT_WELCOME = "👋 你好！我是 Kiro AI 助手，有什么可以帮你的？"
WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")


class Channel:
    def __init__(self, config: dict):
        self.bot_id = config["bot_id"]
        self.welcome_msg = config.get("welcome_msg", DEFAULT_WELCOME)
        self.ws = WsClient(self.bot_id, config["secret"], self._on_message, self._on_event)
        self.pool = ProcessPool()
        self._delegates: dict[str, DelegateSession] = {}
        self._chats = config.get("chats", {"default": {"agent": None, "cwd": WORK_DIR}})

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

        if msgtype == "mixed":
            asyncio.create_task(self._process_mixed(req_id, chatid, userid, body.get("mixed", {})))
            return

        if msgtype == "image":
            asyncio.create_task(self._process_mixed(req_id, chatid, userid,
                {"msg_item": [{"msgtype": "image", "image": body.get("image", {})}]}))
            return

        if msgtype == "voice":
            asyncio.create_task(self._process_mixed(req_id, chatid, userid,
                {"msg_item": [{"msgtype": "voice", "voice": body.get("voice", {})}]}))
            return

        if msgtype == "file":
            asyncio.create_task(self._process_mixed(req_id, chatid, userid,
                {"msg_item": [{"msgtype": "file", "file": body.get("file", {})}]}))
            return

        if msgtype != "text":
            await self.ws.send_stream(req_id, uuid.uuid4().hex[:16],
                "暂不支持该消息类型，请发送文本、图片、语音或文件。", finish=True)
            return

        text = body.get("text", {}).get("content", "").strip()
        if not text:
            return
        if text.startswith("@"):
            parts = text.split(None, 1)
            text = parts[1] if len(parts) > 1 else text

        hit = check_injection(text)
        if hit:
            log.warning("拦截注入 chatid=%s userid=%s pattern=%s", chatid, userid, hit)
            await self.ws.send_stream(req_id, uuid.uuid4().hex[:16], "⚠️ 检测到异常指令，已拦截。", finish=True)
            return

        text = f"[{userid}]: {text}"
        stream_id = uuid.uuid4().hex[:16]
        asyncio.create_task(self._process_and_reply(req_id, stream_id, chatid, text))

    async def _process_mixed(self, req_id: str, chatid: str, userid: str, mixed: dict):
        """处理 mixed 消息（文本+图片+语音+文件）"""
        stream_id = uuid.uuid4().hex[:16]
        try:
            text_parts = []
            attach_paths = []

            for item in mixed.get("msg_item", []):
                item_type = item.get("msgtype", "")
                if item_type == "text":
                    text_parts.append(item.get("text", {}).get("content", ""))
                elif item_type == "image":
                    data = await download_media(item.get("image", {}), self.ws)
                    if data:
                        if not is_image(data):
                            aeskey = item.get("image", {}).get("aeskey", "")
                            if aeskey:
                                data = aes_decrypt_image(data, aeskey)
                        if data:
                            attach_paths.append(save_media(chatid, data, "images"))
                elif item_type == "voice":
                    transcript = await process_voice(chatid, item.get("voice", {}), self.ws)
                    if transcript:
                        text_parts.append(transcript)
                elif item_type == "file":
                    path = await process_file(chatid, item.get("file", {}), self.ws)
                    if path:
                        attach_paths.append(path)

            if not attach_paths and not text_parts:
                return

            combined_text = " ".join(t.strip() for t in text_parts if t.strip())
            if combined_text:
                hit = check_injection(combined_text)
                if hit:
                    log.warning("拦截注入 chatid=%s pattern=%s", chatid, hit)
                    await self.ws.send_stream(req_id, stream_id, "⚠️ 检测到异常指令，已拦截。", finish=True)
                    return

            if attach_paths and not combined_text:
                combined_text = "请查看附件内容"

            if attach_paths:
                hints = []
                for p in attach_paths:
                    if any(p.endswith(e) for e in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                        hints.append(f"[图片文件: {p}]（请用 fs_read Image 模式读取）")
                    else:
                        hints.append(f"[文件: {p}]（请用 fs_read 读取内容）")
                prompt_text = f"[{userid}]: {combined_text}\n\n" + "\n".join(hints)
            else:
                prompt_text = f"[{userid}]: {combined_text}"

            await self._send_to_agent(req_id, stream_id, chatid, prompt_text)
        except Exception as e:
            log.error("mixed 处理异常 req=%s: %s", req_id, e)
            await self.ws.send_stream(req_id, stream_id, f"❌ 处理异常: {e}", finish=True)

    async def _process_and_reply(self, req_id: str, stream_id: str, chatid: str, text: str):
        await self._send_to_agent(req_id, stream_id, chatid, text)

    async def _send_to_agent(self, req_id: str, stream_id: str, chatid: str, text: str):
        """统一发送到 agent — 根据 agent_mode 路由"""
        chat_cfg = self._get_chat_config(chatid)
        agent_mode = chat_cfg.get("agent_mode", "single")
        seg = StreamSegmenter(self.ws, req_id, stream_id)

        try:
            await self.ws.send_stream(req_id, stream_id, "🤔", finish=False)

            if agent_mode == "single":
                proc = await self.pool.get_or_create(
                    chatid, agent=chat_cfg.get("agent"),
                    cwd=chat_cfg.get("cwd", WORK_DIR), mode=chat_cfg.get("mode", "full"))
                result = await proc.send(text, on_chunk=seg.feed)
                if result:
                    await seg.finish()
            elif agent_mode == "delegate":
                session = await self._get_delegate(chatid, chat_cfg)
                result = await session.send_to_main(text, on_chunk=seg.feed)
                if result:
                    await seg.finish()
            # elif agent_mode == "groupchat":
            #     TODO: Phase 2
            else:
                await self.ws.send_stream(req_id, stream_id, f"未知的 agent_mode: {agent_mode}", finish=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("agent 异常 req=%s: %s", req_id, e)
            await self.ws.send_stream(req_id, stream_id, f"❌ 处理异常: {e}", finish=True)

    # ---- 事件回调 ----

    async def _get_delegate(self, chatid: str, chat_cfg: dict) -> DelegateSession:
        if chatid not in self._delegates:
            session = DelegateSession(chatid, chat_cfg, self.ws)
            await session.start()
            self._delegates[chatid] = session
        return self._delegates[chatid]

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
