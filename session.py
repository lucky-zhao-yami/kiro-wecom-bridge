"""ACP 长驻进程池 — per-chatid kiro-cli ACP (JSON-RPC over stdio) 进程管理"""
import asyncio, json, logging, os, time
from collections import OrderedDict

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_FILE = "summary.md"
HISTORY_FILE = "history.jsonl"

RECYCLE_PROMPT = """以下是一段对话记录。请完成两件事：

1. **总结**：用简洁的要点总结对话的关键信息（讨论了什么、做了什么决定、有什么待办），不超过500字。

2. **知识提取**：从对话中提取值得长期记住的实体和关系，调用 wecom-memory skill 的 save_entity 和 save_relation 保存。
   只提取重要的事实（人员职责、服务信息、技术决策、用户偏好等），忽略闲聊。
   如果没有值得保存的信息，跳过这步。
   chatid: {chatid}

先输出总结（以"## 总结"开头），然后执行知识提取。

---
{history}"""


def _load_summary(session_dir: str) -> str:
    p = os.path.join(session_dir, SUMMARY_FILE)
    if os.path.isfile(p):
        try:
            with open(p, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def _save_summary(session_dir: str, text: str):
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, SUMMARY_FILE), "w") as f:
            f.write(text)
    except Exception as e:
        log.error("保存摘要失败: %s", e)


def _extract_summary(reply: str) -> str:
    """从回收回复中提取总结部分，兼容多种标题格式"""
    import re
    # 匹配 # 总结 / ## 总结 / ### 总结
    m = re.search(r"^#{1,3}\s*总结\s*$", reply, re.MULTILINE)
    if m:
        after = reply[m.end():].strip()
        # 截断到下一个 # 标题（跳过首行避免 start=0）
        first_nl = after.find("\n")
        if first_nl > 0:
            m2 = re.search(r"^#{1,3}\s+", after[first_nl:], re.MULTILINE)
            return after[:first_nl + m2.start()].strip() if m2 else after[:1000]
        return after[:1000]
    return reply[:500]


