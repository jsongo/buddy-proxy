"""Qoder 通道常量与端点解析。

Qoder 有两代协议面，本模块把「用哪一面 / 对哪个域名」集中成一处：

1. **原生 Completions 面**（``/model/v1/chat/completions``）——标准 OpenAI
   线缆（Bearer ``dt-`` token + 普通 SSE），但**不提供 Qwen3.8 系列**，
   只有 ``auto/ultimate/performance/efficient/qmodel/kmodel/dmodel/...``。
2. **COSY 面**（``/algo/api/v2/service/pro/sse/agent_chat_generation``）——
   COSY 签名 + 自定义编码 body + SSE 信封，是 **Qwen3.8-Max / Flash 唯一
   可用的一面**。

两个区域（region）各有一套域名，模型目录与会话流程完全一致：

- ``global``：api3.qoder.sh / openapi.qoder.sh / qoder.com
- ``cn``    ：gateway.qoder.com.cn / openapi.qoder.com.cn / qoder.com.cn

端点还支持从 Qoder 客户端自己的 ``endpoint-cache.json`` 读取（桌面端会做
区域发现并落盘），因此这里只保留**兜底常量**与探测顺序。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 区域定义
# ---------------------------------------------------------------------------

#: COSY 面向的 CLI 版本（签名里的 cosyVersion，需与所调端点接受的版本一致）。
COSY_VERSION = "1.1.57"

#: device flow 的 client_id（全球版与 CN 版实测同值）。
CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"

#: 备用 client_id（客户端在少数场景使用的另一枚）。
CLIENT_ID_ALT = "e93fe488-5778-4c35-a6fc-0f54ed7b3139"

#: COSY 身份加密用的 RSA 公钥（1024-bit，PKCS#1 v1.5）。
COSY_RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

#: 自定义 base64 字母表（COSY body 编码）。
CUSTOM_B64_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"

#: 标准 base64 字母表（编码时逐位映射到上面的自定义表）。
STD_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


@dataclass(frozen=True)
class Region:
    """一个区域的端点集合。"""

    key: str
    label: str
    #: COSY / 推理面基址（聊天与目录都挂它下面）。
    infer_base: str
    #: openapi 基址（额度、userinfo、deviceToken）。
    openapi_base: str
    #: 浏览器授权页基址。
    auth_base: str
    #: 桌面端配置目录名（``~/.qoder`` / ``~/.qoder-cn``）。
    config_dirname: str

    def model_list_url(self) -> str:
        """模型目录（需 COSY 签名）。"""
        return f"{self.infer_base}/algo/api/v2/model/list?Encode=1"

    def chat_url(self) -> str:
        """COSY 聊天面（Qwen3.8 系列唯一入口）。"""
        return (
            f"{self.infer_base}/algo/api/v2/service/pro/sse/agent_chat_generation"
            "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
        )

    def native_chat_url(self) -> str:
        """原生 Completions 面（无 COSY 签名，但只有非 3.8 模型）。"""
        return f"{self.infer_base}/model/v1/chat/completions"

    def quota_url(self) -> str:
        """额度用量。"""
        return f"{self.openapi_base}/api/v2/quota/usage"

    def userinfo_url(self) -> str:
        """用户信息（便于展示账号名/套餐）。"""
        return f"{self.openapi_base}/api/v1/userinfo"

    def device_poll_url(self) -> str:
        return f"{self.openapi_base}/api/v1/deviceToken/poll"

    def device_refresh_url(self) -> str:
        return f"{self.openapi_base}/api/v1/deviceToken/refresh"

    def job_token_exchange_url(self) -> str:
        return f"{self.openapi_base}/api/v1/jobToken/exchange"

    def auth_url(self) -> str:
        return f"{self.auth_base}/device/selectAccounts"


#: 已知区域。全球版与 CN 版各自一套；默认区域可由环境变量覆盖。
REGIONS: dict[str, Region] = {
    "global": Region(
        key="global",
        label="Qoder Global",
        infer_base="https://api3.qoder.sh",
        openapi_base="https://openapi.qoder.sh",
        auth_base="https://qoder.com",
        config_dirname=".qoder",
    ),
    "cn": Region(
        key="cn",
        label="Qoder CN",
        infer_base="https://gateway.qoder.com.cn",
        openapi_base="https://openapi.qoder.com.cn",
        auth_base="https://qoder.com.cn",
        config_dirname=".qoder-cn",
    ),
}

#: 探测顺序：CN 走 CN 域，全球走全球域。
_PREFERRED = {"cn": ("cn", "global"), "global": ("global", "cn")}


def default_region_key() -> str:
    """默认区域：``QODER_REGION`` 显式指定 > 探测本地客户端 > ``cn``。

    CN 与全球版账号不通用，误连会 401，因此区域必须显式可配。
    """
    env = (os.environ.get("QODER_REGION") or "").strip().lower()
    if env in REGIONS:
        return env
    return "cn"


def resolve_region(key: str | None = None) -> Region:
    """按 key 取区域；未知 key 回退默认区域。"""
    k = (key or default_region_key()).strip().lower()
    return REGIONS.get(k) or REGIONS[default_region_key()]


def region_candidates(key: str | None = None) -> list[Region]:
    """返回按优先级排序的区域候选（用于探测 endpoint-cache）。"""
    k = (key or default_region_key()).strip().lower()
    order = _PREFERRED.get(k, (k,))
    out = [REGIONS[c] for c in order if c in REGIONS]
    for name, reg in REGIONS.items():
        if reg not in out:
            out.append(reg)
    return out


def cached_endpoints(config_dirname: str) -> dict[str, str]:
    """读 Qoder 客户端落盘的 ``endpoint-cache.json``（区域发现结果）。

    桌面端/CLI 会把自己选中的端点写进缓存；读到就直接用，省一次探测。
    文件不存在或结构异常时返回空 dict。
    """
    path = Path.home() / config_dirname / ".cache" / "endpoint-cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = (data or {}).get("entries") or {}
    prod = entries.get("prod") or {}
    out: dict[str, str] = {}
    for src, dst in (
        ("endpoint", "infer_base"),
        ("inferEndpoints", "infer_base"),
        ("openapiEndpoint", "openapi_base"),
        ("openapiEndpoints", "openapi_base"),
    ):
        if dst in out:
            continue
        value = prod.get(src)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, str) and value.strip():
            out[dst] = value.strip().rstrip("/")
    return out


def with_cached_endpoints(region: Region) -> Region:
    """用客户端缓存里的端点覆盖区域兜底值（缓存优先）。"""
    cached = cached_endpoints(region.config_dirname)
    if not cached:
        return region
    return Region(
        key=region.key,
        label=region.label,
        infer_base=cached.get("infer_base", region.infer_base),
        openapi_base=cached.get("openapi_base", region.openapi_base),
        auth_base=region.auth_base,
        config_dirname=region.config_dirname,
    )
