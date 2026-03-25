"""SOP 开发流程 — LangGraph 状态机

图结构:
  pm → api_design → architect → arch_review
    ↗ (REJECT, ≤3)              ↓ (PASS)
  architect ←────────── arch_review
                                ↓
                         human_confirm_arch
                                ↓
                             coder → code_review
                           ↗ (REJECT, ≤6)    ↓ (PASS)
                         coder ←──── code_review
                                          ↓
                                   human_confirm_code
                                          ↓
                                       deliver
"""

import asyncio, json, logging, os, time
from typing import TypedDict, Literal, Annotated
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
    review_result: str  # PASS / REJECT
    human_input: str    # human-in-the-loop 输入
    messages: list[str] # 推送给用户的消息队列


def _task_dir(task_id: str) -> str:
    return os.path.join(AI_WORKSPACE, task_id)


def _ensure_dirs(task_id: str):
    base = _task_dir(task_id)
    for sub in ["01_pm_docs", "02_api_contracts", "03_architecture",
                "04_review_logs", "05_codebase_context", "06_deliverables"]:
        os.makedirs(os.path.join(base, sub), exist_ok=True)


async def _run_agent(state: SOPState, agent_name: str, prompt: str) -> str:
    """启动一个 KiroProcess 执行任务并返回结果"""
    chatid = state["chatid"]
    session_dir = os.path.join(_task_dir(state["task_id"]), "agents", agent_name)
    os.makedirs(session_dir, exist_ok=True)
    proc = KiroProcess(
        f"{chatid}/sop/{agent_name}", session_dir,
        agent=agent_name, cwd=state["cwd"], mode=state["mode"])
    try:
        await proc.start()
        result = await proc.send(prompt, timeout=600)
        return result or ""
    finally:
        await proc.close()


# ── 节点函数 ──

async def pm_node(state: SOPState) -> dict:
    """Phase 1: 需求澄清 — Orchestrator 亲自执行"""
    task_id = state["task_id"]
    _ensure_dirs(task_id)

    # 如果已有需求文档（human 确认后回来的），直接过
    if state.get("requirements"):
        return {"phase": "api_design"}

    prompt = (
        f"你现在是 PM，任务 {task_id}，涉及服务: {', '.join(state['services'])}。\n"
        f"用户的需求描述: {state.get('human_input', '')}\n\n"
        f"请按信息采集清单分析，列出所有需要确认的问题，打包提问。\n"
        f"如果信息已经充分，直接生成需求文档。"
    )
    result = await _run_agent(state, "orchestrator-agent", prompt)

    # 写入文件
    doc_path = os.path.join(_task_dir(task_id), "01_pm_docs", "requirements.md")
    with open(doc_path, "w") as f:
        f.write(result)

    return {
        "requirements": result,
        "phase": "pm_confirm",
        "messages": [f"📋 需求文档已生成，请确认:\n\n{result[:1000]}"]
    }


async def api_design_node(state: SOPState) -> dict:
    """Phase 2a: API 设计"""
    prompt = (
        f"任务: {state['task_id']}, 服务: {', '.join(state['services'])}\n\n"
        f"需求文档:\n{state['requirements']}\n\n"
        f"请设计 API 接口契约（OpenAPI 3.0 YAML）。"
    )
    result = await _run_agent(state, "api-designer-agent", prompt)

    doc_path = os.path.join(_task_dir(state["task_id"]), "02_api_contracts", "api.yaml")
    with open(doc_path, "w") as f:
        f.write(result)

    return {"api_contract": result, "phase": "architect"}


async def architect_node(state: SOPState) -> dict:
    """Phase 2b: 架构设计"""
    prompt = (
        f"任务: {state['task_id']}, 服务: {', '.join(state['services'])}\n\n"
        f"需求文档:\n{state['requirements']}\n\n"
        f"API 契约:\n{state['api_contract']}\n\n"
    )
    if state.get("review_feedback"):
        prompt += f"审查意见（请修改）:\n{state['review_feedback']}\n\n"
    prompt += "请制定架构规范。"

    result = await _run_agent(state, "architect-agent", prompt)

    doc_path = os.path.join(_task_dir(state["task_id"]), "03_architecture", "arch.md")
    with open(doc_path, "w") as f:
        f.write(result)

    return {"architecture": result, "phase": "arch_review", "review_feedback": ""}


