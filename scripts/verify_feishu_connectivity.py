"""飞书连通性验证：仅发送一条标记为 [系统验证] 的卡片，不发送任何交易信号。

用于运维验证飞书配置是否正常，避免与真实信号混淆。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.notifier import FeishuNotifier


async def main() -> None:
    notifier = FeishuNotifier()
    if not notifier._is_configured():
        print("❌ 飞书未配置（缺 app_id/app_secret/chat_id）")
        return

    print(f"✅ 飞书已配置: app_id={notifier._app_id[:6]}***, chat_id={notifier._chat_id[:6]}***")

    # 仅验证 token 获取（不发消息）
    try:
        token = await notifier._get_tenant_token()
        if token:
            print(f"✅ tenant_access_token 获取成功: {token[:10]}***")
        else:
            print("❌ tenant_access_token 获取失败（返回空）")
            await notifier.close()
            return
    except Exception as e:
        print(f"❌ tenant_access_token 获取异常: {e}")
        await notifier.close()
        return

    # 发送一条标记为系统验证的卡片（避免被误认为交易信号）
    verify_card = {
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "[系统验证] 飞书连通性测试"},
                "template": "grey",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**这是一条系统连通性验证消息**\n\n"
                            "非交易信号，请忽略。\n\n"
                            "验证项：\n"
                            "✅ 凭据配置正确\n"
                            "✅ tenant_access_token 获取成功\n"
                            "✅ 卡片消息发送链路正常\n"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "gugu 信号系统运维验证"}],
                },
            ],
        }
    }

    print("🚀 发送验证卡片到飞书群...")
    ok = await notifier.send_card(verify_card)
    print(f"  {'✅ 发送成功' if ok else '❌ 发送失败'}")
    await notifier.close()


if __name__ == "__main__":
    asyncio.run(main())
