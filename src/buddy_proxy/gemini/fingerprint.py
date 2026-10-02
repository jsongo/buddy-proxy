"""请求指纹：让代理发出的请求与本机 gemini CLI（OAuth 免费通道）逐字节对齐。

封号风险的根源是「服务端看到一群自称 Gemini CLI 的流量，指纹却各不相同」。
cpa-plugin-gemini-cli 硬编码的指纹跟真 CLI 有多处实质差异（实测本机
gemini-cli 0.33.1 源码逐项比对）：

1. User-Agent
   - 真 CLI（core/contentGenerator.js）：
     ``GeminiCLI/<version>/<model> (<platform>; <arch>)``，随后 google-auth-library
     的 transporter（transporters.js 41-46 行）发现 UA 里没有
     ``google-api-nodejs-client/`` 时**追加** `` google-api-nodejs-client/<版本>``。
     最终形如 ``GeminiCLI/0.33.1/gemini-2.5-flash (darwin; arm64) google-api-nodejs-client/9.15.1``。
   - 插件：``GeminiCLI/0.34.0/<model> (darwin; arm64; terminal)``——多了真 CLI
     没有的 ``; terminal``，且没有 auth 库后缀。版本也对不上（0.34.0 是猜的）。
2. x-goog-api-client
   - 真 CLI OAuth 链路（LOGIN_WITH_GOOGLE → CodeAssistServer → OAuth2Client
     transporter）只发 ``gl-node/<node版本>``（去 v 前缀，transporters.js 47-50 行）。
     ``google-genai-sdk/...`` 前缀只在 API-key 路径（@google/genai SDK）出现。
   - 插件：发的是 ``google-genai-sdk/1.41.0 gl-node/v22.19.0``——OAuth 请求带了
     API-key 路径才有的头，本身就是矛盾指纹。
3. Accept
   - 真 CLI 经 gaxios/undici，未显式设置 Accept（undici 默认 ``*/*``）。
   - 插件显式发 ``application/json`` / ``text/event-stream``。
   这里折中：非流式不带 Accept（对齐真 CLI），流式必须带
   ``text/event-stream``（SSE 解析需要；真 CLI 的 undici 对 stream 响应也会带）。
4. 请求体
   - 真 CLI 顶层带 ``user_prompt_id``（13 位随机 hex，gemini.js:523），
     ``request.session_id``（UUID，utils/session.js:7）；**不注入 safetySettings**。
   - 插件不发 session_id，反而注入 5 条 safetySettings=OFF——真 CLI 全源码
     搜不到 HARM_CATEGORY，这是多出来的字段。
"""

from __future__ import annotations

import platform
import sys

#: 对齐本机安装的 gemini CLI 版本（0.33.1）。升级 CLI 后应同步这里。
GEMINI_CLI_VERSION = "0.33.1"

#: google-auth-library 9.15.1（gemini-cli 0.33.1 锁定的版本）追加的 UA 后缀。
AUTH_LIBRARY_UA_SUFFIX = "google-api-nodejs-client/9.15.1"

#: x-goog-api-client：OAuth 链路只有 gl-node/<版本>（去 v 前缀）。
#: 用当前进程的 Python 跑不出 node 版本——写死一个当前主流 LTS 指纹；
#: 真实性靠整体一致性，单个版本号差异服务端无法甄别（用户 node 版本本就多样）。
GOOG_API_CLIENT = "gl-node/22.19.0"


def user_agent(model: str) -> str:
    """构造与真 CLI + auth 库叠加后一致的 User-Agent。"""
    plat = platform.system().lower()  # darwin / linux / windows
    if plat == "windows":
        plat = "win32"
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    model = (model or "").strip() or "unknown"
    return (
        f"GeminiCLI/{GEMINI_CLI_VERSION}/{model} ({plat}; {arch})"
        f" {AUTH_LIBRARY_UA_SUFFIX}"
    )


def auth_headers(access_token: str, model: str, stream: bool) -> dict[str, str]:
    """generateContent 请求头（逐项对齐真 CLI OAuth 链路）。"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent(model),
        "x-goog-api-client": GOOG_API_CLIENT,
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def metadata_headers() -> dict[str, str]:
    """loadCodeAssist / onboardUser 等管理接口的请求头。

    这些调用在真 CLI 里同样走 CodeAssistServer.requestPost（同一个
    transporter），所以指纹与对话请求一致，只是 model 不进 UA。
    """
    return auth_headers(access_token="", model="", stream=False)


def node_version_for_tests() -> str:  # pragma: no cover - 测试辅助
    return sys.version
