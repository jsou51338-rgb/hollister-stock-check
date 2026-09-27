"""
Hollister restock checker.

Watches one product page for:
  1. "white" colorway, size XXS coming back in stock
  2. "black" colorway appearing on the page at all (it's currently missing
     from the color list entirely), and being in stock in size XXS

Sends a text message (via your carrier's email-to-SMS gateway) the moment
either of those flips from "not available" to "available". Designed to be
run on a schedule (e.g. every 15 minutes) by the included GitHub Actions
workflow, but you can also run it locally with `python check_stock.py`.

If the site's HTML structure changes, the selectors below may need
adjusting -- see SETUP_GUIDE.md for how to inspect the page and fix them.
"""

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    # Only used when running locally (e.g. on your Mac). In GitHub Actions,
    # the secrets are already set as real environment variables, and this
    # file won't exist, so it's safely skipped.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PRODUCT_URL = "https://www.hollisterco.com/shop/us/p/lace-trim-layering-cami-63270319"
STATE_FILE = Path(__file__).parent / "state.json"

# Which size we care about, exactly as it's labeled on the page
TARGET_SIZE = "XXS"

# Carrier email-to-SMS gateways (Verizon shown; add others if you switch carriers)
CARRIER_GATEWAYS = {
    "verizon": "vtext.com",
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
}


class BlockedError(Exception):
    """Raised when the page looks like a CAPTCHA/bot-block page instead of the real product page."""


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "white_available": False,
        "white_xxs_in_stock": False,
        "black_available": False,
        "black_xxs_in_stock": False,
        "blocked_alert_sent": False,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_text(message: str) -> None:
    sender_email = os.environ["EMAIL_ADDRESS"]
    sender_password = os.environ["EMAIL_APP_PASSWORD"]
    phone_number = os.environ["PHONE_NUMBER"]  # 10 digits, no dashes/spaces
    carrier = os.environ.get("CARRIER", "verizon").lower()

    gateway = CARRIER_GATEWAYS.get(carrier)
    if not gateway:
        raise ValueError(f"Unknown carrier '{carrier}'. Add it to CARRIER_GATEWAYS.")

    recipient = f"{phone_number}@{gateway}"

    msg = MIMEText(message)
    msg["From"] = sender_email
    msg["To"] = recipient
    msg["Subject"] = ""  # SMS gateways generally ignore/strip the subject

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, [recipient], msg.as_string())

    print(f"Text sent to {recipient}: {message}")


def is_size_available(page, size_label: str) -> bool:
    """
    Looks for the size selector button matching `size_label` and returns
    True if it looks selectable (in stock), False if it looks disabled/sold out.
    Tries a few different signals since sites often mark "sold out" in more
    than one way (disabled attribute, aria-disabled, or a class name).
    """
    # Try to find a button/element whose visible text is exactly the size label
    candidates = page.locator(f"button:has-text('{size_label}')")
    count = candidates.count()
    for i in range(count):
        el = candidates.nth(i)
        text = el.inner_text().strip()
        if text != size_label:
            continue  # skip partial matches, e.g. "XXS" matching inside "XXS/XS"

        disabled = el.get_attribute("disabled") is not None
        aria_disabled = (el.get_attribute("aria-disabled") or "").lower() == "true"
        class_attr = (el.get_attribute("class") or "").lower()
        looks_sold_out = "sold" in class_attr or "disabled" in class_attr or "unavailable" in class_attr

        if disabled or aria_disabled or looks_sold_out:
            return False
        return True  # found the size button and it's not flagged as disabled

    # Couldn't find the size button at all -- treat as not available, but this
    # is also the case most likely to mean the page structure changed.
    return False


def get_color_swatch_names(page) -> list[str]:
    """Returns the lowercase names/alt text of every visible color swatch."""
    names = []
    swatches = page.locator("img[alt]")
    count = swatches.count()
    for i in range(count):
        alt = (swatches.nth(i).get_attribute("alt") or "").strip().lower()
        if alt and "_sw" not in alt and len(alt) < 40:
            # crude filter to skip unrelated images; swatch alt text on this
            # site looks like "white", "navy", "green stripe", etc.
            names.append(alt)
    return names


