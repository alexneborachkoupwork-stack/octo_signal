"""
reCAPTCHA Enterprise solver wrapper.
Supports: CapSolver, Anti-Captcha, 2Captcha, CapMonster.

All Enterprise v2 tasks use ProxyLess task types. Solver-service IPs (CapSolver,
AntiCaptcha, etc.) have dedicated high-trust Google reputation that produces
higher reCAPTCHA Enterprise scores than passing our own ISP proxy IPs through
proxy-aware tasks. Competitor analysis confirmed ProxyLess is the working approach.

solve() rotates through the provided key list on failure.
"""

import time
import threading
import requests

SITE_KEY   = "6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4"
AUTH_URL   = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"
POLL_DELAY = 3
MAX_POLLS  = 40  # ~2 minutes

_V3_ACTIONS: frozenset[str] = frozenset()  # all actions use ReCaptchaV2Enterprise


def _parse_proxy(proxy_url: str | None) -> dict | None:
    """
    Parse http://user:pass@host:port → {proxyType, proxyAddress, proxyPort,
    proxyLogin, proxyPassword} for CAPTCHA service APIs.
    Returns None if proxy_url is None/empty.
    """
    if not proxy_url:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        return {
            "proxyType":     p.scheme or "http",
            "proxyAddress":  p.hostname,
            "proxyPort":     p.port or 6666,
            "proxyLogin":    p.username or "",
            "proxyPassword": p.password or "",
        }
    except Exception:
        return None


def score_token(token: str) -> int:
    """
    Estimate reCAPTCHA Enterprise score from token length.
    Shorter = auto-pass checkbox (no image challenge) = higher Google score.
    Based on observed token lengths: successful logins tend toward shorter tokens.
    Returns 0-100; higher is better.
    """
    n = len(token)
    if n < 2320:  return 100  # auto-pass — no image challenge, highest score
    if n < 2380:  return 85
    if n < 2430:  return 70
    if n < 2470:  return 55   # confirmed working at this length (neupir2449: len=2446)
    if n < 2510:  return 40
    if n < 2550:  return 25
    return 10


def race_best(
    capsolver_keys: list[str], anticaptcha_keys: list[str],
    twocaptcha_keys: list[str], capmonster_keys: list[str],
    action: str, proxy: str | None = None,
    n_races: int = 3,
    min_score: int = 85,
) -> str:
    """
    Run up to n_races sequential solver races; return the highest-scoring token.
    Stops early only if a token reaches min_score (auto-pass quality, < ~2380 chars).
    For image-challenge tokens (~2400-2574), always runs all races and picks shortest.
    Running multiple races statistically improves success rate for strict Enterprise
    endpoints like LOGIN_EVISA.
    """
    best_token = ""
    best_score = 0

    for i in range(n_races):
        print(f"[solver] race_best: race {i+1}/{n_races}  best_score_so_far={best_score}")
        try:
            token = race_all(
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                action, proxy=proxy,
            )
            score = score_token(token)
            print(f"[solver] race_best: race {i+1} done  score={score}  token_len={len(token)}")
            if score > best_score:
                best_score = score
                best_token = token
            if score >= min_score:
                print(f"[solver] race_best: threshold met ({score}>={min_score}) — stopping early")
                break
        except Exception as e:
            print(f"[solver] race_best: race {i+1} failed: {e}")

    if best_token:
        print(f"[solver] race_best: final  score={best_score}  token_len={len(best_token)}")
        return best_token
    raise RuntimeError(f"race_best: all {n_races} races failed")


