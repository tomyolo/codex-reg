import argparse
import os
import random
import string
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from DrissionPage import Chromium, ChromiumOptions
from dotenv import load_dotenv

from email_pool import load_pool, load_state, mark, save_state, DEFAULT_POOL_PATH
from hero_sms import CMR_ID, HeroSMSError, OPENAI_ID
from oauth import CallbackServer, OAuthError, build_authorize_url, exchange_code, _new_pkce, _new_state
from outlook import EmailError, OutlookClient
from sms_pool import SmsPool
from sub2api import Sub2APIClient, Sub2APIError

load_dotenv(Path(__file__).with_name(".env"))

EMAIL_TIMEOUT = 180.0
SMS_TIMEOUT = 130.0
SMS_MAX_ATTEMPTS = 5
PAGE_SETTLE = float(os.environ.get("REGISTER_SETTLE", "3"))

# ---- 反批量指纹信号 ----
# 每单随机化浏览器身份 (UA / viewport / 语言) + 人性化输入/等待。
# 同机器连续跑下来最大的可识别信号就是"每单指纹完全一样 + 时序完全一样",
# 把这两项打散后, 跟人偶尔注册一次的分布重叠度更高.

# 近期真实 Chrome 版本 (Win/Mac/Linux 各几条), 不挑小众版本避免反指纹
# 触发"UA 谎报 OS"的二次信号.
_DESKTOP_UAS = [
    # Windows Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # macOS Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Linux Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

# 正常桌面视口, 集中在 1366x768 ~ 1920x1080, 不放大屏不挑怪异值
_VIEWPORTS = [
    (1366, 768), (1440, 900), (1536, 864), (1600, 900),
    (1680, 1050), (1920, 1080), (1280, 800), (1920, 1200),
]

# 主要英语地区 + 中文; 不混入罕见 locale 避免 Cloudflare 拒.
_LANG_HEADERS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.9,zh-CN;q=0.8",
    "en,en-US;q=0.9",
]



