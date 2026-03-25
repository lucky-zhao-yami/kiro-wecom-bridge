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


async def _run_agent(state: SOPState, agent_name: str, prompt: str) -> str:
    """启动 KiroProcess 执行任务"""
    session_dir = os.path.join(_task_dir(state["task_id"]), "agents", agent_name)
    os.makedirs(session_dir, exist_ok=True)
    proc = KiroProcess(
        f"{state['chatid']}/sop/{agent_name}", session_dir,
        agent=agent_name, cwd=state["cwd"], mode=state["mode"])
    await proc.start()
    return await proc.send(prompt, timeout=600) or ""


# ── 节点 ──

async def pm_ask(state: SOPState) -> dict:
    """Phase 1a: PM 分析需求，输出需要确认的问题"""
    _ensure_dirs(state["task_id"])
    prompt = (
        f"你是 PM，任务 {state['task_id']}。\n"
        f"用户需求: {state.get('human_input', '')}\n\n"
        f"请分析需求，列出所有需要跟用户确认的问题（功能边界、业务规则、涉及服务、数据模型、验收标准等）。\n"
        f"打包提问，不要一个一个问。如果信息已经充分，直接说'信息充分，可以生成需求文档'。"
    )
    result = await _run_agent(state, "orchestrator-agent", prompt)
    return {"notify": f"📋 PM 分析完成:\n\n{result[:1500]}", "requirements": result}


async def pm_generate(state: SOPState) -> dict:
    """Phase 1b: 根据用户回答生成需求文档"""
    prompt = (
        f"任务 {state['task_id']}。\n"
        f"之前的分析:\n{state['requirements']}\n\n"
        f"用户回复:\n{state.get('human_input', '')}\n\n"
        f"请生成完整的需求文档，包含：功能需求、业务规则、涉及服务、数据模型、验收标准。"
    )
    result = await _run_agent(state, "orchestrator-agent", prompt)
    doc_path = os.path.join(_task_dir(state["task_id"]), "01_pm_docs", "requirements.md")
    with open(doc_path, "w") as f:
        f.write(result)
    return {
        "requirements": result,
        "notify": f"📄 需求文档已生成，请确认:\n\n{result[:1500]}"
    }


async def pm_confirm(state: SOPState) -> dict:
    """Phase 1c: 用户确认需求文档"""
    user = state.get("human_input", "").strip().lower()
    if user in ("确认", "ok", "pass", "yes", "通过", ""):
        return {"phase": "api_design", "human_input": ""}
    # 用户有修改意见，重新生成
    return {"phase": "pm_generate", "human_input": state["human_input"]}


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
    g.add_node("pm_generate", pm_generate)
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
    g.add_edge("pm_ask", "pm_generate")          # interrupt 后用户回答 → 生成文档
    g.add_edge("pm_generate", "pm_confirm")       # interrupt 后用户确认
    g.add_conditional_edges("pm_confirm", lambda s: s.get("phase", "api_design"))
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
            interrupt_before=["pm_generate", "pm_confirm",
                              "human_confirm_arch", "human_confirm_code"]
        )
        self._thread_id = chatid
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, task_id: str, services: list[str], initial_request: str):
        """启动 SOP"""
        state = SOPState(
            task_id=task_id, chatid=self._chatid, services=services,
            cwd=self._config.get("cwd", WORK_DIR),
            mode=self._config.get("mode", "full"),
            requirements="", api_contract="", architecture="",
            code_diff="", review_feedback="",
            phase="pm_ask", arch_review_count=0, code_review_count=0,
            review_result="", human_input=initial_request, notify="",
        )
        config = {"configurable": {"thread_id": self._thread_id}}
        self._task = asyncio.create_task(self._run(state, config))

    async def resume(self, human_input: str):
        """用户回复后恢复"""
        config = {"configurable": {"thread_id": self._thread_id}}
        self._graph.update_state(config, {"human_input": human_input})
        self._task = asyncio.create_task(self._run(None, config))

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
