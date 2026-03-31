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
| `GOFILE_WEBSITE_TOKEN` | ❌ | gofile.io website token for API calls; update if API auth fails (default: `4fd6sg89d7s6`) |

---

## Architecture

```
bot.py
 ├── _login()              – Opens LOGIN_URL in Firefox, sets session cookies
 ├── _scrape_via_api()     – Calls gofile.io REST API with browser token
 ├── _scrape_via_dom()     – Selenium DOM scrape as API fallback
 ├── scrape_folder()       – Orchestrates auth + extraction + retry logic
 └── handle_message()      – Telegram message handler
```

---

## Notes

* The direct-login URL (`LOGIN_URL` in `bot.py`) is tied to a specific gofile.io
  account. Replace it if you use a different account.
* The `_GOFILE_WEBSITE_TOKEN` constant (`4fd6sg89d7s6`) is embedded in the
  gofile.io web bundle and is required by the API. Update it if API calls start
  returning auth errors.
