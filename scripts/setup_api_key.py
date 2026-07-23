#!/usr/bin/env python3
"""Create a Belgian Mobility developer-portal subscription and print its key.

    export BMC_EMAIL=you@example.com
    export BMC_PASSWORD='...'
    python scripts/setup_api_key.py

The portal is an Azure API Management developer portal. Getting a key is a
five-click flow that has to be done in a browser: sign in, pick the free
"Standard" product, name a subscription, subscribe, then reveal the key. This
script drives exactly that flow headlessly with Playwright and prints the
resulting primary key so it can be pasted into ``.env``.

WHY THIS EXISTS
The alternative is a paragraph of README prose telling the next person to click
through a UI, which rots the moment the portal is restyled. A script fails
loudly instead of silently being out of date, and it documents the flow
precisely.

WHAT IT DOES NOT DO
It does not create an account — sign-up requires confirming a link sent by
email. Register once at
https://api-management-opendata-production.developer.azure-api.net/signup
and this script handles everything after that.

It never writes your key to disk. It prints it; you decide where it goes.

REQUIREMENTS
    pip install -r requirements-dashboard.txt   # includes playwright
    python -m playwright install chromium

SAFETY
Credentials are read from the environment, never hard-coded and never logged.
The browser profile is written to ``scripts/.chrome-profile/`` (git-ignored) so
a re-run reuses the session instead of signing in again; delete that directory
to start clean. Pass ``--headed`` to watch it work, which is the fastest way to
diagnose a portal redesign.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover
    sys.exit(
        "playwright is not installed.\n"
        "  pip install -r requirements-dashboard.txt\n"
        "  python -m playwright install chromium"
    )

PORTAL = "https://api-management-opendata-production.developer.azure-api.net"
SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = SCRIPT_DIR / ".chrome-profile"
SHOTS_DIR = SCRIPT_DIR / ".shots"

#: 32 lowercase hex characters — the shape of an APIM subscription key.
KEY_PATTERN = re.compile(r"\b[0-9a-f]{32}\b")

DEFAULT_SUBSCRIPTION_NAME = "railpulse-sql-analysis"
#: "Standard" is self-service and free. "Gold" needs a bilateral contract
#: (email opendata@belgianmobilitycompany.be), so it cannot be automated.
PRODUCT = "Standard"


def _shot(page: Page, name: str, enabled: bool) -> None:
    if not enabled:
        return
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS_DIR / f"{name}.png"), full_page=True)
    print(f"    screenshot: scripts/.shots/{name}.png")


def sign_in(page: Page, email: str, password: str, *, shots: bool) -> None:
    page.goto(f"{PORTAL}/signin", wait_until="domcontentloaded", timeout=60_000)

    # The portal is a single-page app: the form is rendered after load, so
    # waiting for the selector matters more than waiting for the network.
    try:
        page.wait_for_selector("#email", timeout=30_000)
    except Exception:
        if "/signin" not in page.url:
            print("  already signed in (reusing the cached browser profile)")
            return
        raise

    # The Sign in button stays disabled until the fields raise real input
    # events, so fill() is not enough — the keystrokes have to be typed.
    page.locator("#email").click()
    page.locator("#email").press_sequentially(email, delay=20)
    page.locator("#password").click()
    page.locator("#password").press_sequentially(password, delay=20)
    page.wait_for_timeout(500)
    _shot(page, "01-signin-filled", shots)

    page.get_by_role("button", name=re.compile(r"^\s*Sign in\s*$")).first.click(
        timeout=20_000
    )
    page.wait_for_timeout(6_000)
    _shot(page, "02-signed-in", shots)

    if "/signin" in page.url:
        raise SystemExit(
            "sign-in failed — check BMC_EMAIL / BMC_PASSWORD, and that the "
            "account has confirmed its registration email."
        )
    print(f"  signed in as {email}")


def existing_subscription(page: Page, name: str) -> bool:
    page.goto(f"{PORTAL}/profile", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6_000)
    return name in page.inner_text("body")


def subscribe(page: Page, name: str, *, shots: bool) -> None:
    """Select the Standard product and create a named subscription."""
    page.goto(f"{PORTAL}/profile", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6_000)

    page.locator("input[placeholder='Select Product']").click()
    page.wait_for_timeout(1_500)
    page.get_by_role("option", name=PRODUCT).click()
    page.wait_for_timeout(2_000)

    field = page.locator("input[placeholder='Your new product subscription name']")
    field.click()
    field.press_sequentially(name, delay=20)
    page.wait_for_timeout(800)
    _shot(page, "03-subscribe-form", shots)

    page.get_by_role("button", name="Subscribe").first.click(timeout=20_000)
    page.wait_for_timeout(5_000)

    # Some portal builds raise a confirmation dialog; accept it when present.
    for label in ("Subscribe", "Confirm", "I agree", "OK"):
        button = page.get_by_role("button", name=label)
        if button.count():
            try:
                if button.last.is_visible():
                    button.last.click()
                    page.wait_for_timeout(4_000)
                    break
            except Exception:
                pass

    page.wait_for_timeout(3_000)
    _shot(page, "04-subscribed", shots)
    print(f"  subscribed to '{PRODUCT}' as '{name}'")


def read_keys(page: Page, *, shots: bool) -> list[str]:
    """Return the subscription keys, newest account state first.

    The profile table masks the keys as ``XXXX…`` and only reveals them through
    a row menu. Rather than depending on that widget's markup, this listens for
    the portal's own ``/developer/users/...`` response, which carries the keys
    in its JSON payload — markup changes, the API contract mostly does not.
    """
    found: list[str] = []

    def on_response(response) -> None:
        if "/developer/users/" not in response.url or response.status != 200:
            return
        try:
            body = response.text()
        except Exception:
            return
        for key in KEY_PATTERN.findall(body):
            if key not in found:
                found.append(key)

    page.on("response", on_response)
    page.goto(f"{PORTAL}/profile", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(9_000)
    _shot(page, "05-profile", shots)
    page.remove_listener("response", on_response)

    if not found:
        # Fallback: some builds render the key into an input value.
        for element in page.query_selector_all("input"):
            value = element.get_attribute("value") or ""
            if KEY_PATTERN.fullmatch(value):
                found.append(value)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a Belgian Mobility API subscription and print the key."
    )
    parser.add_argument("--name", default=DEFAULT_SUBSCRIPTION_NAME,
                        help=f"subscription name (default: {DEFAULT_SUBSCRIPTION_NAME})")
    parser.add_argument("--headed", action="store_true",
                        help="show the browser window instead of running headless")
    parser.add_argument("--screenshots", action="store_true",
                        help="save screenshots of each step to scripts/.shots/")
    args = parser.parse_args(argv)

    email = os.environ.get("BMC_EMAIL", "").strip()
    password = os.environ.get("BMC_PASSWORD", "").strip()
    if not email or not password:
        return _fail(
            "BMC_EMAIL and BMC_PASSWORD must be set.\n"
            "  export BMC_EMAIL=you@example.com\n"
            "  export BMC_PASSWORD='...'\n"
            f"Register first at {PORTAL}/signup if you have no account."
        )

    print(f"Belgian Mobility developer portal: {PORTAL}")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=not args.headed,
            viewport={"width": 1440, "height": 1400},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            sign_in(page, email, password, shots=args.screenshots)

            if existing_subscription(page, args.name):
                print(f"  subscription '{args.name}' already exists — reusing it")
            else:
                subscribe(page, args.name, shots=args.screenshots)

            keys = read_keys(page, shots=args.screenshots)
        finally:
            context.close()

    if not keys:
        return _fail(
            "signed in, but no subscription key could be read.\n"
            "Re-run with --headed --screenshots and check scripts/.shots/, or "
            f"copy the Primary key by hand from {PORTAL}/profile"
        )

    print("\nSubscription key(s) found:")
    for index, key in enumerate(keys):
        role = "primary" if index == 0 else f"secondary/{index}"
        print(f"  {role:<12} {key}")

    print("\nAdd the primary key to your .env (which is git-ignored):")
    print(f"  BMC_API_KEY={keys[0]}")
    print("\nThen verify it:")
    print("  python -m railpulse fetch")
    return 0


def _fail(message: str) -> int:
    print(f"\nERROR: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
