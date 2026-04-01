# gofile-touch

A Telegram bot that authenticates to a **gofile.io** account and returns all
file names and direct download links inside any gofile.io folder you share.

---

## Features

* Logs in to your gofile.io account automatically before browsing any folder.
* **Extraction: Properties-dialog method** – loads the folder page in Firefox,
  then for every file opens its 3-dot action menu → Properties, reads the
  **file ID** and **server list** from the dialog, and constructs the download URL:

  ```
  https://{server}.gofile.io/download/web/{file_id}/{filename}
  ```

  This works for all file types (`.rar`, `.zip`, `.mkv`, multi-file folders, etc.)
  because the Properties dialog always shows full file metadata.
* `/debug <url>` command – sends a detailed diagnostic `.txt` report including
  the raw Properties dialog output and constructed URLs for each file.
* Automatically re-authenticates if the session expires.
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

1. 📄 Foundation.S01E05.mkv
   ⬇️ https://file-na-phx-1.gofile.io/download/web/519b42e9-…/Foundation.S01E05.mkv

2. 📄 Foundation.S01E06.mkv
   ⬇️ https://file-na-phx-1.gofile.io/download/web/…/Foundation.S01E06.mkv
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

---

## Architecture

```
bot.py
 ├── _login()                  – Opens LOGIN_URL in Firefox, sets session cookies
 ├── _pick_server()            – Picks best download server (prefers file-* over store-*)
 ├── _scrape_via_properties()  – Loads folder page; for each file:
 │    ├── Finds the file name anchor (a[href="javascript:void(0);"])
 │    ├── Walks up the DOM to click the last <button> (3-dot menu)
 │    ├── Clicks "Properties" in the dropdown
 │    ├── Extracts file ID (UUID) and servers from the modal
 │    └── Constructs: https://{server}.gofile.io/download/web/{id}/{name}
 ├── scrape_folder()           – Calls _scrape_via_properties; re-auths on failure
 └── handle_message()          – Telegram message handler
```

---

## Notes

* The direct-login URL (`LOGIN_URL` in `bot.py`) is tied to a specific gofile.io
  account. Replace it (or set `GOFILE_LOGIN_URL`) if you use a different account.

