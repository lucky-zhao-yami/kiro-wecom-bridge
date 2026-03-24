# Teams 模式需求文档

## 概述

新增 `teams` agent_mode，参考 Claude Code Agent Teams 架构，实现 Lead + Teammates 协作模式。与 groupchat 的核心区别：任务驱动而非指令驱动，teammates 之间可直接通信，支持并行执行。

## 架构

```
用户(企微) ↔ Bridge ↔ Lead Agent (完整能力，也能干活)
                          ↓ 创建/管理 shared task list
                    ┌─────┼─────┐
               Teammate A  B    C    ← 独立 ACP 进程
                    │      │    │
                    └──────┼────┘
                      共享 task list (JSONL + 文件锁)
                      + mailbox (JSONL，直接通信)
```

## 核心组件

### 1. Shared Task List

文件：`{session_dir}/tasks.jsonl`

```json
{"id": "t1", "title": "设计分表路由方案", "status": "pending", "assignee": null, "depends_on": [], "created_by": "lead", "result": null}
{"id": "t2", "title": "实现 Mapper XML 改造", "status": "pending", "assignee": null, "depends_on": ["t1"], "created_by": "lead", "result": null}
{"id": "t3", "title": "编写单元测试", "status": "pending", "assignee": null, "depends_on": ["t2"], "created_by": "lead", "result": null}
```

状态流转：`pending → in_progress → completed / failed`

规则：
- Lead 创建任务，设置依赖关系
- Teammate 自己 claim pending 且无未完成依赖的任务
- 用文件锁防止多个 teammate 同时 claim 同一任务
- Teammate 完成后更新 status + result

### 2. Mailbox（Agent 间通信）

文件：`{session_dir}/mailbox.jsonl`

```json
{"from": "architect", "to": "lead", "content": "方案设计完成，建议用 ${year} 动态表名", "ts": 1234567890}
{"from": "lead", "to": "coder", "content": "architect 的方案已通过，请按此实现", "ts": 1234567891}
{"from": "coder", "to": "reviewer", "content": "代码已提交，请审查 OrderDao 的改动", "ts": 1234567892}
```

规则：
- 任何 agent 可以给任何 agent 发消息
- 每个 agent 启动时读取发给自己的未读消息
- Lead 可以 broadcast 给所有 teammates

### 3. Lead Agent

- 从 ProcessPool 分配或新建，使用配置中指定的 agent（如 orchestrator-agent）
- **Lead 也能干活**：可以自己执行任务（如需求澄清、方案评审），不只是调度
- 收到用户消息后：
  1. 如果是新需求 → 拆解为 tasks，写入 task list
  2. 如果是对某个 teammate 的回复 → 通过 mailbox 转发
  3. 如果是简单问题 → 自己回答
- 定期检查 task list，向用户推送进度

### 4. Teammate

- 独立 ACP 进程，按需创建，完成后销毁
- 启动时收到：自己的 agent prompt + 当前 task 描述 + mailbox 中发给自己的消息
- 工作循环：
  1. 从 task list claim 一个可执行的任务
  2. 执行任务
  3. 更新 task status + result
  4. 通过 mailbox 通知相关 agent
  5. 检查是否有新的可 claim 任务，有则继续，无则退出
- **不共享对话历史**：每个 teammate 只看到自己的 task 和 mailbox 消息

### 5. TeamsSession（Bridge 侧）

```python
class TeamsSession:
    lead: KiroProcess           # Lead agent 进程
    teammates: dict[str, KiroProcess]  # name → process
    task_list: TaskList         # 共享任务列表
    mailbox: Mailbox            # 消息系统
    
    async def send_from_human(text, on_chunk) → str
    async def _poll_loop()      # 后台轮询：检查任务完成、推送进度
    async def _spawn_teammate(agent_name, task) → KiroProcess
    async def stop()
```

## 与 GroupChat 的区别

| 维度 | GroupChat | Teams |
|------|-----------|-------|
| 调度方式 | Manager 通过 @指令串行调度 | Lead 创建 task list，teammates 自己 claim |
| 通信 | 全部经过 Manager | Mailbox 直接通信 |
| 并行 | 不支持 | 无依赖任务并行执行 |
| Lead 能力 | Manager 只调度不干活 | Lead 也能执行任务 |
| 上下文 | shared_messages 共享全部历史 | 各自独立，通过 task + mailbox 传递 |
| 可靠性 | 依赖 LLM 输出 @指令（脆弱） | 代码控制任务流转（确定性） |

