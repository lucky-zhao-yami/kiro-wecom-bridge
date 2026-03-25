"""SOP 开发流程 — LangGraph 状态机

图结构:
  pm_ask -(interrupt)→ pm_generate → pm_confirm -(interrupt)→
  api_design → architect → arch_review
    ↗ (REJECT, ≤3)              ↓ (PASS)
  architect ←────────── arch_review
                                ↓
                         human_confirm_arch -(interrupt)→
                             coder → code_review
                           ↗ (REJECT, ≤6)    ↓ (PASS)
                         coder ←──── code_review
                                          ↓
                                   human_confirm_code -(interrupt)→
                                       deliver
"""

import asyncio, json, logging, os, re, time
from typing import TypedDict
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from agents.process import KiroProcess

log = logging.getLogger("agents.sop")

WORK_DIR = os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all")
AI_WORKSPACE = os.path.join(WORK_DIR, "ai-workspace")


class SOPState(TypedDict):
    task_id: str
    chatid: str
    services: list[str]
    cwd: str
    mode: str
    # Phase 产出
    requirements: str
    api_contract: str
    architecture: str
    code_diff: str
    review_feedback: str
    # 控制
    phase: str
    arch_review_count: int
    code_review_count: int
    review_result: str
    human_input: str
    notify: str  # 推送给用户的消息


def _task_dir(task_id: str) -> str:
    return os.path.join(AI_WORKSPACE, task_id)


def _ensure_dirs(task_id: str):
    base = _task_dir(task_id)
    for sub in ["01_pm_docs", "02_api_contracts", "03_architecture",
                "04_review_logs", "05_codebase_context", "06_deliverables"]:
        os.makedirs(os.path.join(base, sub), exist_ok=True)


# 全局进程缓存，key = (task_id, agent_name)
_agent_procs: dict[tuple[str, str], KiroProcess] = {}


async def _run_agent(state: SOPState, agent_name: str, prompt: str) -> str:
    """复用 KiroProcess — 同一 task 同一 agent 共享进程和上下文"""
    key = (state["task_id"], agent_name)
    proc = _agent_procs.get(key)
    if proc and proc.alive:
        log.info("SOP 复用进程 agent=%s pid=%s", agent_name, proc._proc.pid if proc._proc else "?")
    else:
        session_dir = os.path.join(_task_dir(state["task_id"]), "agents", agent_name)
        os.makedirs(session_dir, exist_ok=True)
        proc = KiroProcess(
            f"{state['chatid']}/sop/{agent_name}", session_dir,
            agent=agent_name, cwd=state["cwd"], mode=state["mode"])
        await proc.start()
        _agent_procs[key] = proc
        log.info("SOP 新建进程 agent=%s key=%s", agent_name, key)
    return await proc.send(prompt, timeout=600) or ""


# ── 节点 ──

async def pm_ask(state: SOPState) -> dict:
    """Phase 1: PM 多轮对话 — 分析需求、提问、判断信息是否充分"""
    _ensure_dirs(state["task_id"])
    prev = state.get("requirements", "")
    user_input = state.get("human_input", "")

    if not prev:
        # 首次
        prompt = (
            f"你是 PM，任务 {state['task_id']}。\n"
            f"⚠️ 你只跟当前用户对话，禁止联系其他人、禁止调用 notify-wecom、禁止发送消息给任何第三方。\n\n"
            f"用户需求: {user_input}\n\n"
            f"请分析需求，列出所有需要确认的问题（功能边界、业务规则、涉及服务、数据模型、验收标准等）。\n"
            f"打包提问。\n\n"
            f"如果信息已经完全充分，在回复开头写 [INFO_SUFFICIENT]，然后直接生成需求文档。"
        )
    else:
        # 后续轮次——进程有完整对话历史，只发用户新回复
        prompt = (
            f"用户回复:\n{user_input}\n\n"
            f"请判断信息是否充分。不充分就继续提问。\n"
            f"充分则在回复开头写 [INFO_SUFFICIENT]，然后直接生成需求文档。"
        )

    result = await _run_agent(state, "orchestrator-agent", prompt)

    if "[INFO_SUFFICIENT]" in result:
        doc = result.replace("[INFO_SUFFICIENT]", "").strip()
        doc_path = os.path.join(_task_dir(state["task_id"]), "01_pm_docs", "requirements.md")
        with open(doc_path, "w") as f:
            f.write(doc)
        return {
            "requirements": doc, "phase": "pm_confirm", "human_input": "",
            "notify": f"📄 需求文档已生成，请确认:\n\n{doc[:1500]}",
        }
    else:
        return {
            "requirements": "pm_in_progress",
            "phase": "pm_ask", "human_input": "",
            "notify": f"📋 PM 提问:\n\n{result[:1500]}",
        }


