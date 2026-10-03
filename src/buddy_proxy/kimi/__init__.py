"""Kimi Code（月之暗面 kimi cli 同款 OAuth）通道。

上游是 Kimi 开放平台的 coding 端点（OpenAI chat-completions 兼容，
``{base_url}/v1/chat/completions``），OAuth 走标准 Device Flow——浏览器
打开授权链接、CLI 轮询换 token。模块导览：

- ``oauth.py``      Device Flow 协议层（授权/轮询/刷新，纯 HTTP）
- ``upstream.py``   上游请求/响应的纯函数（URL 归一、设备头、thinking 映射、额度解析）
- ``credentials.py`` 多账号存储 + token 自动刷新（antigravity 同款结构）
- ``failover.py``   冷却/顺位/账号状态（主备降级，内存态）
- ``login.py``      交互登录 + kimi cli 导出 JSON 导入
- ``provider.py``   KimiProvider（转发编排 + 流式闸门 + quota）

上游协议细节见各模块 docstring；kimi cli 官方实现（MoonshotAI/kimi-code
仓库 ``packages/oauth``）是端点与参数的权威来源。
"""
