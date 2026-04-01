# gofile-touch

A Telegram bot that authenticates to a **gofile.io** account and returns all
file names and direct download links inside any gofile.io folder you share.

---

## Features

* Logs in to your gofile.io account automatically before browsing any folder.
* **Primary extraction: gofile.io JSON API** – calls `api.gofile.io/contents/{id}`
  directly.  Works reliably for all file types (`.rar`, `.zip`, `.mkv`, multi-file
  folders, etc.) because the API always returns the full file list with download
  links, regardless of whether the file is a media type or not.
* **Fallback: DOM scraping** – loads the folder page in Firefox and extracts links
  using four passes (anchor tags → broad attribute scan → JavaScript state → page
  source regex).  Used only when the API returns nothing.
* `/debug <url>` command – sends a full diagnostic `.txt` report including the raw
  API JSON response, so any future issues can be diagnosed precisely.
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

Use `/debug https://gofile.io/d/xxxxx` to get a full diagnostic report as a
`.txt` file when something isn't working.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `GOFILE_LOGIN_URL` | ❌ | Direct-login URL for the gofile.io account (defaults to the bundled URL) |
| `GOFILE_WEBSITE_TOKEN` | ❌ | Website token used as the `wt` parameter in API requests (default: `4fd6sg89d7s6`). Update if gofile.io rotates it. |

---

## Architecture

```
bot.py
 ├── _login()           – Opens LOGIN_URL in Firefox, sets session cookies
 ├── _get_api_token()   – Reads account token from browser localStorage,
 │                        or creates a guest token via POST /accounts
 ├── _scrape_via_api()  – PRIMARY: GET api.gofile.io/contents/{id}
 │                        → returns all files for any file type / folder size
 ├── _scrape_via_dom()  – FALLBACK: loads folder in browser; 4-pass extraction:
 │    ├── Pass 1: <a href> anchor tags
 │    ├── Pass 2: broad JS attribute scan (data-link, data-url, …)
 │    ├── Pass 3: JavaScript state traversal
 │    └── Pass 4: page-source regex fallback
 ├── scrape_folder()    – API → if empty, DOM → if empty, re-auth → API+DOM retry
 └── handle_message()   – Telegram message handler
```

---

## Notes

* The direct-login URL (`LOGIN_URL` in `bot.py`) is tied to a specific gofile.io
  account. Replace it (or set `GOFILE_LOGIN_URL`) if you use a different account.
* The website token (`wt=4fd6sg89d7s6`) is gofile.io's public web-app token
  embedded in their JavaScript bundle.  If it ever changes, set the
  `GOFILE_WEBSITE_TOKEN` environment variable to the new value.
