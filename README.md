# codex-reg

自动化批量注册 ChatGPT / Codex 账号，写入 sub2api 网关。

> ⚠️ **仅供学习与个人研究使用。**
>
> 本仓库代码仅用于学习浏览器自动化、OAuth PKCE、SMS 接码 API 等技术原理。
> **禁止** 用于任何违反 OpenAI / Microsoft / sub2api 用户协议的活动，包括但不限于：
>
> - 批量注册账号进行转售、刷量、薅羊毛
> - 绕过 OpenAI 的地区限制或付费墙
> - 任何违反所在地法律法规的用途
>
> 使用本代码产生的一切后果由使用者自行承担，与作者无关。

## 流程

对一个 Outlook 邮箱，跑完整链路：

```
1. chatgpt.com 注册 → 邮箱验证码
2. (可选) 填姓名 + 年龄
3. OAuth (Codex CLI client) → 中途跳 add-phone → 拿号 → 短信 → Allow → callback
4. sub2api import (用 step 3 拿到的 token)
```

每一步自动重试、单邮箱失败立即进位到下一个、状态写 `email-pool-state.json` 避免重跑。

## 准备

### 1. 安装 Chrome / Chromium

DrissionPage 需要本机有 Chrome 浏览器才能启动。

- **macOS**：去 https://www.google.com/chrome/ 下载安装
- **Ubuntu / Debian**：`sudo apt install -y chromium-browser` 或用 snap
- **Windows**：从 https://www.google.com/chrome/ 下载安装

**不想装 Chrome** 也行 — DrissionPage 内置 Chromium，第一次启动时会自动下载到 `~/.DrissionPage/chromium`，无需额外操作。

验证 Chrome 已装：

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --version
# 或
google-chrome --version  # Linux
```

### 2. 安装依赖

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

### 3. 配置 `.env`

复制 `.env.example` 为 `.env`，填三项：

```bash
SMS_APIKEY=你的 HeroSMS API key         # https://hero-sms.com 个人中心
SUB2API_BASE_URL=https://你的sub2api域名  # 例如 https://api.yhnotes.com
SUB2API_API_KEY=admin-你的key           # sub2api 管理后台 → API keys
```

### 4. 准备邮箱池

创建 `email-list.txt`（已 gitignore，不会进 git），每行一个 Outlook 账号：

```
邮箱----密码----client_id----refresh_token
```

格式说明：
- `邮箱` — Outlook / Hotmail / Live 都行
- `密码` — 账号密码
- `client_id` — Microsoft Graph 的 OAuth client_id（固定为 `9e5f94bc-e8a4-4e73-b8be-63364c29d753` 这一组即可，跟具体账号无关）
- `refresh_token` — 用 Microsoft 账号 OAuth flow 一次性换出来的 refresh_token；过期可重换

例：

```
mbrt4152@outlook.com----zxlo6199----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C533_BAY.0.U....
```

## 运行

### 跑一个测试

```bash
uv run python main.py --limit 1
```

`--limit N` 限制本次最多跑 N 个邮箱；不传则跑完邮箱池里所有未处理项。

### 批量

```bash
uv run python main.py            # 跑所有未处理
uv run python main.py --limit 10 # 这次最多跑 10 个
```

### 后台无头跑

```bash
HEADLESS=1 uv run python main.py --limit 20
```

## 日志

每一步有清晰的 `[STEP x/5]` 横幅：

```
============================================================
  [1/1] mbrt4152@outlook.com
============================================================
[STEP 1/5] [mbrt4152@outlook.com] 打开 chatgpt.com + 输邮箱 mbrt4152@outlook.com
[STEP 2/5] [mbrt4152@outlook.com] 等 Outlook 邮件验证码 (≤3 分钟)
[OK   step=2] [mbrt4152@outlook.com] 收到验证码 482931
[STEP 3/5] [mbrt4152@outlook.com] 等 1.5s 让页面 settle, 然后判断姓名/年龄 input
[SKIP step=3] [mbrt4152@outlook.com] 注册流无此步
[STEP 4/5] [mbrt4152@outlook.com] OAuth: authorize → add-phone → 拿号 → 短信 → callback
[OK   step=4] [mbrt4152@outlook.com] access=eyJhbGciOiJSUzI1NiIs...
[STEP 5/5] [mbrt4152@outlook.com] sub2api import
[OK   step=5] [mbrt4152@outlook.com] sub2api: created=1 ...

>>> ✅ OK  [mbrt4152@outlook.com]
```

阶段汇总标签：

| 标签 | 含义 |
|---|---|
| `[STEP x/5]` | 进入第 x 步 |
| `[OK   step=x]` | 第 x 步成功 |
| `[SKIP step=x]` | 第 x 步跳过（如姓名/年龄页面不存在）|
| `[FAIL step=x]` | 第 x 步失败（stderr）|
| `>>> ✅ OK / ❌ FAIL` | 整账号最终结论 |

### 调页面等待时长

每个阶段切换有 settle 时间（默认 1.5s），通过环境变量调整：

```bash
REGISTER_SETTLE=3.0 uv run python main.py --limit 1   # 网络慢时调大
REGISTER_SETTLE=0.5 uv run python main.py --limit 50  # 批量跑调小 (风险自负)
```

## 状态

`email-pool-state.json`（已 gitignore）记录每个邮箱上次处理结果：

```json
{
  "mbrt4152@outlook.com": {
    "status": "ok",
    "reason": "",
    "at": "2026-06-20T14:32:11+00:00"
  }
}
```

启动时自动跳过 state 里已有的邮箱；要重跑某个，删掉对应条目。

## 风险与提醒

⚠️ **这个工具违反 OpenAI ToS**，用于自动化批量注册账号。OpenAI 风控可能：
- 触发 Cloudflare 验证码（目前 clicker loop 会自动点，但训练数据外的复选框类型可能失败）
- 锁定账号 / 短信号段拒收
- IP / 指纹拉黑（隐身模式只隔离 cookie，不换 IP 或浏览器指纹）

批量跑风险显著高于单号调试。建议：
- 先 `--limit 1` 跑通单号 happy path
- 跑 3~5 个观察 sub2api created/updated 比例
- 失败率 > 30% 时停，加住宅代理或 sleep

## 安全

- `.env` 包含真实凭据，**永不进 git**
- `email-list.txt` 和 `email-pool-state.json` 同样 gitignore
- 跑过的账号请及时在 Microsoft / OpenAI / sub2api 后台 revoke session

## 模块结构

| 文件 | 职责 |
|---|---|
| `main.py` | 串联 5 步流程、循环邮箱池、写 state |
| `email_pool.py` | `email-list.txt` 解析 + `email-pool-state.json` 持久化 |
| `outlook.py` | Microsoft Graph API 拉取 Outlook 收件箱、提取 6 位验证码 |
| `hero_sms.py` | HeroSMS (SMS-Activate 协议) 拿号 + 等短信 |
| `oauth.py` | OpenAI Codex CLI OAuth PKCE，含 add-phone 中间页处理 + 自动点 Allow |
| `sub2api.py` | sub2api admin API: import codex session |