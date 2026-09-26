"""统一登录入口：为各上游 provider 执行一次交互式登录/授权。

用法（一般通过 proxy.sh 调用，也可直接运行）：

    python -m buddy_proxy.auth.login codebuddy           # CodeBuddy（腾讯）浏览器授权
    python -m buddy_proxy.auth.login workbuddy           # 同 codebuddy（workbuddy 是其别名）
    python -m buddy_proxy.auth.login trae                # Trae Work (SOLO)：浏览器登录后粘贴回调链接
    python -m buddy_proxy.auth.login zcode               # 检查并打印 zcode 凭据配置指引（API key，无交互登录）
    python -m buddy_proxy.auth.login doubao              # 打印豆包（CDP）说明
    python -m buddy_proxy.auth.login mimo                # 检查并打印 mimo 凭据配置指引（API key 或桌面登录态）

可选参数：
    --no-browser    codebuddy 登录不自动打开浏览器，只打印授权链接

注意：登录态写入的凭据文件与网关共享（如 ~/.codebuddy-session.json）。
登录成功后若网关正在运行，需要 ``proxy.sh restart`` 才会加载新会话——
网关进程在启动时把 session 读进了内存，不会感知文件变化。
"""

from __future__ import annotations

import argparse
import os
import sys

# 别名归一：workbuddy 是 codebuddy 的旧称/别称（腾讯 WorkBuddy 同一产品线），
# 用户两种名字都可能敲，这里统一映射。
PROVIDER_ALIASES: dict[str, str] = {
    "workbuddy": "codebuddy",
    "cb": "codebuddy",
    # Qoder 常被敲成 quoder/qodor/qder（用户拼写变体），一并归一。
    "quoder": "qoder",
    "qodor": "qoder",
    "qder": "qoder",
    "qodercn": "qoder",
    "qoder-cn": "qoder",
}

KNOWN_PROVIDERS = ("codebuddy", "trae", "zcode", "doubao", "mimo", "qoder")


def _login_codebuddy(open_browser: bool = True) -> int:
    """CodeBuddy（copilot.tencent.com）浏览器 OAuth 登录。"""
    import json

    from buddy_proxy.codebuddy_provider.client import CodeBuddyClient, CodeBuddyError

    endpoint = os.getenv("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com")
    client = CodeBuddyClient(endpoint)
    # login() 只落盘 session 文件、不更新内存 session，且浏览器超时会静默返回。
    # 因此以「文件前后快照是否变化」判定登录是否真的完成：直接读内存旧会话
    # 会把超时误判成成功（旧 session 里还留着死 token），或把首登误判成失败。
    try:
        before = client.session_file.read_bytes()
    except FileNotFoundError:
        before = b""
    try:
        client.login(open_browser=open_browser)
    except CodeBuddyError as exc:
        print(f"[!] 登录失败: {exc}", file=sys.stderr)
        return 1
    try:
        after = client.session_file.read_bytes()
    except FileNotFoundError:
        after = b""
    if not after or after == before:
        print("[!] 登录未完成：浏览器授权超时或被取消，请重试", file=sys.stderr)
        return 1
    try:
        session = json.loads(after)
    except json.JSONDecodeError:
        print("[!] 登录成功但 session 文件解析失败", file=sys.stderr)
        return 1
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    if not auth.get("accessToken"):
        print("[!] 登录流程结束但 session 中没有 accessToken", file=sys.stderr)
        return 1
    print()
    print(f"[OK] CodeBuddy 登录成功（{endpoint}）")
    if account.get("nickname") or account.get("uid"):
        print(f"    账号: {account.get('nickname') or ''} uid={account.get('uid')}")
    print(f"    会话已写入 {client.session_file}")
    return 0


