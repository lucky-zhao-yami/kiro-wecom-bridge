"""JsonlStore — JSONL 文件读写 + 文件锁基类"""
import fcntl, json, logging, os, time

log = logging.getLogger(__name__)


class JsonlStore:
    LOCK_RETRY_INTERVAL = 0.1
    LOCK_MAX_RETRIES = 30

    def __init__(self, path: str):
        self._path = path
        self._lock_path = path + ".lock"
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _with_lock(self, fn):
        os.makedirs(os.path.dirname(self._lock_path), exist_ok=True)
        with open(self._lock_path, "w") as lock_f:
            for _ in range(self.LOCK_MAX_RETRIES):
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(self.LOCK_RETRY_INTERVAL)
            else:
                raise TimeoutError(f"无法获取文件锁 {self._lock_path}")
            try:
                return fn()
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)

    def _read_all(self) -> list[dict]:
        if not os.path.isfile(self._path):
            return []
        items = []
        with open(self._path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    log.error("JSONL 损坏行 %s: %s", self._path, line[:100])
        return items

    def _write_all(self, items: list[dict]):
        with open(self._path, "w") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
