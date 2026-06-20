"""email-list.txt 邮箱池加载 + 状态持久化。

文件格式: 每行 "邮箱----密码----client_id----refresh_token"。
state 文件 email-pool-state.json (跟 main.py 同目录):
  { "邮箱": {"status": "ok"|"fail", "reason": "...", "at": "ISO8601"}, ... }
启动时跳过 state 里已有的, 跑完一个邮箱 (无论成败) 立刻写 state。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 邮箱池路径: 优先 EMAIL_LIST_PATH 环境变量, 否则用脚本同目录的 email-list.txt
DEFAULT_POOL_PATH = Path(__file__).with_name("email-list.txt")
DEFAULT_STATE_PATH = Path(__file__).with_name("email-pool-state.json")

_LINE_RE = re.compile(
    r"^(?P<email>[^-\s]+@[^-\s]+)----"
    r"(?P<password>[^-\s]+)----"
    r"(?P<client_id>[0-9a-fA-F-]+)----"  # UUID, 允许连字符
    r"(?P<refresh_token>[^\s]+)$"
)


@dataclass
class EmailAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str

    @classmethod
    def from_line(cls, line: str) -> "EmailAccount":
        line = line.strip()
        if not line or line.startswith("#"):
            raise ValueError("skip blank/comment line")
        m = _LINE_RE.match(line)
        if not m:
            raise ValueError(f"malformed line: {line!r}")
        return cls(**m.groupdict())


def load_pool(path: Path = DEFAULT_POOL_PATH) -> list[EmailAccount]:
    accounts: list[EmailAccount] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            accounts.append(EmailAccount.from_line(raw))
        except ValueError as e:
            if "skip" in str(e):
                continue
            raise ValueError(f"{path}:{n}: {e}") from e
    return accounts


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 损坏就当空, 重新开始; 用户也容易修
        return {}


def save_state(state: dict, path: Path = DEFAULT_STATE_PATH) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def mark(state: dict, email: str, *, status: str, reason: str = "") -> None:
    state[email] = {
        "status": status,
        "reason": reason[:200],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
