"""Grafana Dashboard 轮询监控 — 复刻 so-webhook 逻辑"""
import asyncio, json, logging, os, time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import httpx

log = logging.getLogger(__name__)

GRAFANA_URL = os.getenv("GRAFANA_URL", "https://grafana.yamibuy.com")
GRAFANA_TOKEN = os.getenv("GRAFANA_TOKEN", "")
DASHBOARD_UIDS = os.getenv("MONITOR_DASHBOARD_UIDS", "f2e5194d-ff2d-4b4b-8413-24ccf058fbe0").split(",")
POLL_INTERVAL = int(os.getenv("MONITOR_POLL_INTERVAL", "60"))  # 秒
DATASOURCE_ID = int(os.getenv("GRAFANA_DATASOURCE_ID", "3"))

# 每个面板的告警状态: {panel_title: {last_time, create_time, times}}
_state: dict[str, dict] = {}


def _headers():
    return {"Authorization": f"Bearer {GRAFANA_TOKEN}"}


async def _get_panels(client: httpx.AsyncClient, uid: str) -> dict:
    """获取 Dashboard 所有 stat 面板的 SQL 和告警规则"""
    r = await client.get(f"{GRAFANA_URL}/api/dashboards/uid/{uid}", headers=_headers())
    r.raise_for_status()
    panels_raw = r.json().get("dashboard", {}).get("panels", [])
    # 展开 row 类型里嵌套的 panels
    all_panels = []
    for p in panels_raw:
        if p.get("type") == "row":
            all_panels.extend(p.get("panels", []))
        else:
            all_panels.append(p)

    metrics = {}
    for p in all_panels:
        if p.get("type") != "stat":
            continue
        desc = p.get("description", "")
        if not desc:
            continue
        try:
            rule = json.loads(desc)
        except (json.JSONDecodeError, TypeError):
            continue
        if not rule.get("notify"):
            continue
        sql = (p.get("targets") or [{}])[0].get("rawSql", "")
        if sql:
            metrics[p["title"]] = {"sql": sql, "rule": rule}
    return metrics


async def _query_sql(client: httpx.AsyncClient, sql: str):
    """通过 Grafana ds/query 执行 SQL，返回单值"""
    payload = {"queries": [{"refId": "A", "intervalMs": 60000, "maxDataPoints": 100,
                            "datasourceId": DATASOURCE_ID, "rawSql": sql, "format": "table"}]}
    r = await client.post(f"{GRAFANA_URL}/api/ds/query", headers=_headers(), json=payload)
    r.raise_for_status()
    return r.json()["results"]["A"]["frames"][0]["data"]["values"][0][0]


def _check_time_range(rule: dict) -> bool:
    """检查当前时间是否在 timeRange 内（如 '00:00~08:00'）"""
    tr = rule.get("timeRange", "")
    if not tr or "~" not in tr:
        return True
    start_s, end_s = tr.split("~")
    now = datetime.utcnow()
    try:
        start = datetime.strptime(start_s.strip(), "%H:%M").replace(year=now.year, month=now.month, day=now.day)
        end = datetime.strptime(end_s.strip(), "%H:%M").replace(year=now.year, month=now.month, day=now.day)
    except ValueError:
        return True
    return start <= now <= end


def _parse_duration(s: str) -> float:
    """解析 '5m' / '1h' 为秒数"""
    s = s.strip().lower()
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("s"):
        return float(s[:-1])
    return 0


def _fmt(value, rule: dict) -> str:
    fmt = rule.get("format", "")
    decimals = rule.get("decimals")
    if decimals is not None:
        value = float(Decimal(str(value)).quantize(Decimal(10) ** -int(decimals), rounding=ROUND_HALF_UP))
    suffix = "%" if fmt == "percent" else ""
    return f"{value}{suffix}"


async def _poll_once(on_alert):
    """扫描一轮所有 Dashboard"""
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        for uid in DASHBOARD_UIDS:
            uid = uid.strip()
            if not uid:
                continue
            try:
                metrics = await _get_panels(client, uid)
            except Exception as e:
                log.error("获取 Dashboard %s 失败: %s", uid, e)
                continue

            errors = {}
            for title, m in metrics.items():
                try:
                    sql, rule = m["sql"], m["rule"]
                    if not _check_time_range(rule):
                        continue

                    threshold = float(str(rule.get("thresholds", "0")).split(",")[0])
                    below = rule.get("belowThreshold", False)
                    duration_sec = _parse_duration(rule.get("duration", "0m"))
                    interval_sec = _parse_duration(rule.get("interval", "0m"))

                    value = await _query_sql(client, sql)
                    db_val = float(value) if value is not None else 0.0
                    log.info("[%s] value=%s threshold=%s", title, db_val, threshold)

                    now = time.time()
                    state = _state.setdefault(title, {"last_time": 0, "create_time": now, "times": 1})

                    over = db_val < threshold if below else db_val >= threshold

                    if over:
                        interval_ok = state["last_time"] == 0 or (now - state["last_time"]) > interval_sec
                        duration_ok = (now - state["create_time"]) >= duration_sec
                        log.info("[%s] over=True, duration_ok=%s (%.0fs/%.0fs), interval_ok=%s", title, duration_ok, now - state["create_time"], duration_sec, interval_ok)
                        if interval_ok and duration_ok:
                            val_str = _fmt(db_val, rule)
                            thr_str = _fmt(threshold, rule)
                            dur_str = f"{int(now - state['create_time'])}s"
                            alert_msg = rule.get("alertMsg", "{value} 超过阈值 {threshold}")
                            alert_msg = alert_msg.replace("{value}", val_str).replace("{threshold}", thr_str).replace("{duration}", dur_str).replace("{times}", str(state["times"]))
                            errors[title] = {"msg": alert_msg, "sql": sql}
                            state["last_time"] = now
                            state["times"] = state.get("times", 1) + 1
                    else:
                        if state["last_time"] != 0:
                            # 恢复
                            val_str = _fmt(db_val, rule)
                            ok_msg = rule.get("okMsg", "已恢复, 当前 {value}")
                            ok_msg = ok_msg.replace("{value}", val_str).replace("{duration}", f"{int(now - state['create_time'])}s")
                            errors[title] = {"msg": f"✅ {ok_msg}", "sql": None}
                        _state.pop(title, None)
                except Exception as e:
                    log.error("[%s] 查询异常: %s", title, e)

            if errors and on_alert:
                for t, info in errors.items():
                    alert_text = f"[payment数据监控](https://grafana.yamibuy.com/d/{uid})\n面板：{t}\n状态：{info['msg']}"
                    if info.get("sql"):
                        alert_text += f"\n监控SQL：\n{info['sql']}"
                    await on_alert(alert_text)


async def start_monitor(on_alert):
    """启动轮询循环，on_alert(text) 为告警回调"""
    if not GRAFANA_TOKEN:
        log.warning("未配置 GRAFANA_TOKEN，监控未启动")
        return
    log.info("监控启动: dashboards=%s, interval=%ds", DASHBOARD_UIDS, POLL_INTERVAL)
    while True:
        try:
            await _poll_once(on_alert)
        except Exception as e:
            log.error("轮询异常: %s", e)
        await asyncio.sleep(POLL_INTERVAL)
