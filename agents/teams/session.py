"""TeamsSession — Lead + Teammates 生命周期管理"""
import asyncio, logging, os, time

from agents.process import KiroProcess
from agents.teams.task_list import TaskList
from agents.teams.mailbox import Mailbox

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")

LEAD_PREAMBLE = """[TEAMS 模式 — Lead Agent]

你是 Team Lead，负责和用户对话、拆解任务、协调 Teammates。

## 你的能力
- 你有完整的工具能力（code/grep/fs_read/fs_write/execute_bash），可以自己分析代码、查资料
- 你也可以把任务分配给 Teammates 执行

## 可用 Teammates
{agents_list}

## 创建任务
当需要分配任务时，用 execute_bash 调用 Task Helper API：
```bash
curl -s -X POST http://127.0.0.1:8900/teams/add_task -H 'Content-Type: application/json' \\
  -d '{{"session_dir":"{session_dir}","id":"t1","title":"任务描述","agent":"agent-name","depends_on":[]}}'
```

规则：
- id 用 t1, t2, t3... 递增
- agent 必须是上面列出的 Teammates 之一
- depends_on 填依赖的任务 id 列表，如 ["t1","t2"]
- 可以一次调用多次创建多个任务，系统会自动按依赖关系调度

## 发送消息给 Teammate
```bash
curl -s -X POST http://127.0.0.1:8900/teams/send_mail -H 'Content-Type: application/json' \\
  -d '{{"session_dir":"{session_dir}","from":"lead","to":"agent-name","content":"消息内容"}}'
```

## 工作方式
1. 收到用户需求 → 先自己分析理解，必要时查代码
2. 拆解为具体任务 → 通过 API 创建任务
3. 回复用户任务计划
4. 系统自动调度 Teammates 执行
5. Teammate 完成后结果会通过 mailbox 发给你
6. 所有任务完成后汇总结果

## 什么时候自己干
- 需求澄清、方案评审、简单问答 → 自己处理
- 需要查看代码理解上下文再拆任务 → 自己先分析

## 什么时候派任务
- 编码实现、代码审查、测试编写 → 派给对应 Teammate
- 多个独立子任务 → 创建多个任务并行执行
"""

TEAMMATE_PROMPT = """你是 {agent_name}，一个 Team 中的工作成员。

## 你的任务
任务ID: {task_id}
任务描述: {task_title}

## 来自其他成员的消息
{messages}

## 已完成的前置任务结果
{dependency_results}

## 工作规则
1. 专注执行你的任务
2. 完成后用 execute_bash 调用 API 更新任务状态：
```bash
curl -s -X POST http://127.0.0.1:8900/teams/complete_task -H 'Content-Type: application/json' \\
  -d '{{"session_dir":"{session_dir}","id":"{task_id}","result":"你的结果摘要"}}'
```
3. 如果需要通知其他成员，发送消息：
```bash
curl -s -X POST http://127.0.0.1:8900/teams/send_mail -H 'Content-Type: application/json' \\
  -d '{{"session_dir":"{session_dir}","from":"{agent_name}","to":"lead","content":"完成通知"}}'
```
4. 如果任务失败：
```bash
curl -s -X POST http://127.0.0.1:8900/teams/fail_task -H 'Content-Type: application/json' \\
  -d '{{"session_dir":"{session_dir}","id":"{task_id}","error":"失败原因"}}'
```
"""


