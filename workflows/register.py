"""
Registration workflow for https://pedidodevistos.mne.gov.pt/VistosOnline/

Flow:
  1. Navigate to home page and switch UI language to English
  2. Click Login button -> Authentication.jsp
  3. Click Register link (span[name='registration']) -> AJAX loads #formReg into #mainContent
  4. Generate fake person data (faker) + temp email (mail.tm)
  5. Fill registration form
  6. Wait for reCAPTCHA solve (manual — script blocks until textarea gets a value)
  7. Submit form -> isFormValid() -> openPopUpRegistro() shows #registroMsg popup
  8. Click #registroSubmit -> POST to /VistosOnline/register
  9. Accept success alert (native dialog) and handle failure alerts with retry
  10. Detect ?token= in URL -> wait for insertToken.jsp in #mainContent
  11. Poll mail.tm inbox for verification token -> fill form -> submit
  12. Wait for redirect back to Authentication.jsp
  13. Export account data as CSV to exports/
"""
import asyncio
import csv
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Dialog, Page, TimeoutError as PWTimeout

from data.person import generate_person
from data.temp_email import create_account, extract_token, wait_for_message
from workflows.login import _click_login_link, _switch_to_english

# ---------------------------------------------------------------------------
# Selectors (confirmed from console dumps and HTML inspection)
# ---------------------------------------------------------------------------

_REGISTER_LINK_SELECTORS = [
    "span[name='registration']",
    "input[name='registration']",
    "button[name='registration']",
    "[onclick*='registration']",
]
_REGISTER_LINK_TEXTS = ["register", "sign up", "criar conta", "registar", "novo utilizador"]

_FORM_ID = "#formReg"

_FIELD_MAP = {
    "name":      "#name",
    "surname":   "#surname",
    "username":  "#username",
    "bday":      "#bday",
    "traveldoc": "#traveldoc",
    "email":     "#email",
}
_PASSWORD_FIELD    = "#password"
_VALPASSWORD_FIELD = "#valPassword"
_GENDER_SEL        = "#gender"
_NATIONALITY_SEL   = "#nationality"
_RECAPTCHA_FIELD   = "#g-recaptcha-response-1"
_PRIVACY_POPUP     = "#registroMsg"
_PRIVACY_SUBMIT    = "#registroSubmit"
_FORM_SUBMIT_SEL   = "#formReg button[type='submit'], #formReg input[type='submit']"

_TOKEN_INPUT_SELECTORS = [
    "input[name='token']",
    "input[id*='oken']",
    "#mainContent input[type='text']",
    "#mainContent input[type='number']",
]
_TOKEN_SUBMIT_SELECTORS = [
    "#mainContent button[type='submit']",
    "#mainContent input[type='submit']",
    "#mainContent .btn-primary",
]

EXPORT_DIR = Path("exports")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _wait_visible(page: Page, selector: str, timeout_ms: int = 10000):
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        return page.locator(selector).first
    except PWTimeout:
        return None


async def _click_register_link(page: Page) -> None:
    for sel in _REGISTER_LINK_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                print(f"  [register] Clicking register element: {sel}")
                await el.click()
                await asyncio.sleep(1.0)
                return
        except Exception:
            continue

    # Text-based fallback
    for text in _REGISTER_LINK_TEXTS:
        try:
            el = page.get_by_text(text, exact=False).first
            if await el.count() > 0:
                print(f"  [register] Clicking register by text: {text!r}")
                await el.click()
                await asyncio.sleep(1.0)
                return
        except Exception:
            continue

    raise RuntimeError(f"Register link not found. Tried: {_REGISTER_LINK_SELECTORS}")


async def _wait_for_form(page: Page) -> None:
    print("  [register] Waiting for registration form (#formReg)...")
    el = await _wait_visible(page, _FORM_ID, timeout_ms=12000)
    if not el:
        raise RuntimeError("Registration form (#formReg) did not load within 12 s.")
    print("  [register] Registration form loaded.")


async def _fill_form(page: Page, person: dict, email: str) -> None:
    for key, sel in _FIELD_MAP.items():
        value = email if key == "email" else person.get(key, "")
        el = await _wait_visible(page, sel, timeout_ms=4000)
        if not el:
            print(f"  [register] WARNING: {sel} not found, skipping.")
            continue
        await el.fill(value)
        await asyncio.sleep(0.05)

    pwd = person["password"]
    for sel, val in [(_PASSWORD_FIELD, pwd), (_VALPASSWORD_FIELD, pwd)]:
        el = await _wait_visible(page, sel, timeout_ms=4000)
        if el:
            await el.fill(val)
            await asyncio.sleep(0.05)

    try:
        await page.select_option(_GENDER_SEL, value=person["gender"])
    except Exception as e:
        print(f"  [register] WARNING: gender select: {e}")

    try:
        await page.select_option(_NATIONALITY_SEL, value=person["nationality"])
    except Exception as e:
        print(f"  [register] WARNING: nationality select: {e}")

    print("  [register] Form filled.")


async def _wait_for_recaptcha(page: Page, timeout_ms: int = 300_000) -> None:
    """Block until the reCAPTCHA textarea gets a token (manual solve)."""
    print("  [register] >> Waiting for reCAPTCHA solve (manual)...")
    await page.wait_for_function(
        "() => (document.querySelector('#g-recaptcha-response-1')?.value?.length ?? 0) > 0",
        timeout=timeout_ms,
    )
    print("  [register] reCAPTCHA solved.")