def race_all(capsolver_keys: list[str], anticaptcha_keys: list[str],
             twocaptcha_keys: list[str], capmonster_keys: list[str],
             action: str, proxy: str | None = None,
             session_cookies: dict | None = None) -> str:
    """
    Race all configured solvers in parallel — first token wins.
    Mirrors the extension's _raceSolvers() with CAPTCHA_SOLVER_PARALLEL=true.
    Each service gets its first key; all start simultaneously.

    session_cookies: dict of cookies from the primp session (e.g. Vistos_sid).
    Passed to solvers that support a cookies field so the CAPTCHA token is
    generated inside the same server session as the subsequent POST.
    """
    proxy_info = _parse_proxy(proxy)

    tasks: list[tuple[str, callable, str]] = []
    if capsolver_keys:
        tasks.append(("capsolver",   _capsolver,   capsolver_keys[0]))
    if anticaptcha_keys:
        tasks.append(("anticaptcha", _anticaptcha, anticaptcha_keys[0]))
    if twocaptcha_keys:
        tasks.append(("2captcha",    _twocaptcha,  twocaptcha_keys[0]))
    if capmonster_keys:
        tasks.append(("capmonster",  _capmonster,  capmonster_keys[0]))

    if not tasks:
        raise RuntimeError("race_all: no solver keys configured")

    result: list[str | None] = [None]
    errors: list[str] = []
    done = threading.Event()
    lock = threading.Lock()
    total = len(tasks)

    def _run(label: str, fn: callable, key: str) -> None:
        try:
            print(f"[solver] race:{label} starting")
            token = fn(key, action, proxy_info)
            with lock:
                if result[0] is None:
                    result[0] = token
                    print(f"[solver] race:{label} WON  token_len={len(token)}")
                    done.set()
        except Exception as e:
            with lock:
                errors.append(f"{label}: {e}")
                print(f"[solver] race:{label} failed: {e}")
                if len(errors) == total:
                    done.set()

    stop = threading.Event()  # signals losing threads to exit early

    def _run_guarded(label: str, fn: callable, key: str) -> None:
        try:
            print(f"[solver] race:{label} starting")
            token = fn(key, action, proxy_info, session_cookies)
            with lock:
                if not stop.is_set() and result[0] is None:
                    result[0] = token
                    print(f"[solver] race:{label} WON  token_len={len(token)}")
                    stop.set()
                    done.set()
        except Exception as e:
            with lock:
                errors.append(f"{label}: {e}")
                print(f"[solver] race:{label} failed: {e}")
                if len(errors) == total:
                    done.set()

    # Monkey-patch _poll to check stop flag between polls
    import solver as _self_mod
    _orig_poll = _self_mod._poll

    def _stoppable_poll(url: str, api_key: str, task_id: str) -> str:
        for i in range(MAX_POLLS):
            if stop.is_set():
                raise RuntimeError("race: stopped (another solver won)")
            time.sleep(POLL_DELAY)
            if stop.is_set():
                raise RuntimeError("race: stopped (another solver won)")
            r = requests.post(url, json={"clientKey": api_key, "taskId": task_id}, timeout=30)
            data = r.json()
            if data.get("errorId", 0) != 0:
                raise RuntimeError(f"poll error: {data}")
            if data.get("status") == "ready":
                token = data["solution"]["gRecaptchaResponse"]
                print(f"[solver] ready in ~{(i+1)*POLL_DELAY}s  token_len={len(token)}")
                return token
            print(f"[solver] poll {i+1}/{MAX_POLLS}: {data.get('status', '?')}")
        raise RuntimeError(f"Solver timeout after {MAX_POLLS * POLL_DELAY}s")

    _self_mod._poll = _stoppable_poll

    threads = [
        threading.Thread(target=_run_guarded, args=(label, fn, key), daemon=True)
        for label, fn, key in tasks
    ]
    for t in threads:
        t.start()

    done.wait(timeout=180)
    _self_mod._poll = _orig_poll  # restore

    if result[0]:
        return result[0]
    raise RuntimeError(f"race_all — all {total} solvers failed: {' | '.join(errors)}")


def solve(service: str, keys: list[str], action: str = "LOGIN_EVISA",
          proxy: str | None = None,
          session_cookies: dict | None = None) -> str:
    """
    Try each key in order. Returns the first successful token.
    proxy: full proxy URL (http://user:pass@host:port); enables proxy-aware
           task type which gets much higher reCAPTCHA Enterprise scores.
    session_cookies: dict from primp client.get_cookies(); passed to the solver
           so the token is generated inside the same server session as the POST.
    Raises RuntimeError if all keys fail.
    """
    proxy_info = _parse_proxy(proxy)
    print(f"[solver] proxyless mode (solver-service IPs — higher Enterprise score)")

    last_err = None
    for i, key in enumerate(keys):
        try:
            label = f"key[{i}]={key[:8]}..."
            print(f"[solver] {service} {label} action={action}")
            return _SOLVERS[service](key, action, proxy_info, session_cookies)
        except Exception as e:
            print(f"[solver] {label} failed: {e}")
            last_err = e
    raise RuntimeError(f"All {len(keys)} keys failed for {service}. Last: {last_err}")


# ── CapSolver ─────────────────────────────────────────────────────────────────

