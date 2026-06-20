"""Outlook 邮件接码客户端 (Microsoft Graph API)。

接 ChatGPT/OpenAI 注册邮件,提取验证码 (6 位数字) 或验证链接。

认证方式: OAuth2 refresh_token -> access_token (ROPC 路径在首次换 token 时
用过用户名+密码,本封装不再要求密码)。
需要 Azure 应用 (public client, personal account) 已开启 Mail.Read scope。

公开常量:
    DEFAULT_CODE_RE  -- 通用 6 位验证码正则 (匹配 OpenAI/ChatGPT 邮件里的数字)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

# 默认 scope: Graph 委托权限读取邮件。refresh_token 必须是带这个 scope 换的。
DEFAULT_SCOPES = ["https://graph.microsoft.com/.default"]

# 6 位验证码作为独立 token: 前后是空白/标点/换行,不嵌入长数字串。
# 比通用 4-8 位更精确,可避免从"订单号 1234567890"或年份中误抽。
DEFAULT_CODE_RE = re.compile(r"(?:^|[\s:：])\s*(\d{6})\s*(?:$|[\s.,;!?])",
                             re.MULTILINE)

# 匹配验证链接 (典型 chatgpt/email-verification 路径)
DEFAULT_LINK_RE = re.compile(
    r"https?://[^\s\"'<>]*?(?:verify|confirm|validate|email-verif)[^\s\"'<>]*",
    re.IGNORECASE,
)

# 验证码邮件 subject 关键词 (中文/英文)。
# 含 code/代码/验证码 才视为接码邮件;排除 "sign in / 登录 / new sign-in" 等通知。
DEFAULT_CODE_SUBJECT_KEYWORDS = (
    "code", "代码", "验证码", "verification", "登录代码",
)

# 默认过滤 ChatGPT/OpenAI 相关发件人 (子串匹配,涵盖 tm.openai.com 等子域)
DEFAULT_FROM_FILTER = (
    "openai.com",
    "chatgpt.com",
    "tm.openai.com",
    "platform.openai.com",
)


class EmailError(Exception):
    """邮件 API 错误。"""

    def __init__(self, message: str, *, status_code: int | None = None,
                 info: dict | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.info = info or {}


@dataclass
class InboxMessage:
    subject: str
    sender: str
    received: str
    body_preview: str
    body_html: str | None = None
    body_text: str | None = None

    def extract_code(self, code_re: re.Pattern[str] = DEFAULT_CODE_RE) -> str | None:
        # body_preview 已包含完整验证码(实测);subject 通常不带数字。
        for text in (self.body_preview, self.body_text or ""):
            if not text:
                continue
            if m := code_re.search(text):
                return m.group(1)
        return None

    def is_verification_email(self,
                              keywords: Iterable[str] = DEFAULT_CODE_SUBJECT_KEYWORDS
                              ) -> bool:
        """判定 subject 是否属于"验证码邮件"而非"登录通知"。

        "New sign-in" / "登录提醒" 等通知也会来自 openai.com,
        但 subject 不含 code/代码 等关键词,必须排除。
        """
        subj = self.subject.lower()
        return any(kw.lower() in subj for kw in keywords)

    def extract_link(self, link_re: re.Pattern[str] = DEFAULT_LINK_RE) -> str | None:
        for text in (self.body_preview, self.body_text or "", self.body_html or ""):
            if not text:
                continue
            if m := link_re.search(text):
                return m.group(0)
        return None


@dataclass
class VerificationResult:
    code: str | None
    link: str | None
    message: InboxMessage


class OutlookClient:
    """Outlook 邮箱客户端。复用 token 自动刷新。

    构造参数 (四件套):
        account       -- 邮箱地址 (例如 user@outlook.com),用于记录/审计,
                         不参与认证请求。
        password      -- 邮箱密码。当前 refresh_token 路径下不参与认证,
                         仅作为占位保留以兼容"账号----密码----ID----Token"调用形式。
                         若以后改走 ROPC 现场换 token,会用到。
        client_id     -- Azure AD 应用的 Application (client) ID。
        refresh_token -- 已经换好的 refresh_token,后续用它续命 access_token。
    """

    def __init__(self, *, account: str, password: str, client_id: str,
                 refresh_token: str,
                 scopes: Iterable[str] = DEFAULT_SCOPES,
                 timeout: float = 30.0) -> None:
        if not account:
            raise ValueError("account is required")
        if not client_id:
            raise ValueError("client_id is required")
        if not refresh_token:
            raise ValueError("refresh_token is required")
        self.account = account
        self.password = password  # noqa: F841 - 当前未使用,保留以备 ROPC
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.scopes = list(scopes)
        self.timeout = timeout
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OutlookClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- token ----------

    def _ensure_token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": " ".join(self.scopes),
        }
        resp = self._client.post(TOKEN_URL, data=data)
        payload = self._parse_json(resp)
        if "access_token" not in payload:
            raise EmailError(
                f"Token refresh failed: {payload}",
                status_code=resp.status_code,
                info=payload,
            )
        self._access_token = payload["access_token"]
        # 默认 3600s,刷新前 60s 算过期
        self._expires_at = time.monotonic() + int(payload.get("expires_in", 3600))
        if "refresh_token" in payload:
            self.refresh_token = payload["refresh_token"]
        return self._access_token

    # ---------- inbox ----------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Prefer": 'outlook.body-content-type="html"',
        }

    def list_inbox(self, *, top: int = 10,
                   filter_from: Iterable[str] | None = None,
                   only_unread: bool = True) -> list[InboxMessage]:
        """列出收件箱最新 N 封邮件。默认只看未读。"""
        params: dict[str, str] = {
            "$top": str(top),
            "$orderby": "receivedDateTime desc",
            "$select": "subject,from,receivedDateTime,bodyPreview,body",
        }
        if only_unread:
            # Graph 的 from 多 OR 过滤会触发 InefficientFilter,
            # 改成服务端只按未读+时间拉,客户端再按 domain 过滤。
            params["$filter"] = "isRead eq false"

        resp = self._client.get(
            f"{GRAPH_BASE}/me/mailFolders/Inbox/messages",
            headers=self._headers(),
            params=params,
        )
        data = self._parse_json(resp)
        msgs = [self._to_msg(m) for m in data.get("value", [])]
        if filter_from:
            domains = tuple(d.lower() for d in filter_from)
            msgs = [m for m in msgs
                    if any(d in m.sender.lower() for d in domains)]
        return msgs

    def wait_for_verification(self, *,
                             after: str | datetime,
                             timeout: float = 180.0,
                             poll_interval: float = 5.0,
                             filter_from: Iterable[str] = DEFAULT_FROM_FILTER,
                             code_re: re.Pattern[str] | None = None,
                             link_re: re.Pattern[str] | None = None,
                             require_code_subject: bool = True
                             ) -> VerificationResult:
        """轮询直到收到 ChatGPT/OpenAI 邮件并提取验证码或链接。

        after: 起始时间戳(字符串或 datetime)。只查 receivedDateTime >= after
               的邮件,精确锁定本次注册触发的验证码,不被历史邮件干扰。
        timeout: 最长轮询时间,硬上限 180s (=3 分钟),超过即抛
                 EmailError("Timeout fetching verification code")。
        require_code_subject=True: 只把"含 code/代码/验证码"主题的邮件当作接码邮件,
                                   排除 "sign in / 登录" 通知。

        命中时返回 receivedDateTime 最新的那封(同时间戳多次发送以最新为准)。
        """
        if timeout > 180.0:
            raise ValueError("timeout must be <= 180s (3 minute hard cap)")
        after_str = self._normalize_after(after)

        deadline = time.monotonic() + timeout
        best: VerificationResult | None = None
        while True:
            for msg in self._list_after(after_str, filter_from=filter_from):
                if require_code_subject and not msg.is_verification_email():
                    continue
                code = msg.extract_code(code_re or DEFAULT_CODE_RE)
                link = msg.extract_link(link_re or DEFAULT_LINK_RE)
                if not (code or link):
                    continue
                cand = VerificationResult(code=code, link=link, message=msg)
                # 同时间戳选 received 最新的那封。
                if (best is None
                        or cand.message.received > best.message.received):
                    best = cand
            if best is not None:
                return best
            if time.monotonic() >= deadline:
                raise EmailError("Timeout fetching verification code")
            time.sleep(poll_interval)

    def _list_after(self, after_str: str, *,
                    filter_from: Iterable[str]) -> list[InboxMessage]:
        """拉 receivedDateTime >= after_str 的所有邮件 (含已读), 客户端过滤 domain。

        服务端按时间范围拉一次拿到全部候选 (避免 InefficientFilter)；
        客户端按 domain + verification 主题筛选。
        """
        params: dict[str, str] = {
            "$top": "50",
            "$orderby": "receivedDateTime desc",
            "$select": "subject,from,receivedDateTime,bodyPreview,body",
            "$filter": f"receivedDateTime ge {after_str}",
        }
        resp = self._client.get(
            f"{GRAPH_BASE}/me/mailFolders/Inbox/messages",
            headers=self._headers(),
            params=params,
        )
        data = self._parse_json(resp)
        msgs = [self._to_msg(m) for m in data.get("value", [])]
        if filter_from:
            domains = tuple(d.lower() for d in filter_from)
            msgs = [m for m in msgs
                    if any(d in m.sender.lower() for d in domains)]
        return msgs

    @staticmethod
    def _normalize_after(after: str | datetime) -> str:
        """datetime -> ISO 8601 Z 字符串;字符串原样返回 (要求已是 OData 可解析格式)。"""
        if isinstance(after, datetime):
            if after.tzinfo is None:
                after = after.replace(tzinfo=timezone.utc)
            # Graph OData 要求形如 2026-06-19T17:00:00Z (无微秒)
            return after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return after

    # ---------- 内部 ----------

    @staticmethod
    def _to_msg(raw: dict) -> InboxMessage:
        sender = raw.get("from", {}).get("emailAddress", {})
        body = raw.get("body", {}) or {}
        return InboxMessage(
            subject=raw.get("subject", ""),
            sender=sender.get("address", ""),
            received=raw.get("receivedDateTime", ""),
            body_preview=raw.get("bodyPreview", ""),
            body_html=body.get("content") if body.get("contentType") == "html" else None,
            body_text=body.get("content") if body.get("contentType") == "text" else None,
        )

    @staticmethod
    def _parse_json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise EmailError(
                f"Non-JSON response (HTTP {resp.status_code})",
                status_code=resp.status_code,
            ) from e
        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            if isinstance(err, str):
                msg = err
                info: dict = {}
            else:
                msg = f"{err.get('code', 'Error')}: {err.get('message', data)}"
                info = err
            raise EmailError(msg, status_code=resp.status_code, info=info)
        return data if isinstance(data, dict) else {}