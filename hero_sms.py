"""HeroSMS 接码 API 的最小封装。

覆盖一次性接码流程：余额查询 -> 拿号 -> 轮询 -> 完成/取消。

API 形式:
  - 现代 REST (/api/v1/...) 仅暴露价格/邮箱接口,认证用
        Authorization: ApiKey <token>
  - SMS-Activate 兼容接口 (stubs/handler_api.php?api_key=...&action=...)
    才是拿号/状态机的入口。所有号码相关操作走这条路径。

公开常量:
    USA_ID        -- 美国 country id (187)
    OPENAI_ID     -- OpenAI/ChatGPT service code (dr)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

API_V1_BASE = "https://hero-sms.com/api/v1"
SA_BASE = "https://hero-sms.com/stubs/handler_api.php"

USA_ID = 187
OPENAI_ID = "dr"

_STATUS_OK_RE = re.compile(r"^STATUS_OK:(?P<code>\d+)$")
_STATUS_CANCEL_RE = re.compile(r"^STATUS_CANCEL$")


class HeroSMSError(Exception):
    """任何 HeroSMS API 错误。包括 HTTP 错误和业务错误。"""

    def __init__(self, message: str, *, title: str | None = None,
                 status_code: int | None = None, info: dict | None = None) -> None:
        super().__init__(message)
        self.title = title
        self.status_code = status_code
        self.info = info or {}


@dataclass
class Activation:
    activation_id: int
    phone_number: str
    activation_cost: float | None = None
    country_code: int | None = None
    can_get_another_sms: bool | None = None

    @property
    def phone_local(self) -> str:
        """OpenAI add-phone 表单期望的纯本地号 (美国 10 位, 去掉 +1)。
        其他国家默认原样返回 (例如中国 11 位, 英国 10 位, 不动)。"""
        if self.country_code == 1 and self.phone_number.startswith("1") \
                and len(self.phone_number) == 11:
            return self.phone_number[1:]
        return self.phone_number

    @classmethod
    def from_sa_response(cls, text: str) -> "Activation":
        """解析 'ACCESS_NUMBER:id:phone' 文本响应。"""
        parts = text.split(":")
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise HeroSMSError(f"Unexpected getNumber response: {text!r}")
        return cls(activation_id=int(parts[1]), phone_number=parts[2])


@dataclass
class PriceQuote:
    service: str
    country: int
    cost: float
    count: int


class HeroSMS:
    """接码客户端。一次创建一个,复用连接。"""

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.timeout = timeout
        self._client = httpx.Client(
            headers={
                "Authorization": f"ApiKey {api_key}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HeroSMS":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- 余额 ----------

    def get_balance(self) -> float:
        """返回账户余额(数字)。"""
        text = self._sa_get("getBalance")
        prefix = "ACCESS_BALANCE:"
        if not text.startswith(prefix):
            raise HeroSMSError(f"Unexpected getBalance response: {text!r}")
        return float(text[len(prefix):])

    # ---------- 行情 ----------

    def get_offers(self, *, services: list[str] | None = None,
                   countries: list[int] | None = None) -> dict:
        """现代 REST 接口,看价格和可用数量。

        返回原始嵌套结构;通常用 get_price 拿到特定 (service, country) 的报价。
        """
        params: dict[str, str] = {}
        if services:
            params["services"] = ",".join(services)
        if countries:
            params["countries"] = ",".join(str(c) for c in countries)
        return self._v1_get("/activations/offers", params=params).get("data", {})

    def get_price(self, service: str, country: int = USA_ID) -> PriceQuote | None:
        """查询 (service, country) 的报价。无库存返回 None。"""
        offers = self.get_offers(services=[service], countries=[country])
        country_block = offers.get(service, {}).get(str(country))
        if not country_block or country_block.get("counts", {}).get("total", 0) == 0:
            return None
        prices = country_block.get("prices", {})
        cost = prices.get("default") or prices.get("retail") or prices.get("min")
        if cost is None:
            return None
        return PriceQuote(
            service=service,
            country=country,
            cost=float(cost),
            count=int(country_block["counts"]["total"]),
        )

    # ---------- 拿号 ----------

    def get_number(self, service: str = OPENAI_ID, country: int = USA_ID, *,
                   max_price: float | None = None,
                   fixed_price: bool = False,
                   phone_exception: str | None = None,
                   min_balance: float | None = None) -> Activation:
        """购买一个号码。

        min_balance: 拿号前要求账户余额至少为这个数。默认 = 当前报价。
                     给 None 表示不检查。余额不足抛 HeroSMSError,
                     title="INSUFFICIENT_BALANCE",info 含余额和需要的金额。
        """
        quote = self.get_price(service, country)
        required = min_balance if min_balance is not None else (
            quote.cost if quote else None)
        if required is None:
            raise HeroSMSError(
                f"No offers for service={service!r} country={country!r}")
        balance = self.get_balance()
        if balance < required:
            raise HeroSMSError(
                f"Insufficient balance: have {balance:.4f}, "
                f"need at least {required:.4f}",
                title="INSUFFICIENT_BALANCE",
                info={"balance": balance, "required": required,
                      "service": service, "country": country},
            )
        params: dict[str, Any] = {"service": service, "country": country}
        if max_price is not None:
            params["maxPrice"] = max_price
        if fixed_price:
            params["fixedPrice"] = "true"
        if phone_exception:
            params["phoneException"] = phone_exception
        text = self._sa_get("getNumber", params=params)
        act = Activation.from_sa_response(text)
        act.country_code = country
        return act

    # ---------- 状态机 ----------

    def get_status(self, activation_id: int) -> str:
        """返回 SMS-Activate 原始状态字符串。

        常见值: STATUS_WAIT_CODE / STATUS_WAIT_RETRY / STATUS_WAIT_RESEND /
                STATUS_CANCEL / STATUS_OK:<code>。
        """
        return self._sa_get("getStatus", params={"id": activation_id})

    def wait_for_code(self, activation_id: int, *,
                      timeout: float = 180.0,
                      poll_interval: float = 5.0,
                      on_retry_request: bool = False,
                      auto_cancel_on_timeout: bool = True) -> str:
        """轮询直到拿到验证码或超时/取消。

        返回验证码字符串(纯数字)。
        如果收到 STATUS_CANCEL 或超时,抛出 HeroSMSError;
        超时时如果 auto_cancel_on_timeout=True (默认) 会先尝试取消并退号,
        然后在异常上设 auto_cancelled=True 让调用方知道是否已退。
        on_retry_request: 收到 STATUS_WAIT_RETRY 时自动调用 setStatus(id, 3)
                          请求平台重发短信。
        """
        deadline = time.monotonic() + timeout
        while True:
            text = self.get_status(activation_id)
            if m := _STATUS_OK_RE.match(text):
                return m.group("code")
            if _STATUS_CANCEL_RE.match(text):
                raise HeroSMSError(f"Activation {activation_id} was cancelled")
            if on_retry_request and text == "STATUS_WAIT_RETRY":
                self.set_status(activation_id, 3)
            if time.monotonic() >= deadline:
                msg = (f"Timeout waiting for code on activation "
                       f"{activation_id} after {timeout:.0f}s")
                cancelled = False
                if auto_cancel_on_timeout:
                    try:
                        self.cancel(activation_id)
                        cancelled = True
                    except HeroSMSError as e:
                        msg += f"; auto-cancel failed: {e}"
                raise HeroSMSError(msg, info={"auto_cancelled": cancelled})
            time.sleep(poll_interval)

    def set_status(self, activation_id: int, status: int) -> str:
        """设置激活状态。status: 3=请求重发, 6=完成, 8=取消。"""
        if status not in (3, 6, 8):
            raise ValueError("status must be 3, 6, or 8")
        return self._sa_get("setStatus",
                            params={"id": activation_id, "status": status})

    def finish(self, activation_id: int) -> None:
        self.set_status(activation_id, 6)

    def cancel(self, activation_id: int) -> None:
        self.set_status(activation_id, 8)

    # ---------- 内部 ----------

    def _v1_get(self, path: str, *, params: dict | None = None) -> dict:
        resp = self._client.get(f"{API_V1_BASE}{path}", params=params)
        return self._parse_json(resp)

    def _sa_get(self, action: str, *, params: dict | None = None) -> str:
        """SMS-Activate 兼容接口,返回原始文本。"""
        params = dict(params or {})
        params["api_key"] = self.api_key
        params["action"] = action
        resp = self._client.get(SA_BASE, params=params)
        self._raise_for_sa(resp)
        return resp.text

    @staticmethod
    def _parse_json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise HeroSMSError(
                f"Non-JSON response (HTTP {resp.status_code})",
                status_code=resp.status_code,
            ) from e
        if resp.status_code >= 400:
            if isinstance(data, dict) and "title" in data:
                raise HeroSMSError(
                    f"{data.get('title')}: {data.get('details')}",
                    title=data.get("title"),
                    status_code=resp.status_code,
                    info=data.get("info"),
                )
            raise HeroSMSError(f"HTTP {resp.status_code}",
                               status_code=resp.status_code)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _raise_for_sa(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            data = resp.json()
        except ValueError:
            raise HeroSMSError(f"HTTP {resp.status_code}",
                               status_code=resp.status_code)
        raise HeroSMSError(
            f"{data.get('title')}: {data.get('details')}",
            title=data.get("title"),
            status_code=resp.status_code,
            info=data.get("info"),
        )