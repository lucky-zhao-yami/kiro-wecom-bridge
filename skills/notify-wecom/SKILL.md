---
name: "notify-wecom"
description: "通过企微 AI Bot 发送消息通知。当需要主动通知用户（如架构审查通过、代码审查完成、部署提醒等）时使用。"
---

# 企微消息通知

通过 bridge 的 HTTP API 向企微群/私聊发送消息。

## 发送消息

```bash
curl -s -X POST http://localhost:8900/send \
  -H "Content-Type: application/json" \
  -d '{"chatid": "{chatid}", "content": "消息内容（支持 markdown）"}'
```

## 参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| chatid | string | 否 | dm_ZhaoXingPing | 目标群/私聊 ID |
| content | string | 是 | - | 消息内容，支持企微 markdown |
| chat_type | int | 否 | 2 | 1=单聊 2=群聊 |
| bot_index | int | 否 | 0 | 多机器人时指定用哪个 |

## 使用场景

- SOP 流程中通知用户：架构审查通过、代码审查完成、需要部署等
- 定时任务执行结果通知
- 告警转发

## chatid 获取

- 企微场景：从消息上下文中获取
- 命令行场景：需要预先知道目标 chatid
