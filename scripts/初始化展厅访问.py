"""一次性为已有 .env 配置展厅登录令牌，不改动已有机器人/模型密钥。

用法：uv run --locked python scripts/初始化展厅访问.py
"""

import os
import secrets
from pathlib import Path

KEYS = ("ANNIE_EXHIBIT_OPERATOR_TOKEN", "ANNIE_EXHIBIT_VIEWER_TOKEN")


def provision(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    found = {}
    updated = []
    for line in lines:
        raw = line.strip()
        raw_entry = raw[7:].strip() if raw.startswith("export ") else raw
        key, separator, value = raw_entry.partition("=")
        if separator and key in KEYS and not raw.startswith("#"):
            token = value.strip().strip('"\'') or secrets.token_urlsafe(36)
            found[key] = token
            updated.append(f"{key}={token}")
        else:
            updated.append(line)
    for key in KEYS:
        if key not in found:
            found[key] = secrets.token_urlsafe(36)
            updated.append(f"{key}={found[key]}")
    if found[KEYS[0]] == found[KEYS[1]]:
        raise ValueError("展厅操作员与显示终端令牌不得相同，请先手动修正现有配置")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return found


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    credentials = provision(root / ".env")
    print("已初始化展厅访问凭据（请妥善保存，勿发送到聊天或提交 Git）：")
    for name, token in credentials.items():
        print(f"{name}={token}")
    print("未填写 LiveKit URL/API Key/Secret/Room 时不会展示真实摄像头画面。")
