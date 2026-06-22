"""OpenAI OAuth PKCE 流程。

使用 OpenAI 公共 client_id (Codex CLI / chatgpt-oauth 用的同一个), 在已登录
chatgpt.com 的浏览器里点 Allow, 通过本地 HTTP server 收 callback, 换出
access_token + refresh_token + id_token。

公开常量:
    CLIENT_ID -- OpenAI 公共 client_id
    AUTHORIZE_URL, TOKEN_URL, REDIRECT_URI
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

# Codex CLI 用的公开 OAuth 配置 (从 codex-cli 二进制反编译, 与 chatgpt.com
# 网页登录的 client_id 不同). redirect_uri 用 1455 跟 Codex CLI 默认一致.
# 末尾两个 extra query (codex_cli_simplified_flow / id_token_add_organizations)
# 来自 sub2api 实际生成的链接 — 加上才能走 Codex CLI 简化流, 不加会用完整
# 网页 OAuth 流程, 拿到不同的 id_token claim.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"
EXTRA_AUTHORIZE_PARAMS = {
    "codex_cli_simplified_flow": "true",
    "id_token_add_organizations": "true",
}


class OAuthError(Exception):
    """OAuth 流程错误。"""


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_in: int
    token_type: str
    scope: str | None = None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _new_pkce() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)。verifier 43~128 字符 [RFC 7636]。"""
    verifier = _b64url(secrets.token_bytes(64))  # 86 字符
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _new_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(*, state: str, code_challenge: str) -> str:
    """构造 authorize URL。tab.get(url) 后用户在已登录浏览器里点 Allow。"""
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    params.update(EXTRA_AUTHORIZE_PARAMS)
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(*, code: str, code_verifier: str,
                  timeout: float = 30.0) -> OAuthTokens:
    """用 authorization code 换 token。"""
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    resp = httpx.post(TOKEN_URL, data=data, timeout=timeout)
    return _parse_token_response(resp)


def refresh_tokens(*, refresh_token: str,
                   timeout: float = 30.0) -> OAuthTokens:
    """用 refresh_token 续命。"""
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    resp = httpx.post(TOKEN_URL, data=data, timeout=timeout)
    return _parse_token_response(resp)


def _parse_token_response(resp: httpx.Response) -> OAuthTokens:
    try:
        body = resp.json()
    except ValueError as e:
        raise OAuthError(
            f"Token endpoint returned non-JSON: HTTP {resp.status_code}"
        ) from e
    if resp.status_code >= 400:
        msg = (body.get("error_description") or body.get("error")
               if isinstance(body, dict) else None) or f"HTTP {resp.status_code}"
        raise OAuthError(f"Token exchange failed: {msg}")
    if not isinstance(body, dict) or "access_token" not in body:
        raise OAuthError(f"Token response missing access_token: {body}")
    return OAuthTokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        id_token=body.get("id_token"),
        expires_in=int(body.get("expires_in", 3600)),
        token_type=body.get("token_type", "Bearer"),
        scope=body.get("scope"),
    )


