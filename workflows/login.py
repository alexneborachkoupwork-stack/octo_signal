"""
Login workflow for https://pedidodevistos.mne.gov.pt/VistosOnline/

Flow:
  1. Navigate to the home page (default language: Portuguese)
  2. Switch UI language to English
  3. Click the Login button to go to the login page
  4. Fill username and password fields
  5. (Optionally) submit
"""
import asyncio
from playwright.async_api import Page, TimeoutError as PWTimeout


# Login button on the home page (after language switch)
_LOGIN_LINK_SELECTORS = [
    "a[href*='Authentication']",
    "a[href*='authentication']",
    "a[href*='login']",
    "a[href*='Login']",
]
_LOGIN_LINK_TEXT = ["login", "log in", "sign in"]

# Credential fields on the auth page
_USERNAME_SELECTORS = [
    "input[name='username']",
    "input[name='email']",
    "input[name='login']",
    "input[type='email']",
    "input[id*='user']",
    "input[id*='email']",
    "input[placeholder*='user' i]",
    "input[placeholder*='email' i]",
    "input[placeholder*='utilizador' i]",
]
_PASSWORD_SELECTORS = [
    "input[name='password']",
    "input[type='password']",
    "input[id*='pass']",
    "input[placeholder*='password' i]",
    "input[placeholder*='palavra' i]",
]
_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
]
_SUBMIT_TEXT = ["login", "log in", "sign in", "entrar", "submeter"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _find_visible(page: Page, selectors: list[str], timeout: int = 2000):
    """Return the first visible locator from the selector list, or None."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=timeout):
                return el
        except PWTimeout:
            continue
    return None


async def _find_by_text(page: Page, tags: list[str], texts: list[str], timeout: int = 1500):
    """Return the first visible element whose text matches one of the given strings."""
    for tag in tags:
        for text in texts:
            try:
                el = page.locator(tag).filter(has_text=text).first
                if await el.is_visible(timeout=timeout):
                    return el
            except PWTimeout:
                continue
    return None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

async def _switch_to_english(page: Page) -> None:
    """Switch the page UI to English via the language <select>.
    The site has two selects (desktop + mobile), both name='lang', value 'ENG' = English.
    URL does not change — language is stored in the session cookie."""

    # Prefer the desktop select; fall back to any select[name='lang']
    lang_sel = page.locator("select#language-select-d").first
    if await lang_sel.count() == 0:
        lang_sel = page.locator("select[name='lang']").first

    if await lang_sel.count() == 0:
        print("  [login] WARNING: Language select not found, proceeding.")
        return

    current = await lang_sel.evaluate("el => el.value")
    if current == "ENG":
        print(f"  [login] Language already English.")
        return

    print(f"  [login] Current lang: {current!r} — selecting ENG…")
    await lang_sel.select_option(value="ENG")
    await page.wait_for_load_state("networkidle", timeout=10000)
    print(f"  [login] Language switch done.")


async def _click_login_link(page: Page) -> None:
    """Click the Login button on the home page."""
    el = await _find_visible(page, _LOGIN_LINK_SELECTORS)
    if not el:
        el = await _find_by_text(page, ["a", "button"], _LOGIN_LINK_TEXT)

    if not el:
        if "Authentication" in page.url or "login" in page.url.lower():
            print("  [login] Already on login page.")
            return
        raise RuntimeError(
            f"Could not find Login button on {page.url}. "
            f"Tried selectors: {_LOGIN_LINK_SELECTORS} and texts: {_LOGIN_LINK_TEXT}"
        )

    print(f"  [login] Clicking Login button…")
    await el.click()
    await page.wait_for_load_state("networkidle", timeout=15000)
    print(f"  [login] Now on: {page.url}")


async def _fill_credentials(page: Page, username: str, password: str) -> None:
    """Fill username and password on the authentication page."""
    user_el = await _find_visible(page, _USERNAME_SELECTORS, timeout=6000)
    if not user_el:
        raise RuntimeError(f"Username field not found on {page.url}")
    await user_el.fill(username)
    print("  [login] Username filled.")

    await asyncio.sleep(0.3)

    pass_el = await _find_visible(page, _PASSWORD_SELECTORS, timeout=4000)
    if not pass_el:
        raise RuntimeError(f"Password field not found on {page.url}")
    await pass_el.fill(password)
    print("  [login] Password filled.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_login(
    page: Page,
    target_url: str,
    username: str,
    password: str,
    submit: bool = False,
) -> None:
    """
    Full login workflow.

    Args:
        page:       Playwright page attached to the Octo profile.
        target_url: Home URL of the target site.
        username:   Credential to type.
        password:   Credential to type.
        submit:     If True, also click the submit button.
    """
    print(f"[login] Navigating to {target_url}")
    await page.goto(target_url, wait_until="networkidle", timeout=30000)
    print(f"[login] Page title: {await page.title()!r}")

    await _switch_to_english(page)
    print(f"[login] Language confirmed. Page: {page.url}")

    await _click_login_link(page)

    await _fill_credentials(page, username, password)
    print("[login] Credentials entered.")

    if submit:
        sub_el = await _find_visible(page, _SUBMIT_SELECTORS)
        if not sub_el:
            sub_el = await _find_by_text(page, ["button", "input"], _SUBMIT_TEXT)

        if sub_el:
            print("  [login] Submitting form…")
            await sub_el.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            print(f"[login] Post-submit URL: {page.url}")
        else:
            print("[login] WARNING: submit=True but no submit button found.")
    else:
        print("[login] Done (submit=False).")