def _append_history(session_dir: str, user: str, assistant: str):
    """追加对话记录到 JSONL 文件"""
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, HISTORY_FILE), "a") as f:
            f.write(json.dumps({"user": user, "assistant": assistant, "ts": int(time.time())}, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("追加历史失败: %s", e)


def _load_history(session_dir: str, max_turns: int = 20) -> list[dict]:
    """从 JSONL 加载最近的对话历史"""
    p = os.path.join(session_dir, HISTORY_FILE)
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r") as f:
            lines = f.readlines()
        result = []
        for line in lines[-max_turns:]:
            try:
                result.append(json.loads(line.strip()))
            except Exception:
                pass
        return result
    except Exception:
        return []


def _clear_history(session_dir: str):
    p = os.path.join(session_dir, HISTORY_FILE)
    try:
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def _format_history(history: list[dict]) -> str:
    lines = []
    for h in history:
        lines.append(f"用户: {h['user']}")
        if h.get("assistant"):
            # 截断过长的回复
            reply = h["assistant"]
            if len(reply) > 500:
                reply = reply[:500] + "...(截断)"
            lines.append(f"助手: {reply}")
    return "\n".join(lines)


class KiroProcess:
    """单个 kiro-cli ACP 进程"""

    def __init__(self, chatid: str, session_dir: str, agent: str | None, cwd: str,
                 mode: str = "full", interruptible: bool = True):
        self._chatid = chatid
        self._session_dir = session_dir
        self._agent = agent
        self._cwd = cwd
        self._mode = mode
        self._interruptible = interruptible
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None  # 当前正在处理的 prompt task
        self._last_active: float = 0
        self._session_id: str | None = None
        self._msg_id: int = 0
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._full_text: str = ""
        self._history: list[dict] = []  # 对话历史 [{user, assistant}]
        self._first_msg = True  # 是否是第一条消息（用于注入摘要）

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

        await self._send_rpc_and_wait("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "wecom-bridge", "version": "1.0"},
        })
        await self._create_session()

    async def _create_session(self):
        result = await self._send_rpc_and_wait("session/new", {
            "cwd": self._cwd,
            "mcpServers": [],
        })
        if isinstance(result, dict):
            self._session_id = result.get("sessionId", "")
        log.info("session/new 成功 chatid=%s sid=%s", self._chatid, self._session_id)

    async def _send_rpc_and_wait(self, method: str, params: dict, timeout: float = 60):
        mid = await self._send_rpc(method, params)
        fut = self._pending[mid]
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    async def send(self, text: str, on_chunk=None, timeout: float = 300) -> str:
        # 打断或排队
        if self._interruptible:
            if self._current_task and not self._current_task.done():
                log.info("打断旧 prompt chatid=%s", self._chatid)
                self._current_task.cancel()
                try:
                    await self._current_task
                except (asyncio.CancelledError, Exception):
                    pass
        else:
            await self._lock.acquire()

        # 第一条消息时注入上次摘要 + 安全规则
        actual_text = text
        if self._first_msg:
            self._first_msg = False
            from guard import get_preamble
            preamble = get_preamble(self._mode)
            summary = _load_summary(self._session_dir)
            if summary:
                actual_text = f"{preamble}[上次会话摘要]\n{summary}\n\n---\n[chatid={self._chatid}]\n{text}"
                log.info("注入会话摘要 chatid=%s len=%d mode=%s", self._chatid, len(summary), self._mode)
            else:
                actual_text = f"{preamble}[chatid={self._chatid}]\n{text}"

        prompt = [{"type": "text", "text": actual_text}]
        self._current_task = asyncio.current_task()
        try:
            return await self._send_prompt(prompt, text, on_chunk, timeout)
        except asyncio.CancelledError:
            log.info("prompt 被打断 chatid=%s", self._chatid)
            return ""  # 被打断，不回复
        finally:
            self._current_task = None
            if not self._interruptible:
                self._lock.release()

    async def send_multimodal(self, content: list[dict], on_chunk=None, timeout: float = 300) -> str:
        """发送多模态内容（文本+图片），content 是 ACP prompt content 数组"""
        if self._interruptible:
            if self._current_task and not self._current_task.done():
                log.info("打断旧 prompt chatid=%s", self._chatid)
                self._current_task.cancel()
                try:
                    await self._current_task
                except (asyncio.CancelledError, Exception):
                    pass
        else:
            await self._lock.acquire()

        if self._first_msg:
            self._first_msg = False
            from guard import get_preamble
            preamble = get_preamble(self._mode)
            summary = _load_summary(self._session_dir)
            prefix = preamble
            if summary:
                prefix += f"[上次会话摘要]\n{summary}\n\n---\n"
                log.info("注入会话摘要 chatid=%s len=%d mode=%s", self._chatid, len(summary), self._mode)
            for item in content:
                if item.get("type") == "text":
                    item["text"] = prefix + f"[chatid={self._chatid}]\n" + item["text"]
                    break

        user_desc = "[图片消息]"
        for item in content:
            if item.get("type") == "text":
                user_desc = item["text"][:200]
                break
        self._current_task = asyncio.current_task()
        try:
            return await self._send_prompt(content, user_desc, on_chunk, timeout)
        except asyncio.CancelledError:
            log.info("prompt 被打断 chatid=%s", self._chatid)
            return ""
        finally:
            self._current_task = None
            if not self._interruptible:
                self._lock.release()

    async def _send_prompt(self, prompt: list[dict], user_text: str, on_chunk, timeout: float) -> str:
        """公共 prompt 发送 + chunk 收集 + 历史记录"""
        self._full_text = ""
        self._chunk_queue = asyncio.Queue()
        mid = await self._send_rpc("session/prompt", {
            "sessionId": self._session_id,
            "prompt": prompt,
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
            self._history.append({"user": user_text, "assistant": self._full_text})
            _append_history(self._session_dir, user_text, self._full_text)
            return self._full_text
        except asyncio.TimeoutError:
            # 读 stderr 看 kiro 在干什么
            stderr_hint = ""
            if self._proc and self._proc.stderr:
                try:
                    stderr_data = await asyncio.wait_for(self._proc.stderr.read(4096), timeout=1)
                    stderr_hint = stderr_data.decode(errors="replace").strip()[-500:]
                except Exception:
                    pass
            log.warning("prompt 超时 chatid=%s partial_len=%d stderr=%s",
                        self._chatid, len(self._full_text), stderr_hint or "(empty)")
            self._history.append({"user": user_text, "assistant": ""})
            _append_history(self._session_dir, user_text, "")
            return "⏰ 回复超时，请稍后重试"
        except RuntimeError as e:
            log.error("ACP 进程异常 chatid=%s: %s", self._chatid, e)
            self._history.append({"user": user_text, "assistant": ""})
            _append_history(self._session_dir, user_text, "")
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
                if not raw:
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
                    await self._chunk_queue.put(None)
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        fut.set_exception(RuntimeError(str(msg["error"])))
                elif msg.get("method") == "session/update":
                    update = msg.get("params", {}).get("update", {})
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        text = update.get("content", {}).get("text", "")
                        self._full_text += text
                        await self._chunk_queue.put(text)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("ACP process exited"))
            self._pending.clear()
            await self._chunk_queue.put(None)

    async def stop(self):
        # 合并内存+磁盘历史
        history = _load_history(self._session_dir)
        if not history:
            history = self._history.copy()
        # 终止主进程
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
        # 异步回收：起临时进程做摘要+知识提取
        if history:
            asyncio.create_task(_recycle_memory(self._chatid, self._session_dir, self._cwd, history))

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_active


async def _recycle_memory(chatid: str, session_dir: str, cwd: str, history: list[dict]):
    """起临时 kiro-cli 进程做 L2 摘要 + L3 知识提取"""
    if not history:
        return
    history_text = _format_history(history)
    prompt = RECYCLE_PROMPT.format(chatid=chatid, history=history_text)
    log.info("开始回收记忆 chatid=%s turns=%d", chatid, len(history))

    try:
        proc = await asyncio.create_subprocess_exec(
            "kiro-cli", "acp", "--trust-all-tools",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=session_dir,
        )
        msg_id = 0

        async def rpc(method, params, timeout=60):
            nonlocal msg_id
            msg_id += 1
            line = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            proc.stdin.write((line + "\n").encode())
            await proc.stdin.drain()
            # 读到对应 id 的 result
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=deadline - asyncio.get_running_loop().time())
                if not raw:
                    raise RuntimeError("process exited")
                try:
                    msg = json.loads(raw.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        raise RuntimeError(str(msg["error"]))
                    return msg.get("result")
            raise asyncio.TimeoutError()

        async def prompt_and_collect(session_id, text, timeout=180):
            nonlocal msg_id
            msg_id += 1
            mid = msg_id
            line = json.dumps({"jsonrpc": "2.0", "id": mid, "method": "session/prompt",
                               "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}})
            proc.stdin.write((line + "\n").encode())
            await proc.stdin.drain()
            full = ""
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=deadline - asyncio.get_running_loop().time())
                if not raw:
                    break
                try:
                    msg = json.loads(raw.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if msg.get("method") == "session/update":
                    update = msg.get("params", {}).get("update", {})
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        full += update.get("content", {}).get("text", "")
                elif msg.get("id") == mid and "result" in msg:
                    result = msg["result"]
                    if isinstance(result, dict) and result.get("stopReason") == "end_turn":
                        break
            return full

        # initialize
        await rpc("initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "wecom-bridge-recycle", "version": "1.0"},
        })
        # session/new
        result = await rpc("session/new", {"cwd": cwd, "mcpServers": []})
        sid = result.get("sessionId", "") if isinstance(result, dict) else ""

        # 发送回收 prompt
        reply = await prompt_and_collect(sid, prompt)

        # 提取总结部分保存
        if reply:
            summary = _extract_summary(reply)
            _save_summary(session_dir, summary)
            _clear_history(session_dir)
            log.info("回收完成 chatid=%s summary_len=%d", chatid, len(summary))

        # 终止临时进程
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            proc.kill()

    except Exception as e:
        log.error("回收记忆失败 chatid=%s: %s", chatid, e)


class ProcessPool:
    MAX_PROCS = 10
    IDLE_TIMEOUT = 1800
    WARM_POOL_SIZE = int(os.getenv("WARM_POOL_SIZE", "3"))

    def __init__(self):
        self._pool: OrderedDict[str, KiroProcess] = OrderedDict()
        self._warm: list[KiroProcess] = []  # 预热的空闲进程

    async def warmup(self, cwd: str | None = None):
        """预热空闲进程池"""
        effective_cwd = cwd or WORK_DIR
        for i in range(self.WARM_POOL_SIZE):
            try:
                warm_id = f"_warm_{i}"
                session_dir = os.path.join(SESSIONS_DIR, warm_id)
                proc = KiroProcess(warm_id, session_dir, None, effective_cwd, mode="full")
                await proc.start()
                self._warm.append(proc)
                log.info("预热进程 %d/%d 就绪 pid=%d", i + 1, self.WARM_POOL_SIZE, proc._proc.pid)
            except Exception as e:
                log.error("预热进程失败: %s", e)

    async def get_or_create(self, chatid: str, agent: str | None = None, cwd: str | None = None, mode: str = "full") -> KiroProcess:
        if chatid in self._pool:
            proc = self._pool[chatid]
            if proc.alive:
                self._pool.move_to_end(chatid)
                return proc
            del self._pool[chatid]

        if len(self._pool) >= self.MAX_PROCS:
            await self._evict_lru()

        # 尝试从预热池取一个
        if self._warm:
            proc = self._warm.pop(0)
            session_dir = os.path.join(SESSIONS_DIR, chatid)
            proc._chatid = chatid
            proc._session_dir = session_dir
            proc._mode = mode
            # 重新创建 session（新的 cwd）
            effective_cwd = cwd or WORK_DIR
            proc._cwd = effective_cwd
            await proc._create_session()
            self._pool[chatid] = proc
            log.info("从预热池分配进程 chatid=%s pid=%d (剩余预热=%d)",
                     chatid, proc._proc.pid, len(self._warm))
            # 后台补充预热池
            asyncio.create_task(self._refill_warm(effective_cwd))
            return proc

        session_dir = os.path.join(SESSIONS_DIR, chatid)
        effective_cwd = cwd or WORK_DIR
        proc = KiroProcess(chatid, session_dir, agent, effective_cwd, mode=mode)
        await proc.start()
        self._pool[chatid] = proc
        return proc

    async def _refill_warm(self, cwd: str):
        """后台补充一个预热进程"""
        if len(self._warm) >= self.WARM_POOL_SIZE:
            return
        try:
            warm_id = f"_warm_{len(self._warm)}"
            session_dir = os.path.join(SESSIONS_DIR, warm_id)
            proc = KiroProcess(warm_id, session_dir, None, cwd, mode="full")
            await proc.start()
            self._warm.append(proc)
            log.info("补充预热进程 pid=%d (预热池=%d)", proc._proc.pid, len(self._warm))
        except Exception as e:
            log.error("补充预热进程失败: %s", e)

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
        tasks += [proc.stop() for proc in self._warm]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pool.clear()
        self._warm.clear()
