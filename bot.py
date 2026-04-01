#!/usr/bin/env python3
"""Telegram bot that scrapes gofile.io folder links and returns file download URLs.

Environment variables required:
  TELEGRAM_TOKEN – Telegram bot token from @BotFather

The bot authenticates to a gofile.io account before scraping any folder link,
ensuring that private or account-protected content is accessible.
"""

import logging
import os
import re
import time
from urllib.parse import unquote, urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import WebDriverException
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

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
GOFILE_DOWNLOAD_RE = re.compile(
    r"https://[A-Za-z0-9\-]+\.gofile\.io/download/[^\s\"'<>]+"
)

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

# Maximum characters to capture from an element's textContent when using it
# as a filename hint during the JavaScript broad-attribute scan.
_MAX_ELEMENT_TEXT_LEN: int = 200

# Maximum React fiber tree depth to traverse when looking for folder contents.
# Limits worst-case execution time in very deep component trees.
_MAX_FIBER_DEPTH: int = 150

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


def _scrape_via_dom(url: str) -> "list[dict]":
    """Navigate to the folder page in the authenticated browser and scrape all file links.

    Uses four passes in order of preference:

    1. Standard ``<a href="...">`` elements – works for single-file folders
       where gofile.io renders a prominent download button.
    2. JavaScript broad attribute scan – checks every element's ``href``,
       ``data-link``, ``data-url``, ``data-href``, and ``data-download``
       attributes for any gofile.io URL.  Catches download row wrappers and
       buttons that are not plain ``<a>`` tags.
    3. React fiber state extraction – the SPA stores the full API response
       (including all file download URLs) in React component memory.  Walking
       the fiber tree is the most reliable way to get multi-file folder data
       when the download links are not directly injected into the DOM as plain
       anchor tags.
    4. Raw page-source regex – last-resort scan of the rendered HTML.
    """
    driver = _get_driver()
    logger.info("Loading folder page via browser: %s", url)
    driver.get(url)
    # Allow the React SPA to render and complete its async content fetch.
    time.sleep(_PAGE_RENDER_WAIT_SECONDS)

    files: list[dict] = []
    seen: set = set()

    def _add(href: str, name: str = "") -> None:
        """Validate, de-duplicate, and append a download URL (unless a thumbnail)."""
        href = href.strip()
        if not href or href in seen:
            return
        # Accept any https URL that belongs to a gofile.io subdomain.
        if not href.startswith("https://") or "gofile.io" not in href:
            return
        # Strip query-string / fragment before deriving the filename from the path.
        path = urlparse(href).path
        resolved_name = name or unquote(path.rstrip("/").split("/")[-1])
        if _is_thumbnail(resolved_name):
            return
        seen.add(href)
        files.append({"name": resolved_name, "url": href})

    # Pass 1 – standard <a> elements with a href attribute.
    for element in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = element.get_attribute("href") or ""
        except Exception:
            continue
        _add(href)

    # Pass 2 – JavaScript broad attribute scan covering href, data-link, data-url,
    # data-href, and data-download on *any* element type (download buttons, row
    # wrappers, etc. that are not plain <a> tags).
    try:
        js_items: list = driver.execute_script(
            f"""
            const results = [];
            const attrs = ['href', 'data-link', 'data-url', 'data-href', 'data-download'];
            document.querySelectorAll('*').forEach(function(el) {{
                attrs.forEach(function(attr) {{
                    const u = el.getAttribute(attr);
                    if (u && u.indexOf('gofile.io') !== -1) {{
                        results.push([u, el.textContent.trim().slice(0, {_MAX_ELEMENT_TEXT_LEN})]);
                    }}
                }});
            }});
            return results;
            """
        ) or []
        for item in js_items:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                _add(str(item[0]), str(item[1]) if item[1] else "")
    except Exception as exc:
        logger.debug("JavaScript broad-attribute scan failed: %s", exc)

    # Pass 3 – React fiber state extraction.
    #
    # gofile.io is a React SPA.  After the folder page loads, the SPA fetches
    # the folder contents from api.gofile.io and stores the response in React
    # component state.  We walk the React fiber tree looking for the standard
    # gofile.io contents object ``{type: "file", name: ..., link: ...}``.
    # This is the most reliable path for multi-file folders where the individual
    # download links are not injected as plain anchor tags.
    if not files:
        try:
            react_files: list = driver.execute_script(
                f"""
                try {{
                    function findContents(fiber, depth) {{
                        if (!fiber || depth > {_MAX_FIBER_DEPTH}) return null;
                        for (var s = fiber.memoizedState; s; s = s.next) {{
                            var v = s.memoizedState;
                            if (v && typeof v === 'object' && !Array.isArray(v)) {{
                                var contents = null;
                                if (v.contents && typeof v.contents === 'object')
                                    contents = v.contents;
                                else if (v.data && v.data.contents)
                                    contents = v.data.contents;
                                else if (v.status === 'ok' && v.data && v.data.contents)
                                    contents = v.data.contents;
                                if (contents) {{
                                    var items = Object.values(contents).filter(function(i) {{
                                        return i && i.type === 'file' && (i.link || i.directLink);
                                    }});
                                    if (items.length > 0) return items.map(function(i) {{
                                        return {{
                                            name: i.name || '',
                                            url: i.link || i.directLink || '',
                                            size: i.size || null,
                                            md5: i.md5 || null
                                        }};
                                    }});
                                }}
                            }}
                        }}
                        return findContents(fiber.child, depth + 1)
                            || findContents(fiber.sibling, depth + 1);
                    }}
                    var root = document.getElementById('root') || document.body;
                    var key = Object.keys(root).find(function(k) {{
                        return k.indexOf('__reactFiber') === 0
                            || k.indexOf('__reactInternalInstance') === 0;
                    }});
                    if (key) return findContents(root[key], 0);
                }} catch(e) {{}}
                return null;
                """
            )
            if react_files and isinstance(react_files, list):
                logger.info(
                    "React fiber extracted %d file(s) from folder state.", len(react_files)
                )
                for item in react_files:
                    if not isinstance(item, dict):
                        continue
                    item_url = item.get("url", "")
                    item_name = item.get("name", "")
                    if item_url and not _is_thumbnail(item_name):
                        if item_url not in seen:
                            seen.add(item_url)
                            files.append(
                                {
                                    "name": item_name,
                                    "url": item_url,
                                    "size": item.get("size"),
                                    "md5": item.get("md5"),
                                }
                            )
        except Exception as exc:
            logger.debug("React fiber extraction failed: %s", exc)

    if files:
        return files

    # Pass 4 – regex-scan the raw page source as a last-resort fallback.
    try:
        for raw_url in GOFILE_DOWNLOAD_RE.findall(driver.page_source):
            _add(raw_url.rstrip("\"'\\"))
    except Exception as exc:
        logger.warning("Page-source scan failed: %s", exc)

    return files


def scrape_folder(folder_url: str) -> "list[dict]":
    """Return a list of file dicts for all files in the given gofile.io folder URL."""
    global _driver, _authenticated

    _ensure_auth()

    # -----------------------------------------------------------------------
    # Step 1 – Load the folder page in the authenticated browser and scrape.
    # -----------------------------------------------------------------------
    files: list[dict] = []
    try:
        files = _scrape_via_dom(folder_url)
    except WebDriverException as exc:
        logger.error("WebDriver error during DOM scrape: %s", exc)
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

    # -----------------------------------------------------------------------
    # Step 2 – Nothing found; session may have expired.
    #           Re-authenticate once and retry DOM scraping.
    # -----------------------------------------------------------------------
    logger.info("No files found; re-authenticating and retrying…")
    _authenticated = False
    _login()
    try:
        files = _scrape_via_dom(folder_url)
    except WebDriverException as exc:
        logger.error("WebDriver error during retry DOM scrape: %s", exc)
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
# Telegram message handler
# ---------------------------------------------------------------------------


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is polling for messages.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
