---
name: "wecom-memory"
description: "长期记忆存储。当你不确定某个信息、需要回忆之前的对话内容、遇到不认识的人名/项目名/服务名、或者对话中出现了值得长期记住的新信息时使用。每次收到用户消息时，如果涉及具体的人、项目、服务、决策，都应该先搜索记忆获取上下文。"
---

# 长期记忆

你的持久化记忆系统。每个对话（chatid）有独立的记忆数据库。

## 什么时候该用

**在决定调用记忆操作之前，必须先在 `<thought>` 标签内完成元认知思考：**

```
<thought>
1. 意图澄清：用户真的在找特定信息，还是在寻求启发/讨论？
2. 路径规划：我需要搜几次？用什么关键词？
3. 置信度预估：我记得存过这个吗？如果没有，我该如何体面地告知用户？
</thought>
```

**主动搜索（收到消息时）**：
- 用户提到人名、项目名、服务名 → 先 search，看看之前知道什么
- 用户问"之前说过什么"、"上次聊的" → search
- 你不确定某个信息 → search 看看记忆里有没有
- 搜索无结果时，坦诚告知"记忆中没有相关信息"，不要编造

**主动保存（回复完成后）**：
- 对话中出现了新的重要事实（人员职责、技术决策、用户偏好、项目进展）
- 用户纠正了你的错误认知 → save_entity 更新
- 建立了新的关联（谁负责什么、什么依赖什么）→ save_relation

**不需要保存的**：闲聊、临时调试、一次性查询

## 操作

### search — 搜索记忆

```bash
MEMORY_CHATID="{chatid}" /mnt/d/code/yami/kiro-wecom-bridge/.venv/bin/python3 /mnt/d/code/yami/kiro-wecom-bridge/memory_cli.py search '{"query": "关键词"}'
```

每次只搜一个关键词。需要搜多个就调多次。

### save_entity — 保存/更新实体

```bash
MEMORY_CHATID="{chatid}" /mnt/d/code/yami/kiro-wecom-bridge/.venv/bin/python3 /mnt/d/code/yami/kiro-wecom-bridge/memory_cli.py save_entity '{"type": "person", "name": "张三", "description": "后端开发，负责订单服务"}'
```

类型：person / service / project / tool / config / decision / preference

已存在的实体会自动归档旧版本。可加 `"reason": "更新原因"`。

### save_relation — 保存关系

```bash
MEMORY_CHATID="{chatid}" /mnt/d/code/yami/kiro-wecom-bridge/.venv/bin/python3 /mnt/d/code/yami/kiro-wecom-bridge/memory_cli.py save_relation '{"from_name": "张三", "relation": "负责", "to_name": "订单服务"}'
```

### get_history — 查看实体变更历史

```bash
MEMORY_CHATID="{chatid}" /mnt/d/code/yami/kiro-wecom-bridge/.venv/bin/python3 /mnt/d/code/yami/kiro-wecom-bridge/memory_cli.py get_history '{"entity_name": "订单服务"}'
```

## chatid 规则

- 企微场景：chatid 从消息上下文中获取（格式如 `dm_userid` 或群聊 chatid）
- 命令行场景：固定使用 `cli_default`
- `MEMORY_CHATID` 必须设置，不能为空
