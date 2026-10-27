"""把本机运行日志与对话记录增量导出到 docs/运行日志/<日期>/，供协同开发提交 GitHub。

用法：uv run --locked python scripts/导出运行日志.py
只读 var/runtime.db；上次导出到哪里记在 docs/运行日志/导出进度.json，重复执行只导出新增部分。
"""

import datetime
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAR = ROOT / "var"
OUT_ROOT = ROOT / "docs" / "运行日志"
STATE = OUT_ROOT / "导出进度.json"
SKIP_DIRS = {"uv-cache", "uv-python", "models", "node_modules", "pytest", "ruff"}

# 只增不改的表按游标增量导出
APPEND_TABLES = {
    "conversation_turns": "id",
    "runtime_events": "id",
    "action_requests": "updated_ms",
}
# 会被原地更新的小表每次整表快照
SNAPSHOT_TABLES = (
    "conversation_sessions",
    "conversation_chats",
    "conversation_social_state",
    "chat_memories",
    "long_term_memories",
    "persona_instances",
    "runtime_snapshots",
    "memory_progress",
    "phase8_reminders",
    "cloned_voices",
    "robot_language_preferences",
    "chat_language_preferences",
    "voice_preferences",
    "robot_inner_state",
    "robot_experiences",
    "robot_open_threads",
    "robot_reflections",
)
REDACT_ENV_KEYS = (
    "DASHSCOPE_API_KEY",
    "AMAP_WEATHER_API_KEY",
    "ALIYUN_WORKSPACE_ID",
    "ANNIE_D3_TOKEN",
    "ANNIE_D3_URL",
    "ANNIE_EXHIBIT_LIVEKIT_API_KEY",
    "ANNIE_EXHIBIT_LIVEKIT_API_SECRET",
)


def load_redactions():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return []
    pairs = []
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("\"'")
        if key.endswith("_URL"):
            val = val.rstrip("/")
        if key in REDACT_ENV_KEYS and len(val) >= 6:
            pairs.append((val, f"<已脱敏:{key}>"))
    return sorted(pairs, key=lambda item: len(item[0]), reverse=True)


def write_rows(path, rows, mode, redactions=()):
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as f:
        for row in rows:
            data = {k: row[k] for k in row.keys() if not isinstance(row[k], bytes)}
            line = json.dumps(data, ensure_ascii=False)
            for secret, tag in redactions:
                line = line.replace(secret, tag)
            f.write(line + "\n")
    return len(rows)


def export_logs(out, offsets, redactions=()):
    byte_redactions = [(s.encode("utf-8"), t.encode("utf-8")) for s, t in redactions]
    count = 0
    for path in sorted(VAR.rglob("*.log")):
        rel = path.relative_to(VAR)
        if SKIP_DIRS & set(rel.parts):
            continue
        key = rel.as_posix()
        size = path.stat().st_size
        start = offsets.get(key, 0)
        if size < start:  # 日志被轮转或重建，从头导出
            start = 0
        if size == start:
            continue
        dst = out / "日志" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as src, dst.open("ab") as f:
            src.seek(start)
            chunk = src.read(size - start)
            for secret, tag in byte_redactions:
                chunk = chunk.replace(secret, tag)
            f.write(chunk)
        offsets[key] = size
        count += 1
    return count


def export_database(out, cursors, redactions=()):
    db_path = VAR / "runtime.db"
    if not db_path.exists():
        return {}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    data = out / "对话与事件"
    counts = {}
    for table, column in APPEND_TABLES.items():
        if table not in tables:
            continue
        last = cursors.get(table, 0)
        rows = db.execute(
            f"select * from {table} where {column} > ? order by {column}", (last,)
        ).fetchall()
        counts[table] = write_rows(data / f"{table}.jsonl", rows, "a", redactions)
        if rows:
            cursors[table] = rows[-1][column]
    for table in SNAPSHOT_TABLES:
        if table in tables:
            rows = db.execute(f"select * from {table} order by rowid").fetchall()
            counts[table] = write_rows(data / f"{table}.jsonl", rows, "w", redactions)
    db.close()
    return counts


def main():
    redactions = load_redactions()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    offsets = state.setdefault("log_offsets", {})
    cursors = state.setdefault("table_cursors", {})
    out = OUT_ROOT / datetime.date.today().isoformat()
    logs = export_logs(out, offsets, redactions)
    counts = export_database(out, cursors, redactions)
    state["last_export"] = datetime.datetime.now().isoformat(timespec="seconds")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"导出到 {out.relative_to(ROOT)}：日志 {logs} 个文件有新增")
    for table, n in counts.items():
        if n:
            print(f"  {table}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
