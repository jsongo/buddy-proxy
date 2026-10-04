"""Qoder provider（Qwen3.8 / DeepSeek / GLM / Kimi 等）。

协议面与签名见 ``cosy`` 模块；凭据与登录见 ``credentials``。模块导览：

- ``cosy.py``        COSY 签名 + 自定义 body 编码
- ``catalog.py``     模型目录（上游拉取 + 本地兜底 + 别名归一）
- ``credentials.py`` 多账号存储 + token 自动刷新（kimi/antigravity 同款结构；
                     首访问自动把历史单账号 ``qoder_auth.json`` 迁移为账号 #1）
- ``failover.py``    冷却/顺位/账号状态（主备降级，内存态）
- ``campaigns.py``   每日活动权益（/sash 面）
- ``provider.py``    QoderProvider（多账号 failover 转发 + 流式闸门 + quota）
"""
