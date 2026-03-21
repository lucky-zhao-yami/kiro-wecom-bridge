# 多 Agent 协作系统 — 需求文档

## 业务背景

当前 bridge 是单 Agent 模式：每个 chatid 一个 kiro ACP 进程，所有任务串行处理。长任务（代码分析、编码、审查）会阻塞对话，用户体验差。

需要支持多 Agent 协作，让主 Agent 保持快速响应，长任务由工作 Agent 并行处理。

## 三种 Agent 模式

### 1. Single（当前模式）
- 一个 chatid 一个 ACP 进程
- 所有消息串行处理
- 适合：简单问答、日常对话

### 2. Delegate（委派模式）
- 主 Agent + Worker Agent 池（最多 3 个）
- 主 Agent 负责对话，长任务分发给 Worker
- Worker 独立执行，完成后更新 tasks.json
- 适合：独立任务并行执行

## Delegate 详细设计

### 架构

```
bridge 内部

DelegateSession (per chatid)
├── tasks.json                # 任务清单
├── Main Agent (ACP 进程)     # 对话 + 分发任务
├── Worker-0 (ACP 进程)       # 执行长任务
├── Worker-1 (ACP 进程)       # 执行长任务
└── Worker-2 (ACP 进程)       # 执行长任务（上限 3 个）
```

### 工作流程

1. 用户发消息 → 主 Agent 处理
2. 主 Agent 判断是长任务 → fs_write 写入 tasks.json（status=pending）
3. bridge 内部 TaskDispatcher 检测到新任务 → 分配空闲 Worker
4. Worker 执行任务，定期更新 tasks.json（progress）
5. Worker 完成 → 更新 tasks.json（status=done, result=...）
6. 主 Agent 下次被问到时 fs_read tasks.json 查看结果
7. 或 bridge 检测到任务完成 → 通过企微推送通知用户

### Worker 生命周期

- 按需创建，空闲 10 分钟后回收
- 无指定 agent，使用默认 kiro 能力
- 共享主 Agent 的 workspace

### 3. GroupChat（协作模式）
- 多 ACP 进程，每个 Agent 有独立角色（Architect/Coder/Reviewer/...）
- 共享消息列表（shared_messages.jsonl）
- Manager Agent 控制发言顺序和调度
- 用户（Human）是参与者之一，需要决策时被 @
- 支持串行对话、并行/广播、门禁检查
- 适合：需要多角色协作的复杂任务（SOP 流程）

## GroupChat 详细设计

### 架构

```
bridge 内部

ChatRoom (per chatid)
├── shared_messages.jsonl     # 共享对话记录
├── tasks.json                # 任务清单（含依赖、状态）
├── Manager (ACP 进程)        # 控制发言、调度、对用户
├── Agent-0 (ACP 进程)        # 独立角色，如 Architect
├── Agent-1 (ACP 进程)        # 独立角色，如 Coder
└── Agent-2 (ACP 进程)        # 独立角色，如 Reviewer
```

### 共享消息格式 (shared_messages.jsonl)

```json
{"seq": 1, "from": "Human", "content": "给 ec-so-service 加订单备注功能", "ts": 1774063000}
{"seq": 2, "from": "Manager", "content": "@Architect 请设计方案", "ts": 1774063005}
{"seq": 3, "from": "Architect", "content": "方案如下...", "ts": 1774063060}
{"seq": 4, "from": "Manager", "content": "@Human 两个方案请选择", "ts": 1774063065, "wait_human": true}
{"seq": 5, "from": "Human", "content": "选方案A", "ts": 1774063200}
{"seq": 6, "from": "Manager", "content": "@Coder 按方案A实现", "ts": 1774063205}
```

### 任务清单格式 (tasks.json)