async def pm_wait(state: SOPState) -> dict:
    """PM 等待用户回答 — interrupt 在这里"""
    return {}  # 用户输入通过 resume → update_state 注入 human_input


async def pm_confirm(state: SOPState) -> dict:
    """Phase 1c: 用户确认需求文档"""
    user = state.get("human_input", "").strip().lower()
    if user in ("确认", "ok", "pass", "yes", "通过", ""):
        return {"phase": "api_design", "human_input": ""}
    # 用户有修改意见，回到 pm_ask 重新对话
    return {"phase": "pm_ask", "human_input": state["human_input"]}


async def api_design_node(state: SOPState) -> dict:
    """Phase 2a: API 设计"""
    result = await _run_agent(state, "api-designer-agent", (
        f"任务: {state['task_id']}\n需求文档:\n{state['requirements']}\n\n请设计 API 接口契约。"
    ))
    doc_path = os.path.join(_task_dir(state["task_id"]), "02_api_contracts", "api.yaml")
    with open(doc_path, "w") as f:
        f.write(result)
    return {"api_contract": result, "notify": "✅ Phase 2a: API 契约设计完成"}


async def architect_node(state: SOPState) -> dict:
    """Phase 2b: 架构设计"""
    prompt = (
        f"任务: {state['task_id']}\n需求:\n{state['requirements']}\n\nAPI 契约:\n{state['api_contract']}\n\n"
    )
    if state.get("review_feedback"):
        prompt += f"审查意见（请修改）:\n{state['review_feedback']}\n\n"
    prompt += "请制定架构规范。"
    result = await _run_agent(state, "architect-agent", prompt)
    doc_path = os.path.join(_task_dir(state["task_id"]), "03_architecture", "arch.md")
    with open(doc_path, "w") as f:
        f.write(result)
    return {"architecture": result, "review_feedback": "", "notify": "✅ Phase 2b: 架构设计完成"}


async def arch_review_node(state: SOPState) -> dict:
    """Phase 2.5: 架构审查"""
    count = state.get("arch_review_count", 0) + 1
    result = await _run_agent(state, "reviewer-agent", (
        f"任务: {state['task_id']}\n需求:\n{state['requirements']}\n\n"
        f"API 契约:\n{state['api_contract']}\n\n架构规范:\n{state['architecture']}\n\n"
        f"请审查，回复 PASS 或 REJECT + 修改意见。"
    ))
    log_path = os.path.join(_task_dir(state["task_id"]), "04_review_logs", f"arch-review-{count}.md")
    with open(log_path, "w") as f:
        f.write(result)
    is_pass = "PASS" in result.upper().split("\n")[0]
    status = "PASS ✅" if is_pass else f"REJECT ❌ (第{count}轮)"
    return {
        "arch_review_count": count,
        "review_result": "PASS" if is_pass else "REJECT",
        "review_feedback": result if not is_pass else "",
        "notify": f"🔍 架构审查: {status}",
    }


def arch_review_route(state: SOPState) -> str:
    if state["review_result"] == "PASS":
        return "human_confirm_arch"
    if state["arch_review_count"] >= 3:
        return "human_confirm_arch"
    return "architect"


async def human_confirm_arch(state: SOPState) -> dict:
    """等待用户确认架构"""
    user = state.get("human_input", "").strip().lower()
    if user in ("确认", "ok", "pass", "yes", "通过"):
        return {"phase": "coder", "human_input": "", "notify": "👍 架构已确认，开始编码"}
    elif user:
        return {"review_feedback": state["human_input"], "phase": "architect", "human_input": ""}
    return {"notify": f"🏗️ 架构审查完成（{state['arch_review_count']}轮），请确认或提出修改意见。"}


