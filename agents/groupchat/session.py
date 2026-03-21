"""GroupChat 模式 — 多 Agent 协作，共享消息列表"""
import asyncio, json, logging, os, time

from agents.process import KiroProcess
from agents.task_manager import TaskManager

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")
MAX_ROUNDS_PER_PAIR = 6
HUMAN_TIMEOUT = 300  # 5 分钟
PROGRESS_INTERVAL = 60  # 1 分钟推送进度


class SharedMessages:
    """共享消息列表读写"""

    def __init__(self, session_dir: str):
        self._path = os.path.join(session_dir, "shared_messages.jsonl")
        os.makedirs(session_dir, exist_ok=True)

    def append(self, from_agent: str, content: str, **extra) -> dict:
        msgs = self.read()
        seq = (msgs[-1]["seq"] + 1) if msgs else 1
        msg = {"seq": seq, "from": from_agent, "content": content,
               "ts": int(time.time()), **extra}
        with open(self._path, "a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return msg

    def read(self) -> list[dict]:
        if not os.path.isfile(self._path):
            return []
        try:
            with open(self._path, "r") as f:
                return [json.loads(line) for line in f if line.strip()]
        except Exception:
            return []

    def format_for_prompt(self, last_n: int = 50) -> str:
        msgs = self.read()[-last_n:]
        lines = []
        for m in msgs:
            lines.append(f"[{m['from']}]: {m['content']}")
        return "\n\n".join(lines)


class GroupChatSession:
    MAX_AGENTS = 5

    def __init__(self, chatid: str, chat_config: dict, ws, pool=None):
        self._chatid = chatid
        self._config = chat_config
        self._ws = ws
        self._pool = pool
        self._cwd = chat_config.get("cwd", WORK_DIR)
        self._mode = chat_config.get("mode", "full")
        self._session_dir = os.path.join(SESSIONS_DIR, chatid)

        self._manager: KiroProcess | None = None
        self._agents: dict[str, KiroProcess] = {}  # agent_name → process
        self._messages = SharedMessages(self._session_dir)
        self._task_mgr = TaskManager(self._session_dir)

        self._manager_agent = chat_config.get("manager", "orchestrator-agent")
        self._agent_names = chat_config.get("agents", [])[:self.MAX_AGENTS]
        self._round_counts: dict[str, int] = {}  # "agentA:agentB" → count
        self._waiting_human = False
        self._human_event: asyncio.Event = asyncio.Event()
        self._last_active: float = time.monotonic()
        self._running = False
        self._progress_task: asyncio.Task | None = None

    async def start(self):
        """启动 Manager 进程"""
        if self._pool:
            self._manager = await self._pool.get_or_create(
                self._chatid, agent=self._manager_agent,
                cwd=self._cwd, mode=self._mode)
        else:
            mgr_dir = os.path.join(self._session_dir, "manager")
            self._manager = KiroProcess(
                f"{self._chatid}/manager", mgr_dir,
                agent=self._manager_agent, cwd=self._cwd,
                mode=self._mode, interruptible=True)
            await self._manager.start()
        self._running = True
        log.info("GroupChatSession 启动 chatid=%s manager=%s agents=%s",
                 self._chatid, self._manager_agent, self._agent_names)

    async def send_from_human(self, text: str, on_chunk=None) -> str:
        """用户发消息 — 写入共享消息 + 发给 Manager"""
        self._last_active = time.monotonic()
        if not self._manager or not self._manager.alive:
            await self.start()

        self._messages.append("Human", text)

        # 如果在等 Human 回复，通知继续
        if self._waiting_human:
            self._waiting_human = False
            self._human_event.set()

        # 发给 Manager，带上对话历史
        history = self._messages.format_for_prompt()
        prompt = self._build_manager_prompt(history, text)
        return await self._manager.send(prompt, on_chunk=on_chunk)

    def _build_manager_prompt(self, history: str, latest_msg: str) -> str:
        agents_list = ", ".join(self._agent_names)
        return (
            f"[GroupChat 对话历史]\n{history}\n\n"
            f"---\n"
            f"你是 Manager，可用的工作 Agent: {agents_list}\n"
            f"共享消息文件: {self._messages._path}\n"
            f"任务清单文件: {os.path.join(self._session_dir, 'tasks.json')}\n\n"
            f"请决定下一步行动：\n"
            f"- 如果需要某个 Agent 工作，回复: @AgentName 指令内容\n"
            f"- 如果需要用户决策，回复: @Human 问题\n"
            f"- 如果可以直接回答用户，直接回复\n\n"
            f"最新消息: [{latest_msg}]"
        )

    async def dispatch_agent(self, agent_name: str, instruction: str) -> str:
        """调度指定 Agent 执行任务"""
        if agent_name not in self._agents:
            agent_dir = os.path.join(self._session_dir, "agents", agent_name)
            proc = KiroProcess(
                f"{self._chatid}/{agent_name}", agent_dir,
                agent=agent_name, cwd=self._cwd,
                mode=self._mode, interruptible=False)
            await proc.start()
            self._agents[agent_name] = proc
            log.info("启动工作 Agent chatid=%s agent=%s", self._chatid, agent_name)

        # 带上对话历史
        history = self._messages.format_for_prompt()
        prompt = (
            f"[GroupChat 对话历史]\n{history}\n\n"
            f"---\n"
            f"你是 {agent_name}，Manager 给你的指令:\n{instruction}\n\n"
            f"请执行并回复结果。"
        )
        result = await self._agents[agent_name].send(prompt)
        self._messages.append(agent_name, result)
        return result

    async def wait_human(self, question: str) -> str:
        """@Human — 推送企微，等待用户回复"""
        self._waiting_human = True
        self._human_event.clear()
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        await self._ws.send_msg(self._chatid, chat_type, f"🤔 需要你的决策:\n\n{question}")
        self._messages.append("Manager", f"@Human {question}", wait_human=True)

        try:
            await asyncio.wait_for(self._human_event.wait(), timeout=HUMAN_TIMEOUT)
            # 用户已回复，最新消息就是 Human 的回复
            msgs = self._messages.read()
            for m in reversed(msgs):
                if m["from"] == "Human":
                    return m["content"]
        except asyncio.TimeoutError:
            self._waiting_human = False
            self._messages.append("System", "Human 5分钟未回复，Manager 自行决定继续")
            log.warning("Human 超时 chatid=%s", self._chatid)
        return ""

    def check_round_limit(self, agent_a: str, agent_b: str) -> bool:
        """检查两个 Agent 之间是否超过轮次限制"""
        key = f"{min(agent_a, agent_b)}:{max(agent_a, agent_b)}"
        count = self._round_counts.get(key, 0)
        return count >= MAX_ROUNDS_PER_PAIR

    def increment_round(self, agent_a: str, agent_b: str):
        key = f"{min(agent_a, agent_b)}:{max(agent_a, agent_b)}"
        self._round_counts[key] = self._round_counts.get(key, 0) + 1

    def can_recycle(self) -> bool:
        if self._task_mgr.has_active_tasks():
            return False
        if self._waiting_human:
            return False
        return time.monotonic() - self._last_active > 1800

    async def stop(self):
        self._running = False
        if self._progress_task:
            self._progress_task.cancel()
        if self._manager:
            await self._manager.stop()
        for proc in self._agents.values():
            await proc.stop()
        self._agents.clear()
        log.info("GroupChatSession 停止 chatid=%s", self._chatid)
