"""
Session restoration proof-of-concept.

Prerequisite: run batch_login.py with --save-state first:
  uv run python batch_login.py --mode test --count 1 --save-state

This script then:
  1. Finds the saved Playwright state for a test account
  2. Restores it via get_session_from_state()
  3. Navigates to /VistosOnline/Questionario — must NOT redirect to login
  4. Restores primp client via session_store.load() + is_alive()
  5. Prints pass/fail for both restore paths

Usage:
  uv run python test_session_restore.py --mode test
  uv run python test_session_restore.py --account herbar8410
"""

import sys
from pathlib import Path

BASE      = "https://pedidodevistos.mne.gov.pt"
QUEST_URL = BASE + "/VistosOnline/Questionario"

_ENV_FILE  = Path(__file__).parent / ".env"
_STATE_DIR = Path(__file__).parent / "sessions"


def _load_dotenv() -> dict:
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


def main() -> None:
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Test session restoration")
    parser.add_argument("--mode",    default="test")
    parser.add_argument("--account", default="", help="Specific username to restore")
    parser.add_argument("--headed",  action="store_true")
    args = parser.parse_args()

    import session as sess
    import session_store

    # ── Find a saved state file ───────────────────────────────────────────────
    if args.account:
        state_path = _STATE_DIR / f"{args.account}.playwright.json"
        username   = args.account
    else:
        # Pick any saved .playwright.json file
        candidates = sorted(_STATE_DIR.glob("*.playwright.json"))
        if not candidates:
            print(f"[test] No saved session states found in {_STATE_DIR}")
            print(f"[test] Run first:  uv run python batch_login.py --mode test --count 1 --save-state")
            sys.exit(1)
        state_path = candidates[0]
        username   = state_path.stem.replace(".playwright", "")

    if not state_path.exists():
        print(f"[test] State file not found: {state_path}")
        print(f"[test] Run first:  uv run python batch_login.py --mode {args.mode} --account {username} --save-state")
        sys.exit(1)

    profile_path = _STATE_DIR / (state_path.stem + "_profile.json")
    print(f"\n[test] account:     {username}")
    print(f"[test] state file:  {state_path}")
    print(f"[test] profile:     {'found' if profile_path.exists() else 'missing (random fp)'}")

    # ── Step 1: restore browser from Playwright state ─────────────────────────
    print(f"\n[test] STEP 1 — restoring browser from state file")
    sr = sess.get_session_from_state(state_path, headless=not args.headed)

    browser_ok = False
    try:
        print(f"[test] navigating to /Questionario ...")
        landed = sr.browser_nav(QUEST_URL, timeout=30)
        print(f"[test] landed at: {landed}")

        if "Authentication.jsp" in landed or "/ch/" in landed:
            print("[test] BROWSER RESTORE: FAIL — redirected to login / WAF challenge")
        else:
            title = sr.browser_eval("() => document.title", timeout=10)
            print(f"[test] page title: {title!r}")
            # Extra check: confirm the page has questionnaire content
            has_content = sr.browser_eval(
                "() => document.body.innerText.length > 100", timeout=10)
            print(f"[test] has content: {has_content}")
            browser_ok = True
            print("[test] BROWSER RESTORE: PASS")
    except Exception as e:
        print(f"[test] BROWSER RESTORE: FAIL — {e}")
    finally:
        try: sr.close()
        except Exception: pass

    # ── Step 2: restore primp client + is_alive check ─────────────────────────
    print(f"\n[test] STEP 2 — restoring primp client (session_store)")
    primp_ok = False
    loaded = session_store.load(username)
    if loaded is None:
        print(f"[test] PRIMP RESTORE: FAIL — no cookie snapshot at sessions/{username}.json")
    else:
        client, loaded_proxy = loaded
        alive = session_store.is_alive(client)
        print(f"[test] is_alive={alive}  proxy={loaded_proxy}")
        if alive:
            primp_ok = True
            print("[test] PRIMP RESTORE: PASS")
        else:
            print("[test] PRIMP RESTORE: FAIL — session dead (302 redirect to login)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print("[test] SESSION RESTORE SUMMARY")
    print(f"  Browser (Playwright state + fingerprint): {'PASS' if browser_ok else 'FAIL'}")
    print(f"  Primp   (cookie snapshot + is_alive):     {'PASS' if primp_ok  else 'FAIL'}")
    print("=" * 50)

    if not browser_ok or not primp_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
