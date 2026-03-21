"""流式分段：企微 stream 是替换式，每次发当前 segment 的累计全文"""
import asyncio, uuid


STREAM_SEGMENT_LIMIT = 1500
FLUSH_INTERVAL = 0.3


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
                await self._ws.send_stream(self._req_id, self._stream_id, self._seg_text, finish=True)
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
