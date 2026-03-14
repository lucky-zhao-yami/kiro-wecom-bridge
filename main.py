"""kiro-wecom-bridge: Grafana告警分析 + Web聊天"""
import asyncio, logging, os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse

from session import SessionManager
import webhook
from monitor import start_monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

manager = SessionManager()


async def _on_monitor_alert(alert_text: str):
    """监控触发告警 → alert-advisor 分析 → 发企微"""
    try:
        prompt = f"请分析以下告警:\n{alert_text}"
        log.info("监控告警分析: %s", alert_text[:200])
        reply = await manager.chat("__alert__", prompt, agent="alert-advisor")
        # 发企微时去掉监控SQL，只保留面板名和状态
        short = alert_text.split("\n监控SQL：")[0]
        msg = f"⚠️ **告警通知**\n{short}\n\n🤖 **Kiro 分析**\n{reply}"
        await webhook.send_webhook(msg)
    except Exception as e:
        log.error("监控告警处理异常: %s", e)
        short = alert_text.split("\n监控SQL：")[0]
        await webhook.send_webhook(f"⚠️ **告警通知**\n{short}\n\n❌ 分析失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("kiro-wecom-bridge 启动")
    task = asyncio.create_task(start_monitor(_on_monitor_alert))
    yield
    task.cancel()


app = FastAPI(title="kiro-wecom-bridge", lifespan=lifespan)


# ========== MCP 回调接口 ==========

@app.post("/reply")
async def reply(request: Request):
    """MCP 工具回调：接收 kiro-cli 的回复"""
    body = await request.json()
    message = body.get("message", "")
    log.info("收到 /reply: message长度=%d", len(message))
    manager.set_reply(message)
    return {"status": "ok"}


# ========== Grafana 告警接口 ==========

@app.post("/alert")
async def alert(request: Request, bg: BackgroundTasks):
    """接收 Grafana 告警，kiro-cli 分析后发回企微群"""
    body = await request.json()
    bg.add_task(_handle_alert, body)
    return {"status": "ok"}


async def _handle_alert(body: dict):
    try:
        alert_text = _extract_alert_text(body)
        prompt = f"请分析以下告警:\n{alert_text}"
        log.info("告警分析: %s", alert_text[:200])
        reply = await manager.chat("__alert__", prompt, agent="alert-advisor")
        msg = f"⚠️ **告警通知**\n{alert_text}\n\n🤖 **Kiro 分析**\n{reply}"
        await webhook.send_webhook(msg)
    except Exception as e:
        log.error("告警处理异常: %s", e)
        await webhook.send_webhook(f"❌ 告警分析失败: {e}")


def _extract_alert_text(body: dict) -> str:
    """从 Grafana webhook payload 提取告警文本"""
    # 优先用 message 字段（包含完整告警描述）
    if body.get("message"):
        return body["message"].strip()
    # 其次用 title
    if body.get("title"):
        return body["title"].strip()
    # 兜底：从 alerts 数组拼接
    alerts = body.get("alerts", [])
    if not alerts:
        return str(body)
    parts = []
    for a in alerts:
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})
        name = labels.get("alertname", "N/A")
        summary = annotations.get("summary", "")
        desc = annotations.get("description", "")
        values = a.get("valueString", "")
        parts.append(f"{name}: {summary or desc} {values}".strip())
    return "\n".join(parts)


# ========== Web 聊天接口 ==========

@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    user = body.get("user", "default")
    message = body.get("message", "").strip()
    if not message:
        return {"reply": "请输入消息"}
    reply = await manager.chat(user, message)
    return {"reply": reply}


@app.get("/", response_class=HTMLResponse)
async def index():
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Kiro Chat</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui;background:#f5f5f5;height:100vh;display:flex;flex-direction:column}
#header{background:#07c160;color:#fff;padding:12px 20px;font-size:18px;font-weight:600}
#messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:75%;padding:10px 14px;border-radius:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word;font-size:14px}
.user{align-self:flex-end;background:#95ec69;border-bottom-right-radius:4px}
.bot{align-self:flex-start;background:#fff;border-bottom-left-radius:4px;box-shadow:0 1px 2px rgba(0,0,0,.1)}
.typing{color:#999;font-style:italic}
#input-area{display:flex;gap:8px;padding:12px;background:#fff;border-top:1px solid #e0e0e0}
#input{flex:1;padding:10px;border:1px solid #ddd;border-radius:8px;font-size:14px;outline:none}
#input:focus{border-color:#07c160}
#send{padding:10px 24px;background:#07c160;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px}
#send:disabled{background:#ccc}
</style></head><body>
<div id="header">🤖 Kiro Assistant</div>
<div id="messages"></div>
<div id="input-area">
<input id="input" placeholder="输入消息..." autocomplete="off">
<button id="send" onclick="send()">发送</button>
</div>
<script>
const msgs=document.getElementById('messages'),inp=document.getElementById('input'),btn=document.getElementById('send');
const user='user_'+Date.now().toString(36);
function add(text,cls){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d}
async function send(){
  const m=inp.value.trim();if(!m)return;
  inp.value='';btn.disabled=true;
  add(m,'user');
  const t=add('⏳ Kiro 思考中...','bot typing');
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user,message:m})});
    const d=await r.json();t.textContent=d.reply;t.classList.remove('typing');
  }catch(e){t.textContent='请求失败: '+e;t.classList.remove('typing')}
  btn.disabled=false;inp.focus();
}
inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8900")))
