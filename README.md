# kiro-wecom-bridge

将 [Kiro CLI](https://kiro.dev) 桥接到企业微信群机器人，让团队成员在企微群里直接与 Kiro AI 对话。同时支持 Grafana 告警自动分析，告警触发后 Kiro 自动给出诊断建议并推送到群里。

## 它能做什么

- 🤖 **企微群聊天**：在企微群 @机器人 提问，Kiro 分析后自动回复到群里
- 🚨 **Grafana 告警分析**：接收 Grafana 告警 Webhook → Kiro 自动分析根因 → 推送诊断结论到企微群
- 📊 **Dashboard 轮询监控**：定时轮询 Grafana Dashboard 面板指标，超阈值自动触发告警 + AI 分析
- 💬 **Web 聊天界面**：内置简易 Web UI，浏览器直接访问即可对话

## 工作原理

```
企微群/Web/Grafana → REST API → kiro-cli (--no-interactive)
                                     ↓ AI 思考推理...
                                     ↓ 调用 MCP 工具 reply_user(message)
                                 MCP Server → POST /reply → 企微群/Web
```

关键设计：kiro-cli 通过 MCP 工具 `reply_user` 主动发送最终回复，不需要解析终端输出。

## 前置条件

- Python 3.10+
- [Kiro CLI](https://kiro.dev) 已安装并完成登录（`kiro-cli auth login`）
- 企业微信群机器人 Webhook 地址（群设置 → 群机器人 → 添加机器人 → 复制 Webhook URL）

## 安装与启动

### 1. 克隆项目

```bash
git clone https://github.com/<your-username>/kiro-wecom-bridge.git
cd kiro-wecom-bridge
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，至少填入以下两项：

```bash
# 必填：企微群机器人 Webhook 地址
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your_key

# 必填：kiro-cli 的工作目录（包含 .kiro 配置的目录）
KIRO_WORK_DIR=/path/to/your/workspace
```

### 4. 注册 MCP Server

这一步让 kiro-cli 知道如何回调本服务：

```bash
kiro-cli mcp add --name kiro-bridge --scope global \
  --command python3 --args "/absolute/path/to/kiro-wecom-bridge/mcp_server.py" \
  --env "KIRO_BRIDGE_URL=http://localhost:8900" --force
```

> 注意：`--args` 必须是 `mcp_server.py` 的绝对路径。

### 5. 启动服务

```bash
python3 main.py
```

服务默认监听 `0.0.0.0:8900`，可通过 `.env` 中的 `HOST` 和 `PORT` 修改。

## 使用方式

### 企微群聊天

需要配合企微群机器人的消息回调。将群机器人的回调地址指向 `http://<your-host>:8900/chat`，消息格式：

```bash
curl -X POST http://localhost:8900/chat \
  -H "Content-Type: application/json" \
  -d '{"user": "zhangsan", "message": "帮我查一下最近的订单异常"}'
```

响应：

```json
{"reply": "根据分析，最近 1 小时有 3 笔订单支付超时..."}
```

### Web 聊天界面

浏览器访问 `http://localhost:8900`，直接在页面上对话。

### Grafana 告警 Webhook

在 Grafana 中配置：Alerting → Contact Points → 新建 → 类型选 Webhook → URL 填：

```
http://<your-host>:8900/alert
```

告警触发后，Kiro 会自动分析告警内容并将诊断结论推送到企微群。

### Grafana Dashboard 轮询监控

除了被动接收告警，还支持主动轮询 Dashboard 面板指标。在 `.env` 中配置：

```bash
GRAFANA_URL=https://your-grafana.com
GRAFANA_TOKEN=your_grafana_service_account_token
MONITOR_DASHBOARD_UIDS=dashboard-uid-1,dashboard-uid-2
MONITOR_POLL_INTERVAL=60
```

然后在 Dashboard 的 **stat 类型面板**的 Description 字段中写入 JSON 监控规则：

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

规则字段说明：

| 字段 | 说明 | 示例 |
|------|------|------|
| `notify` | 是否启用监控 | `true` |
| `thresholds` | 阈值 | `"10"` |
| `belowThreshold` | `true` 表示低于阈值告警，`false` 表示高于 | `false` |
| `duration` | 持续超过阈值多久才告警 | `"5m"` |
| `interval` | 两次告警之间的最小间隔 | `"10m"` |
| `timeRange` | 仅在此 UTC 时间范围内监控 | `"00:00~08:00"` |
| `format` | 值格式，`percent` 会加 `%` 后缀 | `"percent"` |
| `decimals` | 小数位数 | `2` |
| `alertMsg` | 告警消息模板，支持 `{value}` `{threshold}` `{duration}` `{times}` | |
| `okMsg` | 恢复消息模板，支持 `{value}` `{duration}` | |

## 配置参考

完整的环境变量列表：

| 变量 | 说明 | 必填 | 默认值 |
|------|------|------|--------|
| `WECOM_WEBHOOK_URL` | 企微群机器人 Webhook 地址 | 是 | - |
| `KIRO_WORK_DIR` | kiro-cli 工作目录 | 是 | - |
| `KIRO_SESSION_TIMEOUT` | 会话超时（秒） | 否 | `1800` |
| `HOST` | 服务监听地址 | 否 | `0.0.0.0` |
| `PORT` | 服务监听端口 | 否 | `8900` |
| `GRAFANA_URL` | Grafana 地址 | 监控需要 | - |
| `GRAFANA_TOKEN` | Grafana Service Account Token | 监控需要 | - |
| `MONITOR_DASHBOARD_UIDS` | 监控的 Dashboard UID，逗号分隔 | 监控需要 | - |
| `MONITOR_POLL_INTERVAL` | 轮询间隔（秒） | 否 | `60` |
| `GRAFANA_DATASOURCE_ID` | Grafana 数据源 ID | 否 | `3` |

## 接口列表

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | Web 聊天页面 |
| `/chat` | POST | 聊天 API，body: `{"user":"xxx","message":"xxx"}` |
| `/alert` | POST | Grafana 告警 Webhook 接收 |
| `/reply` | POST | MCP 工具回调（内部使用，不要手动调用） |

## 项目结构

```
kiro-wecom-bridge/
├── main.py           # FastAPI 主服务，路由定义
├── session.py        # kiro-cli 会话管理，启动子进程并等待 MCP 回调
├── webhook.py        # 企微 Webhook 发送，Markdown 格式转换
├── monitor.py        # Grafana Dashboard 轮询监控
├── mcp_server.py     # MCP Server，提供 reply_user 工具给 kiro-cli
├── requirements.txt
├── .env.example      # 环境变量模板
└── README.md
```

## 自定义 Kiro Agent

kiro-cli 支持通过 `--agent` 参数指定不同的 Agent。本项目在处理 Grafana 告警时使用 `alert-advisor` agent。你可以在 `KIRO_WORK_DIR/.kiro/agents/` 下创建自定义 agent 来定制 AI 的行为。

## License

MIT