async def _submit_form(page: Page) -> None:
    el = page.locator(_FORM_SUBMIT_SEL).first
    if await el.count() == 0:
        el = page.get_by_role("button", name=re.compile(r"register|submit", re.I)).first
    await el.click()
    print("  [register] Form submit clicked.")


async def _wait_for_privacy_popup(page: Page) -> None:
    print("  [register] Waiting for privacy popup (#registroMsg)...")
    el = await _wait_visible(page, _PRIVACY_POPUP, timeout_ms=15000)
    if not el:
        raise RuntimeError("#registroMsg did not appear after form submit.")
    print("  [register] Privacy popup visible.")


async def _submit_privacy_popup(page: Page) -> None:
    el = page.locator(_PRIVACY_SUBMIT).first
    await el.click()
    print("  [register] Privacy popup submitted.")


async def _wait_for_token_url(page: Page, timeout_ms: int = 30_000) -> str:
    print("  [register] Waiting for ?token= in URL...")
    start = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start) * 1000 < timeout_ms:
        m = re.search(r"[?&]token=([^&]+)", page.url)
        if m:
            tok = m.group(1)
            print(f"  [register] Token URL: {tok}")
            return tok
        await asyncio.sleep(0.5)
    raise RuntimeError("?token= URL did not appear within timeout.")


async def _wait_for_token_form(page: Page) -> None:
    print("  [register] Waiting for token input form (insertToken.jsp)...")
    for sel in _TOKEN_INPUT_SELECTORS:
        el = await _wait_visible(page, sel, timeout_ms=10000)
        if el:
            print(f"  [register] Token form ready ({sel}).")
            return
    raise RuntimeError("Token input form did not load.")


async def _fill_and_submit_token(page: Page, token: str) -> None:
    for sel in _TOKEN_INPUT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(token)
                print(f"  [register] Token filled: {sel}")
                break
        except Exception:
            continue

    for sel in _TOKEN_SUBMIT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                print("  [register] Token submitted.")
                return
        except Exception:
            continue

    raise RuntimeError("Token submit button not found.")


def _export_csv(account: dict) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"account_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(account.keys()))
        w.writeheader()
        w.writerow(account)
    print(f"  [register] Account exported -> {path}")
    return path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_register(
    page: Page,
    target_url: str,
    export: bool = True,
) -> dict:
    """
    Full registration workflow. Returns the account dict.
    Blocks for up to 5 minutes at the reCAPTCHA step for manual solve.
    """
    print(f"[register] Navigating to {target_url}")
    await page.goto(target_url, wait_until="networkidle", timeout=30000)
    print(f"[register] Title: {await page.title()!r}")

    await _switch_to_english(page)
    await _click_login_link(page)

    await _click_register_link(page)
    await _wait_for_form(page)

    person = generate_person()
    print(f"  [register] Person: {person['username']} / {person['password']}")

    email_acct = await create_account()
    email = email_acct["email"]
    print(f"  [register] Temp email: {email}")

    # Accept all dialogs automatically
    dialogs: list[str] = []

    async def _on_dialog(dlg: Dialog) -> None:
        msg = dlg.message
        dialogs.append(msg)
        print(f"  [register] Dialog: {msg!r} -> accept")
        await dlg.accept()

    page.on("dialog", _on_dialog)

    person["email"] = email
    await _fill_form(page, person, email)

    # reCAPTCHA — manual solve required
    await _wait_for_recaptcha(page)

    # Submit may need retry if reCAPTCHA token expired or server rejects
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        dialogs.clear()
        await _submit_form(page)

        # Wait a moment for either a dialog (fail) or the privacy popup (success)
        try:
            await page.wait_for_selector(_PRIVACY_POPUP, state="visible", timeout=10000)
            break  # privacy popup appeared -> reCAPTCHA passed
        except PWTimeout:
            if any("attempt" in d.lower() or "captcha" in d.lower() for d in dialogs):
                if attempt < max_attempts:
                    print(f"  [register] reCAPTCHA failed (attempt {attempt}). Solve again...")
                    await _wait_for_recaptcha(page)
                else:
                    raise RuntimeError("reCAPTCHA failed too many times.")
            else:
                raise RuntimeError(f"Unexpected state after form submit (dialogs: {dialogs})")

    await _submit_privacy_popup(page)

    # Wait for success dialog + page navigation
    await asyncio.sleep(2)
    await _wait_for_token_url(page)
    await _wait_for_token_form(page)

    print("  [register] Polling mail.tm for verification token (up to 2 min)...")
    msg = await wait_for_message(email_acct["jwt"], timeout_s=120)
    if not msg:
        raise RuntimeError("Verification email not received within 2 minutes.")

    email_token = extract_token(msg)
    if not email_token:
        raw = (msg.get("text") or msg.get("html") or "")[:300]
        raise RuntimeError(f"Could not extract token from email body: {raw!r}")

    print(f"  [register] Verification token: {email_token}")
    await _fill_and_submit_token(page, email_token)

    print("  [register] Waiting for redirect to login page...")
    try:
        await page.wait_for_url("**/Authentication.jsp**", timeout=15000)
    except PWTimeout:
        print("  [register] WARNING: Redirect not detected; continuing anyway.")

    print(f"[register] Registration complete. URL: {page.url}")

    account = {
        "username":     person["username"],
        "password":     person["password"],
        "name":         person["name"],
        "surname":      person["surname"],
        "email":        email,
        "birth_date":   person["birth_date"],
        "gender":       person["gender"],
        "nationality":  person["nationality"],
        "traveldoc":    person["traveldoc"],
        "registered_at": datetime.now().isoformat(),
    }

    if export:
        _export_csv(account)

    return account
