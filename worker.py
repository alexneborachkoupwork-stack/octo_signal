"""
Main automation worker entry point.

Usage:
    python worker.py

Prerequisites:
    1. Octo Browser must be running on this machine.
    2. Fill in .env (copy .env.example) with your profile UUID and credentials.
    3. pip install -r requirements.txt && playwright install chromium
"""
import asyncio
import sys

from playwright.async_api import async_playwright

from config import PROFILE_UUID, TARGET_URL, TEST_USERNAME, TEST_PASSWORD
from octo_api import OctoClient
from workflows.login import run_login


async def main() -> None:
    if PROFILE_UUID == "REPLACE_WITH_YOUR_PROFILE_UUID":
        print("ERROR: Set OCTO_PROFILE_UUID in your .env file before running.")
        sys.exit(1)

    async with OctoClient() as octo:
        # Check if the profile is already running; start it if not
        print(f"[worker] Checking profile {PROFILE_UUID}...")
        session = await octo.get_active_session(PROFILE_UUID)

        if session:
            print(f"[worker] Profile already running. ws_endpoint={session.ws_endpoint}")
        else:
            print("[worker] Starting profile via Octo local API...")
            session = await octo.start_profile(PROFILE_UUID, headless=False)
            print(f"[worker] Profile started. ws_endpoint={session.ws_endpoint}")

        print(f"[worker] Connecting Playwright over CDP → {session.ws_endpoint}")
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(session.ws_endpoint)

            # Use the existing browser context (the Octo profile's context)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            print(f"[worker] Connected. Current URL: {page.url}")

            await run_login(
                page=page,
                target_url=TARGET_URL,
                username=TEST_USERNAME,
                password=TEST_PASSWORD,
                submit=False,   # set to True to actually submit the form
            )

            print("[worker] Workflow complete. Browser session left open.")
            # Do NOT close the browser — the Octo profile stays alive for inspection
            await browser.close()   # detaches playwright without killing Octo


if __name__ == "__main__":
    asyncio.run(main())
