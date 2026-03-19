"""kiro-wecom-bridge: 企微智能机器人长连接"""
import asyncio, logging, os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from pydantic import BaseModel

from channel import ChannelManager

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("kiro-wecom-bridge 启动")
    cm.load(CHANNELS_PATH)
    ws_tasks = await cm.start_all()
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()
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
    chatid: str
    content: str
    bot_index: int = 0
    chat_type: int = 2  # 1=单聊 2=群聊


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
        proc = await ch.pool.get_or_create(req.chatid, agent=agent, cwd=cwd)
        reply = await proc.send(f"[cron]: {req.prompt}", timeout=300)
        await ch.ws.send_msg(req.chatid, 2, reply)
        return {"ok": True, "reply_length": len(reply)}
    except Exception as e:
        log.error("cron trigger 异常 chatid=%s: %s", req.chatid, e)
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8900")))
