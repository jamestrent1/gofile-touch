#!/usr/bin/env python3
"""Telegram bot that scrapes gofile.io folder links and returns file download URLs.

Environment variables required:
  TELEGRAM_TOKEN – Telegram bot token from @BotFather

The bot authenticates to a gofile.io account before scraping any folder link,
ensuring that private or account-protected content is accessible.

Download URL construction: for each file the bot opens the 3-dot action menu,
clicks Properties, and reads the file ID and server list from the dialog to
build:  https://{server}.gofile.io/download/web/{file_id}/{filename}
"""

import io
import logging
import os
import re
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import WebDriverException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")

# Direct-login URL for the gofile.io account.
# Override via the GOFILE_LOGIN_URL environment variable to use a different account.
LOGIN_URL: str = os.environ.get(
    "GOFILE_LOGIN_URL",
    "https://gofile.io/login/xsL5YvfvbsZAcA4Zl5ducfnvQtqg2pq5",
)

GOFILE_FOLDER_RE = re.compile(r"https?://(?:www\.)?gofile\.io/d/(\w+)")

# gofile.io generates thumbnail files alongside every media file; their names
# always start with "thumb_".  We never want to surface these to the user.
def _is_thumbnail(name: str) -> bool:
    """Return True if the filename is a gofile.io auto-generated thumbnail."""
    return name.lower().startswith("thumb_")

# How long (seconds) to wait after the login redirect for cookies to be set.
_LOGIN_WAIT_SECONDS: int = 6

# How long (seconds) to wait for the React SPA to fully render a folder page.
# Multi-file folders require the SPA to make an async API call and re-render,
# so 8 seconds is safer than 5.
_PAGE_RENDER_WAIT_SECONDS: int = 8

# Maximum characters per Telegram message (Telegram limit is 4096;
# we use 4000 to leave a safe buffer for any formatting overhead).
_MAX_MESSAGE_LEN: int = 4000

# ---------------------------------------------------------------------------
# Browser / session state
# ---------------------------------------------------------------------------

_driver: "webdriver.Firefox | None" = None
_authenticated: bool = False



def _build_driver() -> webdriver.Firefox:
    """Create a headless Firefox WebDriver instance."""
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--width=1920")
    options.add_argument("--height=1080")
    return webdriver.Firefox(options=options)


def _get_driver() -> webdriver.Firefox:
    global _driver
    if _driver is None:
        logger.info("Starting Firefox browser…")
        _driver = _build_driver()
    return _driver


def _login() -> None:
    """Authenticate to gofile.io using the direct-login URL."""
    global _authenticated
    driver = _get_driver()
    logger.info("Authenticating with gofile.io via %s", LOGIN_URL)
    driver.get(LOGIN_URL)
    # Allow the login redirect and cookie-set to complete.
    time.sleep(_LOGIN_WAIT_SECONDS)
    logger.info("Authentication step completed. Current URL: %s", driver.current_url)
    _authenticated = True


def _ensure_auth() -> None:
    if not _authenticated:
        _login()


# ---------------------------------------------------------------------------
# File-extraction helpers
# ---------------------------------------------------------------------------


def _pick_server(servers_str: str) -> str:
    """Return the best download server from a comma-separated list.

    Prefers ``file-*`` servers (direct download nodes) over ``store-*``
    servers.  Falls back to the first entry when no ``file-*`` server is
    present.
    """
    if not servers_str:
        return ""
    parts = [s.strip() for s in servers_str.split(",") if s.strip()]
    for s in parts:
        if s.startswith("file-"):
            return s
    return parts[0] if parts else ""


