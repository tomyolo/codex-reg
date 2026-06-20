import argparse
import os
import random
import string
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep

from dotenv import load_dotenv
from DrissionPage import Chromium, ChromiumOptions

from outlook import EmailError, OutlookClient
from hero_sms import HeroSMS, HeroSMSError, OPENAI_ID, FRA_ID
from oauth import OAuthError, run_pkce_flow, run_pkce_flow_with_phone
from sub2api import Sub2APIClient, Sub2APIError
from email_pool import (
    EmailAccount, load_pool, load_state, save_state, mark,
    DEFAULT_POOL_PATH, DEFAULT_STATE_PATH,
)

load_dotenv(Path(__file__).with_name(".env"))

EMAIL_TIMEOUT = 180.0
SMS_TIMEOUT = 180.0
# SMS 收不到时的最大尝试次数. 每次失败: 退号 (hero_sms 内部 cancel) +
# 重跑 OAuth (不重跑 step 1-3, 邮箱已验证), 共 N 次机会.
SMS_MAX_ATTEMPTS = 3

# 注册流页面切换的 settle 时间. 默认 1.5s 够 chatgpt/animate 进, 太短会
# 出现"input 还没渲染就判不存在→跳过"的假阴性; 太长又拖慢批量.
PAGE_SETTLE = float(os.environ.get("REGISTER_SETTLE", "1.5"))


def _wait_email(mail: OutlookClient, *, after: datetime, timeout: float = EMAIL_TIMEOUT):
    return mail.wait_for_verification(after=after, timeout=timeout)


def _wait_sms(sms: HeroSMS, activation_id: int, *, timeout: float = SMS_TIMEOUT):
    return sms.wait_for_code(activation_id, timeout=timeout, auto_cancel_on_timeout=True)


def _step(email: str, n: int, total: int, name: str) -> None:
    """打阶段横幅: [STEP 2/5] [email] 收邮件验证码"""
    print(f"[STEP {n}/{total}] [{email}] {name}", flush=True)