async def arch_review_node(state: SOPState) -> dict:
    """Phase 2.5: 架构审查"""
    count = state.get("arch_review_count", 0) + 1
    prompt = (
        f"任务: {state['task_id']}\n\n"
        f"需求文档:\n{state['requirements']}\n\n"
        f"API 契约:\n{state['api_contract']}\n\n"
        f"架构规范:\n{state['architecture']}\n\n"
        f"请审查架构规范，回复 PASS 或 REJECT + 修改意见。"
    )
    result = await _run_agent(state, "reviewer-agent", prompt)

    # 写审查日志
    log_path = os.path.join(_task_dir(state["task_id"]), "04_review_logs", f"arch-review-{count}.md")
    with open(log_path, "w") as f:
        f.write(result)

    is_pass = "PASS" in result.upper().split("\n")[0]
    return {
        "arch_review_count": count,
        "review_result": "PASS" if is_pass else "REJECT",
        "review_feedback": result if not is_pass else "",
        "phase": "arch_review_route",
    }


def arch_review_route(state: SOPState) -> str:
    """架构审查路由"""
    if state["review_result"] == "PASS":
        return "human_confirm_arch"
    if state["arch_review_count"] >= 3:
        return "human_confirm_arch"  # 超限也交给人
    return "architect"


async def coder_node(state: SOPState) -> dict:
    """Phase 3: 编码"""
    prompt = (
        f"任务: {state['task_id']}, 服务: {', '.join(state['services'])}\n\n"
        f"需求文档:\n{state['requirements']}\n\n"
        f"API 契约:\n{state['api_contract']}\n\n"
        f"架构规范:\n{state['architecture']}\n\n"
    )
    if state.get("review_feedback"):
        prompt += f"审查意见（请修复）:\n{state['review_feedback']}\n\n"
    prompt += "请按架构规范编写代码。"

    result = await _run_agent(state, "coder-agent", prompt)
    return {"code_diff": result, "phase": "code_review", "review_feedback": ""}


async def code_review_node(state: SOPState) -> dict:
    """Phase 3.5: 代码审查"""
    count = state.get("code_review_count", 0) + 1
    prompt = (
        f"任务: {state['task_id']}\n\n"
        f"架构规范:\n{state['architecture']}\n\n"
        f"代码变更:\n{state['code_diff']}\n\n"
        f"请审查代码，回复 PASS 或 REJECT + 修改意见。"
    )
    result = await _run_agent(state, "reviewer-agent", prompt)

    log_path = os.path.join(_task_dir(state["task_id"]), "04_review_logs", f"code-review-{count}.md")
    with open(log_path, "w") as f:
        f.write(result)

    is_pass = "PASS" in result.upper().split("\n")[0]
    return {
        "code_review_count": count,
        "review_result": "PASS" if is_pass else "REJECT",
        "review_feedback": result if not is_pass else "",
        "phase": "code_review_route",
    }


def code_review_route(state: SOPState) -> str:
    if state["review_result"] == "PASS":
        return "human_confirm_code"
    if state["code_review_count"] >= 6:
        return "human_confirm_code"
    return "coder"


async def deliver_node(state: SOPState) -> dict:
    """Phase 4: 交付"""
    prompt = (
        f"任务: {state['task_id']}, 服务: {', '.join(state['services'])}\n\n"
        f"请生成接口文档和测试发布文档。\n"
        f"需求文档: {_task_dir(state['task_id'])}/01_pm_docs/requirements.md\n"
        f"API 契约: {_task_dir(state['task_id'])}/02_api_contracts/api.yaml\n"
        f"架构规范: {_task_dir(state['task_id'])}/03_architecture/arch.md"
    )
    result = await _run_agent(state, "doc-engineer-agent", prompt)

    doc_path = os.path.join(_task_dir(state["task_id"]), "06_deliverables", "summary.md")
    with open(doc_path, "w") as f:
        f.write(result)

    return {
        "phase": "done",
        "messages": [f"✅ 任务 {state['task_id']} 开发完成！\n\n产出目录: {_task_dir(state['task_id'])}"]
    }


# ── human-in-the-loop 节点（interrupt_before） ──