def _scrape_via_properties(url: str) -> "list[dict]":
    """Navigate to a gofile.io folder and scrape files via the Properties dialog.

    For each file listed in the folder:
      1. Click the 3-dot (⋮) action menu button in the file's row.
      2. Click "Properties" in the resulting dropdown.
      3. Read the file ID and server list from the modal dialog.
      4. Construct the download URL:
             https://{server}.gofile.io/download/web/{file_id}/{filename}
      5. Dismiss the modal before moving to the next file.
    """
    driver = _get_driver()
    logger.info("Loading folder page via browser: %s", url)
    driver.get(url)
    time.sleep(_PAGE_RENDER_WAIT_SECONDS)

    files: list[dict] = []

    # Collect file names from anchor tags whose href is exactly
    # "javascript:void(0);" – gofile.io renders every file name as such an
    # anchor in the folder listing.
    file_names: list[str] = []
    for el in driver.find_elements(By.CSS_SELECTOR, 'a[href="javascript:void(0);"]'):
        name = (el.text or "").strip()
        if name and not _is_thumbnail(name):
            file_names.append(name)

    logger.info("Found %d file name(s) in folder.", len(file_names))

    for fname in file_names:
        try:
            # Re-locate the element on every iteration because closing the
            # Properties modal may cause the SPA to partially re-render.
            file_el = None
            for el in driver.find_elements(
                By.CSS_SELECTOR, 'a[href="javascript:void(0);"]'
            ):
                if (el.text or "").strip() == fname:
                    file_el = el
                    break

            if file_el is None:
                logger.warning("Could not re-find anchor for file %r", fname)
                continue

            # Scroll the file row into view so hidden buttons become interactive.
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", file_el
            )
            time.sleep(0.3)

            # Walk up the DOM from the file name anchor to the nearest container
            # that holds at least one <button>, then return the last such button
            # (which is the 3-dot / more-options button).
            three_dot = driver.execute_script(
                """
                var el = arguments[0];
                for (var i = 0; i < 12; i++) {
                    if (!el.parentElement) break;
                    el = el.parentElement;
                    var btns = el.querySelectorAll('button');
                    if (btns.length > 0) return btns[btns.length - 1];
                }
                return null;
                """,
                file_el,
            )

            if three_dot is None:
                logger.warning("3-dot button not found for %r", fname)
                continue

            three_dot.click()
            time.sleep(0.5)

            # Find the visible "Properties" menu item in the dropdown.
            props_item = None
            for el in driver.find_elements(By.XPATH, "//*[text()='Properties']"):
                if el.is_displayed():
                    props_item = el
                    break

            if props_item is None:
                logger.warning("'Properties' menu item not found for %r", fname)
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(0.3)
                continue

            props_item.click()
            time.sleep(1.0)

            # Extract file ID (UUID) and server list from the modal body text.
            body_text = driver.find_element(By.TAG_NAME, "body").text
            id_m = re.search(
                r"ID[\s:]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                r"-[0-9a-f]{4}-[0-9a-f]{12})",
                body_text,
                re.IGNORECASE,
            )
            srv_m = re.search(
                r"Servers[\s:]+([A-Za-z0-9,\-]+)",
                body_text,
                re.IGNORECASE,
            )

            if not id_m:
                logger.warning("Could not extract file ID for %r", fname)
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(0.3)
                continue

            file_id = id_m.group(1)
            server = _pick_server(srv_m.group(1) if srv_m else "")

            if not server:
                logger.warning(
                    "No usable server found for %r (servers=%r)",
                    fname,
                    srv_m.group(1) if srv_m else "",
                )
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(0.3)
                continue

            download_url = (
                f"https://{server}.gofile.io/download/web/{file_id}/{fname}"
            )
            files.append({"name": fname, "url": download_url})
            logger.info("Constructed URL for %r: %s", fname, download_url)

        except Exception as exc:
            logger.warning("Error scraping properties for %r: %s", fname, exc)
        finally:
            # Always try to dismiss any open modal or dropdown before continuing.
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(0.2)
            except Exception:
                pass

    return files


def scrape_folder(folder_url: str) -> "list[dict]":
    """Return a list of file dicts for all files in the given gofile.io folder URL."""
    global _driver, _authenticated

    _ensure_auth()

    # Primary attempt: Properties-dialog method.
    files: list[dict] = []
    try:
        files = _scrape_via_properties(folder_url)
    except WebDriverException as exc:
        logger.error("WebDriver error during properties scrape: %s", exc)
        try:
            if _driver:
                _driver.quit()
        except Exception:
            pass
        _driver = None
        _authenticated = False
        raise

    if files:
        return files

    # Nothing found; session may have expired.  Re-authenticate once and retry.
    logger.info("No files found; re-authenticating and retrying…")
    _authenticated = False
    _login()

    try:
        files = _scrape_via_properties(folder_url)
    except WebDriverException as exc:
        logger.error("WebDriver error during retry: %s", exc)
        try:
            if _driver:
                _driver.quit()
        except Exception:
            pass
        _driver = None
        _authenticated = False
        raise

    return files


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_size(size_bytes: "int | None") -> str:
    """Return a human-readable file size string."""
    if size_bytes is None:
        return ""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _build_reply(files: "list[dict]") -> str:
    """Format the file list into a plain-text Telegram message."""
    lines = [f"📁 Found {len(files)} file(s):\n"]
    for i, f in enumerate(files, 1):
        name = f.get("name") or "Unknown"
        url = f.get("url") or ""
        extras: list[str] = []
        if f.get("size"):
            extras.append(_fmt_size(f["size"]))
        if f.get("md5"):
            extras.append(f"MD5: {f['md5']}")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"{i}. 📄 {name}{extra_str}")
        lines.append(f"   ⬇️ {url}")
        lines.append("")
    return "\n".join(lines).strip()


