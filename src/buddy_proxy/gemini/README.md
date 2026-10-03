# Gemini CLI 通道（Code Assist 免费额度）

把 Gemini CLI 的 OAuth 免费额度（Code Assist individuals）代理成
OpenAI / Anthropic 兼容接口。协议与指纹**以本机真实 gemini CLI 为准**
（不是 cpa-plugin-gemini-cli——它的指纹是旧版猜测拼的，有封号风险，
见 `fingerprint.py` 模块注释的逐项对比）。

## 文件结构

| 文件 | 职责 |
|---|---|
| `models.json` | 模型表（id/描述/上下文）。**加删模型改这里，不改代码** |
| `fingerprint.py` | UA / x-goog-api-client / 请求头指纹 |
| `credentials.py` | OAuth 凭证存取与刷新（`~/.buddy-proxy/gemini_oauth.json`） |
| `setup.py` | loadCodeAssist / onboardUser onboarding（项目 ID 获取） |
| `login.py` | `buddy login gemini`（PKCE + 本地回调；含 CLI 登录态采用） |
| `cli_bridge.py` | 与本机 gemini CLI 凭证互通（读 `~/.gemini`，回写三件套） |
| `convert.py` | OpenAI chat ↔ Gemini v1internal 双向转换 |
| `thought_signature.py` | thoughtSignature 的 id 回环缓存（三条模型族的实测矩阵见模块 docstring） |
| `provider.py` | BaseProvider 实现（转发、SSE、协议转换） |

## 使用

```bash
buddy login gemini          # Google OAuth 登录（浏览器授权）
# 启动代理加 --gemini（或 GEMINI_ENABLED=1）
curl http://127.0.0.1:8787/v1/chat/completions -d '{"model":"gemini/gemini-2.5-flash","messages":[...]}'
```

凭证文件 `~/.buddy-proxy/gemini_oauth.json`（0600）：access_token 过期自动
刷新；refresh_token 长期有效（Google 安装型应用不轮换）。删掉该文件 =
退出登录。

## 与本机 gemini CLI 互通

两边用的是**同一个 OAuth client**（gemini CLI 硬编码的那对），凭证天然
互认。`cli_bridge.py` 负责双向同步：

- **登录后回写**：`buddy login gemini` 成功（或 onboarding 续跑成功）后，
  按 CLI 的格式写三个文件——`~/.gemini/oauth_creds.json`（合并写，保留
  CLI 已有的 `id_token` 等字段，0600）、`settings.json`
  （`security.auth.selectedType=oauth-personal`，CLI 非交互启动的硬要求；
  读改写保留用户已有配置如 hooks）、`google_accounts.json`
  （active 邮箱，照抄 CLI `cacheGoogleAccount` 的合并语义）。写完本机
  `gemini` 命令直接就有登录态，不用再跑 `gemini` 自己的登录。
- **登录时优先复用**：`buddy login gemini` 进入时先读
  `~/.gemini/oauth_creds.json`，有可用登录态（有 refresh_token，或
  access token 未过期）就问「直接使用它吗？（跳过浏览器授权）」，回车
  默认 yes。采用时会自动刷新过期 token、补跑 onboarding、落盘我们的
  凭据文件并回写 CLI——失败则提示原因后可改走浏览器授权。

注意两点：CLI 若开了 `GEMINI_FORCE_ENCRYPTED_FILE_STORAGE=true`（keychain
加密存储），我们没有解密能力，读不到也写不进，互通自动跳过；CLI 运行中
会自己刷新 token 并回写 `oauth_creds.json`，两边各自刷新都正常（Google
安装型应用不轮换 refresh_token），但如果 Google 真的下发了新
refresh_token，先刷新的一边会让另一边的旧 token 失效——真遇到重新登录
一边即可。

测试用 `GEMINI_CLI_HOME` 环境变量把 CLI 目录指到临时位置（与 CLI 的
`GEMINI_CLI_HOME` 覆盖语义一致），不会碰真实配置。

---

## 模型更新怎么做（Gemini 出了 3.8 / 4.0 之后）

模型能不能用由上游决定，代理这边只管「把名字报给上游」。三步：

### 1. 改 `models.json`

先手动验证新模型名可用（见下节「验证一个模型名」），然后把新模型加进
`models.json` 的 `models` 数组：

```json
{ "id": "gemini-4.0-flash", "description": "Gemini 4.0 Flash", "context": 1048576, "max_output": 65536, "verified": true }
```

`verified: true` 表示你亲手跑通过。拿不准就先 `verified: false`。
下线的模型挪进 `_comment` 里记一笔（别直接删，留排查痕迹）。

### 2. 重启代理

```bash
buddy restart
```

`/v1/models` 里就会出现 `gemini/gemini-4.0-flash`。

### 3. （可选）上游开了新 preview 通道才需要

Gemini 3 系 preview 刚出时，免费层可能要先开 EXPERIMENTAL release channel
（cloudaicompanion API 的 releaseChannelSettings）。cpa-plugin-gemini-cli
的 `internal/auth/preview.go` 有完整实现；我们的登录流程没有内置这步
（login 时上游通常已自动开通）。若新模型 404/403 再来移植这段。

