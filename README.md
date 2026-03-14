# kiro-wecom-bridge

将 [Kiro CLI](https://kiro.dev) 桥接到企业微信群，支持 Grafana 告警自动分析和 Web 聊天。

## 架构

```
用户/Grafana → REST API → kiro-cli (--no-interactive)
                              ↓ 思考推理...
                              ↓ 调用 MCP 工具 reply_user(message)
                          MCP Server → POST /reply → 返回给用户/企微群
```

核心设计：kiro-cli 通过 MCP 工具 `reply_user` 主动发送最终回复，避免解析终端输出。

## 功能

- **企微群聊天**：通过企微群机器人与 Kiro 对话
- **Grafana 告警分析**：接收 Grafana 告警 → Kiro 自动分析 → 结论推送到企微群
- **Dashboard 轮询监控**：定时轮询 Grafana Dashboard 面板，超阈值自动告警并分析
- **Web 聊天界面**：内置简易 Web UI

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你的配置

# 注册 MCP Server
kiro-cli mcp add --name kiro-bridge --scope global \
  --command python3 --args "/path/to/kiro-wecom-bridge/mcp_server.py" \
  --env "KIRO_BRIDGE_URL=http://localhost:8900" --force

# 启动
python3 main.py
```

## 配置

复制 `.env.example` 并填入实际值：

| 变量 | 说明 | 必填 |
|------|------|------|
| `WECOM_WEBHOOK_URL` | 企微群机器人 Webhook 地址 | 是 |
| `KIRO_WORK_DIR` | kiro-cli 工作目录 | 是 |
| `KIRO_SESSION_TIMEOUT` | 会话超时时间（秒） | 否 |
| `HOST` | 服务监听地址 | 否 |
| `PORT` | 服务监听端口 | 否 |
| `GRAFANA_URL` | Grafana 地址 | 监控功能需要 |
| `GRAFANA_TOKEN` | Grafana Service Account Token | 监控功能需要 |
| `MONITOR_DASHBOARD_UIDS` | 要监控的 Dashboard UID，逗号分隔 | 监控功能需要 |
| `MONITOR_POLL_INTERVAL` | 监控轮询间隔（秒），默认 60 | 否 |

## 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | Web 聊天页面 |
| `/chat` | POST | 聊天 API `{"user":"xxx","message":"xxx"}` |
| `/alert` | POST | Grafana 告警 Webhook |
| `/reply` | POST | MCP 工具回调（内部使用） |

## Grafana 告警配置

Alerting → Contact Points → 新建 Webhook → URL: `http://<your-host>:8900/alert`

## Grafana Dashboard 轮询监控

在 Dashboard 的 stat 面板的 Description 中写入 JSON 规则即可启用监控：

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

## License

MIT
