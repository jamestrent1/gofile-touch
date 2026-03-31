# gofile-touch

A Telegram bot that authenticates to a **gofile.io** account and returns all
file names and direct download links inside any gofile.io folder you share.

---

## Features

* Logs in to your gofile.io account automatically before browsing any folder.
* Supports two extraction strategies (fastest/most reliable tried first):
  1. **gofile.io REST API** – uses the account token extracted from the
     authenticated browser session for clean structured data.
  2. **Selenium DOM scraping** – opens the folder page in the real Firefox
     browser and locates download links in the rendered HTML.
* Automatically re-authenticates if the session expires.
* Returns file name, download URL, size, and MD5 (when available) for every
  file in the folder.
* Splits long replies into multiple Telegram messages so nothing is cut off.
* Fully Dockerized (Firefox ESR + GeckoDriver included).

---

## Quick start

### Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | Any recent version |
| Telegram bot token | Obtain from [@BotFather](https://t.me/BotFather) |

### Run with Docker Compose

```bash
export TELEGRAM_TOKEN="your_bot_token_here"
docker compose up --build
```

The container will:
1. Install Firefox ESR and GeckoDriver.
2. Launch Firefox in headless mode.
3. Visit the gofile.io direct-login URL to authenticate the session.
4. Start polling Telegram for messages.

### Run without Docker (development)

```bash
pip install -r requirements.txt
# Firefox and GeckoDriver must already be installed and in PATH
export TELEGRAM_TOKEN="your_bot_token_here"
python bot.py
```

---

## Usage

Send the bot any gofile.io folder link:

```
https://gofile.io/d/sx6Jai
```

The bot replies with a list like:

```
📁 Found 3 file(s):

1. 📄 Foundation.S01E05.mkv (2.7 GB, MD5: 6e72e28f…)
   ⬇️ https://cold-na-phx-10.gofile.io/download/web/519b42e9-…/Foundation.S01E05.mkv

2. 📄 Foundation.S01E06.mkv (2.5 GB)
   ⬇️ https://cold-na-phx-10.gofile.io/download/web/…/Foundation.S01E06.mkv
…
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `GOFILE_LOGIN_URL` | ❌ | Direct-login URL for the gofile.io account (defaults to the bundled URL) |
| `GOFILE_WEBSITE_TOKEN` | ❌ | Emergency fallback for the gofile.io website token. The bot automatically extracts the live token from the browser after each page load (it rotates every ~4 hours). Only set this if all automatic extraction methods fail. |

---

## Architecture

```
bot.py
 ├── _login()                  – Opens LOGIN_URL in Firefox, sets session cookies
 ├── _refresh_website_token()  – Extracts live wt token from browser performance entries
 ├── _scrape_via_dom()         – Loads folder in browser, refreshes wt, scrapes links
 ├── _scrape_via_api()         – Calls gofile.io REST API with fresh wt + account token
 ├── scrape_folder()           – DOM load first (refreshes wt) → API call → DOM fallback
 └── handle_message()          – Telegram message handler
```

---

## Notes

* The direct-login URL (`LOGIN_URL` in `bot.py`) is tied to a specific gofile.io
  account. Replace it if you use a different account.
* The gofile.io website token (`wt`) rotates roughly every 4 hours and is
  unique per account. The bot automatically extracts it from the browser's
  performance resource-timing entries after every page load — no manual
  configuration is needed. The `GOFILE_WEBSITE_TOKEN` environment variable
  is an emergency override that is only used if all automatic extraction
  methods fail.
