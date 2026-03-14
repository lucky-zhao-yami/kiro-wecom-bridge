"""企业微信群机器人 Webhook 发消息"""
import logging, os, re
import httpx

log = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")


def _to_wecom_markdown(text: str) -> str:
    """将标准 markdown 转为企微兼容格式"""
    # 先处理行内代码：去掉反引号保留内容
    text = re.sub(r'`([^`]+)`', r'\1', text)
    lines = text.split("\n")
    result = []
    in_code = False
    table_rows = []

    for line in lines:
        # 代码块 → 去掉围栏，内容加引用前缀
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            result.append(f"> {line}")
            continue

        # 表格分隔行跳过
        if re.match(r"^\s*\|[-\s|:]+\|\s*$", line):
            continue

        # 表格行 → 转为 key: value
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not table_rows:
                table_rows = cells  # header
            else:
                parts = []
                for i, cell in enumerate(cells):
                    if cell and i < len(table_rows):
                        parts.append(f"{table_rows[i]}:{cell}")
                if parts:
                    result.append("> " + " | ".join(parts))
            continue

        # 表格结束，重置 header
        if table_rows and not line.strip().startswith("|"):
            table_rows = []

        # ## 标题 → 粗体
        if re.match(r"^#{1,4}\s+", line):
            title = re.sub(r"^#{1,4}\s+", "", line)
            result.append(f"**{title}**")
            continue

        result.append(line)

    return "\n".join(result)


async def send_webhook(content: str):
    if not WEBHOOK_URL:
        log.warning("未配置 WECOM_WEBHOOK_URL，跳过发送")
        return
    content = _to_wecom_markdown(content)
    # 企微 Webhook markdown 限制 4096 字节
    if len(content.encode()) > 4000:
        content = content[:2000] + "\n...(内容过长已截断)"
    async with httpx.AsyncClient() as c:
        r = await c.post(WEBHOOK_URL, json={
            "msgtype": "markdown",
            "markdown": {"content": content},
        })
        data = r.json()
        if data.get("errcode", 0) != 0:
            log.error("Webhook 发送失败: %s", data)
