#!/usr/bin/env python3
"""traepat standard 模型稳定性 overnight 测试（1小时，每5分钟3模型采样）"""
import json, time, random, urllib.request, urllib.error, pathlib, sys

BASE = "http://127.0.0.1:8787"
MODELS = ["traepat/kimi-k3", "traepat/deepseek-v4-flash", "traepat/glm-5.3",
          "traepat/qwen3.8-max", "traepat/minimax-m3"]
DURATION_S = 3600
INTERVAL_S = 300
SAMPLES_PER_ROUND = 3
OUT = pathlib.Path("/tmp/pat_stability_results.jsonl")

QUESTIONS = [
    "用一句话解释什么是递归",
    "9.11 和 9.9 哪个大？只回答结论",
    "把'今天天气不错'翻译成英文",
    "1 到 100 的整数和是多少？只回答数字",
    "说一个关于时间管理的建议，不超过20字",
]

def probe(model, q):
    body = {"model": model, "messages": [{"role": "user", "content": q}],
            "max_tokens": 256, "stream": False}
    req = urllib.request.Request(f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
            content = d["choices"][0]["message"].get("content", "") or ""
            # 剥离 <think>
            if "<think>" in content and "</think>" in content:
                content = content.split("</think>", 1)[1].strip()
            dt = time.time() - t0
            ok = bool(content.strip())
            return {"ok": ok, "latency": round(dt, 1), "content_head": content[:60],
                    "finish": d["choices"][0].get("finish_reason")}
    except urllib.error.HTTPError as e:
        try: detail = json.loads(e.read()).get("detail", "")
        except Exception: detail = ""
        return {"ok": False, "latency": round(time.time()-t0, 1), "error": f"HTTP {e.code}: {str(detail)[:80]}"}
    except Exception as e:
        return {"ok": False, "latency": round(time.time()-t0, 1), "error": f"{type(e).__name__}: {str(e)[:80]}"}

def main():
    random.seed()
    start = time.time()
    rnd = 0
    with open(OUT, "a") as f:
        while time.time() - start < DURATION_S:
            rnd += 1
            models = random.sample(MODELS, SAMPLES_PER_ROUND)
            q = random.choice(QUESTIONS)
            for m in models:
                r = probe(m, q)
                rec = {"round": rnd, "ts": time.strftime("%H:%M:%S"), "model": m, **r}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                mark = "✓" if r["ok"] else f"✗ {r.get('error','')[:60]}"
                print(f"[{rec['ts']}] R{rnd} {m.replace('traepat/',''):20s} {mark} ({r['latency']}s)", flush=True)
            # 睡到下一轮（每轮间隔 INTERVAL_S）
            elapsed = time.time() - start
            next_at = start + rnd * INTERVAL_S
            wait = max(5, next_at + INTERVAL_S - time.time())
            time.sleep(min(wait, INTERVAL_S))
    print("测试完成", flush=True)

if __name__ == "__main__":
    main()
