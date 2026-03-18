# kiro-wecom-bridge

通过企业微信智能机器人 WebSocket 长连接 API，将 [Kiro CLI](https://kiro.dev) 桥接到企微群聊，让团队成员在企微群里直接与 Kiro AI 对话。同时支持 Grafana Dashboard 轮询监控，超阈值自动触发 Kiro AI 分析并推送诊断结论。

## 架构

```
企微智能机器人平台
    ↕ WebSocket 长连接 (wss://openws.work.weixin.qq.com)
kiro-wecom-bridge (FastAPI)
    ↕ 启动 kiro-cli 子进程 (--no-interactive)
kiro-cli
    ↕ MCP 工具 reply_user()
mcp_server.py → POST /reply → 通过 WS 流式回复到企微群

Grafana Dashboard
    ↕ HTTP 轮询 (ds/query)
monitor.py → AI 分析 → 通过 WS 主动推送到企微群
```

## 功能

- 🤖 **企微群 AI 对话**：在企微群 @机器人 提问，Kiro 分析后流式回复
- 📊 **Grafana 监控告警**：定时轮询 Dashboard stat 面板指标，超阈值自动触发 AI 分析并推送
- 🔌 **多机器人支持**：通过 `channels.json` 配置多个企微智能机器人，各自独立运行
- 👋 **入群欢迎语**：用户进入机器人对话时自动发送欢迎消息
- 💓 **心跳保活**：自动心跳 + pong 超时检测 + 断线指数退避重连

## 前置条件

- Python 3.10+
- [Kiro CLI](https://kiro.dev) 已安装并完成登录（`kiro-cli auth login`）
- 企业微信智能机器人的 `bot_id` 和 `secret`（在企微管理后台创建智能机器人获取）

## 安装与启动

### 1. 克隆项目

```bash
git clone <repo-url>
cd kiro-wecom-bridge
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 channels.json

```bash
cp channels.example.json channels.json
```

编辑 `channels.json`，填入智能机器人凭证：

```json
[
  {
    "bot_id": "your_bot_id",
    "secret": "your_secret",
    "agent": null,
    "welcome_msg": "👋 你好！我是 Kiro AI 助手，有什么可以帮你的？"
  }
]
```

| 字段 | 说明 | 必填 |
|------|------|------|
| `bot_id` | 智能机器人 ID | 是 |
| `secret` | 智能机器人密钥 | 是 |
| `agent` | kiro-cli agent 名称，`null` 使用默认 | 否 |
| `welcome_msg` | 用户进入对话时的欢迎语 | 否 |

支持配置多个机器人，每个机器人独立建立 WebSocket 连接。

### 4. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
# 必填：kiro-cli 的工作目录（包含 .kiro 配置的目录）
KIRO_WORK_DIR=/path/to/your/workspace

# 可选：会话超时（秒）
KIRO_SESSION_TIMEOUT=1800

# 可选：服务监听地址和端口
HOST=0.0.0.0
PORT=8900

# 可选：channels.json 路径
# CHANNELS_PATH=channels.json

# 可选：Grafana 监控轮询
# GRAFANA_URL=https://your-grafana.com
# GRAFANA_TOKEN=your_grafana_service_account_token
# MONITOR_DASHBOARD_UIDS=dashboard-uid-1,dashboard-uid-2
# MONITOR_POLL_INTERVAL=60
```

| 变量 | 说明 | 必填 | 默认值 |
|------|------|------|--------|
| `KIRO_WORK_DIR` | kiro-cli 工作目录 | 是 | - |
| `KIRO_SESSION_TIMEOUT` | 会话超时（秒） | 否 | `1800` |
| `HOST` | 服务监听地址 | 否 | `0.0.0.0` |
| `PORT` | 服务监听端口 | 否 | `8900` |
| `CHANNELS_PATH` | channels.json 路径 | 否 | `channels.json` |
| `GRAFANA_URL` | Grafana 地址 | 监控需要 | - |
| `GRAFANA_TOKEN` | Grafana Service Account Token | 监控需要 | - |
| `MONITOR_DASHBOARD_UIDS` | 监控的 Dashboard UID，逗号分隔 | 监控需要 | - |
| `MONITOR_POLL_INTERVAL` | 轮询间隔（秒） | 否 | `60` |
| `GRAFANA_DATASOURCE_ID` | Grafana 数据源 ID | 否 | `3` |

### 5. 注册 MCP Server

让 kiro-cli 知道如何回调本服务：

```bash
kiro-cli mcp add --name kiro-bridge --scope global \
  --command python3 --args "/absolute/path/to/kiro-wecom-bridge/mcp_server.py" \
  --env "KIRO_BRIDGE_URL=http://localhost:8900" --force
```

> `--args` 必须是 `mcp_server.py` 的绝对路径。

### 6. 启动服务

```bash
python3 main.py
```

## Grafana Dashboard 轮询监控

在 Dashboard 的 stat 类型面板的 Description 字段中写入 JSON 监控规则：

```json
{
  "notify": true,
  "thresholds": "10",
  "belowThreshold": false,
  "duration": "5m",
  "interval": "10m",
  "timeRange": "00:00~08:00",
  "format": "percent",
  "decimals": 2,
  "alertMsg": "{value} 超过阈值 {threshold}，持续 {duration}",
  "okMsg": "已恢复, 当前 {value}"
}
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `notify` | 是否启用监控 | `true` |
| `thresholds` | 阈值 | `"10"` |
| `belowThreshold` | `true` 低于阈值告警，`false` 高于 | `false` |
| `duration` | 持续超阈值多久才告警 | `"5m"` |
| `interval` | 两次告警最小间隔 | `"10m"` |
| `timeRange` | 仅在此 UTC 时间范围内监控 | `"00:00~08:00"` |
| `format` | 值格式，`percent` 加 `%` 后缀 | `"percent"` |
| `decimals` | 小数位数 | `2` |
| `alertMsg` | 告警模板，支持 `{value}` `{threshold}` `{duration}` `{times}` | |
| `okMsg` | 恢复模板，支持 `{value}` `{duration}` | |

## 接口列表

| 接口 | 方法 | 说明 |
|------|------|------|
| `/reply` | POST | MCP 工具回调，接收 kiro-cli 的回复（内部使用） |

请求体：

```json
{"request_id": "xxx", "message": "回复内容"}
```

## 项目结构

```
kiro-wecom-bridge/
├── main.py              # FastAPI 主服务，生命周期管理和路由
├── ws_client.py         # 企微智能机器人 WebSocket 长连接客户端
├── channel.py           # Channel 管理，每个机器人一个 Channel
├── session.py           # kiro-cli 会话管理，按 req_id 隔离并发
├── monitor.py           # Grafana Dashboard 轮询监控
├── mcp_server.py        # MCP Server，提供 reply_user 工具给 kiro-cli
├── channels.json        # 机器人配置（git ignored）
├── channels.example.json
├── .env                 # 环境变量（git ignored）
├── .env.example
├── requirements.txt
└── README.md
```

## License

MIT
