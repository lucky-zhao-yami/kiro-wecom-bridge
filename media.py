"""媒体处理：图片/语音/文件的下载、AES 解密、保存、语音识别"""
import base64, logging, os, uuid

import aiohttp

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
FUNASR_URL = os.getenv("FUNASR_URL", "http://localhost:10095")


def detect_media_type(data: bytes) -> str:
    if data[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if data[:4] == b'GIF8':
        return "image/gif"
    if data[:4] == b'RIFF' and len(data) > 12 and data[8:12] == b'WEBP':
        return "image/webp"
    return "image/png"


def is_image(data: bytes) -> bool:
    return (data[:3] == b'\xff\xd8\xff' or
            data[:8] == b'\x89PNG\r\n\x1a\n' or
            data[:4] == b'GIF8' or
            (data[:4] == b'RIFF' and len(data) > 12 and data[8:12] == b'WEBP'))


def aes_decrypt(enc_data: bytes, aeskey: str) -> bytes | None:
    """用企微 aeskey AES-256-CBC 解密数据"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = base64.b64decode(aeskey + '=' * (4 - len(aeskey) % 4) if len(aeskey) % 4 else aeskey)
        if len(key) != 32:
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


def aes_decrypt_image(enc_data: bytes, aeskey: str) -> bytes | None:
    """解密图片数据，搜索图片 magic bytes"""
    plain = aes_decrypt(enc_data, aeskey)
    if not plain:
        return None
    for offset in range(min(64, len(plain))):
        if is_image(plain[offset:]):
            log.info("AES 解密图片成功 offset=%d size=%d", offset, len(plain) - offset)
            return plain[offset:]
    log.warning("AES 解密后未找到图片 magic")
    return None


async def download_url(url: str) -> bytes | None:
    """HTTP 下载"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.read()
                log.info("下载 status=%d type=%s size=%d", resp.status, resp.content_type, len(data))
                if resp.status == 200:
                    return data
    except Exception as e:
        log.error("下载异常: %s", e)
    return None


async def download_media(media_info: dict, ws=None) -> bytes | None:
    """下载企微媒体文件（URL 优先，回退 media_id），自动 AES 解密"""
    data = None
    url = media_info.get("url", "")
    if url:
        data = await download_url(url)
        if data and not is_image(data):
            aeskey = media_info.get("aeskey", "")
            if aeskey:
                decrypted = aes_decrypt(data, aeskey)
                if decrypted:
                    data = decrypted
    if not data and ws:
        media_id = media_info.get("media_id", "")
        if media_id:
            data = await ws.get_media(media_id)
    return data


def save_media(chatid: str, data: bytes, subdir: str, filename: str | None = None) -> str:
    """保存媒体文件到本地，返回路径"""
    save_dir = os.path.join(WORK_DIR, "wecom-sessions", chatid, subdir)
    os.makedirs(save_dir, exist_ok=True)
    if filename:
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    elif subdir == "images":
        ext = {"image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(
            detect_media_type(data), ".png")
        safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
    else:
        safe_name = f"{uuid.uuid4().hex[:8]}.bin"
    path = os.path.join(save_dir, safe_name)
    with open(path, "wb") as f:
        f.write(data)
    log.info("媒体已保存 chatid=%s path=%s size=%dKB", chatid, path, len(data) // 1024)
    return path


async def process_voice(chatid: str, voice_info: dict, ws=None) -> str | None:
    """处理语音 — 优先企微 STT，回退 FunASR"""
    content = voice_info.get("content", "").strip()
    if content:
        log.info("语音转文字(企微) chatid=%s text=%s", chatid, content[:100])
        return content
    data = await download_media(voice_info, ws)
    if not data:
        log.error("语音下载失败 chatid=%s", chatid)
        return None
    audio_path = save_media(chatid, data, "voice")
    return await transcribe_audio(audio_path)


async def process_file(chatid: str, file_info: dict, ws=None) -> str | None:
    """下载文件并保存，返回路径"""
    filename = file_info.get("filename", "unknown")
    data = await download_media(file_info, ws)
    if not data:
        log.error("文件下载失败 chatid=%s filename=%s", chatid, filename)
        return None
    return save_media(chatid, data, "files", filename)


async def transcribe_audio(audio_path: str) -> str | None:
    """调用 FunASR HTTP API"""
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()
        payload = {"audio": base64.b64encode(audio_data).decode(), "language": "auto"}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{FUNASR_URL}/api/v1/asr", json=payload,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    text = result.get("text", "")
                    log.info("语音识别成功 text=%s", text[:100])
                    return text if text else None
                log.error("语音识别失败 status=%d", resp.status)
    except Exception as e:
        log.error("语音识别异常: %s", e)
    return None
