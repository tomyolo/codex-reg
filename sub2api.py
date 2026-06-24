"""sub2api 账号导入客户端。

sub2api 是把 ChatGPT/Codex/Claude 订阅账号聚合为 OpenAI/Anthropic 兼容
API 端点的 gateway。admin API key 走 `x-api-key` header (不是 Bearer)。

公开常量:
    DEFAULT_BASE -- 默认 base URL
    COOKIE_NAMES -- ChatGPT 在浏览器里挂的 session token cookie 名 (按顺序尝试)
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

DEFAULT_BASE = "https://api.yhnotes.com"

# ChatGPT 在生产域名 (chatgpt.com) 用 __Secure- 前缀; 本地/预发可能不带。
COOKIE_NAMES = ("__Secure-next-auth.session-token", "next-auth.session-token")


class Sub2APIError(Exception):
    """sub2api API 错误。"""

    def __init__(self, message: str, *, title: str | None = None,
                 status_code: int | None = None, info: dict | None = None) -> None:
        super().__init__(message)
        # title 用来区分错误大类, 跟 HeroSMSError 对齐:
        #   None       -- 业务错 (HTTP 4xx/5xx, sub2api 业务 code != 0)
        #   "TRANSPORT_ERROR" -- httpx 传输层错 (断连/超时), 可安全重试
        self.title = title
        self.status_code = status_code
        self.info = info or {}


@dataclass
class ImportResult:
    total: int
    created: int
    updated: int
    skipped: int
    failed: int
    account_id: int | None = None
    message: str | None = None


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """解码 JWT payload (不验签, 仅读 exp/email/openai auth claim)。"""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT (no dots)")
    payload = parts[1]
    # base64url -> base64, 补 =
    payload += "=" * (-len(payload) % 4)
    payload = payload.replace("-", "+").replace("_", "/")
    try:
        return json.loads(base64.b64decode(payload))
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"JWT payload not valid base64-json: {e}") from e


def parse_session_token(token: str) -> dict[str, Any]:
    """从 ChatGPT session JWT 抽 (email, expires_at, account_id, plan_type...)。

    sub2api 解析逻辑:
      - sub, email
      - exp (unix 秒) -> 必填, 否则 INVALID_TOKEN
      - https://api.openai.com/auth.chatgpt_account_id / chatgpt_plan_type
    """
    claims = decode_jwt_payload(token)
    auth = claims.get("https://api.openai.com/auth") or {}
    return {
        "email": claims.get("email"),
        "sub": claims.get("sub"),
        "expires_at": claims.get("exp"),
        "account_id": auth.get("chatgpt_account_id"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "plan_type": auth.get("chatgpt_plan_type"),
    }


class Sub2APIClient:
    """sub2api admin 客户端。

    用法:
        client = Sub2APIClient(base_url="https://api.yhnotes.com",
                               api_key="admin-xxxx")
        result = client.import_codex_session(
            access_token=jwt,
            name="user@outlook.com",
            refresh_token="...",  # 可选
        )
    """

    def __init__(self, *, base_url: str = DEFAULT_BASE,
                 api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = httpx.Client(
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Sub2APIClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- 账号管理 ----------

    def list_accounts(self, *, platform: str | None = None,
                      account_type: str | None = None) -> list[dict]:
        """列已有账号 (用来去重 / 找到刚注册的)。
        admin/accounts 响应包在 {"code":0,"data":{"items":[...]}} 里。
        """
        params: dict[str, str] = {}
        if platform:
            params["platform"] = platform
        if account_type:
            params["account_type"] = account_type
        data = self._get("/api/v1/admin/accounts", params=params)
        items = (data.get("data") or {}).get("items") or []
        return items

    def find_account(self, *, name: str | None = None,
                     email: str | None = None) -> dict | None:
        for a in self.list_accounts():
            if name and a.get("name") == name:
                return a
            if email:
                # name 通常是邮箱; 另外兜底查 extra / credentials
                if a.get("name") == email:
                    return a
        return None

    def import_codex_session(self, *, access_token: str,
                             name: str | None = None,
                             refresh_token: str | None = None,
                             id_token: str | None = None,
                             group_ids: Iterable[int] | None = None,
                             update_existing: bool = True,
                             max_retries: int = 2,
                             ) -> ImportResult:
        """导入一个 ChatGPT 账号。

        access_token  -- JWT (从浏览器 cookie __Secure-next-auth.session-token 取)
        name          -- 账号在 sub2api 后台显示名 (一般 = 邮箱)
        refresh_token -- ROPC 首次换出来的 refresh_token (可选, 但提供能自动续期)
        group_ids     -- 加入的分组 id 列表 (可选)
        update_existing -- 重复时是否更新
        max_retries    -- 传输错 (断连/超时) 重试次数, 不含首次. 0 = 不重试.

        传输错 (RemoteProtocolError / ConnectError / 超时 等) 会被 sub2api
        包成 Sub2APIError(title="TRANSPORT_ERROR"), 并在这里自动重试
        max_retries 次, 间隔 0.5s -> 1.5s 退避. 业务错 (HTTP 4xx/5xx)
        不重试 — 重试也没意义, 立刻报给调用方.
        """
        if not access_token:
            raise ValueError("access_token is required")
        meta = parse_session_token(access_token)
        resolved_name = name or meta.get("email") or "codex-account"

        # 提取 access_token (解析失败就回退到原始字符串当 access_token)
        actual_access = meta.get("access_token") or access_token

        body: dict[str, Any] = {
            "content": actual_access,
            "name": resolved_name,
            "update_existing": update_existing,
        }
        extras: dict[str, Any] = {}
        if refresh_token:
            extras["refresh_token"] = refresh_token
            extras["client_id"] = "app_lCaiCq4N6RaY9XMWp2v0ZDet"
        if id_token:
            extras["id_token"] = id_token
        if extras:
            body["credential_extras"] = extras
        if group_ids:
            body["group_ids"] = list(group_ids)

        # 传输错重试, 业务错立刻抛. 不引随机退避 — 短窗口内重试相同
        # endpoint, jitter 反而会拖慢; 固定 0.5/1.5s 够 cover Cloudflare
        # keepalive 抖动.
        backoffs = [0.5, 1.5, 3.0]
        last_err: Sub2APIError | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = self._post("/api/v1/admin/accounts/import/codex-session", body)
                return self._parse_import(resp)
            except Sub2APIError as e:
                if e.title != "TRANSPORT_ERROR":
                    raise
                last_err = e
                if attempt >= max_retries:
                    raise
                sleep_s = backoffs[min(attempt, len(backoffs) - 1)]
                time.sleep(sleep_s)
        # 理论走不到这里 — 上面要么 raise 要么 return. 但 max_retries=0 时
        # 走最后一行时 last_err 还是 None, 兜底抛一次.
        if last_err is not None:
            raise last_err
        raise Sub2APIError("import_codex_session: 重试逻辑异常退出")

    # ---------- 内部 ----------

    def _get(self, path: str, **kw: Any) -> dict:
        return self._parse(self._request("GET", path, kw or None))

    def _post(self, path: str, body: dict) -> dict:
        return self._parse(self._request("POST", path, {"json": body}))

    def _request(self, method: str, path: str, kwargs: dict | None) -> httpx.Response:
        """httpx 调用统一加一层, 把传输错包成 Sub2APIError(TRANSPORT_ERROR).

        不动业务错 (HTTP 4xx/5xx), 让 _parse 照常抛 Sub2APIError. 这一层
        只 catch httpx.TransportError 及其子类 — RemoteProtocolError /
        ConnectError / *Timeout / 协议层异常都归在这里.
        """
        try:
            return self._client.request(method, f"{self.base_url}{path}",
                                        **kwargs)
        except httpx.TransportError as e:
            raise Sub2APIError(
                f"sub2api transport error ({method} {path}): "
                f"{type(e).__name__}: {e}",
                title="TRANSPORT_ERROR",
                info={"error": type(e).__name__, "detail": str(e)},
            ) from e

    @staticmethod
    def _parse(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise Sub2APIError(
                f"Non-JSON response (HTTP {resp.status_code})",
                status_code=resp.status_code,
            ) from e
        if resp.status_code >= 400:
            # sub2api 错误格式: {"code":401,"message":"..."}
            msg = (data.get("message") if isinstance(data, dict) else None) \
                or f"HTTP {resp.status_code}"
            raise Sub2APIError(msg, status_code=resp.status_code,
                               info=data if isinstance(data, dict) else {})
        # sub2api 成功: {"code":0,"message":"success","data":{...}}
        if isinstance(data, dict) and data.get("code") not in (None, 0):
            raise Sub2APIError(
                data.get("message", "unknown error"),
                status_code=resp.status_code,
                info=data,
            )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse_import(payload: dict) -> ImportResult:
        # 成功响应可能是两种形态:
        # 1) CodexSessionImportResult 直接: {total, created, updated, failed, items:[...]}
        # 2) 包装: {code:0, data:{...}} (数据在 data 里)
        data = payload.get("data") if "data" in payload else payload
        if not isinstance(data, dict):
            data = {}
        items = data.get("items") or []
        first = items[0] if items else {}
        return ImportResult(
            total=int(data.get("total", 0)),
            created=int(data.get("created", 0)),
            updated=int(data.get("updated", 0)),
            skipped=int(data.get("skipped", 0)),
            failed=int(data.get("failed", 0)),
            account_id=(first.get("account_id")
                        if isinstance(first, dict) else None),
            message=(first.get("message")
                     if isinstance(first, dict) else None),
        )


# DrissionPage 抓 cookie 的小工具
def extract_session_token(cookies: list[dict] | dict) -> str | None:
    """从 DrissionPage tab.cookies() 返回里挑 ChatGPT session token。

    tab.cookies() 默认返回 [{name, value, domain, ...}, ...] (list 形态),
    也可能返回 dict (取决于版本)。 优先匹配 __Secure- 前缀的, 再退到不带前缀的。
    """
    if isinstance(cookies, dict):
        items = [{"name": k, "value": v} for k, v in cookies.items()]
    else:
        items = list(cookies)
    # 先 __Secure- 前缀
    for c in items:
        if c.get("name") == COOKIE_NAMES[0]:
            return c.get("value")
    for c in items:
        if c.get("name") == COOKIE_NAMES[1]:
            return c.get("value")
    return None
