# 多 Agent 架构设计 — Phase 1: Delegate 模式

## 修改范围

| 文件 | 操作 | 说明 |
|------|------|------|
| agents/__init__.py | 新增 | 模块初始化 |
| agents/task_manager.py | 新增 | 任务文件读写 + 状态管理 |
| agents/delegate.py | 新增 | DelegateSession：主 Agent + Worker 池 |
| channel.py | 修改 | 根据 agent_mode 路由到不同模块 |
| main.py | 修改 | 任务完成检测 + 企微推送 |

agent 调度逻辑全部在 `agents/` 目录下，不和 bridge 核心耦合。每个模式独立目录。

```
kiro-wecom-bridge/
├── main.py                # FastAPI 入口
├── ws_client.py           # 企微 WebSocket
├── channel.py             # 消息路由（纯路由，根据 agent_mode 分发）
├── media.py               # 媒体处理（图片/语音/文件的下载、AES解密、保存）
├── stream.py              # StreamSegmenter 流式分段
├── guard.py               # 安全防护
├── agents/                # Agent 调度模块
│   ├── __init__.py        # 公共接口、AgentSession 基类
│   ├── task_manager.py    # 任务文件读写（delegate/groupchat 共用）
│   ├── process.py         # KiroProcess（三种模式共用）
│   ├── single/
│   │   ├── __init__.py
│   │   └── session.py     # SingleSession（现有 ProcessPool 逻辑）
│   ├── delegate/
│   │   ├── __init__.py
│   │   └── session.py     # DelegateSession
│   └── groupchat/
│       ├── __init__.py
│       ├── session.py     # GroupChatSession
│       └── manager.py     # Manager 调度逻辑（Phase 2）
└── ...
```

### 重构（随本次一起做）

| 原文件 | 拆分到 | 说明 |
|--------|--------|------|
| channel.py 图片/语音/文件处理 | media.py | 下载、AES 解密、保存、格式检测 |
| channel.py StreamSegmenter | stream.py | 流式分段逻辑 |
| session.py KiroProcess | agents/process.py | 三种模式共用 |
| session.py ProcessPool | agents/single/session.py | single 模式专用 |
| session.py 历史/摘要/回收 | agents/process.py | 跟随 KiroProcess |

重构后 channel.py 只剩消息路由（~100 行），session.py 删除。

## 核心类设计

### 1. TaskManager (task_manager.py)

纯文件操作，管理 tasks.json 的读写：

```python
class TaskManager:
    def __init__(self, session_dir: str)
    def create_task(self, description: str, prompt: str, depends_on: list[str] = []) -> dict
    def list_tasks(self) -> list[dict]
    def get_task(self, task_id: str) -> dict | None
    def update_task(self, task_id: str, **kwargs) -> dict | None
    def get_pending_tasks(self) -> list[dict]       # 依赖已满足的 pending 任务
    def get_running_tasks(self) -> list[dict]
    def has_active_tasks(self) -> bool               # 有 running 或 pending
```

tasks.json 路径：`wecom-sessions/{chatid}/tasks.json`

### 2. DelegateSession (delegate.py)

管理一个 chatid 的主 Agent + Worker 池：

```python
class DelegateSession:
    MAX_WORKERS = 3

    def __init__(self, chatid: str, chat_config: dict, ws: WsClient)
    
    # 主 Agent 对话（用户消息直接走这里）
    async def send_to_main(self, text: str, on_chunk=None) -> str
    
    # 内部：检测 tasks.json 变化，分发任务给 worker
    async def dispatch_loop(self)
    
    # 内部：worker 完成后推送企微
    async def on_task_done(self, task: dict)
    
    # 生命周期
    async def start(self)
    async def stop(self)
    def can_recycle(self) -> bool  # 无活跃任务 + 不等 human + 30min 空闲
```

内部结构：
```
DelegateSession
├── main_proc: KiroProcess          # 主 Agent（对话用）
├── workers: dict[str, KiroProcess] # task_id → worker 进程
├── task_mgr: TaskManager           # 任务文件管理
└── ws: WsClient                    # 企微推送用
```

### 3. ProcessPool 改造 (session.py)

现有 ProcessPool 保持不变（single 模式用），DelegateSession 内部自己管理 KiroProcess。

KiroProcess 不需要改动——它已经是独立的 ACP 进程封装。

## 工作流程

```
用户发消息
    ↓
Channel._on_message
    ↓ 检查 agent_mode
    ├── "single" → 现有逻辑（ProcessPool）
    └── "delegate" → DelegateSession.send_to_main
                        ↓
                    主 Agent 处理消息
                        ↓ 如果是长任务
                    主 Agent fs_write tasks.json（status=pending）
                        ↓
                    DelegateSession.dispatch_loop 检测到新任务
                        ↓ 依赖检查通过
                    创建/复用 Worker KiroProcess
                        ↓
                    Worker 执行任务（独立 ACP 进程）
                        ↓ 完成
                    Worker fs_write tasks.json（status=done）
                        ↓
                    dispatch_loop 检测到完成
                        ↓
                    通过 ws.send_msg 推送企微通知用户
```

## dispatch_loop 设计

```python
async def dispatch_loop(self):
    """每 5 秒检查 tasks.json，分发 pending 任务"""
    while self._running:
        await asyncio.sleep(5)
        pending = self.task_mgr.get_pending_tasks()
        for task in pending:
            if len(self.workers) >= self.MAX_WORKERS:
                break  # worker 池满，等下次
            await self._assign_task(task)
        # 检查已完成的任务
        for task_id, worker in list(self.workers.items()):
            task = self.task_mgr.get_task(task_id)
            if task and task["status"] == "done":
                await self.on_task_done(task)
                del self.workers[task_id]
```

## Channel 路由改造

```python
# channel.py _process_and_reply 改造

async def _process_and_reply(self, req_id, stream_id, chatid, text):
    chat_cfg = self._get_chat_config(chatid)
    agent_mode = chat_cfg.get("agent_mode", "single")
    
    if agent_mode == "delegate":
        session = await self._get_delegate_session(chatid, chat_cfg)
        seg = StreamSegmenter(self.ws, req_id, stream_id)
        await self.ws.send_stream(req_id, stream_id, "🤔", finish=False)
        result = await session.send_to_main(text, on_chunk=seg.feed)
        if result:
            await seg.finish()
    else:
        # 现有 single 逻辑
        ...
```

## 主 Agent 怎么知道要分发任务

不需要 bridge 判断——**主 Agent 自己决定**。在主 Agent 的 preamble 里加一段：

```
当用户要求执行长任务（代码分析、编码、文件处理等预计超过 30 秒的任务）时：
1. 不要自己执行，写入任务清单：
   fs_write tasks.json，追加一条 {"status": "pending", "prompt": "具体指令", ...}
2. 回复用户："已安排，我会跟进进度。"
3. 用户问进度时，fs_read tasks.json 查看状态回复。
```

## 文件结构

```
wecom-sessions/{chatid}/
├── history.jsonl       # 用户对话历史（主 Agent）
├── tasks.json          # 任务清单（主 Agent 写，Worker 更新，bridge 监控）
└── workers/
    ├── t_001/          # Worker 工作目录（session 文件）
    └── t_002/
```

## 不改动

- KiroProcess 类不改
- ProcessPool 类不改（single 模式继续用）
- 现有 single 模式的所有逻辑不变
- ws_client.py 不改
- guard.py 不改