```json
[
  {
    "id": "t_001",
    "seq": 1,
    "status": "done",
    "description": "架构设计",
    "depends_on": [],
    "assigned_to": "Architect",
    "created_at": 1774063000,
    "started_at": 1774063005,
    "finished_at": 1774063060,
    "progress": "已完成",
    "result": "方案A：独立表存储..."
  },
  {
    "id": "t_002",
    "seq": 2,
    "status": "running",
    "description": "编码实现",
    "depends_on": ["t_001"],
    "assigned_to": "Coder"
  },
  {
    "id": "t_003",
    "seq": 3,
    "status": "pending",
    "description": "代码审查",
    "depends_on": ["t_002"],
    "assigned_to": "Reviewer"
  }
]
```

### Manager 调度逻辑

1. **发言控制**：Manager 决定下一个发言者
   - 按 SOP 流程顺序（可配置）
   - 检查任务依赖：depends_on 都 done 才能开始
   - 无依赖的任务可并行分发

2. **串行对话**：Manager → Agent A → Manager → Agent B
3. **并行/广播**：Manager → [Agent A, Agent B, Agent C] 同时 → Manager 汇总
4. **@Human**：Manager 推送企微消息，等待用户回复写入 shared_messages.jsonl

### 门禁机制

关键节点需要检查才能继续：

| 节点 | 门禁条件 | 失败处理 |
|------|---------|---------|
| 架构设计完成 | Reviewer PASS + Human 确认 | 回退到 Architect |
| 编码完成 | 测试通过 + Reviewer PASS | 回退到 Coder |
| 最终交付 | Human 确认 | 回退到对应阶段 |

### 轮次限制

- 同一对 Agent 之间最大对话轮次：6 轮
- 超过后 Manager @Human 裁决
- 防止死循环争论

### 上下文管理

- shared_messages.jsonl 定期压缩：Manager 每 10 轮生成摘要，替换旧消息
- 每个 Agent 发言前读取 shared_messages.jsonl 获取上下文
- 每天 0 点整理到长期记忆（复用现有机制）

### 状态持久化与恢复

ChatRoom 的所有状态持久化到文件，进程随时可杀可重建：

```
wecom-sessions/{chatid}/
├── history.jsonl              # 用户对话历史
├── shared_messages.jsonl      # GroupChat 对话记录
├── tasks.json                 # 任务清单和状态
└── room_state.json            # ChatRoom 元数据（模式、阶段、轮次计数）
```

**回收时**：杀掉所有 ACP 进程，文件保留。

**重建时**：
1. 读 room_state.json 恢复模式和阶段
2. 启动 Manager，第一条 prompt 注入文件路径，Manager 自己 fs_read 恢复上下文
3. 工作 Agent 按需启动，同样读 shared_messages.jsonl 获取上下文

### 每日 0 点记忆整理（三种模式统一）

| 模式 | 当天文件 | 0 点处理 |
|------|---------|---------|
| Single | history.jsonl | → 长期记忆 → 清空 |
| Delegate | history.jsonl + tasks.json | → 长期记忆 → 清空已完成任务 |
| GroupChat | shared_messages.jsonl + tasks.json | → 长期记忆 → 清空已完成任务 |

- room_state.json 保留（元数据，不清空）
- 未完成的任务保留在 tasks.json 中，跨天继续执行

### Human 交互

- Manager @Human 时：通过企微推送消息给用户
- 用户在企微回复：bridge 写入 shared_messages.jsonl，通知 Manager 继续
- 超时：5 分钟未回复，Manager 用默认选项继续，事后通知用户
- 进度推送：每 1 分钟推送一次当前进展摘要

### 错误处理

- Agent 执行失败：自动重试 1 次
- 重试仍失败：Manager @Human 通知，等待指示
- Agent 进程崩溃：从预热池重建，重新分配任务

### ChatRoom 生命周期

- **创建时机**：用户发第一条消息时，根据 agent_mode 创建对应的 session 类型
- **GroupChat 进程启动**：Manager 进程立即启动，工作 Agent 按需启动（Manager 分配任务时才创建）
- **任务完成后**：工作 Agent 进程保留，等待下一个任务。空闲 30 分钟后回收
- **ChatRoom 销毁**：所有 Agent 空闲超过 30 分钟，整个 ChatRoom 销毁

