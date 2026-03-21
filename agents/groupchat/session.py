"""GroupChat 模式 — 多 Agent 协作，共享消息列表"""
import asyncio, json, logging, os, re, time

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
        self._loop_task: asyncio.Task | None = None

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
            return ""  # 不需要 Manager 回复，循环会继续

        # 如果后台调度循环正在跑，不打断，只写入消息
        if self._loop_task and not self._loop_task.done():
            log.info("GroupChat 后台循环运行中，消息已写入 chatid=%s", self._chatid)
            return "收到，后台任务正在执行中，完成后会通知你。"

        # 发给 Manager
        history = self._messages.format_for_prompt()
        prompt = self._build_manager_prompt(history, text)
        result = await self._manager.send(prompt, on_chunk=on_chunk)

        # Manager 回复后，启动后台调度循环
        if result:
            self._messages.append("Manager", result)
            parsed = self._parse_at_command(result)
            if parsed:
                self._loop_task = asyncio.create_task(self._dispatch_loop(parsed))

        return result

    def _build_manager_prompt(self, history: str, latest_msg: str) -> str:
        agents_list = ", ".join(self._agent_names)
        return (
            f"[GroupChat 对话历史]\n{history}\n\n"
            f"---\n"
            f"你是 Manager，可用的工作 Agent: {agents_list}\n"
            f"共享消息文件: {self._messages._path}\n"
            f"任务清单文件: {os.path.join(self._session_dir, 'tasks.json')}\n\n"
            f"**重要：你必须自动驱动整个流程。** 当一个 Agent 完成后，立即用 @ 调度下一个 Agent，不要停下来等用户。\n"
            f"只有在需要用户做决策时才 @Human。\n\n"
            f"回复格式：\n"
            f"- 调度 Agent: @AgentName 指令内容\n"
            f"- 需要用户决策: @Human 问题\n"
            f"- 所有工作完成，汇报结果: 直接回复（不带 @）\n\n"
            f"最新消息: [{latest_msg}]"
        )

    def _parse_at_command(self, text: str) -> tuple[str, str] | None:
        """解析 @agent-name 指令"""
        for name in self._agent_names + ["Human"]:
            m = re.search(rf'@{re.escape(name)}\s+(.*)', text, re.DOTALL)
            if m:
                return name, m.group(1).strip()
        return None

    async def _dispatch_loop(self, initial_command: tuple[str, str]):
        """后台调度循环 — 独立于用户消息链路"""
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        name, instruction = initial_command
        max_turns = 20

        try:
            for _ in range(max_turns):
                if name == "Human":
                    human_reply = await self.wait_human(instruction)
                    if not human_reply:
                        break
                    # 用户回复后发给 Manager
                    history = self._messages.format_for_prompt()
                    reply = await self._manager.send(
                        f"[GroupChat 对话历史]\n{history}\n\n---\nHuman 已回复，请立即用 @ 调度下一个 Agent 继续流程。")
                else:
                    # 调度工作 Agent
                    log.info("GroupChat 调度 chatid=%s agent=%s", self._chatid, name)
                    await self._ws.send_msg(self._chatid, chat_type, f"⚙️ 正在调度 {name} 执行...")
                    agent_reply = await self.dispatch_agent(name, instruction)
                    if not agent_reply:
                        break
                    # 结果回传 Manager
                    history = self._messages.format_for_prompt()
                    reply = await self._manager.send(
                        f"[GroupChat 对话历史]\n{history}\n\n---\n"
                        f"{name} 已完成，请立即用 @ 调度下一个 Agent 继续流程。只有所有工作都完成后才直接回复用户。")

                if not reply:
                    break
                self._messages.append("Manager", reply)

                # 解析下一步
                parsed = self._parse_at_command(reply)
                if not parsed:
                    # 没有 @指令，推送给用户
                    await self._ws.send_msg(self._chatid, chat_type, reply[:1500])
                    break
                name, instruction = parsed
        except Exception as e:
            log.error("GroupChat 调度循环异常 chatid=%s: %s", self._chatid, e)
            await self._ws.send_msg(self._chatid, chat_type, f"❌ 调度异常: {e}")
        finally:
            log.info("GroupChat 调度循环结束 chatid=%s", self._chatid)

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
