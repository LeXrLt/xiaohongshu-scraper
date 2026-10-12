"""查看本地运行日志中的风控/认证状态。

用法:
  python scripts/xhs_risk_status.py              # 最近20条风控事件
  python scripts/xhs_risk_status.py --all        # 所有风控事件
  python scripts/xhs_risk_status.py --summary    # 汇总统计
  python scripts/xhs_risk_status.py --hours 48   # 最近48小时
"""

import json
import sys
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

CODE_LABELS = {
    -104: "IP封搜索",
    -100: "Cookie失效",
    -101: "账号限制",
    -109: "账号封禁",
    401: "认证过期",
    403: "访问拒绝",
    429: "限流",
    460: "风控(滑块)",
    461: "验证码",
}


def fmt_code(code):
    if code is None:
        return "?"
    label = CODE_LABELS.get(code, "")
    return f"{code}({label})" if label else str(code)


# ---- 从 runs.jsonl 读取 ----

def _find_runs_jsonl() -> Path | None:
    """查找 runs.jsonl 路径。"""
    # 1. scripts 同级的 data/ 目录
    candidate = Path(__file__).resolve().parent.parent / "data" / "runs.jsonl"
    if candidate.exists():
        return candidate
    # 2. 当前工作目录
    candidate = Path("data") / "runs.jsonl"
    if candidate.exists():
        return candidate
    return None


def _load_local_events(path: Path, hours=None):
    """从 runs.jsonl 加载风控事件。"""
    cutoff = None
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not rec.get("was_rate_limited"):
                    continue
                if cutoff:
                    try:
                        ts = datetime.fromisoformat(rec["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            continue
                    except (KeyError, ValueError):
                        continue
                events.append(rec)
    except OSError as e:
        print(f"读取 {path} 失败: {e}", file=sys.stderr)
    return events


def local_show_recent(path: Path, limit=20, hours=None):
    events = _load_local_events(path, hours=hours)
    if not events:
        print("没有风控事件记录。")
        return
    # 最新的排前面
    events.sort(key=lambda r: r.get("ts", ""), reverse=True)
    events = events[:limit]

    print(f"{'时间':<20} {'账号':<15} {'Code':<18} {'API'}")
    print("-" * 90)
    for rec in events:
        ts = rec.get("ts", "?")[5:19] if rec.get("ts") else "?"
        account = rec.get("account", "?")
        code = rec.get("code") or rec.get("status")
        code_str = fmt_code(code)
        api = rec.get("api", "").replace("/api/sns/web/v1/", "")
        print(f"{ts:<20} {account:<15} {code_str:<18} {api}")


def local_show_summary(path: Path, hours=None):
    from collections import Counter

    events = _load_local_events(path, hours=hours)
    if not events:
        print("没有风控事件记录。")
        return

    period = f"（最近 {hours} 小时）" if hours else "（全部）"
    print(f"=== 风控汇总 [本地模式] {period} ===")

    # 按 code + account 汇总
    counts = Counter()
    for rec in events:
        code = rec.get("code") or rec.get("status")
        account = rec.get("account", "?")
        api = rec.get("api", "")
        counts[(code, account, api)] += 1

    print(f"{'Code':<18} {'账号':<15} {'次数':>6}  {'API'}")
    print("-" * 70)
    for (code, account, api), cnt in counts.most_common():
        code_str = fmt_code(code)
        api_short = api.replace("/api/sns/web/v1/", "")
        print(f"{code_str:<18} {account:<15} {cnt:>6}  {api_short}")

    # 多账号同时受影响检测
    print()
    from collections import defaultdict
    hour_accounts = defaultdict(set)
    for rec in events:
        ts = rec.get("ts", "")
        hour_key = ts[:13] if ts else "?"  # YYYY-MM-DDTHH
        account = rec.get("account", "?")
        code = rec.get("code") or rec.get("status")
        hour_accounts[(hour_key, code)].add(account)

    multi = [(k, code, accs) for (k, code), accs in hour_accounts.items() if len(accs) > 1]
    if multi:
        print("=== 同一时段多账号受影响（疑似IP级封锁）===")
        for (hour_key, code), accs in sorted(multi, reverse=True):
            print(f"  {hour_key} code={fmt_code(code)} accounts={', '.join(sorted(accs))}")



# ---- 入口 ----

def main():
    parser = argparse.ArgumentParser(description="查看本地风控/认证状态")
    parser.add_argument("--all", action="store_true", help="显示所有风控事件")
    parser.add_argument("--summary", action="store_true", help="汇总统计")
    parser.add_argument("--hours", type=int, default=None, help="限制最近N小时")
    parser.add_argument("--limit", type=int, default=20, help="显示条数（默认20）")
    args = parser.parse_args()

    runs_path = _find_runs_jsonl()
    if not runs_path:
        print("本地 runs.jsonl 未找到，无法查看风控状态。", file=sys.stderr)
        sys.exit(1)

    print(f"[数据源] 本地 {runs_path}", file=sys.stderr)
    if args.summary:
        local_show_summary(runs_path, hours=args.hours)
    else:
        limit = 9999 if args.all else args.limit
        local_show_recent(runs_path, limit=limit, hours=args.hours)


if __name__ == "__main__":
    main()