class TeamsSession:
    POLL_INTERVAL = 5
    MAX_TEAMMATES = 3
    IDLE_EXIT_SECS = 120

    def __init__(self, chatid: str, chat_config: dict, ws, pool=None):
        self._chatid = chatid
        self._config = chat_config
        self._ws = ws
        self._pool = pool
        self._init_from_config(chat_config)
        self._init_state()

    def _init_from_config(self, cfg: dict):
        self._cwd = cfg.get("cwd", WORK_DIR)
        self._mode = cfg.get("mode", "full")
        self._session_dir = os.path.join(SESSIONS_DIR, self._chatid)
        self._max_teammates = cfg.get("max_parallel", self.MAX_TEAMMATES)
        self._lead_agent = cfg.get("lead", "team-lead")
        self._agent_names = cfg.get("agents", [])

    def _init_state(self):
        self._lead: KiroProcess | None = None
        self._teammates: dict[str, KiroProcess] = {}
        self._task_list = TaskList(self._session_dir)
        self._mailbox = Mailbox(self._session_dir)
        self._poll_task: asyncio.Task | None = None
        self._last_active: float = time.monotonic()
        self._first_msg = True
        self._prev_statuses: dict[str, str] = {}
        self._validated = False
        self._paused = False
        self._sending = False

    # ---- 生命周期 ----

    async def start(self):
        await self._ensure_lead()
        log.info("TeamsSession 启动 chatid=%s lead=%s agents=%s",
                 self._chatid, self._lead_agent, self._agent_names)

    async def stop(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if self._lead:
            await self._lead.stop()
            self._lead = None
        for tid in list(self._teammates):
            await self._cleanup_teammate(tid)
        log.info("TeamsSession 停止 chatid=%s", self._chatid)

    def can_recycle(self) -> bool:
        if self._task_list.has_active():
            return False
        return time.monotonic() - self._last_active > 1800

    # ---- 暂停/取消/门禁 预留 ----

    async def pause(self):
        self._paused = True

    async def resume(self):
        self._paused = False

    async def cancel_task(self, task_id: str, reason: str = "用户取消"):
        pass

    async def gate_check(self, task: dict) -> bool:
        return task.get("gate") is None

    # ---- 用户交互 ----

    async def send_from_human(self, text: str, on_chunk=None) -> str:
        self._last_active = time.monotonic()
        # Lead 正在处理中，不重复发送
        if self._sending:
            return "收到，Lead 正在处理上一条消息，请稍候。"
        await self._ensure_lead()
        prompt = self._build_lead_prompt(text)
        self._sending = True
        try:
            result = await self._lead.send(prompt, on_chunk=on_chunk)
        finally:
            self._sending = False
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
        return result

    # ---- Lead 管理 ----

    async def _ensure_lead(self):
        if self._lead and self._lead.alive:
            return
        if self._pool:
            self._lead = await self._pool.get_or_create(
                self._chatid, agent=self._lead_agent,
                cwd=self._cwd, mode=self._mode)
        else:
            lead_dir = os.path.join(self._session_dir, "lead")
            self._lead = KiroProcess(
                f"{self._chatid}/lead", lead_dir,
                agent=self._lead_agent, cwd=self._cwd,
                mode=self._mode, interruptible=True)
            await self._lead.start()

    def _build_lead_prompt(self, text: str) -> str:
        parts = []
        if self._first_msg:
            self._first_msg = False
            agents_list = "\n".join(f"- {a}" for a in self._agent_names)
            parts.append(LEAD_PREAMBLE.format(
                agents_list=agents_list, session_dir=self._session_dir))
        # 未读 mailbox
        mails = self._mailbox.read_for("lead")
        if mails:
            mail_text = "\n".join(f"[{m['from']}→lead]: {m['content']}" for m in mails)
            parts.append(f"[Mailbox 未读消息]\n{mail_text}")
        # final_summary 提示
        summary_path = os.path.join(self._session_dir, "final_summary.md")
        if os.path.isfile(summary_path):
            parts.append(f"[所有任务已完成，详见 {summary_path}]")
        parts.append(text)
        return "\n\n".join(parts)

    # ---- Poll Loop ----

    async def _poll_loop(self):
        idle_since: float | None = None
        while True:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
                await self._check_and_dispatch()
                # 更新 idle 计时
                if self._task_list.all_done():
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > self.IDLE_EXIT_SECS:
                        log.info("Teams poll loop 空闲超时退出 chatid=%s", self._chatid)
                        return
                else:
                    idle_since = None
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("Teams poll loop 异常 chatid=%s: %s", self._chatid, e)

    async def _check_and_dispatch(self):
        tasks = self._task_list.read_all()
        if not tasks:
            return
        tasks = await self._validate_once(tasks)
        await self._detect_status_changes(tasks)
        if await self._handle_all_done():
            return
        if not self._paused:
            await self._dispatch_claimable()

    async def _validate_once(self, tasks: list[dict]) -> list[dict]:
        if self._validated:
            return tasks
        self._validated = True
        cycles = self._task_list.validate()
        if cycles:
            for tid in cycles:
                self._task_list.fail(tid, "循环依赖")
            await self._push_msg(f"⚠️ 检测到循环依赖，已标记失败: {', '.join(cycles)}")
            return self._task_list.read_all()
        return tasks

    async def _handle_all_done(self) -> bool:
        if not self._task_list.all_done():
            return False
        if not os.path.isfile(os.path.join(self._session_dir, "final_summary.md")):
            await self._push_all_done()
        return True

    async def _dispatch_claimable(self):
        claimable = self._task_list.get_claimable()
        for task in claimable:
            if len(self._teammates) >= self._max_teammates:
                break
            if not await self.gate_check(task):
                continue
            await self._spawn_teammate(task)

    async def _detect_status_changes(self, tasks: list[dict]):
        for t in tasks:
            old = self._prev_statuses.get(t["id"])
            if old != t["status"]:
                if old is not None:
                    await self._push_progress(t, old)
                self._prev_statuses[t["id"]] = t["status"]

    async def _push_progress(self, task: dict, old_status: str):
        icons = {"completed": "✅", "failed": "❌", "in_progress": "⚙️"}
        icon = icons.get(task["status"], "📋")
        msg = f"{icon} [{task['id']}] {task['title']}: {old_status} → {task['status']}"
        if task["status"] == "failed" and task.get("result"):
            msg += f"\n   原因: {task['result'][:200]}"
        await self._push_msg(msg)

    async def _push_all_done(self):
        await self._write_final_summary()
        summary = self._task_list.summary()
        await self._push_msg(f"📋 所有任务已完成:\n{summary}")

    async def _write_final_summary(self):
        path = os.path.join(self._session_dir, "final_summary.md")
        summary = self._task_list.summary()
        with open(path, "w") as f:
            f.write(f"# Teams 任务汇总\n\n{summary}\n")

    async def _push_msg(self, text: str):
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        try:
            await self._ws.send_msg(self._chatid, chat_type, text[:1500])
        except Exception as e:
            log.error("推送企微失败 chatid=%s: %s", self._chatid, e)

    # ---- Teammate 管理 ----

    async def _spawn_teammate(self, task: dict):
        task_id = task["id"]
        if not self._task_list.claim(task_id, task["agent"]):
            return
        agent_dir = os.path.join(self._session_dir, "agents", task_id)
        proc = KiroProcess(
            f"{self._chatid}/{task_id}", agent_dir,
            agent=task["agent"], cwd=self._cwd,
            mode=self._mode, interruptible=False)
        await proc.start()
        self._teammates[task_id] = proc
        log.info("Teammate 启动 chatid=%s task=%s agent=%s pid=%d",
                 self._chatid, task_id, task["agent"], proc._proc.pid)
        asyncio.create_task(self._run_teammate(task_id, task))

    async def _run_teammate(self, task_id: str, task: dict):
        try:
            prompt = self._build_teammate_prompt(task)
            await self._teammates[task_id].send(prompt, timeout=300)
            # 如果 teammate 没自己更新状态，帮它标记完成
            tasks = self._task_list.read_all()
            for t in tasks:
                if t["id"] == task_id and t["status"] == "in_progress":
                    self._task_list.complete(task_id, "Teammate 执行完毕（未显式调用 complete API）")
                    break
        except Exception as e:
            log.error("Teammate 异常 chatid=%s task=%s: %s", self._chatid, task_id, e)
            self._task_list.fail(task_id, str(e))
        finally:
            await self._cleanup_teammate(task_id)

    def _build_teammate_prompt(self, task: dict) -> str:
        agent_name = task["agent"]
        mails = self._mailbox.read_for(agent_name)
        messages = "\n".join(f"[{m['from']}→{agent_name}]: {m['content']}" for m in mails) if mails else "无"
        dep_results = self._get_dependency_results(task)
        return TEAMMATE_PROMPT.format(
            agent_name=agent_name, task_id=task["id"], task_title=task["title"],
            messages=messages, dependency_results=dep_results,
            session_dir=self._session_dir)

    def _get_dependency_results(self, task: dict) -> str:
        deps = task.get("depends_on", [])
        if not deps:
            return "无前置任务"
        tasks = self._task_list.read_all()
        by_id = {t["id"]: t for t in tasks}
        lines = []
        for dep_id in deps:
            dep = by_id.get(dep_id)
            if dep and dep.get("result"):
                lines.append(f"[{dep_id}] {dep['title']}: {dep['result']}")
        return "\n".join(lines) if lines else "无前置任务结果"

    async def _cleanup_teammate(self, task_id: str):
        proc = self._teammates.pop(task_id, None)
        if proc:
            try:
                await proc.stop()
            except Exception as e:
                log.error("清理 Teammate 失败 task=%s: %s", task_id, e)
            log.info("Teammate 清理完成 chatid=%s task=%s", self._chatid, task_id)
