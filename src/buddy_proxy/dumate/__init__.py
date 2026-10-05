"""百度搭子（DuMate / 千帆桌面端）provider 包。

通过 DuMate 桌面 App 内置的本地代理（``dumate-main-server``，监听
``127.0.0.1`` 的 ``--port``）直连其 OpenAI 兼容网关，无需任何云端 token：

- 转发：``POST {base}/api/qianfanproxy/v1/chat/completions``（流式/非流式透传；
  Anthropic ``/v1/messages`` 经 protocols.anthropic_adapter 转回，与 kimi/qoder 同款）
- 鉴权：本地 header ``X-Dumate-Inapp-Key``（Electron 启动时生成的随机 hex，
  经子进程 env ``DUMATE_INAPP_KEY`` 共享，可从运行中的 ``dumate-main-server``
  进程环境里读取）
- 额度：数字积分走 App 的 bceConsole 通道 ``GET /api/dumate/points/quota_overview``
  （下划线版；camelCase 永远 500），见 :mod:`.checkin`；未登录时退回本地代理布尔态
  ``GET {base}/api/dumate/points/remaining`` → ``{"hasRemainingPoints": bool}``
- 消耗流水：``GET /api/dumate/points/records/usage``（bceConsole 通道，秒级时间戳）

前置条件：本机已安装并登录百度搭子（DuMate.app），且 App 正在运行。
"""