def _capsolver(api_key: str, action: str, proxy_info: dict | None,
               session_cookies: dict | None = None) -> str:
    if action in _V3_ACTIONS:
        task = {
            "type":        "ReCaptchaV3TaskProxyLess",
            "websiteURL":  AUTH_URL,
            "websiteKey":  SITE_KEY,
            "pageAction":  action,
            "isEnterprise": True,
        }
    else:
        task = {
            "type":       "ReCaptchaV2EnterpriseTaskProxyLess",
            "websiteURL": AUTH_URL,
            "websiteKey": SITE_KEY,
        }

    r = requests.post("https://api.capsolver.com/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"createTask failed: {data}")
    print(f"[solver] task_id={task_id}")
    return _poll("https://api.capsolver.com/getTaskResult", api_key, task_id)


# ── Anti-Captcha ──────────────────────────────────────────────────────────────

_AC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _anticaptcha(api_key: str, action: str, proxy_info: dict | None,
                 session_cookies: dict | None = None) -> str:
    if action in _V3_ACTIONS:
        task = {
            "websiteURL":   AUTH_URL,
            "websiteKey":   SITE_KEY,
            "minScore":     0.3,
            "pageAction":   action,
            "isEnterprise": True,
        }
        task["type"] = "RecaptchaV3TaskProxyless"
    else:
        task = {
            "type":       "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": AUTH_URL,
            "websiteKey": SITE_KEY,
        }
    task["userAgent"] = _AC_USER_AGENT

    r = requests.post("https://api.anti-captcha.com/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    if data.get("errorId", 0) != 0:
        raise RuntimeError(f"createTask error: {data}")
    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"createTask no taskId: {data}")
    print(f"[solver] task_id={task_id}")
    return _poll("https://api.anti-captcha.com/getTaskResult", api_key, task_id)


# ── 2Captcha ──────────────────────────────────────────────────────────────────

def _twocaptcha(api_key: str, action: str, proxy_info: dict | None,
                session_cookies: dict | None = None) -> str:
    if action in _V3_ACTIONS:
        task = {
            "type":         "RecaptchaV3TaskProxyless",
            "websiteURL":   AUTH_URL,
            "websiteKey":   SITE_KEY,
            "minScore":     0.3,
            "pageAction":   action,
            "isEnterprise": True,
        }
    else:
        task = {
            "type":       "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": AUTH_URL,
            "websiteKey": SITE_KEY,
        }

    r = requests.post("https://api.2captcha.com/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    if data.get("errorId", 0) != 0:
        raise RuntimeError(f"createTask error: {data}")
    task_id = data["taskId"]
    print(f"[solver] task_id={task_id}")
    return _poll("https://api.2captcha.com/getTaskResult", api_key, task_id)


# ── CapMonster ────────────────────────────────────────────────────────────────

def _capmonster(api_key: str, action: str, proxy_info: dict | None,
                session_cookies: dict | None = None) -> str:
    if action in _V3_ACTIONS:
        task = {
            "type":         "RecaptchaV3TaskProxyless",
            "websiteURL":   AUTH_URL,
            "websiteKey":   SITE_KEY,
            "minScore":     0.3,
            "pageAction":   action,
            "isEnterprise": True,
        }
    else:
        task = {
            "type":       "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": AUTH_URL,
            "websiteKey": SITE_KEY,
        }

    r = requests.post("https://api.capmonster.cloud/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    if data.get("errorId", 0) != 0:
        raise RuntimeError(f"createTask error: {data}")
    task_id = data["taskId"]
    print(f"[solver] task_id={task_id}")
    return _poll("https://api.capmonster.cloud/getTaskResult", api_key, task_id)


# ── Shared poll ───────────────────────────────────────────────────────────────

def _poll(url: str, api_key: str, task_id: str) -> str:
    for i in range(MAX_POLLS):
        time.sleep(POLL_DELAY)
        r = requests.post(url, json={"clientKey": api_key, "taskId": task_id}, timeout=30)
        data = r.json()
        if data.get("errorId", 0) != 0:
            raise RuntimeError(f"poll error: {data}")
        if data.get("status") == "ready":
            token = data["solution"]["gRecaptchaResponse"]
            print(f"[solver] ready in ~{(i+1)*POLL_DELAY}s  token_len={len(token)}")
            return token
        print(f"[solver] poll {i+1}/{MAX_POLLS}: {data.get('status', '?')}")
    raise RuntimeError(f"Solver timeout after {MAX_POLLS * POLL_DELAY}s")


def _playwright_solver(api_key: str, action: str, proxy_info: dict | None) -> str:
    """Uses real Chromium to solve the CAPTCHA — highest reCAPTCHA Enterprise score.
    api_key is PLAYWRIGHT_KEYS value ("pw" = dummy). Falls back to CAPSOLVER_KEYS
    for ReCaptchaV2Classification when image grid challenges appear."""
    from captcha_pw import solve_captcha
    proxy_url = None
    if proxy_info:
        login    = proxy_info.get("proxyLogin", "")
        password = proxy_info.get("proxyPassword", "")
        host     = proxy_info.get("proxyAddress", "")
        port     = proxy_info.get("proxyPort", "")
        scheme   = proxy_info.get("proxyType", "http")
        if login:
            proxy_url = f"{scheme}://{login}:{password}@{host}:{port}"
        else:
            proxy_url = f"{scheme}://{host}:{port}"
    # Load capsolver keys for image grid classification
    if api_key and api_key != "pw":
        capsolver_keys = [api_key]
    else:
        import os
        from pathlib import Path
        raw = os.environ.get("CAPSOLVER_KEYS", "")
        if not raw:
            try:
                env_file = Path(__file__).parent / ".env"
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("CAPSOLVER_KEYS="):
                        raw = line.partition("=")[2].strip()
                        break
            except Exception:
                pass
        capsolver_keys = [k.strip() for k in raw.split(",") if k.strip()] or None
    return solve_captcha(proxy=proxy_url, action=action, capsolver_keys=capsolver_keys)


_SOLVERS = {
    "capsolver":   _capsolver,
    "anticaptcha": _anticaptcha,
    "2captcha":    _twocaptcha,
    "capmonster":  _capmonster,
    "playwright":  _playwright_solver,
}