def select_color(page, color_name: str) -> bool:
    """Clicks the swatch whose alt text matches color_name. Returns success."""
    swatch = page.locator(f"img[alt='{color_name}' i]").first
    try:
        swatch.click(timeout=5000)
        page.wait_for_timeout(1500)  # let the size buttons re-render
        return True
    except Exception:
        return False


def check() -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.goto(PRODUCT_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2000)

        title = (page.title() or "").lower()
        body_text = page.locator("body").inner_text().lower()

        block_signals = [
            "access denied", "attention required", "captcha",
            "verify you are human", "unusual traffic", "robot check",
            "are you a robot", "pardon our interruption",
        ]
        if any(signal in title or signal in body_text for signal in block_signals):
            browser.close()
            raise BlockedError(f"Page looks like a bot-block/CAPTCHA page (title: '{page.title()}')")

        swatch_names = get_color_swatch_names(page)
        black_available = any("black" in name for name in swatch_names)

        if len(swatch_names) == 0:
            # No color swatches found at all is suspicious for this page --
            # either we're blocked in a way the text check above missed, or
            # the site's structure changed. Either way, don't trust this read.
            browser.close()
            raise BlockedError("No color swatches found on the page -- likely blocked or page structure changed")

        # Check both colors the same way: only try to select+check a color if
        # it's actually showing up as a swatch on the page right now.
        white_available = any("white" in name for name in swatch_names)

        white_xxs = False
        if white_available:
            if select_color(page, "white"):
                white_xxs = is_size_available(page, TARGET_SIZE)

        black_xxs = False
        if black_available:
            if select_color(page, "black"):
                black_xxs = is_size_available(page, TARGET_SIZE)

        browser.close()

        return {
            "white_available": white_available,
            "white_xxs_in_stock": white_xxs,
            "black_available": black_available,
            "black_xxs_in_stock": black_xxs,
        }


def main():
    previous = load_state()

    try:
        current = check()
    except BlockedError as e:
        print(f"Possible block detected: {e}", file=sys.stderr)
        if not previous.get("blocked_alert_sent"):
            try:
                send_text(
                    "\u26a0\ufe0f Stock checker: Hollister may be blocking the "
                    "automated check. It'll keep quietly retrying, but you may "
                    "want to check manually for now."
                )
            except Exception as send_err:
                print(f"Also failed to send the block alert text: {send_err}", file=sys.stderr)
            previous["blocked_alert_sent"] = True
            save_state(previous)
        sys.exit(1)
    except Exception as e:
        print(f"Check failed: {e}", file=sys.stderr)
        # Don't spam texts on every transient failure; just exit non-zero so
        # the GitHub Actions run shows as failed and you can glance at it.
        sys.exit(1)

    # A successful check means we're not blocked (anymore) -- reset the flag
    # so a future block gets a fresh alert instead of staying silent forever.
    current["blocked_alert_sent"] = False

    messages = []

    if current["white_available"] and not previous.get("white_available"):
        messages.append(f"White just reappeared as a color option! {PRODUCT_URL}")

    if current["white_xxs_in_stock"] and not previous.get("white_xxs_in_stock"):
        messages.append(f"White XXS is back in stock! {PRODUCT_URL}")

    if current["black_available"] and not previous.get("black_available"):
        messages.append(f"Black just appeared as a color option! {PRODUCT_URL}")

    if current["black_xxs_in_stock"] and not previous.get("black_xxs_in_stock"):
        messages.append(f"Black XXS is in stock! {PRODUCT_URL}")

    for m in messages:
        send_text(m)

    save_state(current)

    if not messages:
        print(f"No change. Current state: {current}")


if __name__ == "__main__":
    main()
