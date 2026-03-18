"""kiro-wecom-bridge: 企微智能机器人长连接 + Grafana 告警分析"""
import asyncio, logging, os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI

from channel import ChannelManager
from monitor import start_monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CHANNELS_PATH = os.getenv("CHANNELS_PATH", "channels.json")
cm = ChannelManager()


async def _on_monitor_alert(alert_text: str):
    asyncio.create_task(_handle_monitor_alert(alert_text))


async def _handle_monitor_alert(alert_text: str):
    try:
        for ch in cm.channels:
            await ch.send_alert(alert_text)
    except Exception as e:
        log.error("监控告警处理异常: %s", e)


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
    monitor_task = asyncio.create_task(start_monitor(_on_monitor_alert))
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()
    monitor_task.cancel()
    for t in ws_tasks:
        t.cancel()
    for ch in cm.channels:
        await ch.pool.shutdown()


app = FastAPI(title="kiro-wecom-bridge", lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8900")))
