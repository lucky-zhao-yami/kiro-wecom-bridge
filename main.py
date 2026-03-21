"""kiro-wecom-bridge: 企微智能机器人长连接"""
import asyncio, logging, os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from pydantic import BaseModel

from typing import Optional
from channel import ChannelManager
import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CHANNELS_PATH = os.getenv("CHANNELS_PATH", "channels.json")
cm = ChannelManager()


async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)
        for ch in cm.channels:
            try:
                await ch.pool.cleanup_idle()
            except Exception as e:
                log.error("cleanup_idle 异常: %s", e)


async def _daily_memory_loop():
    """每天 0 点把昨天的 history 整理到长期记忆"""
    import glob, time as _time
    while True:
        # 计算到明天 0:00:05 的秒数
        now = _time.time()
        tomorrow = now - (now % 86400) + 86400 + 5  # UTC 明天 00:00:05
        # 调整为本地时区（UTC+8）
        local_midnight = tomorrow - 8 * 3600
        wait = max(local_midnight - now, 60)
        log.info("下次记忆整理: %.0f 秒后", wait)
        await asyncio.sleep(wait)
        # 扫描所有 chatid 的 history.jsonl
        sessions_dir = os.path.join(os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all"), "wecom-sessions")
        for hist_file in glob.glob(os.path.join(sessions_dir, "*/history.jsonl")):
            chatid = os.path.basename(os.path.dirname(hist_file))
            if chatid.startswith("_warm"):
                continue
            try:
                with open(hist_file, "r") as f:
                    lines = f.readlines()
                if not lines:
                    continue
                # 检查是否是昨天的（第一行的 ts）
                import json as _json
                first_ts = _json.loads(lines[0].strip()).get("ts", 0)
                if _time.strftime("%Y-%m-%d", _time.localtime(first_ts)) == _time.strftime("%Y-%m-%d"):
                    continue  # 今天的，不处理
                log.info("整理昨日记忆 chatid=%s turns=%d", chatid, len(lines))
                from agents.process import _recycle_memory, _load_history, _clear_history
                session_dir = os.path.dirname(hist_file)
                history = _load_history(session_dir, max_turns=50)
                cwd = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
                await _recycle_memory(chatid, session_dir, cwd, history)
                _clear_history(session_dir)
            except Exception as e:
                log.error("整理记忆失败 chatid=%s: %s", chatid, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("kiro-wecom-bridge 启动")
    cm.load(CHANNELS_PATH)
    ws_tasks = await cm.start_all()
    cleanup_task = asyncio.create_task(_cleanup_loop())
    memory_task = asyncio.create_task(_daily_memory_loop())
    scheduler.sync_all()
    # 预热进程池
    for ch in cm.channels:
        await ch.pool.warmup()
    yield
    cleanup_task.cancel()
    memory_task.cancel()
    for t in ws_tasks:
        t.cancel()
    for ch in cm.channels:
        await ch.pool.shutdown()


app = FastAPI(title="kiro-wecom-bridge", lifespan=lifespan)


# ---- 定时任务触发接口 ----

class CronTriggerRequest(BaseModel):
    chatid: str
    prompt: str
    bot_index: int = 0  # 多机器人时指定用哪个 channel，默认第一个


class SendMsgRequest(BaseModel):
    chatid: str = "dm_ZhaoXingPing"
    content: str
    bot_index: int = 0
    chat_type: int = 1  # 1=单聊 2=群聊，默认私聊给 ZhaoXingPing


@app.post("/send")
async def send_msg(req: SendMsgRequest):
    """主动发送消息到企微（供 notify-wecom 等 skill 调用）"""
    if req.bot_index >= len(cm.channels):
        return {"ok": False, "error": f"bot_index {req.bot_index} 超出范围"}
    ch = cm.channels[req.bot_index]
    try:
        await ch.ws.send_msg(req.chatid, req.chat_type, req.content)
        return {"ok": True}
    except Exception as e:
        log.error("send_msg 异常 chatid=%s: %s", req.chatid, e)
        return {"ok": False, "error": str(e)}


@app.post("/cron/trigger")
async def cron_trigger(req: CronTriggerRequest):
    """供 crontab 调用：向指定群发送 prompt，结果推送回企微"""
    if req.bot_index >= len(cm.channels):
        return {"ok": False, "error": f"bot_index {req.bot_index} 超出范围"}
    ch = cm.channels[req.bot_index]
    chat_cfg = ch._get_chat_config(req.chatid)
    agent = chat_cfg.get("agent")
    cwd = chat_cfg.get("cwd", os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all"))
    try:
        proc = await ch.pool.get_or_create(req.chatid, agent=agent, cwd=cwd, mode="full")
        reply = await proc.send(f"[cron]: {req.prompt}", timeout=300)
        chat_type = 1 if req.chatid.startswith("dm_") else 2
        await ch.ws.send_msg(req.chatid, chat_type, reply)
        return {"ok": True, "reply_length": len(reply)}
    except Exception as e:
        log.error("cron trigger 异常 chatid=%s: %s", req.chatid, e)
        return {"ok": False, "error": str(e)}



# ---- 定时任务调度 API ----

class JobCreateRequest(BaseModel):
    cron: str           # crontab 表达式，如 "0 9 * * *"
    chatid: str         # 目标 chatid
    prompt: str         # 要执行的 prompt
    bot_index: int = 0
    description: str = ""

class JobUpdateRequest(BaseModel):
    cron: Optional[str] = None
    chatid: Optional[str] = None
    prompt: Optional[str] = None
    bot_index: Optional[int] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None

@app.post("/scheduler/jobs")
async def create_job(req: JobCreateRequest):
    job = scheduler.create_job(req.cron, req.chatid, req.prompt, req.bot_index, req.description)
    return {"ok": True, "job": job}

@app.get("/scheduler/jobs")
async def list_jobs():
    return {"ok": True, "jobs": scheduler.list_jobs()}

@app.get("/scheduler/jobs/{job_id}")
async def get_job(job_id: str):
    job = scheduler.get_job(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}

@app.patch("/scheduler/jobs/{job_id}")
async def update_job(job_id: str, req: JobUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    job = scheduler.update_job(job_id, **updates)
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}

@app.delete("/scheduler/jobs/{job_id}")
async def delete_job(job_id: str):
    if not scheduler.delete_job(job_id):
        return {"ok": False, "error": "job not found"}
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8900")))