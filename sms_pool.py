"""SMS 手机号池 — 包装 HeroSMS，加冷却管理。

池子只负责三件事:
    get_number(country)          -> Activation  买号并入池
    get_code(activation_id, ...) -> str         拿验证码，成功后出池
    cancel(activation_id)        -> None        取消购买，成功后出池

删除规则:
    - get_code 成功 -> 删
    - cancel 成功   -> 删
    - 其他异常      -> 保留，后续还能继续 get_code / cancel

HeroSMS 平台刚买号不能立刻退。cancel 时如果持有时间不到 120s，池子内部
会阻塞等待剩余时间，再调用平台 cancel。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from hero_sms import OPENAI_ID

if TYPE_CHECKING:
    from hero_sms import Activation, HeroSMS


MIN_HOLD_SECONDS = 120


class SmsPoolError(Exception):
    """SmsPool 内部错误。"""


class SmsPool:
    """HeroSMS 手机号池。"""

    def __init__(self, api_key: str, *, min_hold_seconds: float = MIN_HOLD_SECONDS) -> None:
        from hero_sms import HeroSMS  # 局部 import 避免循环

        self._client: HeroSMS = HeroSMS(api_key)
        self._min_hold = min_hold_seconds
        self._pool: dict[int, tuple[Activation, float]] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SmsPool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_number(self, country: int, *, service: str = OPENAI_ID) -> "Activation":
        """买号并入池。成功返回 Activation。"""
        act = self._client.get_number(service=service, country=country)
        self._pool[act.activation_id] = (act, time.monotonic())
        return act

    def get_code(self, activation_id: int, *,
                 timeout: float = 130.0,
                 poll_interval: float = 5.0) -> str:
        """轮询验证码。成功拿到后从池里删除。"""
        if activation_id not in self._pool:
            raise SmsPoolError(
                f"activation {activation_id} 不在池里 (可能已被消费/取消)")

        code = self._client.wait_for_code(
            activation_id,
            timeout=timeout,
            poll_interval=poll_interval,
            auto_cancel_on_timeout=False,
        )
        self._pool.pop(activation_id, None)
        return code

    def cancel(self, activation_id: int) -> None:
        """取消购买。持有不足 min_hold_seconds 时先等待，成功后从池里删除。"""
        if activation_id not in self._pool:
            return

        _act, purchased_at = self._pool[activation_id]
        elapsed = time.monotonic() - purchased_at
        if elapsed < self._min_hold:
            time.sleep(self._min_hold - elapsed)

        self._client.cancel(activation_id)
        self._pool.pop(activation_id, None)
