"""TaskList — JSONL 任务列表，文件锁保证并发安全"""
import logging, os, time
from collections import deque

from agents.teams.jsonl_store import JsonlStore

log = logging.getLogger(__name__)


class TaskList(JsonlStore):

    def __init__(self, session_dir: str):
        super().__init__(os.path.join(session_dir, "tasks.jsonl"))

    # ---- 公共方法 ----

    def add_task(self, task: dict):
        def _do():
            tasks = self._read_all()
            tasks.append(task)
            self._write_all(tasks)
        self._with_lock(_do)

    def read_all(self) -> list[dict]:
        return self._with_lock(self._read_all)

    def claim(self, task_id: str, assignee: str) -> bool:
        result = [False]
        def _do():
            tasks = self._read_all()
            for t in tasks:
                if t["id"] == task_id and t["status"] == "pending":
                    t["status"] = "in_progress"
                    t["assignee"] = assignee
                    t["started_at"] = int(time.time())
                    result[0] = True
                    break
            self._write_all(tasks)
        self._with_lock(_do)
        return result[0]

    def complete(self, task_id: str, result: str):
        def _do():
            tasks = self._read_all()
            for t in tasks:
                if t["id"] == task_id:
                    t["status"] = "completed"
                    t["result"] = result
                    t["finished_at"] = int(time.time())
                    break
            self._write_all(tasks)
        self._with_lock(_do)

    def fail(self, task_id: str, error: str):
        def _do():
            tasks = self._read_all()
            for t in tasks:
                if t["id"] == task_id:
                    t["status"] = "failed"
                    t["result"] = error
                    t["finished_at"] = int(time.time())
                    break
            self._write_all(tasks)
        self._with_lock(_do)

    def get_claimable(self) -> list[dict]:
        tasks = self.read_all()
        completed = {t["id"] for t in tasks if t["status"] == "completed"}
        result = []
        for t in tasks:
            if t["status"] != "pending":
                continue
            if t.get("gate") is not None:
                continue
            if all(d in completed for d in t.get("depends_on", [])):
                result.append(t)
        return result

    def has_active(self) -> bool:
        return any(t["status"] in ("pending", "in_progress") for t in self.read_all())

    def all_done(self) -> bool:
        tasks = self.read_all()
        return bool(tasks) and all(t["status"] in ("completed", "failed") for t in tasks)

    def summary(self) -> str:
        tasks = self.read_all()
        if not tasks:
            return "无任务"
        lines = []
        icons = {"completed": "✅", "failed": "❌", "in_progress": "⚙️", "pending": "⏳", "blocked": "🚫"}
        for t in tasks:
            icon = icons.get(t["status"], "❓")
            line = f"{icon} [{t['id']}] {t['title']} ({t['status']})"
            if t.get("result"):
                line += f"\n   → {t['result'][:200]}"
            lines.append(line)
        return "\n".join(lines)

    def validate(self) -> list[str]:
        """Kahn 拓扑排序检测循环依赖，返回参与循环的 task_id 列表"""
        tasks = self.read_all()
        ids = {t["id"] for t in tasks}
        in_degree = {t["id"]: 0 for t in tasks}
        adj = {t["id"]: [] for t in tasks}
        for t in tasks:
            for dep in t.get("depends_on", []):
                if dep in ids:
                    in_degree[t["id"]] += 1
                    adj[dep].append(t["id"])
        queue = deque(tid for tid, d in in_degree.items() if d == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for nxt in adj[node]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        return [tid for tid, d in in_degree.items() if d > 0] if visited < len(tasks) else []