async def coder_node(state: SOPState) -> dict:
    """Phase 3: 编码"""
    prompt = (
        f"任务: {state['task_id']}\n需求:\n{state['requirements']}\n\n"
        f"API 契约:\n{state['api_contract']}\n\n架构规范:\n{state['architecture']}\n\n"
    )
    if state.get("review_feedback"):
        prompt += f"审查意见（请修复）:\n{state['review_feedback']}\n\n"
    prompt += "请按架构规范编写代码。"
    result = await _run_agent(state, "coder-agent", prompt)
    return {"code_diff": result, "review_feedback": "", "notify": "✅ Phase 3: 编码完成"}


async def code_review_node(state: SOPState) -> dict:
    """Phase 3.5: 代码审查"""
    count = state.get("code_review_count", 0) + 1
    result = await _run_agent(state, "reviewer-agent", (
        f"任务: {state['task_id']}\n架构规范:\n{state['architecture']}\n\n"
        f"代码变更:\n{state['code_diff']}\n\n请审查，回复 PASS 或 REJECT + 修改意见。"
    ))
    log_path = os.path.join(_task_dir(state["task_id"]), "04_review_logs", f"code-review-{count}.md")
    with open(log_path, "w") as f:
        f.write(result)
    is_pass = "PASS" in result.upper().split("\n")[0]
    status = "PASS ✅" if is_pass else f"REJECT ❌ (第{count}轮)"
    return {
        "code_review_count": count,
        "review_result": "PASS" if is_pass else "REJECT",
        "review_feedback": result if not is_pass else "",
        "notify": f"🔍 代码审查: {status}",
    }


def code_review_route(state: SOPState) -> str:
    if state["review_result"] == "PASS":
        return "human_confirm_code"
    if state["code_review_count"] >= 6:
        return "human_confirm_code"
    return "coder"


async def human_confirm_code(state: SOPState) -> dict:
    user = state.get("human_input", "").strip().lower()
    if user in ("确认", "ok", "pass", "yes", "通过"):
        return {"phase": "deliver", "human_input": "", "notify": "👍 代码已确认，开始交付"}
    elif user:
        return {"review_feedback": state["human_input"], "phase": "coder", "human_input": ""}
    return {"notify": f"🔍 代码审查完成（{state['code_review_count']}轮），请确认或提出修改意见。"}


async def deliver_node(state: SOPState) -> dict:
    """Phase 4: 交付"""
    result = await _run_agent(state, "doc-engineer-agent", (
        f"任务: {state['task_id']}\n请生成接口文档和测试发布文档。\n"
        f"产出目录: {_task_dir(state['task_id'])}"
    ))
    doc_path = os.path.join(_task_dir(state["task_id"]), "06_deliverables", "summary.md")
    with open(doc_path, "w") as f:
        f.write(result)
    return {"phase": "done", "notify": f"🎉 任务 {state['task_id']} 开发完成！\n产出: {_task_dir(state['task_id'])}"}


# ── 构建图 ──

def build_sop_graph() -> StateGraph:
    g = StateGraph(SOPState)

    g.add_node("pm_ask", pm_ask)
    g.add_node("pm_wait", pm_wait)
    g.add_node("pm_confirm", pm_confirm)
    g.add_node("api_design", api_design_node)
    g.add_node("architect", architect_node)
    g.add_node("arch_review", arch_review_node)
    g.add_node("human_confirm_arch", human_confirm_arch)
    g.add_node("coder", coder_node)
    g.add_node("code_review", code_review_node)
    g.add_node("human_confirm_code", human_confirm_code)
    g.add_node("deliver", deliver_node)

    g.set_entry_point("pm_ask")
    # pm_ask → pm_wait(等回答) 或 pm_confirm(信息充分)
    g.add_conditional_edges("pm_ask", lambda s: s.get("phase", "pm_ask"),
                            {"pm_ask": "pm_wait", "pm_confirm": "pm_confirm"})
    g.add_edge("pm_wait", "pm_ask")  # 用户回答后回到 pm_ask
    g.add_conditional_edges("pm_confirm", lambda s: s.get("phase", "api_design"),
                            {"api_design": "api_design", "pm_ask": "pm_ask"})
    g.add_edge("api_design", "architect")
    g.add_edge("architect", "arch_review")
    g.add_conditional_edges("arch_review", arch_review_route)
    g.add_conditional_edges("human_confirm_arch", lambda s: s.get("phase", "coder"))
    g.add_edge("coder", "code_review")
    g.add_conditional_edges("code_review", code_review_route)
    g.add_conditional_edges("human_confirm_code", lambda s: s.get("phase", "deliver"))
    g.add_edge("deliver", END)

    return g


