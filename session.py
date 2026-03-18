"""ACP 长驻进程池 — per-chatid kiro-cli ACP (JSON-RPC over stdio) 进程管理"""
import asyncio, json, logging, os, time
from collections import OrderedDict

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")


def _load_session_id(session_dir: str) -> str | None:
    p = os.path.join(session_dir, "session_id.txt")
    if os.path.isfile(p):
        try:
            with open(p, "r") as f:
                sid = f.read().strip()
            return sid if sid else None
        except Exception:
            return None
    return None


def _save_session_id(session_dir: str, session_id: str | None):
    if not session_id:
        return
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "session_id.txt"), "w") as f:
            f.write(session_id)
    except Exception as e:
        log.error("保存 session_id 失败: %s", e)


class KiroProcess:
    """单个 kiro-cli ACP 进程"""

    def __init__(self, chatid: str, session_dir: str, agent: str | None, cwd: str):
        self._chatid = chatid
        self._session_dir = session_dir
        self._agent = agent
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._last_active: float = 0
        self._session_id: str | None = None
        self._msg_id: int = 0
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._full_text: str = ""

    async def start(self):
        os.makedirs(self._session_dir, exist_ok=True)
        cmd = ["kiro-cli", "acp", "--trust-all-tools"]
        if self._agent:
            cmd += ["--agent", self._agent]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._session_dir,
        )
        self._last_active = time.monotonic()
        self._reader_task = asyncio.create_task(self._reader_loop())
        log.info("KiroProcess 启动 chatid=%s pid=%d agent=%s", self._chatid, self._proc.pid, self._agent)

        # initialize
        await self._send_rpc_and_wait("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "wecom-bridge", "version": "1.0"},
        })

        # session load or new
        await self._create_or_load_session()

    async def _create_or_load_session(self):
        saved_id = _load_session_id(self._session_dir)
        if saved_id:
            try:
                result = await self._send_rpc_and_wait("session/load", {"sessionId": saved_id})
                if isinstance(result, dict) and "error" not in result:
                    self._session_id = saved_id
                    log.info("session/load 成功 chatid=%s sid=%s", self._chatid, saved_id)
                    return
            except Exception as e:
                log.warning("session/load 失败 chatid=%s: %s, 创建新 session", self._chatid, e)

        result = await self._send_rpc_and_wait("session/new", {
            "cwd": self._cwd,
            "mcpServers": [],
        })
        if isinstance(result, dict):
            self._session_id = result.get("sessionId", "")
        _save_session_id(self._session_dir, self._session_id)
        log.info("session/new 成功 chatid=%s sid=%s", self._chatid, self._session_id)

    async def _send_rpc_and_wait(self, method: str, params: dict, timeout: float = 60):
        """发送 JSON-RPC 并等待 result"""
        mid = await self._send_rpc(method, params)
        fut = self._pending[mid]
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    async def send(self, text: str, on_chunk=None, timeout: float = 300) -> str:
        async with self._lock:
            self._full_text = ""
            self._chunk_queue = asyncio.Queue()
            mid = await self._send_rpc("session/prompt", {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": text}],
            })
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    chunk = await asyncio.wait_for(self._chunk_queue.get(), timeout=remaining)
                    if chunk is None:
                        break
                    if on_chunk:
                        await on_chunk(chunk)
                self._last_active = time.monotonic()
                return self._full_text
            except asyncio.TimeoutError:
                log.warning("prompt 超时 chatid=%s", self._chatid)
                return "⏰ 回复超时，请稍后重试"
            except RuntimeError as e:
                log.error("ACP 进程异常 chatid=%s: %s", self._chatid, e)
                return f"❌ ACP 进程异常: {e}"
            finally:
                self._pending.pop(mid, None)

    async def _send_rpc(self, method: str, params: dict) -> int:
        self._msg_id += 1
        mid = self._msg_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        line = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        self._proc.stdin.write((line + "\n").encode())
        await self._proc.stdin.drain()
        return mid

    async def _reader_loop(self):
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:  # EOF
                    break
                try:
                    msg = json.loads(raw.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                if "id" in msg and "result" in msg:
                    result = msg["result"]
                    if isinstance(result, dict) and result.get("stopReason") == "end_turn":
                        await self._chunk_queue.put(None)
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        fut.set_result(result)
                elif "id" in msg and "error" in msg:
                    await self._chunk_queue.put(None)  # OBS-1: error 也终止 chunk 消费
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        fut.set_exception(RuntimeError(str(msg["error"])))
                elif msg.get("method") == "session/update":
                    update = msg.get("params", {}).get("update", {})
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        text = update.get("content", {}).get("text", "")
                        self._full_text += text
                        await self._chunk_queue.put(text)
                # ignore _kiro.dev/* and other notifications
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("ACP process exited"))
            self._pending.clear()
            await self._chunk_queue.put(None)

    async def stop(self):
        _save_session_id(self._session_dir, self._session_id)
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.stdin.close()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_active


class ProcessPool:
    MAX_PROCS = 10
    IDLE_TIMEOUT = 1800

    def __init__(self):
        self._pool: OrderedDict[str, KiroProcess] = OrderedDict()

    async def get_or_create(self, chatid: str, agent: str | None = None, cwd: str | None = None) -> KiroProcess:
        if chatid in self._pool:
            proc = self._pool[chatid]
            if proc.alive:
                self._pool.move_to_end(chatid)
                return proc
            del self._pool[chatid]

        if len(self._pool) >= self.MAX_PROCS:
            await self._evict_lru()

        session_dir = os.path.join(SESSIONS_DIR, chatid)
        effective_cwd = cwd or WORK_DIR
        proc = KiroProcess(chatid, session_dir, agent, effective_cwd)
        await proc.start()
        self._pool[chatid] = proc
        return proc

    async def _evict_lru(self):
        if not self._pool:
            return
        victim_id, proc = next(iter(self._pool.items()))
        del self._pool[victim_id]
        log.info("淘汰 LRU 进程 chatid=%s", victim_id)
        await proc.stop()

    async def cleanup_idle(self):
        to_remove = [cid for cid, p in self._pool.items() if p.idle_seconds > self.IDLE_TIMEOUT]
        for cid in to_remove:
            proc = self._pool.pop(cid)
            log.info("空闲超时清理 chatid=%s idle=%.0fs", cid, proc.idle_seconds)
            await proc.stop()

    async def shutdown(self):
        tasks = [proc.stop() for proc in self._pool.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pool.clear()