async def _send_reply(
    update: Update, status_msg: "object", text: str
) -> None:
    """Edit the status message with the reply, splitting if it exceeds 4000 chars."""
    if len(text) <= _MAX_MESSAGE_LEN:
        await status_msg.edit_text(text)
        return

    # Split on newlines so we don't cut mid-line.
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > _MAX_MESSAGE_LEN and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))

    await status_msg.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Debug-report helper
# ---------------------------------------------------------------------------


def _scrape_debug_report(url: str) -> str:
    """Run a full instrumented scrape and return a detailed plain-text report.

    Captures: auth state, page source excerpt, all file names found on the
    page, and for each file the result of opening its Properties dialog
    (3-dot → Properties) including the raw modal text, extracted ID and
    servers, and the constructed download URL.
    """
    lines: list[str] = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    lines.append("=== gofile-touch DEBUG REPORT ===")
    lines.append(f"Generated : {ts}")
    lines.append(f"URL       : {url}")
    lines.append("")

    _ensure_auth()
    driver = _get_driver()

    lines.append(f"[AUTH] _authenticated = {_authenticated}")
    lines.append(f"[AUTH] Browser URL before load: {driver.current_url}")
    lines.append("")

    lines.append(f"[LOAD] Navigating to: {url}")
    driver.get(url)
    lines.append(f"[LOAD] Waiting {_PAGE_RENDER_WAIT_SECONDS}s for SPA render …")
    time.sleep(_PAGE_RENDER_WAIT_SECONDS)
    lines.append(f"[LOAD] Final URL : {driver.current_url}")
    lines.append(f"[LOAD] Page title: {driver.title!r}")
    lines.append("")

    # Page source excerpt (useful for verifying the page rendered correctly).
    page_source: str = driver.page_source or ""
    lines.append(f"[PAGE_SOURCE] Total length: {len(page_source)} chars")
    lines.append("[PAGE_SOURCE] ── first 2000 chars ──")
    lines.append(page_source[:2000])
    lines.append("")

    # File names discovered from anchor tags.
    lines.append("=== FILE NAMES FOUND ===")
    file_names_found: list[str] = []
    for el in driver.find_elements(By.CSS_SELECTOR, 'a[href="javascript:void(0);"]'):
        name = (el.text or "").strip()
        if name and not _is_thumbnail(name):
            file_names_found.append(name)
    lines.append(f"Total: {len(file_names_found)}")
    for i, n in enumerate(file_names_found, 1):
        lines.append(f"  [{i:03d}] {n!r}")
    lines.append("")

    # Per-file Properties scrape.
    lines.append("=== PROPERTIES SCRAPE (3-dot → Properties per file) ===")
    for fname in file_names_found:
        lines.append(f"\n--- File: {fname!r} ---")
        try:
            file_el = None
            for el in driver.find_elements(
                By.CSS_SELECTOR, 'a[href="javascript:void(0);"]'
            ):
                if (el.text or "").strip() == fname:
                    file_el = el
                    break

            if file_el is None:
                lines.append("  ERROR: could not re-find element")
                continue

            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", file_el
            )
            time.sleep(0.3)

            three_dot = driver.execute_script(
                """
                var el = arguments[0];
                for (var i = 0; i < 12; i++) {
                    if (!el.parentElement) break;
                    el = el.parentElement;
                    var btns = el.querySelectorAll('button');
                    if (btns.length > 0) return btns[btns.length - 1];
                }
                return null;
                """,
                file_el,
            )

            if three_dot is None:
                lines.append("  ERROR: 3-dot button not found in parent tree")
                continue

            lines.append("  3-dot button found – clicking …")
            three_dot.click()
            time.sleep(0.5)

            # Capture visible dropdown text for diagnostics.
            dropdown_text = (
                driver.execute_script(
                    "var m = document.querySelector('[role=menu],[role=listbox]');"
                    "return m ? m.innerText : '';"
                )
                or ""
            )
            lines.append(f"  Dropdown text: {dropdown_text[:300]!r}")

            props_item = None
            for el in driver.find_elements(By.XPATH, "//*[text()='Properties']"):
                if el.is_displayed():
                    props_item = el
                    break

            if props_item is None:
                lines.append("  ERROR: 'Properties' menu item not visible")
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(0.3)
                continue

            props_item.click()
            time.sleep(1.0)

            body_text = driver.find_element(By.TAG_NAME, "body").text

            # Show the relevant portion of the modal.
            modal_start = body_text.find("File Details")
            if modal_start == -1:
                modal_start = max(0, body_text.find("ID") - 20)
            lines.append(
                "  Modal text excerpt:\n"
                + body_text[modal_start: modal_start + 600]
            )

            id_m = re.search(
                r"ID[\s:]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                r"-[0-9a-f]{4}-[0-9a-f]{12})",
                body_text,
                re.IGNORECASE,
            )
            srv_m = re.search(
                r"Servers[\s:]+([A-Za-z0-9,\-]+)",
                body_text,
                re.IGNORECASE,
            )

            file_id = id_m.group(1) if id_m else "NOT FOUND"
            servers_raw = srv_m.group(1) if srv_m else "NOT FOUND"
            lines.append(f"  Extracted ID     : {file_id}")
            lines.append(f"  Extracted servers: {servers_raw}")

            if id_m:
                server = _pick_server(srv_m.group(1) if srv_m else "")
                constructed = (
                    f"https://{server}.gofile.io/download/web/{file_id}/{fname}"
                    if server
                    else "NO SERVER AVAILABLE"
                )
                lines.append(f"  Constructed URL  : {constructed}")

        except Exception as exc:
            lines.append(f"  EXCEPTION: {exc}")
        finally:
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(0.2)
            except Exception:
                pass

    lines.append("")
    lines.append("=== END OF REPORT ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram message handler
# ---------------------------------------------------------------------------


async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /debug <url> – run a full diagnostic scrape and reply with a .txt report."""
    if not update.message:
        return

    raw_text = update.message.text or ""
    # Extract the URL from the command arguments.
    parts = raw_text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Usage: /debug <gofile.io/d/xxxxx>\n"
            "Example: /debug https://gofile.io/d/abc123"
        )
        return

    url_text = parts[1].strip()
    match = GOFILE_FOLDER_RE.search(url_text)
    if not match:
        await update.message.reply_text(
            "❌ No valid gofile.io folder URL found.\n"
            "Expected format: https://gofile.io/d/xxxxxx"
        )
        return

    folder_url = match.group(0)
    status_msg = await update.message.reply_text(
        f"🔍 Running debug scan on {folder_url} …\n"
        "This may take ~15 seconds."
    )

    try:
        report = _scrape_debug_report(folder_url)
    except Exception as exc:
        logger.exception("Debug scrape failed for %s", folder_url)
        await status_msg.edit_text(f"❌ Debug scrape error: {str(exc)[:300]}")
        return

    report_bytes = report.encode("utf-8", errors="replace")
    await status_msg.edit_text("✅ Debug report ready – sending as file…")
    await update.message.reply_document(
        document=io.BytesIO(report_bytes),
        filename="gofile_debug.txt",
        caption=f"Debug report for {folder_url}",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming Telegram messages; process gofile.io folder links."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = GOFILE_FOLDER_RE.search(text)
    if not match:
        return  # Ignore messages without a gofile.io folder link.

    folder_url = match.group(0)
    status_msg = await update.message.reply_text(f"🔍 Scanning {folder_url} …")

    try:
        files = scrape_folder(folder_url)
    except Exception as exc:
        logger.exception("Unhandled error while scraping %s", folder_url)
        await status_msg.edit_text(f"❌ Unexpected error: {str(exc)[:300]}")
        return

    if not files:
        await status_msg.edit_text(
            "❌ No files found.\n"
            "The folder may be empty, private, or the link is invalid."
        )
        return

    reply = _build_reply(files)
    await _send_reply(update, status_msg, reply)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

    logger.info("Initializing gofile.io browser session…")
    _login()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("debug", handle_debug))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is polling for messages.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