## channels.json 配置

```json
{
  "dm_ZhaoXingPing": {
    "agent_mode": "teams",
    "lead": "team-lead",
    "agents": ["architect-agent", "coder-agent", "reviewer-agent", "qa-agent"],
    "max_parallel": 3,
    "cwd": "/mnt/d/workspace/all",
    "mode": "full"
  }
}
```

## 用户交互流程

### 场景：用户发送开发需求

```
用户: "给 so_order_goods_ext 做按年分表"
  ↓
Lead: 理解需求，拆解任务：
  t1: 分析现有代码中 so_order_goods_ext 的使用方式 (architect)
  t2: 设计分表路由方案 (architect, depends: t1)
  t3: 审查方案 (reviewer, depends: t2)
  t4: 实现代码改造 (coder, depends: t3)
  t5: 审查代码 (reviewer, depends: t4)
  ↓
Bridge 推送企微: "📋 已创建 5 个任务，开始执行..."
  ↓
architect teammate 自动 claim t1，开始分析
  ↓ (完成后)
architect teammate 自动 claim t2，设计方案
  ↓ (完成后，通过 mailbox 通知 lead)
reviewer teammate 自动 claim t3，审查方案
  ↓ REJECT → 新建 t2.1 修改方案，architect claim
  ↓ PASS → 
coder teammate 自动 claim t4
  ↓ (完成后)
reviewer teammate 自动 claim t5
  ↓ PASS →
Lead 汇总结果推送企微: "✅ 所有任务完成"
```

### 进度推送

每个任务状态变更时推送企微：
```
⚙️ [t1] architect-agent 正在分析代码...
✅ [t1] 代码分析完成
⚙️ [t2] architect-agent 正在设计方案...
✅ [t2] 方案设计完成
⚙️ [t3] reviewer-agent 正在审查方案...
❌ [t3] 方案审查未通过，需要修改
⚙️ [t2.1] architect-agent 正在修改方案...
```

## 实现计划

### Phase 1: 基础设施
- [ ] `TaskList` 类：JSONL 读写 + 文件锁 + claim/complete/fail
- [ ] `Mailbox` 类：JSONL 读写 + 按收件人过滤
- [ ] `TeamsSession` 类：基本框架

### Phase 2: Lead + Teammate 生命周期
- [ ] Lead 启动，接收用户消息，拆解任务
- [ ] Teammate 按需 spawn，执行任务，完成后销毁
- [ ] 后台 poll loop：检查任务状态，spawn teammate，推送进度

### Phase 3: 通信与协调
- [ ] Mailbox 集成到 teammate prompt
- [ ] 任务依赖自动解锁
- [ ] Lead 汇总结果推送用户

### Phase 4: 企微交互
- [ ] 文字进度推送（任务状态变更时）
- [ ] 用户中途干预（暂停/取消/修改任务）
- [ ] @Human 门禁（某些任务需要用户确认才继续）

### Phase 5: 进度可视化（后续）
- [ ] 生成 HTML 进度页面
- [ ] 上传到云服务器，返回公网 URL
- [ ] 任务状态变更时自动更新页面
- [ ] 企微发送链接，用户点击查看实时进度

## 关键设计决策

### Q: Teammate 怎么知道自己该 claim 哪个任务？
A: Bridge 的 poll loop 检查 task list，找到 pending + 无未完成依赖的任务，根据任务的 `agent` 字段 spawn 对应 teammate 并传入任务描述。不是 teammate 自己去抢。

### Q: Lead 用什么 agent？
A: 新建 `team-lead` agent，prompt 里定义：可用 teammates 列表、task list JSON 格式、mailbox 使用方式、什么时候自己干什么时候派任务。Lead 有完整工具（code/grep/fs_read），可以自己分析代码再拆任务。

### Q: Teammate 需要区分角色吗？
A: 需要。每个 teammate 用配置中指定的 agent 启动（architect/coder/reviewer），不同角色有不同的 prompt 和工具权限。任务的 `agent` 字段由 Lead 在创建任务时指定。

### Q: 任务失败怎么办？
A: Lead 收到失败通知后决定：重试、创建新任务、或通知用户。

### Q: 怎么防止内存爆炸？
A: Teammate 完成任务后立即销毁进程。同时运行的 teammate 数量上限（如 3 个）。

### Q: 和 SOP 流程怎么结合？
A: Lead 的 prompt 里定义 SOP 流程，Lead 按流程创建任务和依赖关系。Bridge 不管流程，只管任务执行。
