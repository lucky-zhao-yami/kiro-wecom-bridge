"""Single 模式 — ProcessPool，per-chatid 单进程"""
import asyncio, logging, os
from collections import OrderedDict

from agents.process import KiroProcess

log = logging.getLogger(__name__)

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
SESSIONS_DIR = os.path.join(WORK_DIR, "wecom-sessions")


class ProcessPool:
    MAX_PROCS = 10
    IDLE_TIMEOUT = 1800
    WARM_POOL_SIZE = int(os.getenv("WARM_POOL_SIZE", "3"))

    def __init__(self):
        self._pool: OrderedDict[str, KiroProcess] = OrderedDict()
        self._warm: list[KiroProcess] = []

    async def warmup(self, cwd: str | None = None):
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

        if self._warm:
            proc = self._warm.pop(0)
            session_dir = os.path.join(SESSIONS_DIR, chatid)
            proc._chatid = chatid
            proc._session_dir = session_dir
            proc._mode = mode
            effective_cwd = cwd or WORK_DIR
            proc._cwd = effective_cwd
            await proc._create_session()
            self._pool[chatid] = proc
            log.info("从预热池分配进程 chatid=%s pid=%d (剩余预热=%d)",
                     chatid, proc._proc.pid, len(self._warm))
            asyncio.create_task(self._refill_warm(effective_cwd))
            return proc

        session_dir = os.path.join(SESSIONS_DIR, chatid)
        effective_cwd = cwd or WORK_DIR
        proc = KiroProcess(chatid, session_dir, agent, effective_cwd, mode=mode)
        await proc.start()
        self._pool[chatid] = proc
        return proc

    async def _refill_warm(self, cwd: str):
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
