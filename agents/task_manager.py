"""任务管理 — tasks.json 读写，delegate/groupchat 共用"""
import json, logging, os, time, uuid

log = logging.getLogger(__name__)


def _gen_id() -> str:
    return f"t_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


class TaskManager:
    def __init__(self, session_dir: str):
        self._path = os.path.join(session_dir, "tasks.json")
        os.makedirs(session_dir, exist_ok=True)

    def _read(self) -> list[dict]:
        if not os.path.isfile(self._path):
            return []
        try:
            with open(self._path, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _write(self, tasks: list[dict]):
        with open(self._path, "w") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)

    def create_task(self, description: str, prompt: str, depends_on: list[str] = None) -> dict:
        tasks = self._read()
        seq = max((t.get("seq", 0) for t in tasks), default=0) + 1
        task = {
            "id": _gen_id(),
            "seq": seq,
            "status": "pending",
            "description": description,
            "prompt": prompt,
            "depends_on": depends_on or [],
            "assigned_to": None,
            "created_at": int(time.time()),
            "started_at": None,
            "finished_at": None,
            "progress": "",
            "result": None,
        }
        tasks.append(task)
        self._write(tasks)
        log.info("创建任务 id=%s desc=%s", task["id"], description[:50])
        return task

    def list_tasks(self) -> list[dict]:
        return self._read()

    def get_task(self, task_id: str) -> dict | None:
        for t in self._read():
            if t["id"] == task_id:
                return t
        return None

    def update_task(self, task_id: str, **kwargs) -> dict | None:
        tasks = self._read()
        for t in tasks:
            if t["id"] == task_id:
                t.update(kwargs)
                self._write(tasks)
                return t
        return None

    def get_pending_tasks(self) -> list[dict]:
        """返回依赖已满足的 pending 任务"""
        tasks = self._read()
        done_ids = {t["id"] for t in tasks if t["status"] == "done"}
        result = []
        for t in tasks:
            if t["status"] != "pending":
                continue
            if all(dep in done_ids for dep in t.get("depends_on", [])):
                result.append(t)
        return result

    def get_running_tasks(self) -> list[dict]:
        return [t for t in self._read() if t["status"] == "running"]

    def has_active_tasks(self) -> bool:
        return any(t["status"] in ("pending", "running") for t in self._read())