def _find_browser_path() -> str | None:
    """定位 Chromium 内核浏览器可执行文件, 优先用 Edge.

    查找顺序:
    1. 环境变量 BROWSER_PATH (用户显式指定, 最高优先级)
    2. Windows 常见 Edge 安装路径
    3. Windows 常见 Chrome 安装路径
    4. 返回 None — 交给 DrissionPage 自动探测 (会下载内置 Chromium)
    """
    env_path = os.environ.get("BROWSER_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    candidates = [
        # Edge (Windows 常见安装位置)
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        # Chrome (兜底)
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None

def _human_input(element, text: str, *, base_delay: float = 0.07,
                 jitter: float = 0.04, max_pause: float = 0.25) -> None:
    """逐字符输入 + 抖动延迟 + 偶尔长停顿。

    替代 DrissionPage 的 .input(), 后者速度均匀太快, 跟 paste 没区别.
    人打字的特征: 字符间 ~50-150ms, 偶尔停下来想下一个词 (~200-400ms).
    """
    if not text:
        return
    # element.clear() 也会触发 input 事件, 先清空再打, 不混着跑.
    try:
        element.clear()
    except Exception:  # noqa: BLE001
        pass
    for i, ch in enumerate(text):
        element.input(ch)
        delay = base_delay + random.uniform(-jitter, jitter)
        # 短停顿比长停顿多; 偶尔来一次较长的, 模拟"换词"
        if random.random() < 0.08:
            delay += random.uniform(0.15, max_pause)
        if delay > 0:
            time.sleep(max(delay, 0.02))


def _jitter_sleep(base: float, *, spread: float = 0.4,
                  min_sleep: float = 0.0) -> None:
    """把一个固定 sleep 替换成 base ± spread 范围内的随机延迟."""
    s = max(base + random.uniform(-spread, spread), min_sleep)
    time.sleep(s)


def _random_fingerprint() -> tuple[str, tuple[int, int], str]:
    """每次起浏览器前摇一份 (UA, viewport, accept-language)."""
    return (
        random.choice(_DESKTOP_UAS),
        random.choice(_VIEWPORTS),
        random.choice(_LANG_HEADERS),
    )


def _inter_account_sleep(min_s: float = 60.0, max_s: float = 180.0) -> None:
    """单与单之间的"冷却", 抖开时间签名, 让 Cloudflare 看不到连续节拍.

    默认 60~180s — 经验值, 既够打散连续注册节拍, 又不至于把整批拖太久.
    想更激进防检测: 调到 120~300s. 想快: 调到 20~60s, 但风险是 Cloudflare
    可能把整 IP 段标批量. 调之前先观察现在的失败率.
    """
    s = random.uniform(min_s, max_s)
    print(f"[COOLDOWN] 等 {s:.0f}s 再开下一单 (反批量时序)", flush=True)
    time.sleep(s)


class Reg:

    def __init__(self, sms: SmsPool, country_id: int):
        self.sms = sms
        self.country_id = country_id
        self.browser = self._new_browser()
        self.tab = self.browser.latest_tab
        self.account = None
        self.email = ""
        self.tokens = None
        self.phone_used = None
        self.sms_code = None

    @staticmethod
    def _new_browser() -> Chromium:
        """起一个全新的隐身 Chromium 实例, 配本单专属指纹. 失败抛."""
        ua, (vw, vh), lang = _random_fingerprint()
        opts = ChromiumOptions()
        browser_path = _find_browser_path()
        if browser_path:
            opts.set_browser_path(browser_path)
            print(f"[BROWSER] 使用浏览器: {browser_path}", flush=True)
        opts.set_argument("--incognito")
        opts.set_user_agent(ua)
        opts.set_argument(f"--window-size={vw},{vh}")
        # DrissionPage 没有 set_accept_language, 走 --lang 给启动命令行;
        # Accept-Language header 由 Chromium 自己从 navigator.language 派生,
        # 启动 lang 决定 navigator.language, 二者绑定.
        primary_lang = lang.split(",")[0]
        opts.set_argument(f"--lang={primary_lang}")
        if os.environ.get("HEADLESS") == "1":
            opts.set_argument("--headless=new")
        browser = Chromium(opts)
        # 启动后注入 Accept-Language header — 启动 lang 只控 navigator.language,
        # 真正的 HTTP header 还是默认 en-US,en;q=0.9, 跟 UA 的语言不一致会
        # 暴露"无头套壳". 用 CDP 一次性注入更稳.
        # header 注入失败不致命 — UA 已经随机, Accept-Language 落到浏览器
        # 默认也只是一致性略弱, 不会直接被识别为批量.
        try:
            tab = browser.latest_tab
            tab.run_cdp("Network.enable")
            tab.run_cdp("Network.setExtraHTTPHeaders",
                        headers={"Accept-Language": lang})
        except Exception:  # noqa: BLE001
            pass
        return browser

    @staticmethod
    def __submit_btn(tab):
        tab.ele('x://button[@type="submit"]').click()

    @staticmethod
    def _step(email: str, n: int, total: int, name: str) -> None:
        print(f"[STEP {n}/{total}] [{email}] {name}", flush=True)

    def close(self):
        try:
            self.browser.quit()
        except Exception:  # noqa: BLE001
            pass

    def reset_browser(self):
        self.close()
        self.browser = self._new_browser()
        self.tab = self.browser.latest_tab

    def _step1_open_chatgpt_and_submit_email(self):
        self._step(self.email, 1, 5, f"打开 chatgpt.com/auth/login + 输邮箱 {self.email}")
        self.tab.get("https://chatgpt.com/auth/login")
        # 输完邮箱稍作停顿再点 submit — 人不会输完立刻敲回车
        _human_input(self.tab.ele('x://input[@type="email"]'), self.email)
        _jitter_sleep(0.5, spread=0.3, min_sleep=0.2)
        self.__submit_btn(self.tab)

    def _step2_wait_email_code(self):
        self._step(self.email, 2, 5, "等 Outlook 邮件验证码 (≤3 分钟)")
        email_after = datetime.now(timezone.utc) - timedelta(seconds=5)
        outlook = OutlookClient(
            account=self.account.email,
            password=self.account.password,
            client_id=self.account.client_id,
            refresh_token=self.account.refresh_token,
        )
        try:
            v = outlook.wait_for_verification(after=email_after, timeout=EMAIL_TIMEOUT)
        finally:
            outlook.close()
        if not v.code:
            print(f"[FAIL step=2] [{self.email}] 邮件收到但未提取到 6 位验证码",
                  file=sys.stderr, flush=True)
            return False
        print(f"[OK   step=2] [{self.email}] 收到验证码 {v.code}", flush=True)
        # 6 位验证码也按人速度打 — 6 位 < 100ms 全 paste 看起来像自动
        _human_input(
            self.tab.ele('x://*[@autocomplete="one-time-code"]'),
            v.code,
            base_delay=0.09,
            jitter=0.04,
        )
        _jitter_sleep(0.4, spread=0.25, min_sleep=0.15)
        self.__submit_btn(self.tab)
        return True

    def _step3_profile(self):
        self._step(self.email, 3, 5, f"等 ~{PAGE_SETTLE}s 让页面 settle, 然后判断姓名/年龄 input")
        # 把 PAGE_SETTLE 抖成区间, 避免每单都在完全相同的时间点进入判断
        _jitter_sleep(PAGE_SETTLE, spread=PAGE_SETTLE * 0.4, min_sleep=1.5)
        try:
            name_ele = self.tab.ele('x://input[@autocomplete="name"]', timeout=3)
            age_ele = self.tab.ele('x://input[@inputmode="numeric"]', timeout=3)
            if name_ele is None or age_ele is None:
                print(f"[SKIP step=3] [{self.email}] 注册流无此步", flush=True)
                return
            username = "".join(random.choices(string.ascii_lowercase, k=8))
            # 姓名 / 年龄都走人速输入
            _human_input(name_ele, username)
            _jitter_sleep(0.3, spread=0.2, min_sleep=0.1)
            _human_input(age_ele, "24", base_delay=0.1, jitter=0.05)
            _jitter_sleep(0.4, spread=0.25, min_sleep=0.15)
            self.__submit_btn(self.tab)
            # 提交后让页面 settle, 时间也抖开 (人不会每次都等一样久)
            _jitter_sleep(PAGE_SETTLE, spread=PAGE_SETTLE * 0.4, min_sleep=1.5)
            _jitter_sleep(5, spread=2.0, min_sleep=3.0)
            print(f"[OK   step=3] [{self.email}] 已填 username={username} "
                  f"+ settle ~{PAGE_SETTLE}s + ~5s", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP step=3] [{self.email}] {e!r}", flush=True)

    def _click_existing_session_button(self):
        try:
            btn = self.tab.ele(
                'x://button[@data-dd-action-name="Select existing session"]',
                timeout=0.1,
            )
        except Exception:  # noqa: BLE001
            return
        if btn is not None:
            try:
                btn.click()
            except Exception:  # noqa: BLE001
                pass

    def _click_post_registration_submit(self):
        for xp in (
            'x://button[@type="submit"]',
            'x://button[contains(., "Continue")]',
            'x://button[contains(., "继续")]',
            'x://button[contains(., "下一步")]',
        ):
            try:
                ele = self.tab.ele(xp, timeout=0.5)
            except Exception:  # noqa: BLE001
                continue
            if ele is None:
                continue
            try:
                ele.click()
                return
            except Exception:  # noqa: BLE001
                continue

    def _wait_for_add_phone_then_verify(self, timeout: float = SMS_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "add-phone" in self.tab.url:
                break
            time.sleep(random.uniform(0.4, 0.7))
        else:
            raise OAuthError(
                f"等不到 add-phone (当前 url={self.tab.url!r}, 超时 {timeout}s)"
            )

        act = self.sms.get_number(service=OPENAI_ID, country=self.country_id)
        local = act.phone_local
        phone_ele = self.tab.ele('x://input[@placeholder="电话号码"]')
        # 输手机号也按人速 — 国际号 10+ 位, 全 paste 看起来一眼假
        _human_input(phone_ele, local, base_delay=0.08, jitter=0.04)

        radio_deadline = time.monotonic() + 5
        while time.monotonic() < radio_deadline:
            label = self.tab.ele('tag:label@@text():短信', timeout=0.5)
            if label is not None:
                label.click()
                break
            radio = self.tab.ele(
                'xpath://input[@type="radio" and ancestor::*[contains(., "短信")]]',
                timeout=0.5,
            )
            if radio is not None:
                radio.check(by_js=True)
                break
            time.sleep(random.uniform(0.25, 0.5))

        _jitter_sleep(0.5, spread=0.3, min_sleep=0.2)
        self.__submit_btn(self.tab)
        try:
            code = self.sms.get_code(act.activation_id, timeout=timeout)
        except Exception:
            self.sms.cancel(act.activation_id)
            raise
        # 短信验证码也按人速打 (6 位)
        _human_input(
            self.tab.ele('x://*[@autocomplete="one-time-code"]'),
            code,
            base_delay=0.09,
            jitter=0.04,
        )
        _jitter_sleep(0.4, spread=0.25, min_sleep=0.15)
        self.__submit_btn(self.tab)
        _jitter_sleep(0.5, spread=0.3, min_sleep=0.2)
        self.__submit_btn(self.tab)
        return local, code

    def _step4_oauth_with_phone(self):
        # 进 OAuth 前的小停顿, 也抖开 (3s ± 1.5s)
        _jitter_sleep(3.0, spread=1.5, min_sleep=1.0)
        last_sms_err = None
        for attempt in range(1, SMS_MAX_ATTEMPTS + 1):
            self._step(self.email, 4, 5,
                       f"OAuth + SMS (attempt {attempt}/{SMS_MAX_ATTEMPTS})")
            seen_add_phone = False
            try:
                verifier, challenge = _new_pkce()
                state = _new_state()
                url = build_authorize_url(state=state, code_challenge=challenge)
                with CallbackServer(state) as srv:
                    self.tab.get(url)
                    deadline = time.monotonic() + SMS_TIMEOUT
                    while time.monotonic() < deadline:
                        self._click_existing_session_button()
                        self._click_post_registration_submit()
                        if "add-phone" in self.tab.url and not seen_add_phone:
                            seen_add_phone = True
                            self.phone_used, self.sms_code = self._wait_for_add_phone_then_verify(
                                timeout=SMS_TIMEOUT,
                            )
                            deadline = time.monotonic() + SMS_TIMEOUT
                        if srv._holder.get("code") or srv._holder.get("error"):
                            break
                        # 轮询间隔也抖开 — 固定 300ms 是机器节奏
                        time.sleep(random.uniform(0.25, 0.6))
                    else:
                        if not seen_add_phone:
                            raise OAuthError(
                                f"OAuth 流程超时未到 add-phone 也未到 callback "
                                f"(当前 url={self.tab.url!r})"
                            )
                    code = srv.wait_code(timeout=SMS_TIMEOUT)
                self.tokens = exchange_code(code=code, code_verifier=verifier)
                print(f"[OK   step=4] [{self.email}] phone={self.phone_used!r}"
                      f" sms_code={self.sms_code!r}"
                      f" access={self.tokens.access_token[:20]}..."
                      f" refresh={'y' if self.tokens.refresh_token else 'n'}"
                      f" id_token={'y' if self.tokens.id_token else 'n'}", flush=True)
                return
            except OAuthError as e:
                # OAuth 失败 (没到 add-phone / 流程跑到一半卡住 / authorize
                # 拒绝) 都不重试 — 重跑 OAuth 不会让 cookie 重新挂上, 不会让
                # Cloudflare 验证重新过, 失败的根因 (session 失效 / 风控)
                # 跟换号无关. 直接 fail 上抛, 让这一单 fail. 唯一例外:
                # seen_add_phone 之后还抛 OAuthError, 那可能是 callback /
                # token exchange 的问题, 跟"重试 OAuth"也不是同一类, 但
                # 历史上没见 — 仍按 fail 处理, 跟 HeroSMSError 的 retry 区分开.
                if seen_add_phone:
                    print(f"[FAIL step=4] [{self.email}] OAuth 跑到 add-phone 后失败: {e}",
                          file=sys.stderr, flush=True)
                else:
                    print(f"[FAIL step=4] [{self.email}] OAuth 未进入手机号页, 不重试: {e}",
                          file=sys.stderr, flush=True)
                raise
            except HeroSMSError as e:
                last_sms_err = e
                print(f"[RETRY step=4] [{self.email}] attempt {attempt} 短信失败: {e}",
                      file=sys.stderr, flush=True)
                if attempt >= SMS_MAX_ATTEMPTS:
                    print(f"[FAIL step=4] [{self.email}] SMS 收不到, "
                          f"已重试 {SMS_MAX_ATTEMPTS} 次",
                          file=sys.stderr, flush=True)
                    raise
                try:
                    self.tab.get("https://chatgpt.com")
                except Exception:  # noqa: BLE001
                    pass
                _jitter_sleep(2.0, spread=1.0, min_sleep=1.0)
        if last_sms_err is not None:
            raise last_sms_err

    def _step5_sub2api_import(self, state):
        sub2_base = os.environ.get("SUB2API_BASE_URL")
        sub2_key = os.environ.get("SUB2API_API_KEY")
        if not (sub2_base and sub2_key):
            print(f"[FAIL step=5] [{self.email}] 缺 SUB2API_BASE_URL/API_KEY, 跳过 import",
                  file=sys.stderr, flush=True)
            mark(state, self.email, status="fail", reason="oauth ok but missing SUB2API env")
            save_state(state)
            return False

        print(f"[STEP 5/5] [{self.email}] sub2api import", flush=True)
        client = None
        try:
            client = Sub2APIClient(base_url=sub2_base, api_key=sub2_key)
            r = client.import_codex_session(
                access_token=self.tokens.access_token,
                name=self.email,
                refresh_token=self.tokens.refresh_token,
                id_token=self.tokens.id_token,
            )
            print(f"[OK   step=5] [{self.email}] sub2api: created={r.created} "
                  f"updated={r.updated} failed={r.failed} "
                  f"account_id={r.account_id} msg={r.message!r}", flush=True)
            return r.failed == 0 and (r.created + r.updated) > 0
        except Sub2APIError as e:
            print(f"[FAIL step=5] [{self.email}] sub2api 失败: {e} "
                  f"(status={e.status_code})", file=sys.stderr, flush=True)
            return False
        finally:
            if client is not None:
                client.close()

    def register_one(self, account, state) -> bool:
        self.account = account
        self.email = account.email
        self.tokens = None
        self.phone_used = None
        self.sms_code = None

        self._step1_open_chatgpt_and_submit_email()
        if not self._step2_wait_email_code():
            return False
        self._step3_profile()
        self._step4_oauth_with_phone()
        return self._step5_sub2api_import(state)


def prompt_country(sms: SmsPool, *, fallback: int) -> int:
    print(f"\n[?] 选择接码国家 (按价格升序, 共展示所有可用):", flush=True)
    try:
        offers = sms._client.get_offers(services=[OPENAI_ID])
    except HeroSMSError as e:
        print(f"  拉报价失败: {e}; 用默认 country={fallback}", flush=True)
        return fallback

    rows = []
    service_block = offers.get(OPENAI_ID, {})
    for cid_str, info in service_block.items():
        if not isinstance(info, dict):
            continue
        if (info.get("counts") or {}).get("total", 0) == 0:
            continue
        prices = info.get("prices") or {}
        price = prices.get("default") or prices.get("retail") or prices.get("min")
        if price is None:
            continue
        try:
            rows.append((int(cid_str), float(price), int(info["counts"]["total"])))
        except (ValueError, TypeError, KeyError):
            continue
    rows.sort(key=lambda r: (r[1], r[0]))
    if not rows:
        print(f"  没有可用国家报价; 用默认 country={fallback}", flush=True)
        return fallback

    print(f"  {'country_id':<10}  {'价格':<10}  库存", flush=True)
    print(f"  {'-'*10}  {'-'*10}  {'-'*6}", flush=True)
    for cid, price, count in rows:
        print(f"  {cid:<10}  ${price:<9.4f}  {count}", flush=True)

    while True:
        try:
            line = input(f"[?] 输入 country id (回车={rows[0][0]}, q=跳过={fallback}): ").strip()
        except EOFError:
            return fallback
        if not line:
            return rows[0][0]
        if line.lower() in ("q", "quit", "exit"):
            return fallback
        if line.isdigit():
            return int(line)
        print(f"  无效输入: {line!r} (回车选最便宜, q 退出)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="批量注册 ChatGPT 账号")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多跑几个邮箱 (默认 = 邮箱池剩余全部)")
    parser.add_argument("--country", type=int, default=None,
                        help="强制指定接码国家 ID (跳过交互选区)")
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

    sms = SmsPool(sms_key)
    try:
        if args.country is not None:
            country_id = args.country
            print(f"--country {country_id}: 跳过交互", flush=True)
        else:
            country_id = prompt_country(sms, fallback=CMR_ID)
        print(f"使用 country={country_id}", flush=True)

        reg = Reg(sms, country_id)
        try:
            for i, account in enumerate(todo, 1):
                print(f"\n{'='*60}\n"
                      f"  [{i}/{len(todo)}] {account.email}\n"
                      f"{'='*60}", flush=True)
                reason = ""
                ok = False
                try:
                    ok = reg.register_one(account, state)
                except EmailError as e:
                    reason = f"email: {e}"[:200]
                    print(f"[{account.email}] 邮箱验证码失败: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
                except HeroSMSError as e:
                    reason = f"sms: {e}"[:200]
                    print(f"[{account.email}] 短信失败: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
                except OAuthError as e:
                    reason = f"oauth: {e}"[:200]
                    print(f"[{account.email}] OAuth 失败: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    reason = f"unexpected: {type(e).__name__}: {e}"[:200]
                    # 之前只 print(e), 拿不到栈 — print_exc() 走 sys.excepthook
                    # 同款格式 (文件:行, 栈帧), 写到 stderr. 各分支都补, 根因
                    # 经常藏在调用链中间 (e.g. httpx 在哪一层断的), 没栈排查不动.
                    print(f"[{account.email}] 未预期错误: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)

                mark(state, account.email, status="ok" if ok else "fail", reason=reason)
                save_state(state)
                verdict = "✅ OK  " if ok else "❌ FAIL"
                print(f"\n>>> {verdict} [{account.email}] {reason}\n", flush=True)

                if i < len(todo):
                    reg.reset_browser()
                    # 单与单之间抖一个短冷却 (5~10s). 只是让浏览器重建 /
                    # callback port 释放 / Cloudflare 上一次的连接状态过期,
                    # 不再当作反批量手段 — 60s 以上冷却会让整批线性变长,
                    # 但 Cloudflare 风控主要看 IP / 指纹 / 时序三件套,
                    # 短时序已经在每单内部 jitter 过了. 反批量交回给 UA/
                    # viewport / 输入节奏那一层.
                    _inter_account_sleep(5.0, 10.0)
            return 0
        finally:
            reg.close()
    finally:
        sms.close()


if __name__ == "__main__":
    raise SystemExit(main())