async def human_confirm_arch(state: SOPState) -> dict:
    """等待用户确认架构"""
    # 用户输入通过 graph.update_state 注入 human_input
    user_input = state.get("human_input", "").strip().lower()
    if user_input in ("确认", "ok", "pass", "yes", "通过"):
        return {"phase": "coder", "human_input": ""}
    elif user_input:
        # 用户有修改意见，回到 architect
        return {"review_feedback": state["human_input"], "phase": "architect", "human_input": ""}
    # 没有输入，推送消息等待
    return {"messages": [f"🏗️ 架构审查已完成（{state['arch_review_count']}轮），请确认架构设计或提出修改意见。"]}


async def human_confirm_code(state: SOPState) -> dict:
    """等待用户确认代码"""
    user_input = state.get("human_input", "").strip().lower()
    if user_input in ("确认", "ok", "pass", "yes", "通过"):
        return {"phase": "deliver", "human_input": ""}
    elif user_input:
        return {"review_feedback": state["human_input"], "phase": "coder", "human_input": ""}
    return {"messages": [f"🔍 代码审查已完成（{state['code_review_count']}轮），请确认或提出修改意见。"]}


# ── 构建图 ──

def build_sop_graph() -> StateGraph:
    g = StateGraph(SOPState)

    # 节点
    g.add_node("pm", pm_node)
    g.add_node("api_design", api_design_node)
    g.add_node("architect", architect_node)
    g.add_node("arch_review", arch_review_node)
    g.add_node("human_confirm_arch", human_confirm_arch)
    g.add_node("coder", coder_node)
    g.add_node("code_review", code_review_node)
    g.add_node("human_confirm_code", human_confirm_code)
    g.add_node("deliver", deliver_node)

    # 边
    g.set_entry_point("pm")
    g.add_edge("pm", "api_design")
    g.add_edge("api_design", "architect")
    g.add_edge("architect", "arch_review")
    g.add_conditional_edges("arch_review", arch_review_route)
    g.add_edge("human_confirm_arch", "coder")
    g.add_edge("coder", "code_review")
    g.add_conditional_edges("code_review", code_review_route)
    g.add_edge("human_confirm_code", "deliver")
    g.add_edge("deliver", END)

    return g


class SOPSession:
    """SOP 会话 — 管理一个 LangGraph 实例"""

    def __init__(self, chatid: str, chat_config: dict, ws):
        self._chatid = chatid
        self._ws = ws
        self._config = chat_config
        self._checkpointer = MemorySaver()
        self._graph = build_sop_graph().compile(
            checkpointer=self._checkpointer,
            interrupt_before=["human_confirm_arch", "human_confirm_code"]
        )
        self._thread_id = chatid
        self._running = False

    async def start(self, task_id: str, services: list[str], initial_request: str):
        """启动 SOP 流程"""
        state = SOPState(
            task_id=task_id,
            chatid=self._chatid,
            services=services,
            cwd=self._config.get("cwd", WORK_DIR),
            mode=self._config.get("mode", "full"),
            requirements="",
            api_contract="",
            architecture="",
            code_diff="",
            review_feedback="",
            phase="pm",
            arch_review_count=0,
            code_review_count=0,
            review_result="",
            human_input=initial_request,
            messages=[],
        )
        self._running = True
        config = {"configurable": {"thread_id": self._thread_id}}
        asyncio.create_task(self._run(state, config))

    async def resume(self, human_input: str):
        """用户回复后恢复流程"""
        config = {"configurable": {"thread_id": self._thread_id}}
        self._graph.update_state(config, {"human_input": human_input})
        asyncio.create_task(self._run(None, config))

    async def _run(self, state, config):
        """后台执行图"""
        chat_type = 1 if self._chatid.startswith("dm_") else 2
        try:
            if state:
                result = await asyncio.to_thread(self._graph.invoke, state, config)
            else:
                result = await asyncio.to_thread(self._graph.invoke, None, config)

            # 推送消息
            for msg in (result or {}).get("messages", []):
                await self._ws.send_msg(self._chatid, chat_type, msg[:1500])

        except Exception as e:
            log.error("SOP 执行异常 chatid=%s: %s", self._chatid, e)
            await self._ws.send_msg(self._chatid, chat_type, f"❌ SOP 异常: {e}")
        finally:
            self._running = False

    @property
    def running(self) -> bool:
        return self._running
