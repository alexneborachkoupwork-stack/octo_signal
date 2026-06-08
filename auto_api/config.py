"""
Loads config from auto_api/.env, then applies command-line overrides.
All other scripts import: from config import load
"""

import os
import argparse
from pathlib import Path

_ENV_FILE    = Path(__file__).parent / ".env"
_PROXY_FILE  = Path(__file__).parent / "data" / "proxies_isp.txt"
_ACCOUNT_FILE = Path(__file__).parent / "data" / "accounts.csv"

SERVICES        = ("capsolver", "anticaptcha", "2captcha", "capmonster", "playwright")
PROXY_MODES     = ("recent", "rotate", "random", "first", "direct")
ACCOUNT_STATUSES = ("new", "registered", "verified", "active", "applied", "blocked", "failed")
ACCOUNT_STRATEGIES = ("sequential", "random")

_SERVICE_KEY_MAP = {
    "capsolver":   "CAPSOLVER_KEYS",
    "anticaptcha": "ANTICAPTCHA_KEYS",
    "2captcha":    "TWOCAPTCHA_KEYS",
    "capmonster":  "CAPMONSTER_KEYS",
    "playwright":  "PLAYWRIGHT_KEYS",  # no real key needed — uses dummy "pw"
}


def _load_dotenv(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def _csv(value: str) -> list[str]:
    return [k.strip() for k in value.split(",") if k.strip()]


def _resolve_proxy(mode: str, explicit_url: str | None) -> tuple[str | None, str]:
    if explicit_url:
        return explicit_url, "direct(override)"
    if mode == "direct":
        return None, "direct"
    from proxy_pool import get_pool
    pool = get_pool(_PROXY_FILE)
    proxy = pool.get(mode)
    return proxy, mode


def _resolve_account(
    username: str | None,
    status: str,
    strategy: str,
    explicit_user: str | None,
    explicit_passwd: str | None,
) -> tuple[str, str, str | None]:
    """
    Returns (username, password, account_id_or_None).
    Priority: --user/--passwd explicit args > --account name > pool pick by status.
    """
    if explicit_user and explicit_passwd:
        return explicit_user, explicit_passwd, None

    from account_pool import get_pool as get_acc_pool
    pool = get_acc_pool(_ACCOUNT_FILE)

    if username:
        acct = pool.get_by_username(username)
        if not acct:
            raise ValueError(f"Account '{username}' not found in accounts.csv")
        return acct["username"], acct["password"], acct["id"]

    acct = pool.get(status=status, strategy=strategy)
    if not acct:
        raise ValueError(
            f"No account with status='{status}' found in accounts.csv. "
            f"Run --account-status to see counts."
        )
    return acct["username"], acct["password"], acct["id"]


def load(argv=None) -> argparse.Namespace:
    env = _load_dotenv(_ENV_FILE)

    parser = argparse.ArgumentParser(
        description="E-VISA pure API automation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Solver ──────────────────────────────────────────────────────────────
    parser.add_argument("--service", choices=SERVICES,
                        help="Override SOLVER_SERVICE from .env")
    parser.add_argument("--key",
                        help="Single solver key override (takes priority over .env keys)")

    # ── Account ─────────────────────────────────────────────────────────────
    parser.add_argument("--account",
                        help="Use this specific username from accounts.csv")
    parser.add_argument("--account-status", dest="account_status",
                        choices=ACCOUNT_STATUSES, default=None,
                        help="Pick account with this status from pool (default: verified)")
    parser.add_argument("--account-strategy", dest="account_strategy",
                        choices=ACCOUNT_STRATEGIES, default=None,
                        help="Pool selection strategy: sequential (default) or random")
    parser.add_argument("--account-pool-status", dest="account_pool_status",
                        action="store_true",
                        help="Print account pool status table and exit")
    # Legacy explicit overrides (bypass pool)
    parser.add_argument("--user",   help="Explicit username (bypasses account pool)")
    parser.add_argument("--passwd", help="Explicit password (bypasses account pool)")
    parser.add_argument("--lang",   help="Override SITE_LANGUAGE (PT or ENG)")

    # ── Proxy ────────────────────────────────────────────────────────────────
    parser.add_argument("--proxy",
                        help="Explicit proxy URL (bypasses pool). http://user:pass@host:port")
    parser.add_argument("--proxy-mode", dest="proxy_mode",
                        choices=PROXY_MODES, default=None,
                        help="recent|rotate|random|first|direct")
    parser.add_argument("--proxy-status", dest="proxy_status",
                        action="store_true",
                        help="Print proxy pool status and exit")

    args = parser.parse_args(argv)

    # ── Early-exit status commands ───────────────────────────────────────────
    if args.proxy_status:
        from proxy_pool import get_pool
        get_pool(_PROXY_FILE).status()
        raise SystemExit(0)

    if args.account_pool_status:
        from account_pool import get_pool as get_acc_pool
        get_acc_pool(_ACCOUNT_FILE).status()
        raise SystemExit(0)

    # ── Solver keys ──────────────────────────────────────────────────────────
    service = args.service or env.get("SOLVER_SERVICE", "capsolver")
    if args.key:
        keys = [args.key]
    else:
        env_var = _SERVICE_KEY_MAP.get(service, "")
        raw     = os.environ.get(env_var) or env.get(env_var, "")
        keys    = _csv(raw)

    # ── Proxy ────────────────────────────────────────────────────────────────
    explicit_proxy = args.proxy or os.environ.get("PROXY") or env.get("PROXY") or None
    proxy_mode     = args.proxy_mode or env.get("PROXY_MODE", "recent")
    proxy_url, proxy_mode_used = _resolve_proxy(
        mode=proxy_mode, explicit_url=explicit_proxy
    )

    # ── Account ──────────────────────────────────────────────────────────────
    acct_status   = args.account_status   or env.get("ACCOUNT_STATUS",   "verified")
    acct_strategy = args.account_strategy or env.get("ACCOUNT_STRATEGY", "sequential")
    try:
        username, passwd, account_id = _resolve_account(
            username        = args.account,
            status          = acct_status,
            strategy        = acct_strategy,
            explicit_user   = args.user   or os.environ.get("SITE_USERNAME") or env.get("SITE_USERNAME") or None,
            explicit_passwd = args.passwd or os.environ.get("SITE_PASSWORD") or env.get("SITE_PASSWORD") or None,
        )
    except ValueError as e:
        parser.error(str(e))

    cfg = argparse.Namespace(
        service          = service,
        keys             = keys,
        user             = username,
        passwd           = passwd,
        account_id       = account_id,    # None when using explicit --user/--passwd
        account_status   = acct_status,
        account_strategy = acct_strategy,
        account_file     = str(_ACCOUNT_FILE),
        lang             = args.lang or os.environ.get("SITE_LANGUAGE") or env.get("SITE_LANGUAGE", "PT"),
        proxy            = proxy_url,
        proxy_mode       = proxy_mode_used,
        proxy_file       = str(_PROXY_FILE),
    )

    if not cfg.keys:
        parser.error(f"No keys for '{service}' — set {_SERVICE_KEY_MAP[service]} in .env or use --key")

    return cfg
