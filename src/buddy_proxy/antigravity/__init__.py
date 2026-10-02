"""Antigravity 免费通道（Google cloudcode-pa，Gemini/Claude/GPT 多模型）。

模块结构与 gemini 子包同构：credentials（OAuth 存储/刷新）→ login（PKCE +
loopback 回调）→ setup（loadCodeAssist/onboardUser onboarding）→ convert
（envelope 包装，内层复用 gemini.convert）→ provider（转发 + 流转换）。
"""