# ---------- 本地 callback 接收 ----------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """解析 /auth/callback?code=...&state=..., 存到 server 实例上。"""

    # 静态属性由 run_callback_server 注入
    result_holder: dict = {"code": None, "state_ok": False, "error": None}
    html_response: bytes = b""
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        err = (params.get("error") or [None])[0]
        err_desc = (params.get("error_description") or [None])[0]

        self.result_holder["code"] = code
        self.result_holder["state_ok"] = (state == self.expected_state)
        self.result_holder["error"] = (
            err if err else None
        ) or (err_desc if err_desc else None)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.html_response)))
        self.end_headers()
        self.wfile.write(self.html_response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # 静默


class CallbackServer:
    """阻塞运行, 收一次 callback 后自动停止。

    用法:
        with CallbackServer(state) as srv:
            tab.get(authorize_url)
            code = srv.wait_code(timeout=120)
    """

    HTML_OK = ("<h2>OK</h2><p>可以关闭此页。</p>").encode("utf-8")
    HTML_ERR = ("<h2>授权失败</h2><p>%s</p>").encode("utf-8")

    def __init__(self, state: str, *, host: str = "127.0.0.1",
                 port: int = 1455) -> None:
        self.state = state
        self.host = host
        self.port = port
        self._holder: dict = {"code": None, "state_ok": False, "error": None}
        self._event = threading.Event()
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "CallbackServer":
        # 注入 holder / state
        handler_cls = type(
            "_H",
            (_CallbackHandler,),
            {
                "result_holder": self._holder,
                "expected_state": self.state,
                "html_response": self.HTML_OK,
            },
        )
        try:
            self._server = http.server.HTTPServer(
                (self.host, self.port), handler_cls)
        except OSError as e:
            raise OAuthError(
                f"无法监听 {self.host}:{self.port}: {e};"
                f" 可能上一次流程没退, 或端口被占用"
            ) from e
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        # 失败时换成错误页
        if self._holder.get("error") and self._holder.get("code") is None:
            pass  # 已经在 do_GET 时把页面写出去了, 无所谓

    def wait_code(self, *, timeout: float = 120.0) -> str:
        """阻塞直到收到 callback, 返回 code。失败抛 OAuthError。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._holder.get("code") or self._holder.get("error"):
                break
            time.sleep(0.2)
        if err := self._holder.get("error"):
            raise OAuthError(f"用户拒绝 / 授权失败: {err}")
        if not self._holder.get("state_ok"):
            raise OAuthError("callback state 不匹配 (可能 CSRF)")
        code = self._holder.get("code")
        if not code:
            raise OAuthError("callback 收到但无 code")
        return code


# ---------- 顶层便捷函数 ----------

def wait_for_add_phone_then_verify(
    tab, sms, *, timeout: float = 180.0,
    country: int = 1, on_add_url=None,
) -> tuple[str | None, str | None]:
    """阻塞等到浏览器跳到 add-phone, 拿号 + 输入 + 等短信 + submit。

    设计: 由调用方 (main.py) 在 OAuth 流程发起前调用. 浏览器停在 chatgpt.com
    时, 此函数会先等 URL 变成 add-phone; 拿到之后买号, 填入 placeholder=
    "电话号码" 的 input, 然后等 SMS 接码并填入 autocomplete="one-time-code"
    的 input, 最后 click submit 两次 (跟原注册流一致).

    返回 (phone_local, sms_code) 给调用方打日志; timeout 抛 OAuthError.
    country: 传给 sms.get_number (调用方指定, 1=USA, 78=法国 等).
    美国号拿到后自动剥掉前导 1; 其他国家原样返回.
    on_add_url: 可选 callback, URL 变成 add-phone 那一刻调用一次.
    """
    import time as _time  # 局部 import, 不污染模块顶部

    from hero_sms import OPENAI_ID  # type: ignore

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if "add-phone" in tab.url:
            break
        _time.sleep(0.5)
    else:
        raise OAuthError(
            f"等不到 add-phone (当前 url={tab.url!r}, 超时 {timeout}s)"
        )
    if on_add_url is not None:
        try:
            on_add_url(tab.url)
        except Exception:  # noqa: BLE001
            pass

    act = sms.get_number(service=OPENAI_ID, country=country)
    local = act.phone_local  # 美国自动剥前导 1; 法国自动加 +33
    # 国际号 (+33...) 比纯本地号长, 默认 placeholder 是空, 但 OpenAI add-phone
    # 在某些情况下会预填样例号 (e.g. "+1 555-..."), 必须先 clear 再 input,
    # 否则会把示例号和真号拼一起提交.
    phone_ele = tab.ele('x://input[@placeholder="电话号码"]')
    phone_ele.clear()
    phone_ele.input(local)

    # ---- 选验证方式: 短信 (不是语音) ----
    # OpenAI 提交手机号后让用户在"短信 / 语音"里二选一. 短信这条
    # 是个 <label> 包着 <input type="radio"> + 文本. DOM 可能调整,
    # 用 DrissionPage 的 tag + @@text() 匹配方式比 XPath 简洁且
    # 会查后代所有文本.
    #
    # 点击策略 (按 DrissionPage 文档推荐):
    #   1) 优先点 label — 浏览器原生 label-input 关联, 自动 toggle
    #      内层 radio, 不受 input 是否 CSS 隐藏影响.
    #   2) fallback 用 .check(by_js=True) — DrissionPage 专给 radio
    #      /checkbox 的方法, 内部走 JS 设 checked + 派发 change 事件,
    #      对隐藏 input 也生效 ("只要元素在 DOM 内就能点击得到").
    radio_sms = None
    radio_deadline = _time.monotonic() + 5
    while _time.monotonic() < radio_deadline:
        # 策略 1: 直接点含"短信"的 label
        label = tab.ele('tag:label@@text():短信', timeout=0.5)
        if label is not None:
            label.click()
            radio_sms = label
            break
        # 策略 2: 找 radio 用 .check(by_js=True)
        radio = tab.ele(
            'xpath://input[@type="radio" and '
            'ancestor::*[contains(., "短信")]]',
            timeout=0.5,
        )
        if radio is not None:
            radio.check(by_js=True)
            radio_sms = radio
            break
        _time.sleep(0.3)

    tab.ele('x://button[@type="submit"]').click()

    code = sms.wait_for_code(
        act.activation_id, timeout=timeout, auto_cancel_on_timeout=True,
    )
    tab.ele('x://*[@autocomplete="one-time-code"]').input(code)
    tab.ele('x://button[@type="submit"]').click()
    tab.ele('x://button[@type="submit"]').click()

    try:
        sms.finish(act.activation_id)
    except Exception:  # noqa: BLE001
        pass
    return local, code


def _click_existing_session_button(tab) -> None:
    """如果 OAuth 账号选择页 ("Select existing session") 出现, 帮点一下。

    浏览器已登录过 ChatGPT 时, authorize 流程会插一个"选哪个账号继续"
    的中间页. 这个按钮跟 submit 不同, 没有状态依赖 — 出现就点, 不会跟
    SMS 等待/表单提交打架, 所以可以直接放在主循环里, 不用后台线程.
    不存在/查找/点击失败都静默吞掉 — 主流程有更稳的兜底 (callback 收不到会超时).
    """
    try:
        btn = tab.ele(
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


def _click_post_registration_submit(tab) -> None:
    """扫一下"提交/继续/下一步"风格的按钮, 找到就点一下, 然后返回.

    用在 OAuth 流程的轮询循环里:
      - run_pkce_flow_with_phone: 输完手机号/SMS 验证码/点了 add-phone 表单
        submit 之后, 浏览器跳到中间过渡页 (Continue / 下一步 / 提交 等),
        这时再点一下才能让浏览器最终到达 callback.
      - run_pkce_flow: 同样的中间页, 只是没有 add-phone 那段.

    跟 _auto_click_loop 时代的"submit 后台 clicker"是不同问题: 那个是
    在 SMS 等待时反复命中 submit 干扰表单; 这个是单次调用放进主循环
    显式扫, 跟 _click_existing_session_button 同一拍, 不会跟 SMS 流程
    打架. 找不到/点击失败都静默吞掉 — 这一拍是给"刚好有按钮就点"用,
    没有是正常的, callback 没收到会由 srv.wait_code 超时兜底.
    """
    for xp in (
        'x://button[@type="submit"]',
        'x://button[contains(., "Continue")]',
        'x://button[contains(., "继续")]',
        'x://button[contains(., "下一步")]',
    ):
        try:
            ele = tab.ele(xp, timeout=0.5)
        except Exception:  # noqa: BLE001
            continue
        if ele is None:
            continue
        try:
            ele.click()
            return
        except Exception:  # noqa: BLE001
            continue


def run_pkce_flow_with_phone(
    tab, sms, *, country: int = 1, timeout: float = 180.0,
) -> tuple[OAuthTokens, str | None, str | None]:
    """在已登录的浏览器里跑完整 OAuth 流程 (含手机验证中间页)。

    设计: 调用方需保证浏览器已是 chatgpt.com 登录态 (注册流程已走完).
    流程:
      1) 构造 PKCE 一次性 verifier/challenge/state
      2) 起本地 callback server
      3) tab.get(authorize_url)
      4) 中途浏览器跳到 add-phone → 调用 wait_for_add_phone_then_verify
         → 拿号 → 输号 → 等短信 → submit
      5) 短信通过后浏览器自动跳到 callback → 收 code
      6) exchange code 换 token

    返回 (tokens, phone_local, sms_code) — 后两者给调用方打日志, add-phone
    这一步没走到的话会是 None.
    """
    import time as _time

    verifier, challenge = _new_pkce()
    state = _new_state()
    url = build_authorize_url(state=state, code_challenge=challenge)
    code: str | None = None
    sms_code: str | None = None
    phone_used: str | None = None
    with CallbackServer(state) as srv:
        tab.get(url)
        # ---- 等 add-phone 中间页 ----
        deadline = _time.monotonic() + timeout
        seen_add_phone = False
        while _time.monotonic() < deadline:
            _click_existing_session_button(tab)
            _click_post_registration_submit(tab)
            if "add-phone" in tab.url and not seen_add_phone:
                seen_add_phone = True
                phone_used, sms_code = wait_for_add_phone_then_verify(
                    tab, sms, timeout=timeout, country=country,
                )
                deadline = _time.monotonic() + timeout
            if srv._holder.get("code") or srv._holder.get("error"):
                break
            _time.sleep(0.3)
        else:
            if not seen_add_phone:
                raise OAuthError(
                    f"OAuth 流程超时未到 add-phone 也未到 callback "
                    f"(当前 url={tab.url!r})"
                )
        code = srv.wait_code(timeout=timeout)
    if code is None:
        raise OAuthError("OAuth 流程未拿到 code")
    return exchange_code(code=code, code_verifier=verifier), phone_used, sms_code


def run_pkce_flow(*, tab, timeout: float = 120.0) -> OAuthTokens:
    """在已登录的浏览器里跑完整 PKCE 流程, 返回 token。

    tab: DrissionPage Tab 对象, 调 .get(url) 跳到 authorize 页。
    流程: 起本地 callback server → tab.get(authorize_url) → 浏览器跳到
    127.0.0.1:1455/auth/callback → server 收 code → 换 token。

    重要: 调用方需自行保证浏览器已是 chatgpt.com 登录态. 若尚未 add-phone,
    应在调用本函数之前先调 wait_for_add_phone_then_verify() 或在 OAuth 流中
    触发 add-phone (OpenAI 会自动重定向, 本函数不处理手机验证中间页).
    """
    verifier, challenge = _new_pkce()
    state = _new_state()
    url = build_authorize_url(state=state, code_challenge=challenge)
    with CallbackServer(state) as srv:
        tab.get(url)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _click_existing_session_button(tab)
            _click_post_registration_submit(tab)
            if srv._holder.get("code") or srv._holder.get("error"):
                break
            time.sleep(0.3)
        code = srv.wait_code(timeout=timeout)
    return exchange_code(code=code, code_verifier=verifier)