def _register_one(tab, sms: HeroSMS, account: EmailAccount) -> bool:
    """用 account 这套邮箱跑一次注册 + 推 sub2api。成功返回 True。

    阶段流程 (5 步):
      1. 打开 chatgpt + 输邮箱
      2. 收邮件验证码 + 填
      3. (可选) 个人信息
      4. OAuth: 跳 authorize → 中途 add-phone → 拿号 → 短信 → Allow → callback
      5. sub2api import (用第 4 步拿到的 token)
    """
    email = account.email
    TOTAL = 5

    _step(email, 1, TOTAL, f"打开 chatgpt.com + 输邮箱 {email}")
    tab.get("https://chatgpt.com")
    tab.ele('x://*[@id="conversation-header-actions"]/div/div/button[1]').click()
    tab.ele('x://*[@id="email"]').input(email)
    tab.ele('x://button[@type="submit"]').click()

    email_after = datetime.now(timezone.utc) - timedelta(seconds=5)
    outlook = OutlookClient(
        account=account.email,
        password=account.password,
        client_id=account.client_id,
        refresh_token=account.refresh_token,
    )
    tokens = None
    try:
        # ---- 2. 邮件验证码 ----
        _step(email, 2, TOTAL, "等 Outlook 邮件验证码 (≤3 分钟)")
        v = _wait_email(outlook, after=email_after)
        if not v.code:
            print(f"[FAIL step=2] [{email}] 邮件收到但未提取到 6 位验证码",
                  file=sys.stderr, flush=True)
            return False
        print(f"[OK   step=2] [{email}] 收到验证码 {v.code}", flush=True)
        tab.ele('x://*[@autocomplete="one-time-code"]').input(v.code)
        tab.ele('x://button[@type="submit"]').click()

        # ---- 3. 个人信息 (可选) ----
        # 等页面 settle 再判断 input 是否存在 — 否则可能 DOM 还没渲染就
        # 当成"没有这一页"跳掉, 实际是有但慢.
        _step(email, 3, TOTAL, f"等 {PAGE_SETTLE}s 让页面 settle, 然后判断姓名/年龄 input")
        time.sleep(PAGE_SETTLE)
        try:
            name_ele = tab.ele('x://input[@autocomplete="name"]', timeout=3)
            age_ele = tab.ele('x://input[@inputmode="numeric"]', timeout=3)
            if name_ele is not None and age_ele is not None:
                username = "".join(random.choices(string.ascii_lowercase, k=8))
                name_ele.input(username)
                age_ele.input("24")
                tab.ele('x://button[@type="submit"]', timeout=3).click()
                # 提交后再 settle — OpenAI 提交后会动画 + 跳转, 不等的话下
                # 一步 OAuth authorize 会和动画/重定向打架
                time.sleep(PAGE_SETTLE)
                print(f"[OK   step=3] [{email}] 已填 username={username} "
                      f"+ settle {PAGE_SETTLE}s", flush=True)
            else:
                print(f"[SKIP step=3] [{email}] 注册流无此步",
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP step=3] [{email}] {e!r}", flush=True)


        sleep(15)
        try:
            print('wait 点击继续')
            tab.ele('x://button[@type="submit"]', timeout=3).click()
        except:
            pass
        # ---- 4. 手机号 + OAuth (在 OAuth 流程里走) ----
        # 提交完个人信息后 OpenAI 不会自动跳到 add-phone; 真正触发 add-phone
        # 的入口是 OAuth authorize URL: 点 sub2api import 那一下浏览器会跳到
        # auth.openai.com/authorize → 自动重定向到 add-phone → 拿号 → 短信
        # → Allow → callback. 这一步交给 oauth.run_pkce_flow_with_phone 全权处理.
        #
        # 重试策略: SMS 收不到是 HeroSMS 平台常事 (号被占/已发过/被拒). 一次
        # 失败就整单放弃太亏, 最多重试 SMS_MAX_ATTEMPTS 次, 每次都:
        #   1) 让 hero_sms 把当前号 cancel (退钱, 已内置)
        #   2) 把浏览器拉回 chatgpt.com 让 Cloudflare / OAuth callback server
        #      状态机重置, 避免和上一轮的 port / cookie 残留打架
        #   3) 重跑整个 step 4 (新 OAuth URL + 新号 + 新 SMS)
        # 非 SMS 失败 (OAuth / add-phone / 浏览器) 不在重试范围 — 那些原因
        # 跟"换号"无关, 重试也没用, 直接抛给上层.
        sleep(20)
        last_sms_err: HeroSMSError | None = None
        for attempt in range(1, SMS_MAX_ATTEMPTS + 1):
            _step(email, 4, TOTAL,
                  f"OAuth + SMS (attempt {attempt}/{SMS_MAX_ATTEMPTS})")
            try:
                tokens = run_pkce_flow_with_phone(
                    tab=tab, sms=sms, country=FRA_ID, timeout=180.0)
                break
            except HeroSMSError as e:
                last_sms_err = e
                cancelled = (e.info or {}).get("auto_cancelled", False)
                print(f"[RETRY step=4] [{email}] attempt {attempt} 短信失败"
                      f" (auto_cancelled={cancelled}): {e}",
                      file=sys.stderr, flush=True)
                if attempt >= SMS_MAX_ATTEMPTS:
                    print(f"[FAIL step=4] [{email}] SMS 收不到, "
                          f"已重试 {SMS_MAX_ATTEMPTS} 次",
                          file=sys.stderr, flush=True)
                    raise
                # 重跑前让浏览器离开当前页 — 上一次可能停在 add-phone /
                # Cloudflare / callback, 直接重 OAuth 会跟残留状态打架.
                # 短暂 settle 防止页面跳转中又点新按钮.
                try:
                    tab.get("https://chatgpt.com")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2.0)
        print(f"[OK   step=4] [{email}] access={tokens.access_token[:20]}..."
              f" refresh={'y' if tokens.refresh_token else 'n'}"
              f" id_token={'y' if tokens.id_token else 'n'}", flush=True)
    finally:
        outlook.close()

    # ---- 5. sub2api import (用第 4 步拿到的 token) ----
    sub2_base = os.environ.get("SUB2API_BASE_URL")
    sub2_key = os.environ.get("SUB2API_API_KEY")
    if not (sub2_base and sub2_key):
        print(f"[FAIL step=5] [{email}] 缺 SUB2API_BASE_URL/API_KEY, "
              f"跳过 import", file=sys.stderr, flush=True)
        mark(state, account.email, status="fail",
             reason="oauth ok but missing SUB2API env")
        save_state(state)
        return False
    print(f"[STEP 5/5] [{email}] sub2api import", flush=True)
    client: Sub2APIClient | None = None
    try:
        client = Sub2APIClient(base_url=sub2_base, api_key=sub2_key)
        r = client.import_codex_session(
            access_token=tokens.access_token,
            name=email,
            refresh_token=tokens.refresh_token,
            id_token=tokens.id_token,
        )
        print(f"[OK   step=5] [{email}] sub2api: created={r.created} "
              f"updated={r.updated} failed={r.failed} "
              f"account_id={r.account_id} msg={r.message!r}", flush=True)
        return r.failed == 0 and (r.created + r.updated) > 0
    except Sub2APIError as e:
        print(f"[FAIL step=5] [{email}] sub2api 失败: {e} "
              f"(status={e.status_code})", file=sys.stderr, flush=True)
        return False
    finally:
        if client is not None:
            client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="批量注册 ChatGPT 账号")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多跑几个邮箱 (默认 = 邮箱池剩余全部)")
    args = parser.parse_args()

    sms_key = os.environ.get("SMS_APIKEY")
    if not sms_key:
        print("缺少 SMS_APIKEY", file=sys.stderr)
        return 2

    pool = load_pool()
    if not pool:
        print(f"邮箱池为空 ({DEFAULT_POOL_PATH})", file=sys.stderr)
        return 2
    state = load_state()
    todo = [a for a in pool if a.email not in state]
    skipped = len(pool) - len(todo)
    print(f"邮箱池: 总 {len(pool)}, 已用 {skipped}, 待办 {len(todo)}")

    if not todo:
        print("所有邮箱都已处理, 退出")
        return 0

    if args.limit is not None:
        todo = todo[:args.limit]
        print(f"--limit {args.limit}: 实际跑 {len(todo)} 个")

    browser = _new_browser()
    try:
        sms = HeroSMS(sms_key)
        try:
            for i, account in enumerate(todo, 1):
                print(f"\n{'='*60}\n"
                      f"  [{i}/{len(todo)}] {account.email}\n"
                      f"{'='*60}", flush=True)
                reason = ""
                ok = False
                try:
                    ok = _register_one(browser.latest_tab, sms, account)
                except EmailError as e:
                    reason = f"email: {e}"[:200]
                    print(f"[{account.email}] 邮箱验证码失败: {e}",
                          file=sys.stderr)
                except HeroSMSError as e:
                    reason = f"sms: {e}"[:200]
                    print(f"[{account.email}] 短信失败: {e}", file=sys.stderr)
                    # 拿号异常时 activation 可能未生成; try cancel 静默
                except Exception as e:  # noqa: BLE001
                    reason = f"unexpected: {type(e).__name__}: {e}"[:200]
                    print(f"[{account.email}] 未预期错误: {e}", file=sys.stderr)
                mark(state, account.email, status="ok" if ok else "fail",
                     reason=reason)
                save_state(state)
                verdict = "✅ OK  " if ok else "❌ FAIL"
                print(f"\n>>> {verdict} [{account.email}] {reason}\n",
                      flush=True)
                # 每跑完一单 (成功/失败都一样) 重建浏览器: 关掉旧进程
                # 清掉所有 cookie / Cloudflare 验证 / OAuth callback server
                # 等残留状态, 防止污染下一单. 重建失败也继续 — 单点故障
                # 不应拖死整批.
                try:
                    browser.quit()
                except Exception:  # noqa: BLE001
                    pass
                if i < len(todo):
                    browser = _new_browser()
            return 0
        finally:
            sms.close()
    finally:
        try:
            browser.quit()
        except Exception:  # noqa: BLE001
            pass


def _new_browser() -> Chromium:
    """起一个全新的隐身 Chromium 实例. 失败抛 (上层会按意外错误记 fail)."""
    opts = ChromiumOptions()
    opts.set_argument("--incognito")
    if os.environ.get("HEADLESS") == "1":
        opts.set_argument("--headless=new")
    return Chromium(opts)


if __name__ == "__main__":
    raise SystemExit(main())
