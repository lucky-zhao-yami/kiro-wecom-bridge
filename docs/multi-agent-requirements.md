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

### Human 交互

- Manager @Human 时：通过企微推送消息给用户
- 用户在企微回复：bridge 写入 shared_messages.jsonl，通知 Manager 继续
- 超时：5 分钟未回复，Manager 用默认选项继续，事后通知用户
- 进度推送：每 1 分钟推送一次当前进展摘要

### 错误处理

- Agent 执行失败：自动重试 1 次
- 重试仍失败：Manager @Human 通知，等待指示
- Agent 进程崩溃：从预热池重建，重新分配任务

## 配置

channels.json 新增 agent_mode 和 agents 字段：

```json
{
  "dm_ZhaoXingPing": {
    "mode": "full",
    "agent_mode": "groupchat",
    "agents": ["architect", "coder", "reviewer"]
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
