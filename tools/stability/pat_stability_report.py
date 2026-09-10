#!/usr/bin/env python3
"""汇总 pat_stability_results.jsonl → 输出稳定性结论 Markdown 报告"""
import json, pathlib, collections, statistics, sys

SRC = pathlib.Path("/tmp/pat_stability_results.jsonl")
OUT_DEFAULT = "/Users/jsongo/code/life/buddy-proxy/logs/pat_stability_report.md"

def main(out_path=None):
    rows = []
    for line in SRC.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    if not rows:
        print("无数据"); return

    by_model = collections.defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    lines = []
    lines.append("# traepat 普通模型稳定性 overnight 测试报告\n")
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]
    total = len(rows)
    ok_n = sum(1 for r in rows if r["ok"])
    lines.append(f"- 测试窗口: {t0} → {t1}（{total} 次请求，每 5 分钟随机 3 个不同厂商模型）")
    lines.append(f"- 总成功率: **{ok_n}/{total} = {ok_n/total*100:.0f}%**\n")

    lines.append("## 分模型对比\n")
    lines.append("| 模型 | 请求数 | 成功 | 成功率 | P50延迟 | P90延迟 | 典型错误 |")
    lines.append("|---|---|---|---|---|---|---|")
    for m, rs in sorted(by_model.items()):
        ok = [r for r in rs if r["ok"]]
        lats = sorted(r["latency"] for r in ok)
        p50 = lats[len(lats)//2] if lats else "-"
        p90 = lats[int(len(lats)*0.9)] if len(lats) > 1 else (lats[0] if lats else "-")
        errs = collections.Counter(r.get("error", "")[:50] for r in rs if not r["ok"])
        top_err = errs.most_common(1)[0][0] if errs else "-"
        lines.append(f"| {m.replace('traepat/','')} | {len(rs)} | {len(ok)} | {len(ok)/len(rs)*100:.0f}% | {p50}s | {p90}s | {top_err} |")
    lines.append("")

    # 时间线：状态切换点（成功↔失败）
    lines.append("## 可用性时间线\n")
    state = None
    for r in rows:
        s = "可用" if r["ok"] else "不可用"
        if s != state:
            lines.append(f"- {r['ts']} → **{s}**（{r['model'].replace('traepat/','')} {'成功' if r['ok'] else r.get('error','')[:60]}）")
            state = s
    lines.append("")

    # 错误分类
    lines.append("## 错误分类\n")
    errs = collections.Counter()
    for r in rows:
        if not r["ok"]:
            e = r.get("error", "")
            if "4031" in e or "额度冷却" in e: errs["429 上游日额度(4031)耗尽"] += 1
            elif "transport failed" in e: errs["502 传输失败(网络/内网)"] += 1
            elif "timed out" in e or "timeout" in e.lower(): errs["超时"] += 1
            else: errs[e[:60]] += 1
    for e, n in errs.most_common():
        lines.append(f"- {e}: {n} 次")
    lines.append("")

    # 结论
    lines.append("## 结论\n")
    if ok_n == 0:
        lines.append("整个测试窗口内全部请求失败（上游日额度 4031 未恢复）。"
                     "4031 的重置时间为每日 00:00；若测试窗口（凌晨）仍全部失败，"
                     "说明该额度按 24h 滚动窗口或计数口径与自然日不同，需白天复测。")
    elif ok_n == total:
        lines.append("整个窗口 100% 可用，上一轮的 4031 已恢复——判定为临时性限额，非永久封禁。")
    else:
        first_ok = next(r["ts"] for r in rows if r["ok"])
        lines.append(f"部分可用：首个成功点 {first_ok}。结合错误分类判断恢复时刻与原因；"
                     f"若恢复后持续稳定，则上游限额为间歇性/滚动窗口。")

    out = out_path or OUT_DEFAULT
    pathlib.Path(out).write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入 {out}")
    print("\n".join(lines))

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
