"""流式分段：企微 stream 是替换式，每次发当前 segment 的累计全文"""
import asyncio, uuid


STREAM_SEGMENT_LIMIT = 1500
FLUSH_INTERVAL = 2.0  # 企微限制 30条/分钟，2s 间隔 = 最多 30 次


class StreamSegmenter:
    def __init__(self, ws, req_id: str, stream_id: str,
                 limit: int = STREAM_SEGMENT_LIMIT, flush_interval: float = FLUSH_INTERVAL):
        self._ws = ws
        self._req_id = req_id
        self._stream_id = stream_id
        self._limit = limit
        self._flush_interval = flush_interval
        self._seg_text = ""
        self._buf = ""
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
                self._seg_text += self._buf
                self._buf = ""
                await self._ws.send_stream(self._req_id, self._stream_id, self._seg_text, finish=False)
            else:
                cut = space
                nl = self._buf.rfind("\n", 0, space)
                if nl > 0:
                    cut = nl + 1
                part = self._buf[:cut]
                self._seg_text += part
                self._buf = self._buf[cut:]

                # 检查是否在表格中间切断，如果是则在下一段补表头
                table_header = _extract_table_header(self._seg_text)

                await self._ws.send_stream(self._req_id, self._stream_id, self._seg_text, finish=True)
                self._stream_id = uuid.uuid4().hex[:16]
                self._seg_text = ""

                if table_header and self._buf and self._buf.lstrip().startswith("|"):
                    self._seg_text = table_header

    async def finish(self):
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self._buf:
            await self._flush()
        if not self._finished and self._seg_text:
            await self._ws.send_stream(self._req_id, self._stream_id, self._seg_text, finish=True)
            self._finished = True


def _extract_table_header(text: str) -> str:
    """从文本中提取最后一个 Markdown 表格的表头（标题行 + 分隔行）。
    仅当文本末尾仍在表格中间时返回表头；表格已结束则返回空。"""
    lines = text.rstrip().split("\n")
    
    # 末尾行必须是表格行，否则表格已结束
    if not lines or not lines[-1].strip().startswith("|"):
        return ""

    # 从后往前找表格起始行
    table_start = len(lines) - 1
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("|"):
            table_start = i
        elif not stripped:
            continue
        else:
            break

    # 表头 = 前两行（标题 + 分隔符 ---）
    if table_start + 1 < len(lines):
        h1 = lines[table_start].strip()
        h2 = lines[table_start + 1].strip()
        if h1.startswith("|") and h2.startswith("|") and "---" in h2:
            return h1 + "\n" + h2 + "\n"
    return ""