def _login_trae(open_browser: bool = True, **_kwargs) -> int:
    """Trae Work (SOLO) 一键登录：自动起本地回调服务 → 打开登录页 → 自动落盘退出。

    trae.cn 授权页（auth_type=local）要求本机 18080 回调服务在线，否则页面报
    「登录失败 - 网络错误」。这里自动拉起 trae_work_login_server，回调服务会在
    **成功或失败时都写 RESULT_PATH**，CLI 立刻收割结果——不能只把「STATE 被删」
    当成功信号，那样任何失败（nonce 被冲掉 / 缺 refreshToken / 换 token 报错）
    都会让 CLI 干等到 15 分钟超时，浏览器那边却可能已经显示「登录成功」。
    手动粘贴模式保留：python3 -m buddy_proxy.auth.trae_work_login
    """
    import json
    import socket
    import subprocess
    import sys
    import time
    import webbrowser

    from buddy_proxy.auth.trae_work_login import (
        OUT_PATH,
        RESULT_PATH,
        STATE_PATH,
        STATE_TTL,
        build_login_url,
    )

    def _port_busy() -> bool:
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", 18080)) == 0

    def _stop(proc: subprocess.Popen) -> None:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _report_ok() -> int:
        try:
            cred = json.loads(OUT_PATH.read_text())
            expires = cred.get("expires_at", "")
            expires = expires[:10] if isinstance(expires, str) else expires
            print(
                f"[OK] Trae Work 登录完成：uid={cred.get('uid')} "
                f"昵称={cred.get('nickname')} 有效期至={expires}"
            )
        except Exception:
            print("[OK] Trae Work 登录完成")
        return 0

    url, _machine_id, _device_id, _port = build_login_url()
    # 本次尝试的 nonce：只认跟它对得上的 RESULT，免得上一轮迟到的结果误杀本轮
    try:
        my_nonce = json.loads(STATE_PATH.read_text()).get("nonce") or ""
    except Exception:
        my_nonce = ""
    proc = None
    if _port_busy():
        # 端口被占不一定是我们的回调服务（也可能是别的程序或旧实例）。
        # 这种情况回调可能被黑洞掉，必须明说，别让人以为「已复用」就万事大吉。
        print("[!] 18080 已被占用，将直接复用（**请确认它确实是本项目的回调服务**；")
        print("    若不是，回调会被吞掉，登录会一直停在这里）")
    else:
        proc = subprocess.Popen(
            [sys.executable, "-m", "buddy_proxy.auth.trae_work_login_server"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        for _ in range(30):
            if _port_busy():
                break
            if proc.poll() is not None:
                print("[!] 本地回调服务启动失败，请检查 18080 端口占用", file=sys.stderr)
                return 1
            time.sleep(0.1)

    print("=" * 60)
    print("Trae Work (SOLO) 一键登录")
    print("=" * 60)
    print("浏览器将打开 trae.cn 授权页；完成登录后会自动回跳本机落盘凭证。")
    if open_browser:
        webbrowser.open(url)
    else:
        print(f"请手动在浏览器打开：\n  {url}")
    print("\n[*] 等待登录完成（最长 15 分钟，Ctrl+C 取消）...")

    deadline = time.time() + STATE_TTL
    try:
        while time.time() < deadline:
            # RESULT 是权威终态（成功/失败都会写），必须最先看
            if RESULT_PATH.exists():
                try:
                    result = json.loads(RESULT_PATH.read_text())
                except Exception:
                    result = {"ok": False, "message": "登录结果文件损坏"}
                RESULT_PATH.unlink(missing_ok=True)
                # 结果带 nonce 标记：不是本轮的就丢掉继续等（上一轮迟到的结果）
                got_nonce = result.get("nonce") or ""
                if got_nonce and my_nonce and got_nonce != my_nonce:
                    continue
                STATE_PATH.unlink(missing_ok=True)
                if result.get("ok"):
                    # server 还会跑一下 Work 通道测试再退出，稍等收割它的输出；
                    # 极端情况（挂住）超时再强制清理
                    if proc is not None:
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            _stop(proc)
                    return _report_ok()
                _stop(proc)
                print(f"[!] Trae 登录失败：{result.get('message')}", file=sys.stderr)
                print(
                    "    可重试 buddy login trae；仍不行就用手动模式："
                    "python3 -m buddy_proxy.auth.trae_work_login",
                    file=sys.stderr,
                )
                return 1
            # 兜底：旧版 server 只删 STATE 不写 RESULT，仍按成功收割
            if not STATE_PATH.exists():
                if proc is not None:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _stop(proc)
                return _report_ok()
            if proc is not None and proc.poll() is not None:
                print("[!] 本地回调服务意外退出", file=sys.stderr)
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()

    _stop(proc)
    STATE_PATH.unlink(missing_ok=True)
    RESULT_PATH.unlink(missing_ok=True)
    print(
        "[!] 登录未完成（超时/取消）。若浏览器那边已经显示登录成功，说明回调"
        "没落到本机服务上——上面若有 [srv] 访问日志可对照；没有则表示浏览器"
        "压根没回跳（常见于授权页把回调 URL 的 query/path 改写了）。",
        file=sys.stderr,
    )
    print(
        "    可重试 buddy login trae，或手动模式："
        "python3 -m buddy_proxy.auth.trae_work_login",
        file=sys.stderr,
    )
    return 1


def _login_zcode(**_kwargs) -> int:
    """zcode 无交互登录：凭据是 API key，这里检查配置并打印指引。"""
    from buddy_proxy.providers.zcode import resolve_credentials

    key, base = resolve_credentials()
    if key:
        print(f"[OK] zcode 凭据已配置: {key[:6]}***{key[-4:]}  base: {base}")
        print("    无需登录；如需换号，改下面任意一处配置即可：")
        print("    1. 环境变量 ZCODE_API_KEY")
        print("    2. ~/.ethan/.secrets/zcode_api_key")
        print("    3. ~/.zcode/v2/config.json（ZCode CLI 登录态里的 apiKey）")
        return 0
    print("[!] zcode 未配置凭据，按以下任意一种方式配置：")
    print("    1. 环境变量 ZCODE_API_KEY=<智谱 coding-plan API key>")
    print("    2. 写入文件 ~/.ethan/.secrets/zcode_api_key（首行裸 key 或 name=value）")
    print("    3. 在本机 ZCode CLI 登录 coding-plan（自动读取 ~/.zcode/v2/config.json）")
    return 1


def _login_doubao(**_kwargs) -> int:
    """豆包走 CDP 直连豆包工作 App，无独立登录流程。"""
    print("豆包 provider 无独立登录：它通过 Chrome CDP 复用本机豆包工作 App 的登录态。")
    print("启动时加 --doubao（或 DOUBAO_ENABLED=1）即可，无需 proxy.sh login。")
    return 0


def _login_mimo(**_kwargs) -> int:
    """mimo 无交互登录：API key 或复用 MiMo 桌面的小米账号登录态。"""
    from buddy_proxy.mimo.credentials import resolve_api_key
    from buddy_proxy.mimo.sso import load_account_cookies

    key, base = resolve_api_key()
    if key:
        print(f"[OK] mimo 凭据已配置(API key): {key[:4]}…{key[-2:]}  base: {base}")
        print("    如需换号，改下面任意一处配置即可：")
        print("    1. 环境变量 MIMO_API_KEY（配 MIMO_BASE_URL 可切 billing/token-plan）")
        print("    2. ~/.mimocode/auth.json（MiMo 桌面「API Key」模式会写这份）")
        print("    3. ~/.buddy-proxy/mimo_api_key.json")
        return 0

    account = load_account_cookies()
    if account is not None:
        print(f"[OK] mimo 将复用 MiMo 桌面登录态: userId={account.user_id}")
        print("    （本 provider 自动两阶段换 mimopc serviceToken，无需额外配置）")
        print("    若要改用 API key，配置 MIMO_API_KEY 或上述文件即可。")
        return 0

    print("[!] mimo 未配置凭据，按以下任意一种方式配置：")
    print("    1. 环境变量 MIMO_API_KEY=<platform.xiaomimimo.com 开的 key>")
    print("    2. 在本机 MiMo Desktop 登录小米账号（本 provider 自动读取其 cookie）")
    print("    3. 写入 ~/.buddy-proxy/mimo_api_key.json: {\"api_key\": \"...\", \"base_url\": \"...\"}")
    return 1


def _login_qoder(open_browser: bool = True, **_kwargs) -> int:
    """Qoder 登录：官方 device flow（PKCE），全球版/CN 版通用。

    流程：打印（并尝试打开）授权链接 → 用户在浏览器里确认 → 轮询换回
    ``dt-`` device token，写入 ``~/.buddy-proxy/qoder_auth.json``。

    区域由 ``QODER_REGION`` 决定（``cn`` 默认 / ``global``），CN 与全球版
    账号不通用，连错域会 401。
    """
    import asyncio

    from buddy_proxy.qoder.config import REGIONS, default_region_key, resolve_region
    from buddy_proxy.qoder.credentials import (
        AuthError,
        auth_state_path,
        poll_device_flow,
        start_device_flow,
    )

    if os.environ.get("QODER_TOKEN", "").strip():
        print("[!] 检测到环境变量 QODER_TOKEN 已设置——它会优先于登录结果生效。")
        print("    如需改用登录态，请先 unset QODER_TOKEN。")

    region = resolve_region()
    print(f"[Qoder] 区域: {region.label} ({region.key})  端点: {region.infer_base}")
    print(f"        可用 QODER_REGION 切换区域：{', '.join(REGIONS)}")

    flow = start_device_flow(region)
    print()
    print("[Qoder] 请在浏览器中打开下面的链接，并用 Qoder 账号完成授权：")
    print()
    print(f"    {flow.auth_url}")
    print()
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(flow.auth_url)
            print("[Qoder] 已尝试自动打开浏览器…")
        except Exception as exc:  # noqa: BLE001 - 打不开浏览器不算失败
            print(f"[Qoder] 自动打开浏览器失败（{exc}），请手动复制上面的链接。")
    print("[Qoder] 等待授权中（最多 10 分钟，Ctrl-C 可取消）…")

    ticks = {"n": 0}

    def _tick() -> None:
        ticks["n"] += 1
        if ticks["n"] % 6 == 0:
            print(f"    …仍在等待授权（已等待约 {ticks['n'] * 5} 秒）")

    try:
        cred = asyncio.run(poll_device_flow(flow, on_tick=_tick))
    except KeyboardInterrupt:
        print("\n[Qoder] 已取消。")
        return 1
    except AuthError as exc:
        print(f"\n[X] Qoder 登录失败: {exc}")
        return 1

    print()
    print(f"[OK] Qoder 登录成功：{cred.describe()}")
    print(f"     状态文件: {auth_state_path()}")
    print("     启动代理时加 --qoder（或 QODER_ENABLED=1）即可启用该通道。")
    return 0


_DISPATCH = {
    "codebuddy": _login_codebuddy,
    "trae": _login_trae,
    "zcode": _login_zcode,
    "doubao": _login_doubao,
    "mimo": _login_mimo,
    "qoder": _login_qoder,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="buddy_proxy.auth.login",
        description="各上游 provider 的统一登录入口（provider 支持 workbuddy=codebuddy 别名）",
    )
    parser.add_argument("provider", nargs="?", default="codebuddy",
                        help="codebuddy(=workbuddy) / trae / zcode / doubao / mimo / qoder(=quoder)，默认 codebuddy")
    parser.add_argument("--no-browser", action="store_true",
                        help="codebuddy/trae 登录不自动打开浏览器，只打印链接")
    args = parser.parse_args()

    provider = PROVIDER_ALIASES.get(args.provider.strip().lower(), args.provider.strip().lower())
    handler = _DISPATCH.get(provider)
    if handler is None:
        parser.error(f"未知 provider: {args.provider}（支持: {', '.join(KNOWN_PROVIDERS)}，"
                     f"workbuddy 是 codebuddy 的别名）")
    return handler(open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
