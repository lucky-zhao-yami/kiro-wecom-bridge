"""kiro-cli 会话管理 - 通过 MCP 工具接收回复"""
import asyncio, logging, os, re

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")


class SessionManager:
    def __init__(self):
        self._pending: asyncio.Event | None = None
        self._reply: str = ""
        self._lock = asyncio.Lock()

    def set_reply(self, message: str):
        """MCP 工具回调：设置回复内容"""
        self._reply = message
        if self._pending:
            self._pending.set()
            log.info("回复已设置, len=%d", len(message))

    async def chat(self, user_id: str, message: str, agent: str = None) -> str:
        async with self._lock:
            self._pending = asyncio.Event()
            self._reply = ""
            try:
                await self._run_kiro(message, agent)
                await asyncio.wait_for(self._pending.wait(), timeout=300)
                return self._reply
            except asyncio.TimeoutError:
                return "⏰ 回复超时，请稍后重试"
            except Exception as e:
                log.error("执行异常: %s", e)
                return f"❌ 执行异常: {e}"
            finally:
                self._pending = None

    async def _run_kiro(self, message: str, agent: str = None):
        wrapped = (
            "完成任务后，你必须调用 reply_user 工具将最终回复发送给用户。"
            "request_id 填任意值即可。只发送最终结论，不要发送推理过程。\n\n"
            f"用户消息：{message}"
        )
        cmd = [
            "kiro-cli", "chat",
            "--no-interactive", "--wrap", "never", "--trust-all-tools",
        ]
        if agent:
            cmd.extend(["--agent", agent])
        cmd.append(wrapped)
        log.info("执行 kiro-cli: %s", message[:80])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=WORK_DIR,
        )
        asyncio.create_task(self._wait_proc(proc))

    async def _wait_proc(self, proc):
        stdout, _ = await proc.communicate()
        # 等一下让 reply_user 回调有机会到达
        await asyncio.sleep(1)
        if self._pending and not self._pending.is_set():
            text = stdout.decode(errors="replace") if stdout else ""
            text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9]*[a-zA-Z]', '', text).strip()
            if text:
                log.warning("kiro-cli 未调用 reply_user，使用 stdout fallback")
                self.set_reply(_extract_conclusion(text))
            else:
                self.set_reply("(kiro-cli 未生成回复)")


def _extract_conclusion(text: str) -> str:
    """从 kiro-cli stdout 提取最终结论，去掉工具调用日志"""
    # 找最后一个 markdown 标题段落作为结论起点
    for marker in ["## 问题确认", "## 原因分析", "## 处理建议"]:
        idx = text.find(marker)
        if idx != -1:
            # 往前找一段上下文（表格数据等）
            start = max(0, text.rfind("\n\n", 0, idx) - 500)
            return text[start:].strip()
    # 没有标准格式，取最后 3000 字符
    return text[-3000:].strip()
