# kiro-wecom-bridge

通过企业微信智能机器人 WebSocket 长连接 API，将 [Kiro CLI](https://kiro.dev) (ACP 协议) 桥接到企微群聊/单聊，让团队成员在企微里直接与 Kiro AI 对话。

## 架构

```
企微智能机器人平台
    ↕ WebSocket 长连接 (wss://openws.work.weixin.qq.com)
kiro-wecom-bridge (FastAPI)
    ↕ JSON-RPC over stdio (ACP 协议)
kiro-cli acp --trust-all-tools
    ↕ AI 推理 + 工具调用
```

## 功能

- 🤖 **AI 对话**：在企微群/单聊 @机器人 提问，Kiro 流式回复
- 🔀 **群聊隔离**：每个群聊/单聊独立 kiro-cli 进程和会话，互不干扰
- ⚙️ **按群配置**：每个群可配置不同的 agent 和工作目录
- 📝 **流式分段**：长回复自动按 1500 字分段，换行处优先切割
- 💾 **会话持久化**：session_id 持久化到磁盘，重启后尝试恢复
- 🔌 **多机器人**：支持配置多个企微机器人，各自独立运行
- 👋 **入群欢迎语**：用户进入对话时自动发送欢迎消息
- 💓 **心跳保活**：自动心跳 + 断线指数退避重连
- ♻️ **进程池管理**：最多 10 个进程，LRU 淘汰，30 分钟空闲超时清理

## 前置条件

- WSL2 (Ubuntu 22.04+)
- Python 3.10+
- [Kiro CLI](https://kiro.dev) 已安装并登录
- 企业微信智能机器人的 `bot_id` 和 `secret`

## 环境搭建

### 1. 安装 WSL

在 Windows PowerShell (管理员) 中执行：

```powershell
# 安装 WSL（默认 Ubuntu）
wsl --install

# 安装完成后重启电脑，然后打开 Ubuntu 终端设置用户名和密码
```

如果已安装 WSL 但需要更新：

```powershell
wsl --update
```

安装完成后进入 WSL：

```powershell
wsl
```

### 2. WSL 基础配置

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Python 和依赖
sudo apt install -y python3 python3-pip python3-venv git curl unzip
```

### 3. 安装 Kiro CLI

```bash
# 下载安装 kiro-cli
curl -fsSL https://kiro.dev/install.sh | bash

# 验证安装
kiro-cli --version

# 登录（会打开浏览器进行 OAuth 认证）
kiro-cli auth login

# 验证登录状态
kiro-cli auth status
```

> **WSL 浏览器问题**：如果 `kiro-cli auth login` 无法自动打开浏览器，复制终端输出的 URL 到 Windows 浏览器中手动完成认证。

### 4. 克隆项目

```bash
git clone <repo-url>
cd kiro-wecom-bridge
```

### 5. 安装 Python 依赖

```bash
pip install -r requirements.txt
# 如果遇到 externally-managed-environment 错误：
pip install --break-system-packages -r requirements.txt
```

### 6. 配置

```bash
# 环境变量
cp .env.example .env
# 编辑 .env，设置 KIRO_WORK_DIR

# 机器人配置
cp channels.example.json channels.json
# 编辑 channels.json，填入 bot_id 和 secret
```

#### .env 配置项

| 变量 | 说明 | 必填 | 默认值 |
|------|------|------|--------|
| `KIRO_WORK_DIR` | kiro-cli 工作目录 | 是 | `/mnt/d/workspace/all` |
| `HOST` | 服务监听地址 | 否 | `0.0.0.0` |
| `PORT` | 服务监听端口 | 否 | `8900` |
| `CHANNELS_PATH` | channels.json 路径 | 否 | `channels.json` |

#### channels.json 配置

```json
[
  {
    "bot_id": "your_bot_id",
    "secret": "your_secret",
    "welcome_msg": "👋 你好！我是 Kiro AI 助手，有什么可以帮你的？",
    "chats": {
      "default": {"agent": null, "cwd": "/path/to/workspace"},
      "CHAT_ID_DEV": {"agent": null, "cwd": "/path/to/project"},
      "CHAT_ID_OPS": {"agent": "ops-advisor", "cwd": "/path/to/ops"}
    }
  }
]
```

| 字段 | 说明 | 必填 |
|------|------|------|
| `bot_id` | 智能机器人 ID | 是 |
| `secret` | 智能机器人密钥 | 是 |
| `welcome_msg` | 入群欢迎语 | 否 |
| `chats` | 按群聊 ID 配置 agent 和工作目录 | 否 |
| `chats.default` | 未匹配到具体 chatid 时的默认配置 | 否 |
| `chats.<chatid>.agent` | 该群使用的 kiro agent，`null` 为默认 | 否 |
| `chats.<chatid>.cwd` | 该群的工作目录 | 否 |

### 7. 启动

**方式一：直接启动**

```bash
# 前台启动
./start.sh

# 后台启动
nohup ./start.sh >> /tmp/wecom-bridge.log 2>&1 &

# 重启（自动杀旧进程）
./restart.sh

# 查看日志
tail -f /tmp/wecom-bridge.log
```

**方式二：systemd 托管（推荐）**

```bash
# 安装 service（按实际路径修改 service 文件中的 User 和 WorkingDirectory）
sudo cp kiro-wecom-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload

# 启动 & 开机自启
sudo systemctl enable --now kiro-wecom-bridge

# 常用命令
sudo systemctl status kiro-wecom-bridge   # 查看状态
sudo systemctl restart kiro-wecom-bridge  # 重启
sudo systemctl stop kiro-wecom-bridge     # 停止
journalctl -u kiro-wecom-bridge -f        # 查看日志
```

systemd 会自动处理：崩溃重启（5 秒后）、开机自启、日志管理（journalctl）。

## 项目结构

```
kiro-wecom-bridge/
├── main.py               # FastAPI 主服务，生命周期管理
├── ws_client.py          # 企微 WebSocket 长连接客户端
├── channel.py            # Channel 管理 + StreamSegmenter 流式分段
├── session.py            # ACP 进程池，per-chatid kiro-cli 进程管理
├── start.sh              # 启动脚本（前置检查 + 启动）
├── restart.sh            # 重启脚本（杀旧 + 启动新）
├── channels.json         # 机器人配置（git ignored）
├── channels.example.json # 配置模板
├── .env                  # 环境变量（git ignored）
├── .env.example          # 环境变量模板
├── requirements.txt
└── README.md
```

## License

MIT
