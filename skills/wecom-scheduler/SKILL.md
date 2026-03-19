---
name: wecom-scheduler
description: 定时任务管理。当用户要求设置定时任务、周期性执行某个操作、定时提醒、每天/每周自动执行时使用。支持创建、查看、修改、删除、暂停/恢复定时任务。
---

# 定时任务调度

通过 bridge API 管理基于系统 crontab 的定时任务。每个任务到时间后会自动向指定 chatid 发送 prompt，由对应的 kiro 执行并将结果推回企微。

## API 地址

`http://localhost:8900/scheduler/jobs`

## 操作

### 创建任务

```bash
curl -s -X POST http://localhost:8900/scheduler/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "cron": "0 9 * * *",
    "chatid": "dm_ZhaoXingPing",
    "prompt": "检查今天有没有待处理的 OP 任务",
    "description": "每天9点检查OP"
  }'
```

参数说明：
- `cron`: crontab 表达式（分 时 日 月 周），如 `0 9 * * 1-5` 表示工作日每天9点
- `chatid`: 目标 chatid，决定由哪个 kiro agent 执行（按 channels.json 路由）
- `prompt`: 到时间后发给 kiro 的指令
- `bot_index`: 可选，多机器人时指定用哪个 channel，默认 0
- `description`: 可选，任务描述

### 查看所有任务

```bash
curl -s http://localhost:8900/scheduler/jobs | python3 -m json.tool
```

### 查看单个任务

```bash
curl -s http://localhost:8900/scheduler/jobs/{id}
```

### 修改任务

```bash
curl -s -X PATCH http://localhost:8900/scheduler/jobs/{id} \
  -H "Content-Type: application/json" \
  -d '{"cron": "30 8 * * *"}'
```

### 暂停任务

```bash
curl -s -X PATCH http://localhost:8900/scheduler/jobs/{id} \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

### 恢复任务

```bash
curl -s -X PATCH http://localhost:8900/scheduler/jobs/{id} \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

### 删除任务

```bash
curl -s -X DELETE http://localhost:8900/scheduler/jobs/{id}
```

## Cron 表达式速查

| 表达式 | 含义 |
|--------|------|
| `0 9 * * *` | 每天 9:00 |
| `0 9 * * 1-5` | 工作日 9:00 |
| `30 8,17 * * *` | 每天 8:30 和 17:30 |
| `0 */2 * * *` | 每 2 小时 |
| `0 9 * * 1` | 每周一 9:00 |
| `0 9 1 * *` | 每月 1 号 9:00 |

## chatid 规则

- 私聊用户：`dm_用户名`（如 `dm_ZhaoXingPing`）
- 群聊：使用企微群的 chatid
- chatid 决定了由哪个 kiro agent 处理任务（按 channels.json 中的路由配置）

## 使用场景示例

用户说："帮我设一个定时任务，每天早上9点检查 OP 待办"

→ 调用创建任务 API：
```bash
curl -s -X POST http://localhost:8900/scheduler/jobs \
  -H "Content-Type: application/json" \
  -d '{"cron":"0 9 * * 1-5","chatid":"dm_ZhaoXingPing","prompt":"检查 OpenProject 中我的待办任务，列出未完成的","description":"工作日9点检查OP待办"}'
```
