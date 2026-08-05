from __future__ import annotations

import csv
import itertools
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "bot/.env"
OUT_DIR = ROOT / "backtest/results/opt_h4m30"
OUT_CSV = OUT_DIR / "optimization_results.csv"


def set_key(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=).*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda m: m.group(1) + value, text)
    return text + f"\n{key}={value}\n"


def parse_summary(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        fields[k.strip()] = v.strip()
    return fields


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    original_env = ENV_PATH.read_text(encoding="utf-8")

    base_overrides = {
        "GOLD_STRATEGY_NAMES": "trend_following",
        "TREND_FOLLOWING_LTF": "M30",
        "TREND_FOLLOWING_HTF": "H4",
        "BACKTEST_LOOKBACK_VALUE": "12",
        "BACKTEST_LOOKBACK_UNIT": "weeks",
        "BACKTEST_RESULTS_SUBDIR": "opt_h4m30",
        "BACKTEST_FIXED_VOLUME": "0.01",
        "BACKTEST_MAX_VOLUME_CAP": "0.01",
        "BACKTEST_VOLUME_MAX": "0.01",
        "BACKTEST_SIMULATE_MARGIN_REJECTION": "true",
        "BACKTEST_DATA_DIR": "bot/backtest/data/",
    }

    ema_fast_vals = [7, 9, 12]
    ema_slow_vals = [21, 34]
    ema_trend_vals = [150, 200]
    sl_vals = [80, 100, 120, 150]
    rr_vals = [2.0, 2.5, 3.0]

    cases: list[tuple[int, int, int, int, int]] = []
    for ef, es, et, sl, rr in itertools.product(ema_fast_vals, ema_slow_vals, ema_trend_vals, sl_vals, rr_vals):
        if ef >= es:
            continue
        tp = int(round(sl * rr))
        if tp < 160:
            continue
        if (ef, es, et) not in {(9, 21, 200), (7, 21, 150), (12, 34, 200), (9, 34, 150)}:
            continue
        cases.append((ef, es, et, sl, tp))

    seen: set[tuple[int, int, int, int, int]] = set()
    ordered_cases: list[tuple[int, int, int, int, int]] = []
    for case in cases:
        if case in seen:
            continue
        seen.add(case)
        ordered_cases.append(case)

    valid_results: list[dict[str, object]] = []

    try:
        for ef, es, et, sl, tp in ordered_cases:
            env_text = original_env
            for k, v in base_overrides.items():
                env_text = set_key(env_text, k, v)
            env_text = set_key(env_text, "GOLD_EMA_FAST", str(ef))
            env_text = set_key(env_text, "GOLD_EMA_SLOW", str(es))
            env_text = set_key(env_text, "GOLD_EMA_TREND_PERIOD", str(et))
            env_text = set_key(env_text, "TREND_FOLLOWING_GOLD_STOP_LOSS_PIPS", str(sl))
            env_text = set_key(env_text, "TREND_FOLLOWING_GOLD_TAKE_PROFIT_PIPS", str(tp))
            ENV_PATH.write_text(env_text, encoding="utf-8")

            before = {p.name for p in OUT_DIR.glob("*_summary.txt")}
            cmd = [
                str(ROOT / ".venv/bin/python"),
                "main.py",
                "--env",
                "bot/.env",
                "--mode",
                "backtest",
                "--no-trade",
                "--symbols",
                "XAUUSD",
                "--strategy",
                "trend_following",
                "--backtest-data-dir",
                "bot/backtest/data",
                "--backtest-lookback-value",
                "12",
                "--backtest-lookback-unit",
                "weeks",
                "--backtest-results-subdir",
                "opt_h4m30",
            ]
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            if proc.returncode != 0:
                continue

            after = {p.name for p in OUT_DIR.glob("*_summary.txt")}
            new_files = sorted(after - before)
            if new_files:
                summary_path = OUT_DIR / new_files[-1]
            else:
                summaries = sorted(OUT_DIR.glob("*_summary.txt"), key=lambda p: p.stat().st_mtime)
                if not summaries:
                    continue
                summary_path = summaries[-1]

            summary = parse_summary(summary_path)
            valid_results.append(
                {
                    "ema_fast": ef,
                    "ema_slow": es,
                    "ema_trend": et,
                    "sl_pips": sl,
                    "tp_pips": tp,
                    "total_signals": int(summary.get("total_signals", "0")),
                    "wins": int(summary.get("wins", "0")),
                    "losses": int(summary.get("losses", "0")),
                    "win_rate": float(summary.get("win_rate", "0")),
                    "end_balance": float(summary.get("end_balance", "0")),
                    "balance_change": float(summary.get("balance_change", "0")),
                    "summary": str(summary_path.relative_to(ROOT)),
                }
            )
    finally:
        ENV_PATH.write_text(original_env, encoding="utf-8")

    valid_results.sort(key=lambda r: (float(r["end_balance"]), float(r["win_rate"])), reverse=True)

    if valid_results:
        with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(valid_results[0].keys()))
            writer.writeheader()
            writer.writerows(valid_results)

    print(f"TOTAL_CASES={len(ordered_cases)}")
    print(f"VALID_CASES={len(valid_results)}")
    for row in valid_results[:12]:
        print(
            "TOP",
            f"ema={row['ema_fast']}/{row['ema_slow']}/{row['ema_trend']}",
            f"sl={row['sl_pips']}",
            f"tp={row['tp_pips']}",
            f"end={float(row['end_balance']):.2f}",
            f"change={float(row['balance_change']):+.2f}",
            f"wr={float(row['win_rate']):.2f}",
            f"w/l={row['wins']}/{row['losses']}",
            f"signals={row['total_signals']}",
            f"summary={row['summary']}",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