### 验证一个模型名

```bash
# 用 CLI 验证（最真实——CLI 能用=通道能用）
gemini -m gemini-4.0-flash -p "hi"

# 或直接 curl 上游（需要 ~/.buddy-proxy/gemini_oauth.json 里有票）
PYTHONPATH=src python3 -c "
from buddy_proxy.gemini.credentials import ensure_access_token
from buddy_proxy.gemini.provider import _CODE_ASSIST_BASE
import json, urllib.request
tok = ensure_access_token()
import pathlib; proj = json.loads(pathlib.Path('~/.buddy-proxy/gemini_oauth.json').expanduser().read_text())['project_id']
body = {'model': 'models/gemini-4.0-flash', 'project': proj, 'user_prompt_id': 't', 'request': {'contents': [{'role': 'user', 'parts': [{'text': 'hi'}]}]}}
req = urllib.request.Request(f'{_CODE_ASSIST_BASE}/v1internal:generateContent', data=json.dumps(body).encode(), method='POST', headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
print(urllib.request.urlopen(req, timeout=60).read()[:300])
"
```

---

## 指纹怎么跟随 gemini CLI 升级（防封号关键）

上游风控看的是「自称 GeminiCLI 的流量指纹是否像一个真实 CLI」。CLI 升级
（比如 0.33 → 0.36）后，旧指纹可能变成异常点。**每次本机 gemini CLI
升级后**（`brew upgrade gemini-cli` 或 npm），跑一遍下面的对齐检查：

```bash
# 1. 看本机 CLI 版本
gemini --version

# 2. 提取真 CLI 的三个指纹值
CORE=$(find /opt/homebrew/lib/node_modules/@google/gemini-cli/node_modules/@google -maxdepth 1 -name "gemini-cli-core" | head -1)
grep -o 'GeminiCLI/\${version}.*process.arch' "$CORE/dist/src/core/contentGenerator.js" | head -2   # UA 模板
grep -rn "gl-node" "$(dirname $(dirname $CORE))/google-auth-library/build/src/transporters.js" | head -3  # api-client 头
python3 -c "import json; print(json.load(open('/opt/homebrew/lib/node_modules/@google/gemini-cli/node_modules/@google/genai/package.json'))['version'])"  # 参考：genai SDK 版本

# 3. 对比 fingerprint.py 里的三个常量，不一致就改：
#    GEMINI_CLI_VERSION / AUTH_LIBRARY_UA_SUFFIX / GOOG_API_CLIENT
```

同时确认 `fingerprint.py` 的 UA **追加后缀**行为没变：真 CLI 的
google-auth-library transporter 会把 `google-api-nodejs-client/<版本>`
追加到 UA 末尾（transporters.js 41-46 行）。CLI 换掉 auth 库（比如改用
genai SDK 直连）的话，UA/x-goog-api-client 都要跟着重写——那时请重新读一遍
`$CORE/dist/src/code_assist/server.js` 的 requestPost 和
`contentGenerator.js` 的 baseHeaders。

**验证指纹**：跑 `python -m buddy_proxy.gemini.provider`（冒烟直连上游），
能出 "pong" 就说明指纹仍被接受。

---

## 封号风险与已知边界

- **免费额度**：2.5-pro 约 100 请求/天、flash 系约 250 请求/天（社区实测，
  官方未固化文档化）。超了上游直接 429，代理原样透传。
- **高风险行为**（cpa-plugin 用户被封的常见归因，按可疑度排序）：
  1. 指纹错配（本实现已规避，见 fingerprint.py 注释）
  2. 高频/并发轰炸（代理这边没有做限流——自己控制）
  3. preview 模型未开通道硬调（403 反复重试会引人注意）
- **数据隐私**：free tier 的 prompt 会被 Google 人工审查用于训练
  （loadCodeAssist 响应里 privacyNotice 明说）。敏感内容别走这条通道。
- token 只落本机 `~/.buddy-proxy/`（0700 目录 + 0600 文件），别进备份/仓库。

## 与 cpa-plugin-gemini-cli 的实现差异（为什么没有照抄它）

| 项 | 插件 | 本实现（对齐真 CLI 0.33.1） |
|---|---|---|
| UA | `GeminiCLI/0.34.0/<model> (darwin; arm64; terminal)` | `GeminiCLI/0.33.1/<model> (darwin; arm64) google-api-nodejs-client/9.15.1` |
| x-goog-api-client | `google-genai-sdk/1.41.0 gl-node/v22.19.0`（API-key 路径的头，OAuth 路径矛盾） | `gl-node/22.19.0` |
| safetySettings | 注入 5 条 OFF（真 CLI 不发） | 不发 |
| session_id | 不发 | 发（真 CLI 每会话 UUID） |
| onboarding | 盲发不带 project 的 onboardUser 试探 | 真 CLI 逻辑：currentTier 判断 → free tier 才用托管项目 onboarding |