class SOPSession:
    """SOP 会话"""

    def __init__(self, chatid: str, chat_config: dict, ws):
        self._chatid = chatid
        self._ws = ws
        self._config = chat_config
        self._checkpointer = MemorySaver()
        self._graph = build_sop_graph().compile(
            checkpointer=self._checkpointer,
            interrupt_before=["pm_wait", "pm_confirm",
                              "human_confirm_arch", "human_confirm_code"]
        )
        self._thread_id = chatid
        self._running = False
        self._started = False
        self._task: asyncio.Task | None = None

    async def start(self, task_id: str, services: list[str], initial_request: str):
        """启动 SOP（后台）"""
        self._started = True
        state = self._make_state(task_id, services, initial_request)
        config = {"configurable": {"thread_id": self._thread_id}}
        self._task = asyncio.create_task(self._run(state, config))

    async def start_and_wait(self, task_id: str, services: list[str], initial_request: str) -> str:
        """启动 SOP 并等待第一个 interrupt，返回 notify 消息"""
        self._started = True
        state = self._make_state(task_id, services, initial_request)
        config = {"configurable": {"thread_id": self._thread_id}}
        return await self._run_and_get_notify(state, config)

    async def resume(self, human_input: str):
        """恢复（后台）"""
        config = {"configurable": {"thread_id": self._thread_id}}
        self._graph.update_state(config, {"human_input": human_input})
        self._task = asyncio.create_task(self._run(None, config))

    async def resume_and_wait(self, human_input: str) -> str:
        """恢复并等待下一个 interrupt，返回 notify 消息"""
        config = {"configurable": {"thread_id": self._thread_id}}
        self._graph.update_state(config, {"human_input": human_input})
        return await self._run_and_get_notify(None, config)

    def _make_state(self, task_id, services, initial_request):
        return SOPState(
            task_id=task_id, chatid=self._chatid, services=services,
            cwd=self._config.get("cwd", WORK_DIR),
            mode=self._config.get("mode", "full"),
            requirements="", api_contract="", architecture="",
            code_diff="", review_feedback="",
            phase="pm_ask", arch_review_count=0, code_review_count=0,
            review_result="", human_input=initial_request, notify="",
        )

    async def _run_and_get_notify(self, state, config) -> str:
        """执行图直到 interrupt，返回 notify"""
        self._running = True
        try:
            if state:
                result = await self._graph.ainvoke(state, config)
            else:
                result = await self._graph.ainvoke(None, config)
            return (result or {}).get("notify", "")
        except Exception as e:
            log.error("SOP 异常 chatid=%s: %s", self._chatid, e)
            return f"❌ SOP 异常: {e}"
        finally:
            self._running = False

    async def _run(self, state, config):
        """执行图，遇到 interrupt 时推送通知"""
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        self._running = True
        try:
            if state:
                result = await self._graph.ainvoke(state, config)
            else:
                result = await self._graph.ainvoke(None, config)

            # 推送 notify
            notify = (result or {}).get("notify", "")
            if notify:
                await self._ws.send_msg(self._chatid, chat_type, notify[:1500])

        except Exception as e:
            log.error("SOP 异常 chatid=%s: %s", self._chatid, e)
            await self._ws.send_msg(self._chatid, chat_type, f"❌ SOP 异常: {e}")
        finally:
            self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def started(self) -> bool:
        return self._started
