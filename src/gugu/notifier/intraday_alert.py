"""盘中异动告警飞书卡片构建器。"""
from __future__ import annotations

from typing import Any

from gugu.engine.intraday_monitor import AlertItem


def build_intraday_alert_card(alerts: list[AlertItem]) -> dict[str, Any]:
    """构建盘中异动告警飞书卡片。

    返回飞书 interactive card JSON，可直接发送。
    """
    if not alerts:
        return {
            "header": {
                "title": {"tag": "plain_text", "content": "📡 盘中扫描"},
                "template": "grey",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**当前无异动**\n扫描完成，未发现异常资金流",
                    },
                }
            ],
        }

    # 按 severity 分组
    critical = [a for a in alerts if a.severity == "critical"]
    warning = [a for a in alerts if a.severity == "warning"]
    info = [a for a in alerts if a.severity == "info"]

    # 构建消息内容
    sections = []

    # 标题（按最高严重程度）
    if critical:
        title = f"🔴 盘中异动告警（{len(alerts)} 条，{len(critical)} 严重）"
        template = "red"
    elif warning:
        title = f"🟡 盘中异动告警（{len(alerts)} 条）"
        template = "orange"
    else:
        title = f"🟢 盘中异动扫描（{len(alerts)} 条）"
        template = "blue"

    # 严重告警
    if critical:
        sections.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🔴 严重告警**\n" + "\n".join(a.message for a in critical),
            },
        })

    # 警告
    if warning:
        sections.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🟡 预警**\n" + "\n".join(a.message for a in warning),
            },
        })

    # 信息
    if info:
        sections.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🟢 资金流入**\n" + "\n".join(a.message for a in info),
            },
        })

    # 决策建议
    buy_count = sum(1 for a in alerts if a.direction == "buy")
    sell_count = sum(1 for a in alerts if a.direction == "sell")

    suggestion = "**决策建议**\n"
    if buy_count > sell_count:
        suggestion += f"资金整体偏向流入（{buy_count} 流入 vs {sell_count} 流出），关注买入机会"
    elif sell_count > buy_count:
        suggestion += f"资金整体偏向流出（{sell_count} 流出 vs {buy_count} 流入），警惕减仓风险"
    else:
        suggestion += f"资金流向均衡（{buy_count} 流入 vs {sell_count} 流出）"

    sections.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": suggestion},
    })

    # 时间戳
    sections.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"扫描时间 {alerts[0].timestamp} · 冷却 5 分钟",
            }
        ],
    })

    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": sections,
    }


def format_intraday_alert_text(alerts: list[AlertItem]) -> str:
    """纯文本版本（用于日志或降级输出）。"""
    if not alerts:
        return "盘中扫描完成，未发现异动"

    lines = [f"📡 盘中异动告警 ({len(alerts)} 条)", ""]
    for alert in alerts:
        lines.append(alert.message)
    return "\n".join(lines)