### 用户消息路由

- **GroupChat 未激活时**：用户消息直接发给 Manager，Manager 决定是否启动 GroupChat 流程
- **GroupChat 进行中，用户主动发消息**：
  - 如果是回复 @Human 的问题 → 写入 shared_messages.jsonl，通知 Manager 继续
  - 如果是无关消息 → 发给 Manager，Manager 判断是插入当前流程还是单独回复
- **GroupChat 进行中，用户发"取消"** → Manager 暂停流程，@Human 确认是否终止

### 并行结果汇总

- 广播给多个 Agent 后，**等全部完成**再由 Manager 汇总
- 单个 Agent 超时（5 分钟无输出）→ 标记该 Agent 失败，其他结果正常汇总
- Manager 汇总时把所有 Agent 的回复拼接写入 shared_messages.jsonl

### 模式切换

- 支持动态切换：用户可以对主 Agent 说"启动 groupchat 模式"
- 主 Agent 通过 fs_write 修改 chatid 的运行时配置
- bridge 检测到配置变化后切换模式
- 切换时保留对话历史（history.jsonl），新模式可以读取

### 资源限制

| 资源 | 限制 |
|------|------|
| 单个 ChatRoom 最大 Agent 数 | 6（1 Manager + 5 工作 Agent） |
| 系统总 ACP 进程上限 | 20（含预热池） |
| 预热池大小 | 3（仅 single 模式使用） |
| Worker 空闲回收时间 | 30 分钟 |
| ChatRoom 空闲回收时间 | 30 分钟 |

## Agent 角色定义

复用 `.kiro/agents/` 下已有的 agent 定义，每个 ACP 进程通过 `--agent` 参数指定角色：

| Agent | --agent 参数 | 角色 |
|-------|-------------|------|
| Manager | orchestrator-agent | 调度、对用户、控制流程 |
| Architect | architect-agent | 架构设计 |
| API Designer | api-designer-agent | 接口契约设计 |
| Coder | coder-agent | 编码实现 |
| Reviewer | reviewer-agent | 代码审查 |
| QA | qa-agent | 测试编写 |
| Doc Engineer | doc-engineer-agent | 文档生成 |

不需要额外编写角色 prompt，kiro 自动加载 `.kiro/agents/{name}/prompt.md`。

## 配置

channels.json 新增 agent_mode 和 agents 字段：

```json
{
  "dm_ZhaoXingPing": {
    "mode": "full",
    "agent_mode": "groupchat",
    "manager": "orchestrator-agent",
    "agents": ["architect-agent", "coder-agent", "reviewer-agent", "qa-agent"]
  },
  "CHAT_ID_OPS": {
    "mode": "safe",
    "agent_mode": "delegate",
    "max_workers": 3
  },
  "default": {
    "mode": "safe",
    "agent_mode": "single"
  }
}
```

## 核心模块

| 模块 | 职责 |
|------|------|
| ChatRoom | 管理一组 ACP 进程 + 共享消息文件 |
| TaskDispatcher | 任务调度：依赖检查、并行分发、门禁 |
| Manager 逻辑 | 发言控制、摘要压缩、进度推送 |
| Human 交互 | @Human 推送企微、接收回复、超时处理 |

## 不涉及

- 不改其他微服务
- 不引入外部框架（AutoGen 等）
- 不改现有 Single 模式的逻辑

## 验收标准

- AC-1: Single 模式不受影响，行为和现在一致
- AC-2: Delegate 模式下，主 Agent 分发任务后能继续对话不卡
- AC-3: GroupChat 模式下，多 Agent 能串行对话协作
- AC-4: GroupChat 模式下，无依赖任务能并行执行
- AC-5: @Human 能推送企微，用户回复能继续流程
- AC-6: 5 分钟 Human 超时后自动继续
- AC-7: 同一对 Agent 超过 6 轮自动 @Human 裁决
- AC-8: 门禁节点检查失败能回退
- AC-9: 每 1 分钟推送进度摘要
- AC-10: Agent 失败自动重试 1 次
