# CodeBuddy Proxy

> 一个轻量级本地代理，把 CodeBuddy 底层的聊天接口转换成标准的 **OpenAI Chat Completions**、**Responses** 和 **Anthropic Messages** 协议——让你能把 CodeBuddy 模型接到 Codex CLI、Claude Code / CC Switch、OpenCode、Grok、Oh My Pi 以及任意 OpenAI 兼容客户端上。

> **English docs: [README.md](README.md).**

---

## 特性

- **协议转换** — `/v1/chat/completions`(OpenAI)、`/v1/responses`(Codex CLI)、`/v1/messages`(Anthropic / Claude Code)
- **管理界面** — 内置 Web 控制台 `/ui`：按 provider 分组管理模型、一键「设为默认启用模型」、每个模型一键测试（发条 hi）、按模型维度聚合请求统计图表。按页签懒加载，首屏不会卡在最慢的日志接口上
- **模型列表** — `/v1/models` 返回 OpenAI 兼容的模型列表，附带每个模型的完整元数据（上下文窗口、积分倍率、输入模态 / 图片支持）
- **脱敏**（`--desensitize`）— 向 system 消息里的合规关键词插入零宽空格，避免后端关键词审核误拦
- **消息压缩**（`--optimize-context`）— 压缩长历史 / 大 schema / 超大工具输出，大幅降低 token 消耗
- **工具调用** — 完整的 function calling 支持，自动过滤无效工具定义；`tool_choice` 在 OpenAI / Anthropic 两种形态之间统一归一化，绝不会以 object 形式发给上游
- **DSML 解析** — 自动识别并转换 DeepSeek Markup Language 工具调用
- **流式输出** — SSE 实时返回，带空闲 / 总时长双重超时保护
- **多账号** — 隔离的 session 文件，方便工作 / 个人账号切换
- **多 Provider** — 除 CodeBuddy 外，内置 **Trae**（解密 Trae IDE 登录态直连底层模型）、**ZCode**（智谱 GLM）、**豆包**（纯 stdlib CDP 直连豆包工作 App）、**小米 MiMo**（API key，或复用 MiMo 桌面登录态）与 **Qoder**（COSY 签名纯 Python 复刻，千问3.8 / GLM / Kimi），统一经 `/v1/models` 列出、按模型名路由
- **双协议** — 同一批模型同时提供 OpenAI（`/v1/chat/completions`）与 Anthropic（`/v1/messages`，即 Claude Code）；各 provider 负责把响应转回客户端要的协议

---

## 安装与启动

