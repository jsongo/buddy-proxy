"""统一登录入口：为各上游 provider 执行一次交互式登录/授权。

用法（一般通过 proxy.sh 调用，也可直接运行）：

    python -m buddy_proxy.login codebuddy           # CodeBuddy（腾讯）浏览器授权
    python -m buddy_proxy.login workbuddy           # 同 codebuddy（workbuddy 是其别名）
    python -m buddy_proxy.login trae                # Trae Work (SOLO)：浏览器登录后粘贴回调链接
    python -m buddy_proxy.login zcode               # 检查并打印 zcode 凭据配置指引（API key，无交互登录）
    python -m buddy_proxy.login doubao              # 打印豆包（CDP）说明

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
}

KNOWN_PROVIDERS = ("codebuddy", "trae", "zcode", "doubao")


def _login_codebuddy(open_browser: bool = True) -> int:
    """CodeBuddy（copilot.tencent.com）浏览器 OAuth 登录。"""
    import json

    from buddy_proxy.codebuddy_client import CodeBuddyClient, CodeBuddyError

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
    「登录失败 - 网络错误」。这里自动拉起 trae_work_login_server，授权成功后
    回调服务会删除 state 文件，以此作为完成信号。手动粘贴模式保留：
    python3 -m buddy_proxy.trae_work_login
    """
    import json
    import socket
    import subprocess
    import sys
    import time
    import webbrowser

    from buddy_proxy.trae_work_login import (
        OUT_PATH,
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

    url, _machine_id, _device_id = build_login_url()
    proc = None
    if _port_busy():
        print("[*] 18080 端口已有回调服务在监听，直接复用")
    else:
        proc = subprocess.Popen(
            [sys.executable, "-m", "buddy_proxy.trae_work_login_server"],
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
            if proc is not None and proc.poll() is not None:
                print("[!] 本地回调服务意外退出", file=sys.stderr)
                return 1
            if not STATE_PATH.exists():
                # 回调服务成功落盘凭证后删除 state 文件（一次性消费）
                time.sleep(3)  # 给服务端留时间打印 Work 通道测试输出
                _stop(proc)
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
            time.sleep(1)
    except KeyboardInterrupt:
        print()

    _stop(proc)
    print(
        "[!] 登录未完成（超时/取消）。可重试 buddy login trae，"
        "或手动模式：python3 -m buddy_proxy.trae_work_login",
        file=sys.stderr,
    )
    return 1


def _login_zcode(**_kwargs) -> int:
    """zcode 无交互登录：凭据是 API key，这里检查配置并打印指引。"""
    from buddy_proxy.zcode_provider import resolve_credentials

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


_DISPATCH = {
    "codebuddy": _login_codebuddy,
    "trae": _login_trae,
    "zcode": _login_zcode,
    "doubao": _login_doubao,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="buddy_proxy.login",
        description="各上游 provider 的统一登录入口（provider 支持 workbuddy=codebuddy 别名）",
    )
    parser.add_argument("provider", nargs="?", default="codebuddy",
                        help="codebuddy(=workbuddy) / trae / zcode / doubao，默认 codebuddy")
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
