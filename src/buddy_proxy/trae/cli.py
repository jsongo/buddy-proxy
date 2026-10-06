"""Trae 账号 CLI：签到/积分/权益查询与对话测试。

python -m buddy_proxy.trae_provider status|claim|usage|chat
"""

from __future__ import annotations

import json
import logging
import sys

from .benefits_api import claim_checkin_credits, fetch_checkin_status, fetch_ent_usage
from .credentials import _auth, _load_work_cred
from .sse import _parse_sse, _trae_error_text
from .transport import send_trae_chat

log = logging.getLogger(__name__)

# ───────────────────────── CLI 入口 ─────────────────────────

def _cli() -> None:
    """命令行工具：查询/领取积分、测试对话。示例：

    python -m buddy_proxy.trae_provider status   # 签到/积分状态
    python -m buddy_proxy.trae_provider claim    # 领取今日签到积分
    python -m buddy_proxy.trae_provider usage    # 权益/用量
    python -m buddy_proxy.trae_provider usage --region global   # 海外额度（美元）
    python -m buddy_proxy.trae_provider chat -m glm-5.2 -q "你好"

    ``--region``（cn/global）只影响 UG 接口取址与计费口径展示；chat 的区域
    由**当前 work 凭证自己的 region** 决定（transport 内解析），不用传。
    """
    import argparse

    from .config import TRAE_REGIONS, resolve_trae_region

    parser = argparse.ArgumentParser(
        prog="trae_provider",
        description="Trae 账号工具（签到/积分/对话）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_region(p):
        p.add_argument("--region", choices=sorted(TRAE_REGIONS), default=None,
                       help="cn（默认）/ global；海外无签到，status/claim 直接报跳过")

    p_status = sub.add_parser("status", help="查询签到/积分状态")
    _add_region(p_status)
    p_claim = sub.add_parser("claim", help="领取今日签到积分")
    _add_region(p_claim)
    p_usage = sub.add_parser("usage", help="查询权益/用量")
    _add_region(p_usage)
    p_chat = sub.add_parser("chat", help="发一条对话测试")
    p_chat.add_argument("-m", "--model", default="glm-5.2", help="模型名")
    p_chat.add_argument("-q", "--query", default="用一句话介绍你自己", help="问题")

    args = parser.parse_args()
    reg = resolve_trae_region(getattr(args, "region", None))

    if args.cmd == "status":
        data = fetch_checkin_status(region=reg.key)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if data.get("no_checkin"):
            print(f"\n{reg.label} 无每日签到系统（上游无 checkin 端点，实测 404）")
        elif data.get("credits") is not None:
            print(f"\n可用积分: {data['credits']} | 今日已签到: {data.get('checked_in')}")
    elif args.cmd == "claim":
        data = claim_checkin_credits(region=reg.key)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if data.get("no_checkin"):
            print(f"\n{reg.label} 无每日签到系统，无可领取")
        elif data.get("code") == 0:
            print(f"\n✅ 签到成功，今日积分 +{data.get('credits_granted', '?')}")
        else:
            print(f"\n签到失败: {data.get('message', data)}")
    elif args.cmd == "usage":
        # 优先取**同区**账号的 token：legacy 的 _auth()/_load_work_cred() 只会给
        # 到 CN 凭据，拿它打海外 UG 端点必 401（两区账号不通用）。
        token, uid = "", ""
        try:
            from . import failover
            from .credentials import ensure_account_token

            accts = failover.available_accounts(reg.key)
            if accts:
                token, _cred = ensure_account_token(accts[0].id)
                uid = _cred.get("uid", "")
        except Exception as e:  # noqa: BLE001 - 退回 legacy 路径，错误在下面报
            log.debug("trae usage 取 %s 区账号失败: %s", reg.key, e)
        if not token:
            try:
                token, uid = _auth()
            except Exception:
                work = _load_work_cred() or {}
                token, uid = work.get("access_token", ""), work.get("uid", "")
        if not token:
            print(f"Trae 无 {reg.label} 凭据：请先 `buddy login trae --region {reg.key}`")
            return
        data = fetch_ent_usage(token, uid, region=reg.key)
        # 计费口径：上游 flag 权威（is_dollar_usage_billing），区域兜底
        dollar = bool(data.get("is_dollar_usage_billing")) or (
            not data.get("is_credits_billing") and reg.billing == "dollar")
        unit = "$" if dollar else "积分"
        us = data.get("usage_summary", {})
        print(f"[{reg.label} · {'美元 Usage' if dollar else '积分'}] "
              f"总额度: {us.get('total_amount')} {unit} | 已用: {us.get('consumed_amount')} "
              f"({us.get('consumption_ratio', 0) * 100:.1f}%)")
        for p in data.get("user_entitlement_pack_list", []):
            eb = p.get("entitlement_base_info", {})
            print(f"权益包: {p.get('display_desc')} | status={eb.get('ent_status')} "
                  f"| end={eb.get('end_time')} | endpoint={eb.get('available_endpoint')}")
    elif args.cmd == "chat":
        # send_trae_chat 内部会自动路由 Work 通道（有 Work 凭证时），
        # 无需在此重复 if/else 分派
        raw = send_trae_chat(
            [{"role": "user", "content": [{"type": "text", "text": args.query}]}],
            model=args.model, stream=True,
        )
        for event, data in _parse_sse(raw):
            if event == "error":
                # Trae 走 HTTP 200 + event: error（如免费账号撞 4011 限额），
                # 必须显式报错退出，否则打印空白像成功
                print(f"[错误] {_trae_error_text(data)}", file=sys.stderr)
                sys.exit(1)
            if event == "output":
                if data.get("reasoning_content"):
                    print(f"[思考] {data['reasoning_content']}")
                if data.get("response"):
                    print(f"[回答] {data['response']}")


if __name__ == "__main__":
    _cli()
