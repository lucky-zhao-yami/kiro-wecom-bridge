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
- 🧠 **长期记忆**：per-chatid SQLite 知识图谱，FTS5 全文检索 + sqlite-vec 向量语义检索
- ⏰ **定时任务**：REST API 管理定时任务，系统 crontab 触发，SQLite 持久化
- 🛡️ **安全防护**：提示词注入检测 + chatid 权限分级（safe/full 模式）
- 🧠 **元认知**：每条消息前自动思考"我知道什么"，主动检索/沉淀记忆

## 前置条件

- WSL2 (Ubuntu 22.04+)
- Python 3.10+
- [Kiro CLI](https://kiro.dev) 已安装并登录
- 企业微信智能机器人的 `bot_id` 和 `secret`

## 环境搭建

### 1. 安装 WSL

在 Windows PowerShell (管理员) 中执行：

```powershell
# 查看可用的 Linux 发行版
wsl --list --online

# 安装指定发行版（推荐 Ubuntu 24.04）
wsl --install -d Ubuntu-24.04

# 安装完成后重启电脑，然后打开 Ubuntu 终端设置用户名和密码
```

如果 `wsl --install` 网络问题导致下载失败，可以用官方离线镜像：

```powershell
# 1. 下载 Ubuntu 22.04 官方 WSL 镜像
#    https://cloud-images.ubuntu.com/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz

# 2. 导入（D:\wsl 为安装目录，可自定义）
mkdir D:\wsl\Ubuntu-22.04
wsl --import Ubuntu-22.04 D:\wsl\Ubuntu-22.04 .\ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz

# 3. 进入
wsl -d Ubuntu-22.04

# 4. 创建普通用户（导入方式默认是 root）
adduser yourname
usermod -aG sudo yourname
# 之后在 /etc/wsl.conf 中设置默认用户：
echo -e "[user]\ndefault=yourname" >> /etc/wsl.conf
# 退出后重启 WSL 生效
exit
wsl --shutdown
wsl -d Ubuntu-22.04
```

其他安装方式：

```powershell
# 从微软商店安装
# 打开 Microsoft Store，搜索 "Ubuntu 24.04"，点击安装

# 导入同事已配好的环境
#   导出端: wsl --export Ubuntu-24.04 ubuntu-backup.tar
#   导入端:
wsl --import Ubuntu-24.04 D:\wsl\Ubuntu-24.04 .\ubuntu-backup.tar
```

WSL 常用管理命令：

```powershell
wsl --update                          # 更新 WSL 内核
wsl --list --verbose                  # 查看已安装的发行版和状态
wsl --set-default Ubuntu-24.04        # 设置默认发行版
wsl -d Ubuntu-24.04                   # 进入指定发行版
wsl --shutdown                        # 关闭所有 WSL 实例
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
curl -fsSL https://cli.kiro.dev/install | bash

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
# 核心依赖（企微桥接功能）
bash install.sh

# 完整安装（含记忆系统，会额外下载 ~500MB 的 PyTorch + sentence-transformers）
bash install.sh --full
```

脚本会自动：安装系统依赖 → 创建 venv → pip install。`--full` 额外安装记忆系统的向量检索依赖。不装也能用，记忆系统会降级为纯 FTS5 全文检索。

安装完成后，后续操作都需要先激活虚拟环境：

```bash
source .venv/bin/activate
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
      "default": {"agent": null, "cwd": "/path/to/workspace", "mode": "safe"},
      "dm_YourUserId": {"agent": null, "cwd": "/path/to/workspace", "mode": "full"},
      "CHAT_ID_DEV": {"agent": null, "cwd": "/path/to/project", "mode": "safe"},
      "CHAT_ID_OPS": {"agent": "ops-advisor", "cwd": "/path/to/ops", "mode": "safe"}
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
| `chats.<chatid>.mode` | 权限模式：`full`（完整权限）或 `safe`（只读，禁用 bash/写文件），默认 `full` | 否 |

> **安全建议**：`default` 设为 `safe`，只给你自己的私聊 `dm_YourUserId` 设 `full`。群聊里任何人都能发消息，safe 模式可防止提示词注入攻击导致执行危险操作。

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
# 安装 service（按实际路径修改 service 文件中的 User 和 WorkingDirectory，ExecStart 指向 start.sh）
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

## 定时任务

通过 REST API 管理定时任务，底层使用系统 crontab 触发 + SQLite 持久化。

### 调度 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/scheduler/jobs` | 创建任务 |
| GET | `/scheduler/jobs` | 列出所有任务 |
| GET | `/scheduler/jobs/{id}` | 查看单个任务 |
| PATCH | `/scheduler/jobs/{id}` | 修改任务（含暂停/恢复） |
| DELETE | `/scheduler/jobs/{id}` | 删除任务 |

**创建示例**：

```bash
curl -s -X POST http://localhost:8900/scheduler/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "cron": "0 9 * * 1-5",
    "chatid": "dm_YourUserId",
    "prompt": "检查 OpenProject 中我的待办任务",
    "description": "工作日9点检查OP"
  }'
```

| 参数 | 说明 | 必填 |
|------|------|------|
| `cron` | crontab 表达式（分 时 日 月 周） | 是 |
| `chatid` | 目标 chatid，决定由哪个 kiro agent 执行 | 是 |
| `prompt` | 到时间后发给 kiro 的指令 | 是 |
| `bot_index` | 多机器人时指定 channel，默认 0 | 否 |
| `description` | 任务描述 | 否 |

**暂停/恢复**：

```bash
# 暂停
curl -s -X PATCH http://localhost:8900/scheduler/jobs/{id} \
  -H "Content-Type: application/json" -d '{"enabled": false}'

# 恢复
curl -s -X PATCH http://localhost:8900/scheduler/jobs/{id} \
  -H "Content-Type: application/json" -d '{"enabled": true}'
```

### 触发接口

定时任务到时间后，crontab 会调用 `/cron/trigger` 接口：

```
POST http://localhost:8900/cron/trigger
{"chatid": "dm_YourUserId", "prompt": "要执行的任务"}
```

Kiro 执行 prompt 并将结果主动推送到对应的企微聊天。

## Skills（Kiro 技能）

项目自带 3 个 Kiro skill，位于 `skills/` 目录。安装后 Kiro 可以在对话中自动调用。

### 安装方式

将 skill 目录复制（或软链接）到你的 Kiro workspace 的 `.kiro/skills/` 下：

```bash
# 假设 workspace 在 /mnt/d/workspace/all
WORKSPACE=/mnt/d/workspace/all
BRIDGE=/mnt/d/code/yami/kiro-wecom-bridge

# 方式一：软链接（推荐，bridge 更新后自动生效）
ln -sf $BRIDGE/skills/wecom-memory $WORKSPACE/.kiro/skills/wecom-memory
ln -sf $BRIDGE/skills/wecom-scheduler $WORKSPACE/.kiro/skills/wecom-scheduler
ln -sf $BRIDGE/skills/notify-wecom $WORKSPACE/.kiro/skills/notify-wecom

# 方式二：直接复制
cp -r $BRIDGE/skills/* $WORKSPACE/.kiro/skills/
```

### wecom-memory — 长期记忆

让 Kiro 拥有跨会话的持久记忆。每个 chatid 独立的 SQLite 知识图谱。

**Kiro 会自动**：
- 收到消息时搜索记忆，获取相关上下文
- 对话中产生新知识时主动保存（人员、项目、决策、偏好等）
- 检测到记忆冲突时询问用户并更新

**支持的操作**：
- `search` — 搜索记忆（FTS5 全文 + 向量语义）
- `save_entity` — 保存/更新实体（person/service/project/tool/config/decision/preference）
- `save_relation` — 保存实体间关系
- `get_history` — 查看实体变更历史

**手动测试**：

```bash
source .venv/bin/activate

# 搜索
MEMORY_CHATID=test python3 memory_cli.py search '{"query": "订单服务"}'

# 保存实体
MEMORY_CHATID=test python3 memory_cli.py save_entity '{"type":"person","name":"张三","description":"后端开发，负责订单服务"}'

# 保存关系
MEMORY_CHATID=test python3 memory_cli.py save_relation '{"from_name":"张三","relation":"负责","to_name":"ec-so-service"}'
```

### wecom-scheduler — 定时任务

让 Kiro 通过对话创建/管理定时任务。用户说"每天9点检查OP待办"，Kiro 自动调用 API 创建。

详见上方 [定时任务](#定时任务) 章节。

### notify-wecom — 消息通知

让 Kiro 主动向企微推送消息（不需要用户先发消息）。

```bash
curl -s -X POST http://localhost:8900/send \
  -H "Content-Type: application/json" \
  -d '{"chatid": "dm_YourUserId", "content": "任务完成通知"}'
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `chatid` | 目标 chatid（私聊用 `dm_用户ID`，群聊用群 chatid） | `dm_ZhaoXingPing` |
| `content` | 消息内容，支持企微 markdown | — |
| `chat_type` | 1=单聊 2=群聊 | 1 |
| `bot_index` | 多机器人时指定 channel | 0 |

## 项目结构

```
kiro-wecom-bridge/
├── main.py               # FastAPI 主服务，生命周期管理
├── ws_client.py          # 企微 WebSocket 长连接客户端
├── channel.py            # Channel 管理 + StreamSegmenter 流式分段
├── session.py            # ACP 进程池，per-chatid kiro-cli 进程管理
├── scheduler.py          # 定时任务调度：SQLite 持久化 + 系统 crontab
├── guard.py              # 安全防护：注入检测 + 权限分级 preamble + 元认知指令
├── memory.py             # 长期记忆：SQLite 实体关系图谱 + FTS5 + 向量检索
├── memory_cli.py         # 记忆系统 CLI，供 kiro skill 通过 bash 调用
├── install.sh            # 一键环境安装（venv + 依赖）
├── start.sh              # 启动脚本（前置检查 + 启动）
├── restart.sh            # 重启脚本（杀旧 + 启动新）
├── kiro-wecom-bridge.service # systemd 服务文件
├── channels.json         # 机器人配置（git ignored）
├── channels.example.json # 配置模板
├── .env                  # 环境变量（git ignored）
├── .env.example          # 环境变量模板
├── requirements.txt
└── README.md
```

## 长期记忆

每个 chatid 拥有独立的 SQLite 记忆库，存储在 `{KIRO_WORK_DIR}/wecom-sessions/{chatid}/memory.db`。

Kiro 通过 `wecom-memory` skill 自动调用，支持：
- **实体管理**：保存人员、服务、项目、决策等实体，自动版本归档
- **关系管理**：记录实体间关系（谁负责什么、什么依赖什么）
- **双通道检索**：FTS5 全文匹配 + sqlite-vec 向量语义搜索

手动测试：

```bash
source .venv/bin/activate

# 保存实体
MEMORY_CHATID=test python3 memory_cli.py save_entity '{"type":"person","name":"张三","description":"后端开发，负责订单服务"}'

# 搜索
MEMORY_CHATID=test python3 memory_cli.py search '{"query":"订单"}'

# 保存关系
MEMORY_CHATID=test python3 memory_cli.py save_relation '{"from_name":"张三","relation":"负责","to_name":"ec-so-service"}'

# 删除实体
MEMORY_CHATID=test python3 memory_cli.py delete_entity '{"name":"张三"}'

# 删除关系
MEMORY_CHATID=test python3 memory_cli.py delete_relation '{"from_name":"张三","relation":"负责","to_name":"ec-so-service"}'

# 查看变更历史
MEMORY_CHATID=test python3 memory_cli.py get_history '{"entity_name":"张三"}'
```

查看原始数据：

```bash
sqlite3 /mnt/d/workspace/all/wecom-sessions/test/memory.db "SELECT * FROM entities;"
```

## License

MIT
