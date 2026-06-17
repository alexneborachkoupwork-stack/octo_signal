"""
Benchmark each CAPTCHA solver service independently for LOGIN_EVISA.

Tests capsolver, anticaptcha, 2captcha, capmonster one at a time (no racing)
to measure their real score distributions without capmonster starving the rest.

Usage:
  uv run python benchmark_solvers.py
  uv run python benchmark_solvers.py --rounds 3
  uv run python benchmark_solvers.py --services capsolver,2captcha
"""

import argparse
import sys
import time
from pathlib import Path

_ENV_FILE = Path(__file__).parent / ".env"


def _load_env() -> dict:
    env = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _keys(env, name):
    return [k.strip() for k in env.get(name, "").split(",") if k.strip()]


def _run_one(fn, key, action):
    """Call a solver function directly (no race, no stop event)."""
    import solver as sol
    t0 = time.time()
    token = fn(key, action, None, None)
    elapsed = time.time() - t0
    score = sol.score_token(token)
    return token, score, elapsed


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Benchmark CAPTCHA solvers independently")
    parser.add_argument("--rounds",   type=int, default=5,
                        help="Attempts per service (default: 5)")
    parser.add_argument("--services", default="",
                        help="Comma-separated subset: capsolver,anticaptcha,2captcha,capmonster")
    parser.add_argument("--action",   default="LOGIN_EVISA",
                        help="reCAPTCHA action (default: LOGIN_EVISA)")
    args = parser.parse_args()

    env = _load_env()

    import solver as sol

    service_map = {
        "capsolver":   (sol._capsolver,   _keys(env, "CAPSOLVER_KEYS")),
        "anticaptcha": (sol._anticaptcha, _keys(env, "ANTICAPTCHA_KEYS")),
        "2captcha":    (sol._twocaptcha,  _keys(env, "TWOCAPTCHA_KEYS")),
        "capmonster":  (sol._capmonster,  _keys(env, "CAPMONSTER_KEYS")),
    }

    wanted = [s.strip() for s in args.services.split(",") if s.strip()] if args.services else list(service_map)
    services = {k: v for k, v in service_map.items() if k in wanted and v[1]}

    if not services:
        print("ERROR: no solver keys found in .env for the requested services")
        sys.exit(1)

    print(f"\n{'='*65}")
    print(f"  Benchmark: {args.action}  rounds={args.rounds}  services={list(services)}")
    print(f"{'='*65}\n")

    summary: dict[str, list[int]] = {}

    for name, (fn, keys) in services.items():
        key = keys[0]
        scores = []
        times  = []
        print(f"── {name.upper()} ──────────────────────────────────────")
        for i in range(1, args.rounds + 1):
            try:
                token, score, elapsed = _run_one(fn, key, args.action)
                scores.append(score)
                times.append(elapsed)
                print(f"  [{i}/{args.rounds}] len={len(token)}  score={score}  time={elapsed:.1f}s")
            except Exception as e:
                print(f"  [{i}/{args.rounds}] FAIL: {e}")
        print()
        summary[name] = scores

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"{'='*65}")
    print(f"  SUMMARY  (score: 100=auto-pass, 85=short, 70=ok, 55=border, 40=low)")
    print(f"{'='*65}")
    print(f"  {'Service':<14} {'Scores':<30} {'Avg':>5}  {'Min':>5}  {'Max':>5}  {'≥80':>5}")
    print(f"  {'-'*60}")
    for name, scores in summary.items():
        if not scores:
            print(f"  {name:<14} no results")
            continue
        avg  = sum(scores) / len(scores)
        mn   = min(scores)
        mx   = max(scores)
        good = sum(1 for s in scores if s >= 80)
        print(f"  {name:<14} {str(scores):<30} {avg:>5.0f}  {mn:>5}  {mx:>5}  {good:>4}/{len(scores)}")
    print(f"{'='*65}")

    # Recommendation
    best_name = max(summary, key=lambda n: sum(summary[n]) / len(summary[n]) if summary[n] else 0)
    best_scores = summary.get(best_name, [])
    if best_scores:
        best_avg = sum(best_scores) / len(best_scores)
        print(f"\n  Best service: {best_name}  (avg score={best_avg:.0f})")
        if best_avg < 70:
            print("  WARNING: best avg score < 70 — all services producing low-quality tokens.")
            print("  Consider: race_all with min_score so faster-but-worse solvers don't starve others.")


if __name__ == "__main__":
    main()