代理是 `src/` 下的一个普通 Python 包，用 [uv](https://docs.astral.sh/uv/) 从源码运行：

```bash
uv sync
uv run python -m buddy_proxy --desensitize
```

首次运行会自动创建状态目录 `~/.buddy-proxy/`（权限 `0700`，可用
`BUDDY_PROXY_STATE_DIR` 整体覆盖）。机器本地的东西都在这里：`settings.json`
（默认模型、已停用模型、限时窗口）、Trae Work 凭证 `trae_work.json`、PAT token
缓存 `trae_pat_token.json`、客户端名映射 `buddy_client_names.json`。目录内含凭证，
注意别被备份或版本控制带走。启动时会以 `[State] ...` 打印实际路径。

首次使用需要登录（会打开浏览器）：

```bash
uv run python -m buddy_proxy --login --desensitize
```

默认监听 `http://127.0.0.1:8787`，管理界面在 **http://127.0.0.1:8787/ui**。

### `buddy` 命令（推荐）

`buddy` 是日常使用的入口：一条命令启动 + 自动打开管理页，也可以把代理注册成 macOS 系统服务（launchd，登录自启 + 崩溃自动拉起）。

```bash
./buddy start              # 启动（未运行时）并打开 http://127.0.0.1:8787/ui
./buddy stop / restart / status / logs
./buddy login [provider]   # 登录上游账号（codebuddy(=workbuddy)/trae/zcode/doubao/mimo/qoder）
./buddy ui                 # 仅打开管理页（必要时先启动）

# 一次性安装：把 buddy 放进 PATH，之后任意目录敲 buddy 即可
./buddy install            # 装到 /usr/local/bin（不可写时退回 ~/.local/bin）

# 注册为系统服务（launchd）：登录自启、崩溃自动拉起
./buddy service install    # 之后 start/stop/restart 自动走 launchctl
./buddy service status
./buddy service uninstall
```

- 有系统服务时 `buddy start/stop/restart` 自动转 `launchctl`，否则走 `proxy.sh` 的 pid 管理
- 环境变量 `PROXY_HOST`（默认 0.0.0.0）、`PROXY_PORT`（默认 8787）、`PROXY_EXTRA_ARGS` 对 `start` 与 `service install` 生效

### 豆包 Provider（可选）

除 CodeBuddy 外，内置豆包 provider（纯 stdlib CDP 直连本地「豆包工作」App，零额外依赖）：

```bash
# 启用豆包 provider（需要本机已安装并登录豆包工作 App）
uv run python -m buddy_proxy --desensitize --doubao
```

- **原理**：复用豆包工作 App 的登录态与内置 Chromium（CDP 直连），在页面 JS 环境 fetch
  自动注入 a_bogus 风控签名，无需扫码、无需额外凭证
- **依赖**：纯 Python 标准库，不需要 Playwright / chromium

#### 正确使用姿势（重要）

豆包通道的本质是**代理替你操作本机豆包桌面 App**（DoubaoWork.app）——通过 Chrome 调试协议
（CDP，端口 9223）连进 App 内置浏览器，以你的登录态发请求。因此有一条关键原则：

> **让代理来拉起豆包，不要自己先打开豆包 App。**

- 首次 doubao 请求（或在管理页对豆包模型点「测试」）时，代理会自动：以调试模式拉起豆包 →
  连接内置浏览器 → 导航到聊天页 → 确认登录态，之后一直复用这条连接
- 如果豆包**已经先被你打开了**（没带调试参数），代理无法给运行中的 App 追加启动参数，
  首次请求会报 502：「豆包主 App 正在运行但未开启 CDP 调试端口」。这是刻意设计
  （代理绝不强杀你正在使用的 App）

#### 常见失败与恢复

| 现象 | 原因 | 恢复 |
| --- | --- | --- |
| 502「豆包主 App 正在运行但未开启 CDP 调试端口」 | 豆包先于代理启动（或你手动开过豆包） | **完全退出豆包（Cmd+Q）→ 再点一次测试/重发请求**，代理会自动以正确参数拉起它 |
| 豆包 App 升级/重启后请求开始报错 | CDP 连接已失效（代理内存里还标记为已连接） | 同上：退出豆包再重试；或 `buddy restart` 重启代理 |
| 401「doubao not logged in」 | 豆包内登录态失效 | 打开豆包 App 重新扫码登录 → 重试 |
| 报「请先完成豆包扫码登录」 | 首次使用尚未登录 | 代理拉起豆包后在 App 里登录，等几十秒自动就绪 |

> 实测口诀：**豆包报错，先 Cmd+Q 退出豆包，再点一次测试**。90% 的豆包通道问题这一步就解决。

#### 模型列表

- 经典通道：`doubao`（默认模型快速）、`doubao-pro`（旧别名）、`doubao-think`（深度思考）、
  `doubao-expert`（专家）——服务端固定路由到当前默认豆包模型，请求里的 model 字段会被忽略
- agent 通道（App 模型菜单同款协议，真正按模型路由，2026-09 实测）：`doubao-auto`（App「自动」）、
  `doubao-2.1-turbo`、`doubao-2.1-pro`（额度消耗更快）、`orange-5.0`（支持极高/最高推理强度）、
  `gemini-3.7-flash`、`gpt-5.6-sol`（App 内提供的第三方模型）；
  请求体可带 `reasoning_effort`（3低/4中/5高/6极高/7最高，默认 5）

### Trae Provider（可选）

除 CodeBuddy 和豆包外，内置 Trae provider（解密 Trae IDE 登录态，直连底层模型）：

```bash
# 启用 Trae provider（需要本机已安装并登录 Trae IDE）
uv run python -m buddy_proxy --desensitize --trae
```

- **原理**：自动解密 Trae IDE 本地存储的 tc 加密登录态（AES-128-CBC + SHA-512），
  或从 `.env` 读 `TRAE_TOKEN` / `TRAE_USER_ID`，直连 `trae-api-cn.mchost.guru`
- **模型**：T1-T5 分级（glm-5.2 / qwen-3.7-plus / kimi-k2.6 / DeepSeek-V4-Pro 等），
  支持外部名别名（如 `claude-sonnet-4-5` → `glm-5.2`）
- **原生通道（2026-09 起）**：全部请求（含纯聊天）默认走 `chat_v3` 直通——
  带 `tools` 时为原生 function calling（结构化 `tool_calls` + `role:"tool"` 历史回放），
  纯聊天无服务端 agent 预设、不再注入压制指令与泄漏清洗；实测 17 个模型
  16 个原生可用（仅 glm-5-turbo 不在通道，自动回落文本协议）；通道还带真实 token usage
- **依赖**：纯 Python 标准库（含零依赖 AES 兜底实现），不需要 Node.js
- **注意**：免费账号有日/周调用额度，耗尽时报 `4011`（今日用量已达上限），
  错误会以友好中文文案透传

#### Trae 账号工具（`trae-cli`）

安装后（`uv sync` / `pip install .`）自带 `trae-cli` 命令，可查询/领取签到积分、查看权益、测试对话：

```bash
uv run trae-cli status            # 签到/积分状态（剩余积分、今日是否已签到）
uv run trae-cli claim             # 领取今日签到积分
uv run trae-cli usage             # 权益/用量（总额、已用比例、权益包列表）
uv run trae-cli chat -m glm-5.2 -q "你好"    # 发一条对话测试
```

认证自动加载：优先 Work 凭证 `~/.buddy-proxy/trae_work.json`（由
`python -m buddy_proxy.auth.trae_work_login` 或 `buddy login trae` 生成；遗留的
`~/.ethan/trae_work.json` 会在首次读取时自动迁移），其次解密本机 Trae IDE `storage.json`，
无需手动配置 token。

> **`buddy login trae` 排障**：若浏览器显示「登录成功」而 CLI 一直停在等待界面，
> 九成是登录 URL 的参数没和 Trae CN 客户端对齐。关键项 `plugin_version` 必须是
> **`trae-handoff-1.0`**——传版本号形态会被授权页当版本号规范化并**丢掉
> `auth_callback_url`**，回调永不发起。另外回调是**跨源 fetch**（`redirect=0`），
> 本地服务必须回 `Access-Control-Allow-Origin`，否则请求会被浏览器直接拦掉。
> 回调服务带脱敏访问日志（`[srv] GET /authorize?...`），可据此判断浏览器到底有没有打过来。

### MiMo Provider（可选）

小米 **MiMo**（platform.xiaomimimo.com），以 `mimo/<id>` 寻址（`mimo-auto`、`mimo-pro`）：

```bash
uv run python -m buddy_proxy --desensitize --mimo
uv run buddy login mimo     # 打印当前生效的认证模式，或配置指引
```

认证两种，按序尝试：

1. **API key** — `MIMO_API_KEY`（配 `MIMO_BASE_URL` 可切 billing/token-plan 域）、
   `~/.mimocode/auth.json`（MiMo 桌面「API Key」模式写入）、`~/.buddy-proxy/mimo_api_key.json`。
2. **小米 SSO** — 复用本机已安装的 **MiMo 桌面** 登录态：自动读取其账号 cookie，复刻
   桌面端「两阶段换 `serviceToken`」，过期自动刷新、被拒自动重试一次。无需复制粘贴 cookie。

MiMo 上游是 OpenAI 形态，请求原样转发；**但 Anthropic（`/v1/messages`）例外**——上游没有
Anthropic 原生端点，所以要把响应**反向转换**成 Anthropic 事件流（`message_start` /
`content_block_delta` / `message_stop`，含 `thinking` 与 `tool_use` 块），否则 Claude Code
会报「0 stream events received」/「body is JSON but not a Message」。

> 排障提示：`30012` 是「**未开通会员**」而不是 token 失效——遇到它本 provider 不会去重换票。
> 管理页额度面板的 `percent` 已由上游的「剩余」换算成「已用」，且管理页显示的
> 「已用 N%」用的是 `100 - 剩余`。

### 用 `proxy.sh` 后台管理

`buddy` 内部即调用 `proxy.sh`；想手动精细控制时可以直接用它：

```bash
./proxy.sh start          # 后台启动，立刻返回
./proxy.sh stop           # 停止
./proxy.sh restart        # 重启
./proxy.sh status         # 显示 PID 和监听地址
./proxy.sh logs           # tail -F 日志
./proxy.sh ui             # 确保在跑并打开管理页

# 自定义 host / port / 额外参数
./proxy.sh start -p 9000 -H 0.0.0.0
PROXY_PORT=9000 PROXY_EXTRA_ARGS="--desensitize --optimize-context" ./proxy.sh start
```

脚本行为：
- 自动检测 `.venv/bin/python`（优先使用项目 venv）
- 用 `nohup ... &` 启动，`start` 命令**立即返回**，不会阻塞终端
- PID 写到 `logs/proxy.pid`，启动输出写到 `logs/proxy.sh.log`；应用日志按天滚动（`logs/proxy.log` 与 `logs/buddy-proxy.jsonl`，保留 30 天）
- 停止用 `kill` 优雅退出；10s 内未退出会 fallback 到 `kill -9`

## 模型列表

模型目录由 `src/buddy_proxy/web/models_config.json` 维护（启动时与 `/v1/models` 都从这里读取，离线可靠）。当前内置 **39 个模型**，分属两个通道；`GET /v1/models` 的 `data[].credits` / `models[].credits` 会返回积分倍率（消费 × 倍率）：

**CodeBuddy 通道**（12 个）——直接用模型名，无前缀：

| id | name | credits |
|---|---|---|
| `auto` | Auto（快速 / 均衡 / 极致 → 0.21 / 0.65 / 1.20） | 动态 |
| `default` | Default | x2.20 |
| `glm-5.3` | GLM-5.3 | x0.79 |
| `glm-5.3-flash` | GLM-5.3-Flash | x0.06 |
| `hy3` | Hy3（限时免费） | x0.00 |
| `hy4-preview` | Hy4 preview | x0.29 |
| `minimax-m3` | MiniMax-M3 | x0.25 |
| `kimi-k3` | Kimi-K3 | x1.62 |
| `kimi-k2.7` | Kimi-K2.7-Code | x0.57 |
| `deepseek-v4.1-flash` | Deepseek-V4.1-Flash | — |
| `deepseek-v4-flash` | Deepseek-V4-Flash | x0.17 |
| `deepseek-v4-pro` | Deepseek-V4-Pro | x0.51 |

**Trae PAT 通道**（27 个）——以 `traepat/<id>` 寻址。若某个 id 被多个通道声明，裸名会落到**注册顺序里第一个声明它的通道**（通常是个人 `trae` 通道，如果启用了），**不是** CodeBuddy——CodeBuddy 的 `models()` 返回空列表，只能经「未命中兜底」或显式 `codebuddy/` 前缀抵达。所以要用本通道时请始终带 `traepat/` 前缀。此处的 credits 是该通道自己的量表：

| id | name | credits |
|---|---|---|
| `gpt-6-astra-max` | GPT-6-Astra Max | — |
| `gpt-5.6-sol-max` / `gpt-5.6-sol` | GPT-5.6-Sol Max / GPT-5.6-Sol | — |
| `gpt-5.6-luna-max` / `gpt-5.6-terra-max` | GPT-5.6-Luna / Terra Max | — |
| `gpt-5.5-max` / `gpt-5.4` / `gpt-5.2` | GPT-5.5 Max / 5.4 / 5.2 | — |
| `gemini-3.1-pro` / `gemini-3-flash` | Gemini-3.1-Pro / Gemini-3-Flash | — |
| `openrouter-3o-max` / `-2o-max` / `-1o` / `-1` | OpenRouter-3o Max / 2o Max / 1o / 1 | — |
| `glm-5.3` / `glm-5.3-flash` | glm-5.3 / glm-5.3-flash | x0.40 / x0.06 |
| `glm-5.2` | glm-5.2 | x0.40 |
| `qwen3.8-max` / `qwen-3.7-plus` | Qwen3.8-Max / Qwen-3.7-Plus | x1.50 / x0.25 |
| `kimi-k3` / `kimi-k2.7-code` / `kimi-k2.6` | kimi-k3 / Kimi-K2.7-Code / Kimi-K2.6 | x1.83 / x0.83 / — |
| `deepseek-v4-pro` / `deepseek-v4-flash` | DeepSeek-V4-Pro / -Flash | x0.72 / x0.08 |
| `minimax-m3` | MiniMax-M3 | x0.26 |
| `Doubao-Seed-2.1-Pro` / `Doubao-Seed-Code` | Seed-2.1-Pro / Seed-Code | x0.77 / x0.03 |

要新增 / 调整模型，直接编辑 `src/buddy_proxy/web/models_config.json` 后重启即生效。

## 管理界面（/ui）

浏览器打开 <http://127.0.0.1:8787/ui>（或 `buddy start` / `buddy ui` 自动打开）：

- **默认启用模型** — 按 provider 分组浏览所有模型，点「设为默认」即可把某个模型设为默认；
  客户端请求**不带 `model` 字段**时自动用它补齐。设置持久化在 `~/.buddy-proxy/settings.json`
  （可用 `BUDDY_PROXY_SETTINGS` 覆盖），重启后仍生效；启动时 `--default-model zcode/glm-5.3`
  可提供初始值（设置文件里已有的值优先）
- **一键测试** — 每个模型一个「测试」按钮，向上游发一条 `hi`，弹窗里返回延迟、token 用量和回复预览
  （非流式、`max_tokens=256`，走真实上游、会产生真实调用）
- **自动打卡 & 打卡日历** — Trae 与 CodeBuddy 上游都提供每日签到：勾选「自动打卡」后代理每天定时
  （默认 09:30，启动时当天未签会立即补签）自动领取，最近 35 天的打卡情况在日历里展示；也可点
  「立即打卡」手动领。签到活动有档期——CodeBuddy 档期未开时界面显示「今日无签到活动」且不会误打。
  打卡历史逐行落在 `logs/checkin.jsonl`。ZCode（智谱 Coding Plan）/ 豆包没有签到 API
- **额度查询** — 各通道剩余额度一目了然：CodeBuddy 积分包余额合计 + 各资源包明细（credits）；
  Trae 总额度剩余 + 权益包到期时间；ZCode 的 5 小时 / 每周用量窗口与重置时间；MiMo 的
  周额用量 + 套餐有效期。额度数据带
  5 分钟缓存，避免频繁请求上游。Trae PAT 的 standard 池**没有主动查询接口**，用量只能从
  `4031`（额度耗尽）错误体里被动采集；而 4031 只在池已满时才出现，所以超过 `reset_ts` 的
  快照会显示成「已重置 · 用量待确认」，而不是把陈旧的 100% 当现状。
  额度页是管理页里唯一触网的接口，因此查询做了四重收敛：请求上游前先探测网关可达性
  （DNS+TCP 预检，不可达就整轮跳过、直接用各账号缓存），单账号请求超时 6 秒，账号之间并发
  查询（常驻线程池），整轮再兜一个 8 秒上限（超时未回的账号先用缓存顶上，请求仍在后台跑完，
  下轮生效）——避免耗时随账号数线性增长。网关不可达或部分账号查询失败时，页面会明确给出
  「n/m 个账号未取到新数据」的说明条并用警告色区分，而不是一直转圈让人不知道卡在哪；这类
  失败结果只缓存 30 秒（成功结果仍缓存 5 分钟），网络抖动恢复后下一轮就能自愈
- **Trae PAT 账号** — 每个账号一张卡片：本地凭证与冷却状态（纯本地读取，不触网）、一键补签
  Token（只补缺失/临期的，不打扰健康账号）、以及上游各模型在本通道的负载状态（10 分钟缓存）
- **模型停用与时段** — 可停用/启用单个 `(provider, model)` 组合（停用后该组合调用直接失败），
  也可给模型限定时段窗口（如 `22:00–08:00`、`12:00–14:00`）。两者都持久化到设置文件
- **请求日志** — 从 `logs/metrics.jsonl` 与 30 天归档里读取的服务端分页请求日志，可按日期范围 /
  通道 / 模型筛选
- **统计图表** — 按 provider/模型维度聚合请求数、错误数、平均耗时、token 用量：
  近 14 天按通道堆叠的柱状图、模型请求 Top 榜、最近 50 条请求明细。
  每次请求完成追加一行到 `logs/metrics.jsonl`，重启后自动回读恢复历史（保留 30 天）
- **通道健康** — 各 provider 的登录/配置状态一目了然（CodeBuddy 是否登录、zcode key 是否配置等）

管理页里还可以切换**兜底通道**（请求未命中任何模型时的默认路由），设置持久化在同一文件。

页签按需懒加载、需要时并发拉取，各自的骨架屏独立显示——打开管理页不再等待最慢的日志接口
（它每次轮询都要读取并解析整个 JSONL 尾部）。手动刷新会作废在飞的旧请求，因此晚到的旧响应
不可能覆盖更新的结果。

安全约定：`/ui/api/*` 管理接口**仅允许本机（127.0.0.1）访问**；确需从局域网操作管理页时设置
`BUDDY_PROXY_ADMIN_OPEN=1` 放开（自担风险）。`/v1/*` 代理端点不受此限制。

## 快速验证

```bash
curl http://127.0.0.1:8787/health      # 服务 + 认证状态
curl http://127.0.0.1:8787/v1/models    # 模型列表
```

## 接入客户端

### Codex CLI

`~/.codex/config.toml`:

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local proxy)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[profiles.codebuddy]
model = "glm-5.3"
model_provider = "codebuddy"
```

```bash
codex --profile codebuddy "your task"
```

### Claude Code / CC Switch

```json
{
  "DeepSeek-V4": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### OpenCode

`opencode.json`:

```json
{
  "model": "codebuddy/glm-5.3",
  "providers": {
    "codebuddy": {
      "name": "CodeBuddy (via local proxy)",
      "package": "@opencode-ai/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8787/v1", "apiKey": "noop" },
      "models": {
        "glm-5.3":         { "modelID": "glm-5.3",         "name": "GLM-5.3" },
        "deepseek-v4-pro": { "modelID": "deepseek-v4-pro", "name": "DeepSeek V4 Pro" },
        "kimi-k2.7":       { "modelID": "kimi-k2.7",       "name": "Kimi K2.7" }
      }
    }
  }
}
```

### Grok CLI

`~/.grok/config.toml`:

```toml
[models]
default = "hy3"

[model.hy3]
model = "hy3"
base_url = "http://127.0.0.1:8787/v1"
name = "HY3 Main"
api_key = "noop"

[model.dv4f]
model = "deepseek-v4-flash"
base_url = "http://127.0.0.1:8787/v1"
name = "DeepSeek V4 Flash"
api_key = "noop"
```

### Oh My Pi (OMP)

`~/.omp/agent/models.yml`:

```yaml
providers:
  codebuddy:
    baseUrl: http://127.0.0.1:8787/v1
    api: openai-completions
    auth: none
    models:
      - id: hy3
        name: Hy3 (CodeBuddy)
        reasoning: true
        contextWindow: 192000
        maxTokens: 64000
      - id: deepseek-v4-flash
        name: DeepSeek V4 Flash (CodeBuddy)
        reasoning: true
        contextWindow: 1000000
        maxTokens: 50000
```

### 其它 OpenAI 兼容客户端

- Base URL：`http://127.0.0.1:8787/v1`
- API Key：留空（或启动时设置的 `--api-key`）
- Model：`/v1/models` 里的任意 id，如 `glm-5.3`、`deepseek-v4-pro`、`kimi-k2.7`、`auto`

---

## 命令行参数

```
--host HOST               绑定地址（默认 127.0.0.1；proxy.sh 用 0.0.0.0）
--port PORT               绑定端口（默认 8787）
--endpoint ENDPOINT       CodeBuddy 后端地址
--session-file PATH       会话文件（默认 ~/.codebuddy-session.json）
--log-file PATH           JSONL 日志（默认 <项目根>/logs/buddy-proxy.jsonl，可用 BUDDY_PROXY_LOG_FILE 覆盖）
--desensitize             启用脱敏（推荐）
--optimize-context        启用消息压缩（Codex 场景推荐）
--default-model MODEL     默认启用模型（如 zcode/glm-5.3）；请求未带 model 时使用，
                          首次启动写入设置文件，此后以 ~/.buddy-proxy/settings.json 为准
--default-provider NAME   兜底通道：模型名未命中任何 provider 时转发到哪个通道
                          （codebuddy/zcode/trae/doubao/mimo/qoder，默认 codebuddy）
--trae                    启用 Trae provider（解密 Trae IDE 登录态）
--zcode                   启用 ZCode provider（智谱 GLM，Anthropic 端点直通）
--doubao                  启用豆包 provider（经 CDP 驱动桌面 App）
--mimo                    启用 MiMo provider（API key 或复用 MiMo 桌面登录态）
--login                   启动时浏览器登录（会打开浏览器并打印登录链接）
--no-browser              不自动打开浏览器。隐式/后台补认证（如自动打卡轮询）
                          无论如何都不会弹浏览器、也不会打印登录链接，只留一行
                          `[Auth] ...` 日志。真想要登录链接就用 --login
--verbose-llm             输出扩展安全诊断（绝不记录请求/响应体、token 或 UID）
--mock-dir DIR            使用录制的响应（测试用）
```

环境变量：`BUDDY_PROXY_HOST`、`BUDDY_PROXY_PORT`、`CODEBUDDY_ENDPOINT`、`CODEBUDDY_MODEL`、`BUDDY_PROXY_LOG_FILE`、`BUDDY_PROXY_SETTINGS`（设置文件路径）、`BUDDY_PROXY_STATE_DIR`、`BUDDY_PROXY_ADMIN_OPEN=1`（放开管理接口的本机限制）、`PROXY_DEFAULT_PROVIDER`（兜底通道，默认 `codebuddy`）、`TRAE_ENABLED` / `ZCODE_ENABLED` / `DOUBAO_ENABLED` / `MIMO_ENABLED`（置 `1` 等同对应开关）、`TRAE_TOKEN` / `TRAE_USER_ID`（跳过 Trae IDE 解密，直接用这两个值）、`ZCODE_API_KEY`、`ZCODE_OPENAI_BASE`、`MIMO_API_KEY` / `MIMO_BASE_URL`、`BUDDY_CLIENT_NAMES_FILE`（覆盖请求日志里的客户端名映射）。

Trae 流式调优：`WB_TRAE_HEARTBEAT_INTERVAL`（等待上游缓冲响应期间的心跳秒数，默认 45，`0` 关闭——避免像 Ethan 那种 120s 分块超时的客户端中断长生成）、`WB_TRAE_NATIVE_TOOLS`（全部 Trae 请求是否走原生通道，默认 `1`，`0` 回落到旧的提示词教文本协议）、`WB_TRAE_IDE_VERSION_CODE`（Trae 客户端版本头，默认 `20260906`——上游按此头放开各模型能力，某模型突然 4001 时调高它）、`WB_TRAE_NONSTREAM_MAX_S`（非流式聚合上限）、`WB_TRAE_SEMANTIC_TIMEOUT`、`WB_TRAE_TOKEN_KEEPALIVE_S`（后台 Token 刷新间隔）。

## API 端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET  | `/ui`                 | 管理界面（`/` 302 跳转到 `/ui`） |
| GET  | `/ui/api/*`           | 管理接口（overview/models/stats/benefits/settings/checkin/test/logs/model-toggle/model-schedule/traepat 账号与模型状态，仅限本机） |
| GET  | `/health`             | 服务 + 认证状态 |
| GET  | `/v1/models`          | 模型列表 |
| POST | `/v1/chat/completions` | OpenAI 对话（工具 + 流式） |
| POST | `/v1/responses`       | Responses API（Codex CLI） |
| POST | `/v1/messages`        | Anthropic Messages（Claude Code） |

端点使用本地 session 认证，无需额外 token。

---

## Provider 接口一览

各 provider 统一注册到 `providers/` 包的抽象层，`forward_chat` 按模型名路由；
模型名未命中任何 provider 时转发到兜底通道（`--default-provider`，默认 codebuddy）。

### 1. CodeBuddy Provider（`codebuddy_provider/`）

| 接口 | 说明 |
| --- | --- |
| `GET /v1/models` | 模型列表（`models_config.json` 本地配置） |
| `POST /v1/chat/completions` | OpenAI 对话（`stream_upstream` 流式 / `collect_upstream` 非流式） |
| `POST /v1/responses` | Codex CLI Responses 协议适配 |
| `POST /v1/messages` | Claude Code Anthropic Messages 协议适配 |
| `forward_chat(body, "openai"/"codex"/"anthropic")` | 按协议转换 + 按模型路由到对应 provider |
| session / 多账号 | 隔离的 session 文件，`--session-file` 指定 |

### 2. 豆包 Provider（`doubao_provider.py` + `doubao/cdp_client.py`）

| 接口 | 说明 |
| --- | --- |
| `CDPDoubaoClient.start()` | 确保 CDP：优先复用主 App（`open -a DoubaoWork --args --remote-debugging-port=9223`，不杀进程），兜底独立 Helper |
| `CDPDoubaoClient.chat_completion()` | 页面内 fetch `/chat/completion`（自动带 a_bogus 签名），流式 yield SSE；`model_spec` 传入时走 agent 管线（`_build_agent_payload`，App 同款 `model_item_key` 路由），否则经典管线 |
| `DoubaoProvider.forward()` | 流式 / 非流式转换，返回 OpenAI 标准格式 |
| 模型 | 经典：`doubao`（快速）/ `doubao-pro`（旧别名）/ `doubao-think`（深度思考）/ `doubao-expert`（专家）；agent（App 菜单同款）：`doubao-auto` / `doubao-2.1-turbo` / `doubao-2.1-pro` / `orange-5.0` / `gemini-3.7-flash` / `gpt-5.6-sol` |
| 依赖 | 纯 Python 标准库，无 Playwright / chromium |

### 3. Trae Provider（`trae/` 子包）

实现已从单体 `trae_provider.py`（3400+ 行）拆分为 `trae/` 子包，按职责分模块：
`config`（常量/版本头/模型映射）、`auth_storage`（IDE 存储解密）、`benefits_api`（签到/权益）、
`credentials`（凭证与请求头）、`leak_guard`（预设泄漏防护）、`text_protocol`（教学注入/请求改写，
含文本协议解析兜底）、`native_tools`（原生 function calling）、`transport`（HTTP 发送）、
`sse`（SSE 解析/Anthropic 包装）、`provider`（编排入口）、`cli`（`trae-cli` 账号工具）、
`aes_pure`（零依赖 AES 兜底实现）、`pat_provider` + `pat/`（PAT 底层模型通道：多账号、撞码分级冷却、
4031 快速失败）。
`trae_provider.py` 保留为兼容 shim，历史导入与 `python -m buddy_proxy.trae_provider` 不受影响。

| 类别 | 接口 | 说明 |
| --- | --- | --- |
| 认证 | `auth_storage.decrypt_auth_data()` / `find_auth_data()` | 解密 Trae IDE 本地 `storage.json`（AES-128-CBC + SHA-512 派生） |
| 认证 | `credentials._auth()` / `_load_work_cred()` | 凭证加载：`.env`（`TRAE_TOKEN`/`TRAE_USER_ID`）→ Work 凭证 `~/.buddy-proxy/trae_work.json` → 本地解密 |
| 认证 | `auth/trae_work_login.py` / `auth/trae_work_login_server.py` | Work 登录（ExchangeToken 换 token，回调自动捕获） |
| Chat（原生） | `native_tools._send_native_chat()` | **默认通道**：`function=chat_v3` 直通 + 原生 tools（`parameters` 需 JSON 字符串），结构化 `tool_calls` 与 `role:"tool"` 历史回放，带真实 usage；上游按 `X-Ide-Version-Code` 逐模型门控（`WB_TRAE_IDE_VERSION_CODE` 可覆盖） |
| Chat（兜底） | `transport.send_trae_chat()` / `_send_trae_work_chat()` | 文本协议通道（IDE 3 级端点回退 / Work `solo_work_lite`）；原生通道 4001 拒绝时自动回落（`WB_TRAE_NATIVE_TOOLS=0` 可强制全走此路） |
| 解析 | `text_protocol` 内的工具调用解析 / 流式切分器 | 文本协议工具调用解析（教学格式 + 泄漏闸门），仅兜底路径使用 |
| PAT 通道 | `pat_provider` + `pat/`（`cooldown`/`store`/`config`/`models`/`keeper`） | `traepat/<模型>` 底层模型通道：多账号 failover、撞码分级冷却（首次 5 分钟换号，反复撞才升级到次日）、4031 通道级快速失败（`429`，5 分钟后自动重探）、standard 池用量被动采集 |
| 签到 | `benefits_api.fetch_checkin_status()` / `claim_checkin_credits()` | 查询/领取签到积分（`/trae/api/v2/ug/checkin_credits/*`） |
| 权益 | `benefits_api.fetch_ent_usage()` | 查询积分总额 / 已用量 / 权益包 |
| 模型 | `config._map_model()` | T1-T5 分级 + 外部名别名 |
| 错误 | `sse._trae_error_text()` | 14+ 个官方错误码 → 中文文案（4011 今日额度 / 1005 plan 权益不足等） |
| 账号工具 | `cli`（`trae-cli status` / `claim` / `usage` / `chat`） | 命令行查询/领取签到、看权益、发测试对话 |

### 4. ZCode Provider（`providers/zcode.py`）

| 接口 | 说明 |
| --- | --- |
| 凭据 | `ZCODE_API_KEY` → 项目 secrets → `~/.zcode/v2/config.json`（ZCode CLI 同款配置） |
| 端点 | 智谱 GLM Coding Plan 的 Anthropic 兼容端点直通；`ZCODE_OPENAI_BASE` 可覆盖 |
| 模型 | 以 `zcode/<id>` 寻址（如 `zcode/glm-5.3`），经 `/v1/models` 一并列出 |
| 登录 | `buddy login zcode`（凭据落在 `~/.zcode/v2/config.json`） |

### 5. MiMo Provider（`mimo/` 子包）

小米 **MiMo**（platform.xiaomimimo.com），以 `mimo/<id>` 寻址（`mimo-auto`、`mimo-pro`）。

| 接口 | 说明 |
| --- | --- |
| 凭据（方式一） | API key：`MIMO_API_KEY`（配 `MIMO_BASE_URL` 可切 billing/token-plan）、`~/.mimocode/auth.json`（MiMo 桌面「API Key」模式写入）、`~/.buddy-proxy/mimo_api_key.json` |
| 凭据（方式二） | **小米 SSO**：复用本机 MiMo 桌面登录态——自动读取其账号 cookie，并复刻桌面端的「两阶段换 `serviceToken`」，过期自动刷新、被拒自动重试一次 |
| 端点 | OpenAI 形态 `/chat/completions` 直通（流式/非流式） |
| **Anthropic（`/v1/messages`）** | 上游无 Anthropic 原生端点，故**响应反向转换**为 Anthropic 事件（`message_start`/`content_block_delta`/`message_stop`，含 `thinking` 与 `tool_use` 块），供 Claude Code 使用 |
| 登录 | `buddy login mimo`（打印当前生效模式或配置指引；本 provider 无交互式登录） |

管理页的额度面板给两行：**周额用量**与**套餐有效期**。注意这俩是**不同周期**——
额度窗口是「以订阅 `startTime` 为锚点的 7 天」，而套餐期限通常是 **30 天**。
上游 `percent` 字段是**剩余**百分比，面板已换算成「已用」。

```bash
uv run python -m buddy_proxy --desensitize --mimo
```

### 6. Qoder Provider（`qoder/` 子包）

阿里 **Qoder** IDE（国际版 qoder.com / 国内版 qoder.com.cn），以 `qoder/<id>` 寻址。

| 接口 | 说明 |
| --- | --- |
| 聊天面 | `/algo/api/v2/service/pro/sse/agent_chat_generation`（官方 IDE 同一个端点，也是**唯一**提供 Qwen3.8 的入口） |
| 签名 | **COSY 签名纯 Python 复刻**（无额外依赖、不打包官方 wasm）：`Authorization: Bearer COSY.<payload>.<sig>` + 必需的 `Cosy-User` 头，body 走 Qoder 私有字母表编码。国际版与国内版**都要签名**，区域只影响**取 token 的方式** |
| 凭据 | `buddy login qoder`（设备码流程 PKCE S256，可选区域）；也支持 `QODER_TOKEN` 等环境变量 |
| 额度 | 管理页额度面板：订阅额度 + 加油包（含套餐等级、到期时间） |
| **Anthropic（`/v1/messages`）** | 上游无 Anthropic 原生端点，故**响应反向转换**为 Anthropic 事件（`message_start`/`content_block_delta`/`message_stop`，推理内容转 `thinking` 块），供 Claude Code 使用 |

**模型名对外统一为「小写真实名」**（上游内部代号 `qmodel_38max` 这类名字看不出是什么模型）：

| 对外 id | 上游 key | 备注 |
| --- | --- | --- |
| `qoder/qwen3.8-max` | `qmodel_38max` | 推理 + 读图 |
| `qoder/qwen3.8-flash` | `qfmodel` | 推理 + 读图 |
| `qoder/glm-5.3` / `qoder/glm-5.3-flash` | `gmodel` / `gfmodel` | |
| `qoder/kimi-k3` | `kmodel_latest` | |
| `qoder/deepseek-v4-pro` | `dmodel` | |
| `qoder/minimax-m3` | `mmodel` | |
| `qoder/auto` / `ultimate` / `performance` / `efficient` | 同名 | 平台路由档位 |

旧模型（Qwen3.7 系列、GLM-5.2、Kimi-K2.8-Preview、Cantus、Sonus、DeepSeek-Flash）
**不列在列表里但仍可点名调用**——列表少一点，反而好找要用的。三种写法都接受：
对外 id、官方显示名（`Qwen3.8-Flash`）、上游内部 key（`qfmodel`）；内部 key 会在
`/v1/models` 里以 `upstream_key` 回显，便于排障对照。

```bash
uv run python -m buddy_proxy --desensitize --qoder
```

## 免责声明

本项目仅供学习与研究使用，请遵守 CodeBuddy 的服务条款，使用风险自负。
