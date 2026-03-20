"""Channel: 每个企微机器人一个独立 Channel — chatid 路由 + 流式分段"""
import asyncio, base64, json, logging, os, uuid

import aiohttp

from ws_client import WsClient
from session import ProcessPool
from guard import check_injection

log = logging.getLogger(__name__)

DEFAULT_WELCOME = "👋 你好！我是 Kiro AI 助手，有什么可以帮你的？"
WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
STREAM_SEGMENT_LIMIT = 1500
FLUSH_INTERVAL = 0.3
MSG_AGGREGATE_WINDOW = 3.0  # 消息聚合窗口（秒）
FUNASR_URL = os.getenv("FUNASR_URL", "http://localhost:10095")


def _detect_media_type(data: bytes) -> str:
    if data[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if data[:4] == b'GIF8':
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return "image/png"


def _is_image(data: bytes) -> bool:
    return (data[:3] == b'\xff\xd8\xff' or
            data[:8] == b'\x89PNG\r\n\x1a\n' or
            data[:4] == b'GIF8' or
            (data[:4] == b'RIFF' and len(data) > 12 and data[8:12] == b'WEBP'))


def _aes_decrypt(enc_data: bytes, aeskey: str) -> bytes | None:
    """用企微 aeskey AES-256-CBC 解密数据"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = base64.b64decode(aeskey + '=' * (4 - len(aeskey) % 4) if len(aeskey) % 4 else aeskey)
        if len(key) != 32:
            log.warning("aeskey 解码后长度 %d != 32", len(key))
            return None
        iv = key[:16]
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        dec = cipher.decryptor()
        plain = dec.update(enc_data) + dec.finalize()
        pad = plain[-1]
        if 1 <= pad <= 32 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
        return plain
    except Exception as e:
        log.error("AES 解密失败: %s", e)
    return None


def _aes_decrypt_image(enc_data: bytes, aeskey: str) -> bytes | None:
    """解密图片数据，搜索图片 magic bytes"""
    plain = _aes_decrypt(enc_data, aeskey)
    if not plain:
        return None
    for offset in range(min(64, len(plain))):
        if _is_image(plain[offset:]):
            log.info("AES 解密图片成功 offset=%d size=%d", offset, len(plain) - offset)
            return plain[offset:]
    log.warning("AES 解密后未找到图片 magic, first16=%s", plain[:16].hex())
    return None


class MessageAggregator:
    """同一 chatid 的消息在短时间窗口内聚合，支持文本+图片混合"""

    def __init__(self):
        self._buffers: dict[str, dict] = {}  # chatid → {parts, req_id, userid, timer}

    def add(self, chatid: str, req_id: str, userid: str, part: dict, on_ready):
        """添加一个消息片段（text 或 image），窗口到期后回调 on_ready"""
        if chatid not in self._buffers:
            self._buffers[chatid] = {"parts": [], "req_id": req_id, "userid": userid, "timer": None}
        buf = self._buffers[chatid]
        buf["parts"].append(part)
        buf["req_id"] = req_id  # 用最新的 req_id 回复

        # 重置定时器
        if buf["timer"] is not None:
            buf["timer"].cancel()
        buf["timer"] = asyncio.get_running_loop().call_later(
            MSG_AGGREGATE_WINDOW,
            lambda: asyncio.create_task(self._flush(chatid, on_ready))
        )

    async def _flush(self, chatid: str, on_ready):
        buf = self._buffers.pop(chatid, None)
        if not buf or not buf["parts"]:
            return
        await on_ready(buf["req_id"], chatid, buf["userid"], buf["parts"])


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
        self._aggregator = MessageAggregator()

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

    async def _download_image(self, url: str) -> bytes | None:
        """通过 HTTP 下载图片"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    data = await resp.read()
                    log.info("下载图片 status=%d content_type=%s size=%d url=%s",
                             resp.status, resp.content_type, len(data), url[:80])
                    if resp.status == 200 and resp.content_type and resp.content_type.startswith("image/"):
                        return data
                    # 非图片类型，可能是加密数据或错误
                    if resp.status == 200:
                        log.warning("下载内容非图片 content_type=%s, 尝试 AES 解密", resp.content_type)
                        return data  # 返回原始数据，由调用方判断
                    log.error("下载图片失败 status=%d", resp.status)
        except Exception as e:
            log.error("下载图片异常: %s", e)
        return None

    async def _process_mixed(self, req_id: str, chatid: str, userid: str, mixed: dict):
        """处理 mixed 消息（文本+图片+语音混合）"""
        stream_id = uuid.uuid4().hex[:16]
        try:
            text_parts = []
            image_paths = []

            for item in mixed.get("msg_item", []):
                item_type = item.get("msgtype", "")
                if item_type == "text":
                    text_parts.append(item.get("text", {}).get("content", ""))
                elif item_type == "image":
                    img_path = await self._download_and_save_media(
                        chatid, item.get("image", {}), "images")
                    if img_path:
                        image_paths.append(img_path)
                elif item_type == "voice":
                    transcript = await self._process_voice(chatid, item.get("voice", {}))
                    if transcript:
                        text_parts.append(transcript)

            if not image_paths and not text_parts:
                return

            # 组装文本
            combined_text = " ".join(t.strip() for t in text_parts if t.strip())
            if combined_text:
                hit = check_injection(combined_text)
                if hit:
                    log.warning("拦截注入 chatid=%s pattern=%s", chatid, hit)
                    await self.ws.send_stream(req_id, stream_id, "⚠️ 检测到异常指令，已拦截。", finish=True)
                    return

            if image_paths and not combined_text:
                combined_text = "请描述这张图片的内容"

            # 构造 prompt
            if image_paths:
                img_hint = "\n".join(f"[图片文件: {p}]" for p in image_paths)
                prompt_text = f"[{userid}]: {combined_text}\n\n{img_hint}\n\n请先用 fs_read 工具的 Image 模式读取上述图片文件，然后回答用户的问题。"
            else:
                prompt_text = f"[{userid}]: {combined_text}"

            chat_cfg = self._get_chat_config(chatid)
            seg = StreamSegmenter(self.ws, req_id, stream_id)
            proc = await self.pool.get_or_create(
                chatid, agent=chat_cfg.get("agent"),
                cwd=chat_cfg.get("cwd", WORK_DIR), mode=chat_cfg.get("mode", "full"))
            await proc.send(prompt_text, on_chunk=seg.feed)
            await seg.finish()
        except Exception as e:
            log.error("mixed 处理异常 req=%s: %s", req_id, e)
            await self.ws.send_stream(req_id, stream_id, f"❌ 处理异常: {e}", finish=True)

    async def _download_media(self, media_info: dict) -> bytes | None:
        """下载企微媒体文件（URL优先，回退 media_id），自动 AES 解密"""
        data = None
        url = media_info.get("url", "")
        if url:
            data = await self._download_image(url)
            if data and not _is_image(data):
                aeskey = media_info.get("aeskey", "")
                if aeskey:
                    decrypted = _aes_decrypt(data, aeskey)
                    if decrypted:
                        data = decrypted
        if not data:
            media_id = media_info.get("media_id", "")
            if media_id:
                data = await self.ws.get_media(media_id)
        return data

    async def _download_and_save_media(self, chatid: str, media_info: dict, subdir: str) -> str | None:
        """下载媒体文件并保存到本地，返回文件路径"""
        data = await self._download_media(media_info)
        if not data:
            return None
        ext = {"image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(
            _detect_media_type(data), ".png") if subdir == "images" else ".audio"
        save_dir = os.path.join(WORK_DIR, "wecom-sessions", chatid, subdir)
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"{uuid.uuid4().hex[:8]}{ext}")
        with open(path, "wb") as f:
            f.write(data)
        log.info("媒体已保存 chatid=%s path=%s size=%dKB", chatid, path, len(data) // 1024)
        return path

    async def _process_voice(self, chatid: str, voice_info: dict) -> str | None:
        """下载语音 → 保存 → FunASR 转文字"""
        data = await self._download_media(voice_info)
        if not data:
            log.error("语音下载失败 chatid=%s", chatid)
            return None
        # 保存音频文件
        save_dir = os.path.join(WORK_DIR, "wecom-sessions", chatid, "voice")
        os.makedirs(save_dir, exist_ok=True)
        audio_path = os.path.join(save_dir, f"{uuid.uuid4().hex[:8]}.audio")
        with open(audio_path, "wb") as f:
            f.write(data)
        log.info("语音已保存 chatid=%s path=%s size=%dKB", chatid, audio_path, len(data) // 1024)
        # 调 FunASR 识别
        return await self._transcribe_audio(audio_path)

    async def _transcribe_audio(self, audio_path: str) -> str | None:
        """调用 FunASR HTTP API 进行语音识别"""
        try:
            with open(audio_path, "rb") as f:
                audio_data = f.read()
            audio_b64 = base64.b64encode(audio_data).decode()
            payload = {
                "audio": audio_b64,
                "language": "auto",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{FUNASR_URL}/api/v1/asr",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        text = result.get("text", "")
                        log.info("语音识别成功 path=%s text=%s", audio_path, text[:100])
                        return text if text else None
                    log.error("语音识别失败 status=%d", resp.status)
        except Exception as e:
            log.error("语音识别异常: %s", e)
        return None

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
