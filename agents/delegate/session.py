"""Delegate 模式 — 主 Agent 对话 + Worker 池执行长任务"""
import asyncio, logging, os, time

from agents.process import KiroProcess
from agents.task_manager import TaskManager

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")


DELEGATE_PREAMBLE = """[DELEGATE 模式]
你是主 Agent，负责和用户对话。你有 Worker Agent 帮你执行长任务。

当用户要求执行预计超过 30 秒的任务（代码分析、编码、文件处理、批量操作等）时：
1. 不要自己执行，用 fs_read 读取 {tasks_path}，然后用 fs_write 追加一条新任务：
   {{"id": "t_日期_随机6位", "seq": 下一个序号, "status": "pending", "description": "简述", "prompt": "详细指令", "depends_on": [], "assigned_to": null, "created_at": 时间戳}}
2. 回复用户："已安排，我会跟进进度。"
3. 用户问进度时，fs_read {tasks_path} 查看状态回复。

简单问答、闲聊、查询类问题直接自己回答，不要分发。
"""


class DelegateSession:
    MAX_WORKERS = 3
    DISPATCH_INTERVAL = 5

    def __init__(self, chatid: str, chat_config: dict, ws, pool=None):
        self._chatid = chatid
        self._config = chat_config
        self._ws = ws
        self._pool = pool
        self._cwd = chat_config.get("cwd", WORK_DIR)
        self._mode = chat_config.get("mode", "full")
        self._session_dir = os.path.join(SESSIONS_DIR, chatid)

        self._main: KiroProcess | None = None
        self._workers: dict[str, KiroProcess] = {}  # task_id → worker
        self._task_mgr = TaskManager(self._session_dir)
        self._dispatch_task: asyncio.Task | None = None
        self._last_active: float = time.monotonic()
        self._first_msg = True

    async def start(self):
        """启动主 Agent（优先从预热池取）+ dispatch 循环"""
        if self._pool:
            self._main = await self._pool.get_or_create(
                self._chatid, agent=self._config.get("agent"),
                cwd=self._cwd, mode=self._mode)
        else:
            self._main = KiroProcess(
                self._chatid, self._session_dir,
                agent=self._config.get("agent"),
                cwd=self._cwd, mode=self._mode, interruptible=True)
            await self._main.start()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        log.info("DelegateSession 启动 chatid=%s", self._chatid)

    async def send_to_main(self, text: str, on_chunk=None) -> str:
        """发消息给主 Agent，首次注入 delegate 指令"""
        self._last_active = time.monotonic()
        if not self._main or not self._main.alive:
            await self.start()
        # 首次消息注入 delegate preamble（在 KiroProcess 的 preamble 之外额外加）
        if self._first_msg:
            self._first_msg = False
            tasks_path = os.path.join(self._session_dir, "tasks.json")
            text = DELEGATE_PREAMBLE.format(tasks_path=tasks_path) + "\n" + text
        return await self._main.send(text, on_chunk=on_chunk)

    async def _dispatch_loop(self):
        """每 N 秒检查 tasks.json，分发 pending 任务，检测完成"""
        while True:
            await asyncio.sleep(self.DISPATCH_INTERVAL)
            try:
                # 分发 pending 任务
                for task in self._task_mgr.get_pending_tasks():
                    if len(self._workers) >= self.MAX_WORKERS:
                        break
                    if task["id"] not in self._workers:
                        await self._assign_task(task)

                # 检测已完成的任务
                for task_id in list(self._workers.keys()):
                    task = self._task_mgr.get_task(task_id)
                    if task and task["status"] == "done":
                        await self._on_task_done(task)
            except Exception as e:
                log.error("dispatch_loop 异常: %s", e)

    async def _assign_task(self, task: dict):
        """分配任务给新 worker"""
        task_id = task["id"]
        worker_dir = os.path.join(self._session_dir, "workers", task_id)
        worker = KiroProcess(
            f"{self._chatid}/worker/{task_id}", worker_dir,
            agent=None, cwd=self._cwd, mode=self._mode, interruptible=False)
        await worker.start()
        self._workers[task_id] = worker
        self._task_mgr.update_task(task_id, status="running", started_at=int(time.time()))
        log.info("分配任务 chatid=%s task=%s worker_pid=%d", self._chatid, task_id, worker._proc.pid)

        # 异步执行任务
        asyncio.create_task(self._run_task(task_id, task["prompt"]))

    async def _run_task(self, task_id: str, prompt: str):
        """worker 执行任务"""
        worker = self._workers.get(task_id)
        if not worker:
            return
        try:
            # 告诉 worker 任务上下文
            task_prompt = (
                f"你是一个工作 Agent，请执行以下任务。\n"
                f"任务ID: {task_id}\n"
                f"任务文件: {self._session_dir}/tasks.json\n"
                f"执行过程中请用 fs_write 更新 tasks.json 中该任务的 progress 字段。\n"
                f"完成后请将 status 改为 done，result 填写结果摘要。\n\n"
                f"任务内容:\n{prompt}"
            )
            await worker.send(task_prompt)
            # worker 完成后检查任务状态
            task = self._task_mgr.get_task(task_id)
            if task and task["status"] != "done":
                # worker 回复了但没更新 tasks.json，帮它标记完成
                self._task_mgr.update_task(task_id, status="done", finished_at=int(time.time()))
        except Exception as e:
            log.error("任务执行失败 task=%s: %s", task_id, e)
            self._task_mgr.update_task(task_id, status="failed", progress=str(e))

    async def _on_task_done(self, task: dict):
        """任务完成，推送企微通知"""
        task_id = task["id"]
        worker = self._workers.pop(task_id, None)
        result = task.get("result", "已完成")
        desc = task.get("description", "")
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        try:
            await self._ws.send_msg(self._chatid, chat_type,
                f"✅ 任务完成: {desc}\n\n{result[:500]}")
        except Exception as e:
            log.error("推送任务完成通知失败: %s", e)
        log.info("任务完成 chatid=%s task=%s desc=%s", self._chatid, task_id, desc[:50])

    def can_recycle(self) -> bool:
        """是否可以回收"""
        if self._task_mgr.has_active_tasks():
            return False
        return time.monotonic() - self._last_active > 1800

    async def stop(self):
        if self._dispatch_task:
            self._dispatch_task.cancel()
        if self._main:
            await self._main.stop()
        for worker in self._workers.values():
            await worker.stop()
        self._workers.clear()
        log.info("DelegateSession 停止 chatid=%s", self._chatid)
