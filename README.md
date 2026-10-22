# Kenya remote job alerts

A small bot that runs twice a day, pulls remote software jobs from free job boards, keeps only those a **Kenya-based candidate can realistically apply for**, removes duplicates, and sends you **only new** jobs by Telegram and/or email.

- **Sources:** Remotive, Remote OK, We Work Remotely (RSS), Jooble, Adzuna. LinkedIn, Indeed and Google are never scraped.
- **Filtering:** role keywords, then location eligibility (Kenya, Nairobi, Africa, EMEA, worldwide, anywhere…), then exclusions (US only, EU only, "must be authorized to work in the US"…). Jobs with an unclear location are kept and marked **needs checking**. An optional Claude check is available for those.
- **Delivery:** one Telegram message per job (or a digest above 10 jobs), plus one HTML email digest per run.
- **Runs on:** GitHub Actions at 08:00 and 16:00 Nairobi time. The SQLite database of jobs already seen is kept between runs.

---

## Contents
1. [Quick start (local)](#1-quick-start-local)
2. [Create a Telegram bot and find your chat ID](#2-create-a-telegram-bot-and-find-your-chat-id)
3. [Create a Gmail App Password](#3-create-a-gmail-app-password)
4. [Get the optional API keys (Jooble, Adzuna, Anthropic)](#4-get-the-optional-api-keys)
5. [Deploy on GitHub Actions](#5-deploy-on-github-actions)
6. [Testing each source individually](#6-testing-each-source-individually)
7. [Configuration](#7-configuration)
8. [Changing the schedule](#8-changing-the-schedule)
9. [Adding a new source](#9-adding-a-new-source)
10. [How it works](#10-how-it-works)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Quick start (local)

Requires Python 3.11+.

```bash
git clone <your-repo-url> job-bot && cd job-bot
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # then fill in the values (see sections 2-4)

python -m pytest -q         # all tests should pass
python main.py --dry-run    # fetch + filter, print the jobs; sends and saves nothing
python main.py              # real run: sends new jobs, then remembers them
```

`.env` is read automatically for local runs and is git-ignored. Variables already set in your shell take priority.

| Command | What it does |
|---|---|
| `python main.py` | Normal run: fetch, filter, dedupe, send new jobs, save them as seen |
| `python main.py --dry-run` | Print the new jobs. Nothing is sent and the database is not touched |
| `python main.py --source remoteok` | Use only one source (works even if it is disabled in config) |
| `python main.py --seed` | Mark every current job as seen **without sending**. Use once to skip the backlog |
| `python main.py --list-sources` | Show the available sources and whether they are enabled |
| `python main.py -v` | Debug logging, including why each job was kept or dropped |
| `python main.py --config other.yaml` | Use a different config file (or set `JOBBOT_CONFIG`) |

Exit codes: `0` OK, `1` every source failed or every notification channel failed, `2` invalid config or unknown `--source`.

---

## 2. Create a Telegram bot and find your chat ID

**Create the bot (BotFather)**
1. In Telegram, open a chat with **[@BotFather](https://t.me/BotFather)** (check for the blue verified tick).
2. Send `/newbot`. Choose a display name (e.g. "My Job Alerts"), then a username ending in `bot` (e.g. `kevin_jobs_alert_bot`).
3. BotFather replies with a token like `123456789:AAH...`. This is your **`TELEGRAM_BOT_TOKEN`**. Keep it secret: anyone with it can control the bot.

**Find your chat ID**
1. Open a chat with your new bot and press **Start** (or send any message, e.g. "hi"). The bot can't message you until you've done this.
2. In a browser, open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` (replace `<YOUR_TOKEN>`).
3. Find `"chat":{"id":123456789,...}`. That number is your **`TELEGRAM_CHAT_ID`**.
   - If the result is `{"ok":true,"result":[]}`, send the bot another message and refresh.
   - To post into a group instead, add the bot to the group, send a message there, and use the group's ID (negative, e.g. `-1001234567890`).
4. Test it:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" -d chat_id=<CHAT_ID> -d text="hello"
   ```

---

## 3. Create a Gmail App Password

Gmail does not accept your normal password over SMTP. It needs a 16-character **App Password**.

1. Turn on **2-Step Verification** for your Google account: <https://myaccount.google.com/security>. App Passwords only exist when 2-Step Verification is on.
2. Open <https://myaccount.google.com/apppasswords>. If the page says the setting isn't available, 2-Step Verification is off, or your Workspace admin has disabled App Passwords.
3. Enter a name such as "job bot" and click **Create**.
4. Copy the 16-character password and **remove the spaces** Google shows between the groups. This is **`SMTP_PASSWORD`**.

Then set:
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=you@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com            # comma-separated for several recipients
```
`SMTP_HOST` and `SMTP_PORT` default to Gmail, and `EMAIL_FROM` defaults to `SMTP_USER`, so you can leave them out. Port `465` uses SSL; any other port (e.g. `587`) uses STARTTLS. That works for Outlook, Zoho, Mailgun and others.

---

## 4. Get the optional API keys

Every source and the LLM check are optional. **A source whose keys are missing is skipped with a warning.** Remotive, Remote OK and We Work Remotely need no keys.

| Key(s) | Where to get it | Notes |
|---|---|---|
| `JOOBLE_API_KEY` | <https://jooble.org/api/about>. Fill in the form and the key arrives by email | Free. Searches `"software developer"` in `"Kenya"` (see config) |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | <https://developer.adzuna.com/signup>, then the dashboard shows the App ID and Key | Free tier. Adzuna has no Kenya market, so the bot searches the UK and US markets for remote roles. Expect few eligible results |
| `ANTHROPIC_API_KEY` | <https://console.anthropic.com/settings/keys>, then **Create Key** (needs billing set up) | Only used if `filters.llm.enabled: true`. Pay-per-use. Each job is checked once and the result cached; max 30 checks per run by default |

---

## 5. Deploy on GitHub Actions

1. **Push the project to GitHub.** A private repo works; so does a public one (see the keepalive note below).
2. **Add your secrets:** repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add each one by name (only the ones you use):

   | Secret | Required for |
   |---|---|
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram |
   | `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | Email |
   | `SMTP_HOST`, `SMTP_PORT`, `EMAIL_FROM` | Email, only if not Gmail defaults |
   | `JOOBLE_API_KEY` | Jooble source |
   | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Adzuna source |
   | `ANTHROPIC_API_KEY` | LLM check (if enabled) |

   If you turn a channel off in `config.yaml` (`notifications.telegram: false` / `notifications.email: false`), you don't need its secrets.
3. **Allow the workflow to push:** **Settings** → **Actions** → **General** → **Workflow permissions** → choose **Read and write permissions** → Save. (The workflow also asks for `contents: write` itself, but some accounts and organisations cap the token at read-only.)
4. **Test it by hand:** **Actions** tab → **Job alerts** → **Run workflow**:
   - tick **dry_run** first: the log shows which jobs would be sent;
   - optionally tick **seed** once: this records today's jobs as seen without sending, so you don't get a 50-job burst on day one;
   - then run it with no options ticked to confirm Telegram and email arrive.
5. From then on it runs automatically at 08:00 and 16:00 Nairobi time.

### What the workflow does (`.github/workflows/jobs.yml`)
1. Installs dependencies and **runs the tests**. If they fail, the bot doesn't run.
2. **Restores** `data/jobs.db` from the `state` branch (on the first run there is none, so it starts fresh).
3. Runs `python main.py` with your secrets as environment variables.
4. **Saves** the database back to the `state` branch (also after a partially failed run, which is safe because jobs are only marked seen after delivery; skipped for dry runs).
5. **Keepalive** (scheduled runs only): see below.

A `concurrency` group ensures two runs never overlap. A manual run started while a scheduled one is going waits for it.

### Why a state branch (and not `actions/cache`)
The database must survive between runs, or every run would resend everything.
- **`actions/cache`** is designed for rebuildable files. Entries are immutable (you'd need a new key every run), are **deleted after 7 days without access** and when the repo exceeds its 10 GB cache quota, and are scoped per branch. Losing the cache means a flood of repeat alerts.
- **A `state` branch** is durable, you can inspect it (download `jobs.db` and open it in any SQLite viewer), and it costs nothing. The workflow force-pushes a **single orphan commit** each run, so the branch only ever holds the latest DB and repository history doesn't grow.

Don't merge `state` into `main`, and don't protect it with branch rules that block force-pushes.

> In a **public** repo the `state` branch is public too. It only contains job titles, companies and URLs (already public), plus the LLM's one-line reasons if that check is enabled.

### The 60-day inactivity pause (public repos)
GitHub automatically **disables scheduled workflows in public repositories after 60 days with no repository activity**. The bot's own commits to the `state` branch shouldn't be relied on to count. On every scheduled run, the **Keepalive** step calls the GitHub API to (re-)enable the workflow:
```
gh api --method PUT repos/<owner>/<repo>/actions/workflows/jobs.yml/enable
```
This is a no-op while the workflow is enabled and resets the inactivity timer, with no dummy commits. It needs the `actions: write` permission, which the workflow requests. The step is `continue-on-error`, so a keepalive hiccup never fails the run.

If the workflow does get disabled anyway, GitHub emails you. Re-enable it under **Actions** → **Job alerts** → **Enable workflow**. Private repos are not affected by this rule.

---

## 6. Testing each source individually

```bash
python main.py --list-sources
python main.py --dry-run --source remotive
python main.py --dry-run --source remoteok
python main.py --dry-run --source weworkremotely
python main.py --dry-run --source jooble          # needs JOOBLE_API_KEY
python main.py --dry-run --source adzuna -v       # needs ADZUNA_APP_ID/KEY; -v shows each decision
```
The log line `Source X returned N job(s)` shows the raw count before filtering. `Filters kept A of B` shows how many survived. On GitHub, use **Run workflow** with `dry_run` ticked and `source` filled in.

---

## 7. Configuration

Everything except secrets lives in [`config.yaml`](config.yaml). Every key has a built-in default in [`jobbot/config.py`](jobbot/config.py). If you set a list in `config.yaml`, it **replaces** the default list rather than adding to it.

| Section | Key settings |
|---|---|
| `sources.<name>` | `enabled`, plus per-source options (Remotive `categories`, WWR `feeds`, Jooble `keywords`/`location`, Adzuna `countries`/`what`) |
| `filters` | `role_keywords`, `title_exclude_keywords`, `allow_keywords`, `exclude_phrases`, `exclude_patterns` (regex), `restricted_location_terms`, `keep_ambiguous`, `description_min_keyword_hits` |
| `filters.llm` | `enabled` (default `false`), `model` (`claude-sonnet-4-6`), `scope` (`ambiguous` = only "needs checking" jobs, or `all`), `max_checks_per_run` |
| `notifications` | `telegram`, `email` (on/off), `send_empty_notice`, `telegram_digest_threshold` (10), `max_jobs_per_run` (50; extra jobs wait for the next run) |
| `http` | timeout, retries, backoff, User-Agent |
| `storage.db_path` | `data/jobs.db` (the workflow's `DB_PATH` must match) |

Tip: run `python main.py --dry-run -v` after changing filters to see each keep/drop decision with its reason.

---

## 8. Changing the schedule

Edit the `cron` lines in [`.github/workflows/jobs.yml`](.github/workflows/jobs.yml). GitHub cron is **always UTC**, and Nairobi is **UTC+3** with no daylight saving, so subtract 3 hours from the local time you want:

| Nairobi time | UTC cron |
|---|---|
| 07:00 | `0 4 * * *` |
| 08:00 | `0 5 * * *` (current) |
| 12:30 | `30 9 * * *` |
| 16:00 | `0 13 * * *` (current) |
| 20:00 | `0 17 * * *` |
| Weekdays 08:00 only | `0 5 * * 1-5` |

Format: `minute hour day-of-month month day-of-week`. Scheduled runs can start a few minutes late when GitHub is busy. Keep it to a few runs a day: Remotive asks for at most 4 API calls per day.

---

## 9. Adding a new source

Create **one file** in `jobbot/sources/`. It's discovered automatically, with no registration needed:

```python
# jobbot/sources/examplejobs.py
from ..models import Job, html_to_text
from .base import Source, parse_date


class ExampleJobsSource(Source):
    name = "examplejobs"                    # used in config.yaml, --source and notifications
    required_env = ("EXAMPLEJOBS_API_KEY",) # source is skipped with a warning if missing; () if none

    def fetch(self) -> list[Job]:
        data = self.http.get_json(          # timeouts, retries and User-Agent built in
            self.setting("url", "https://api.examplejobs.com/v1/jobs"),
            params={"q": self.setting("query", "developer"), "key": self.env("EXAMPLEJOBS_API_KEY")},
        )
        return [
            Job(
                title=item["title"],
                company=item.get("company", ""),
                url=item["url"],
                location_text=item.get("location", ""),
                description=html_to_text(item.get("description", "")),
                source=self.name,
                posted_date=parse_date(item.get("published_at")),
            )
            for item in data.get("jobs", [])
            if item.get("url") and item.get("title")
        ]
```

Then, optionally:
- add settings under `sources.examplejobs` in `config.yaml` **and** `DEFAULTS` in `jobbot/config.py` (a test checks they match). Without settings the source is enabled by default;
- add a display name to `SOURCE_LABELS` in `jobbot/notifiers/base.py`;
- if the API key goes in the URL, add the env var to `SECRET_ENV_VARS` in `jobbot/http.py` so it's hidden in error logs;
- add the secret to the workflow's `env:` block and to GitHub Secrets;
- test it with `python main.py --dry-run --source examplejobs -v`.

Raise an exception on failure (the run logs it and continues with the other sources). Don't swallow errors silently.

---

## 10. How it works

```
fetch every enabled source (one failure doesn't stop the others)
  → rule-based filter (+ optional Claude check)
  → dedupe within the run (same URL, or same company + title across boards)
  → drop jobs already in the database (by job ID or company + title)
  → sort (confirmed first, newest first), cap at max_jobs_per_run
  → send via Telegram and email (each channel isolated)
  → save as seen, ONLY if at least one channel delivered
```

**Filter rules, in order** (first decisive rule wins; full detail in [`jobbot/filters.py`](jobbot/filters.py)):
1. Title contains an excluded word (sales, recruiter…), so it's rejected.
2. No role keyword in the title, and fewer than 3 different role keywords in the description, so it's rejected.
3. An exclusion phrase or pattern anywhere ("US only", "must be authorized to work in the US", "no visa sponsorship… UK"), so it's rejected. This beats "worldwide".
4. The location field mentions Kenya, Nairobi, Africa, EMEA, worldwide, anywhere or global, so it's kept. "South Africa", "North Africa" and "West Africa" don't count as "Africa".
5. The location field names some other region ("USA", "Europe", "LATAM", "UK"…), so it's rejected.
6. The description mentions Kenya, Nairobi, Africa, EMEA or "work from anywhere", so it's kept.
7. The description only says "global", "worldwide" or "anywhere", so it's kept but marked **needs checking**.
8. No location information either way, so it's kept and marked **needs checking**.

"Needs checking" jobs show a yellow badge in email and ⚠️ in Telegram, with the reason. Read the posting before applying.

**Database:** `seen_jobs(job_id, title, company, url, source, first_seen, dedupe_key)` and `llm_cache(job_id, eligible, reason, model, checked_at)`. `job_id` is a hash of the URL, with tracking parameters removed.

**Source etiquette:** each source is called once per run, with a descriptive User-Agent, timeouts and exponential backoff that honours `Retry-After`. Remotive and Remote OK require attribution, so every notification names the source and links to the original listing.

---

## 11. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `Skipping source jooble: missing environment variable(s)` | Expected if you don't use that source. Otherwise add the secret (exact name, no spaces). |
| `telegram is enabled but missing environment variable(s)` | Add `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, or set `notifications.telegram: false`. |
| `Telegram rejected the message: HTTP 401` | Wrong bot token. Copy it again from BotFather (`/mybots` → API Token). |
| `HTTP 400: chat not found` / `HTTP 403: bot was blocked` | Wrong chat ID, or you never pressed **Start** in the bot chat (or you blocked it). Redo [section 2](#2-create-a-telegram-bot-and-find-your-chat-id). |
| `SMTP login failed` | Gmail needs an **App Password**, not your normal password ([section 3](#3-create-a-gmail-app-password)). Check `SMTP_USER` is the full address. |
| Email works locally but times out on another provider | Use port `587` (STARTTLS) or `465` (SSL), whichever the provider documents. |
| No emails at all, nothing in logs about email | `notifications.email` is `false` in `config.yaml`, or there were no new jobs (set `send_empty_notice: true` to get a "nothing new" message). |
| Same jobs arrive again every run | The database isn't being saved. Check the "Save database" step log, and **Settings → Actions → General → Workflow permissions = Read and write**. |
| `No notification channel delivered; jobs NOT saved` | Every channel failed, so the jobs will be retried next run. Fix the channel error shown above it. |
| A flood of jobs on the first run | Normal: everything is new. Use **Run workflow → seed** once, or let `max_jobs_per_run` spread them over a few runs. |
| Remotive returns only ~10 jobs | That's what Remotive's free public API currently returns (24 h delayed). Nothing to fix. |
| Adzuna adds almost nothing | Expected: its markets are country-bound (UK/US) and those jobs are mostly restricted to those countries. Disable it if you like. |
| Jooble jobs are on-site in Nairobi | Jooble's `location: Kenya` finds jobs *located* in Kenya. Change `sources.jooble.keywords` to e.g. `remote software developer`. |
| A job I could apply for was dropped | Run `python main.py --dry-run -v` and search the log for its title to see which rule dropped it. Then adjust `config.yaml`. |
| A US-only job got through | Its posting didn't use a phrase on the exclusion list. Add the wording to `exclude_phrases`, or enable the LLM check. |
| Scheduled runs stopped | Public repo inactive for 60 days: re-enable under **Actions → Job alerts**. The keepalive step should prevent this. Check it isn't failing. |
| Runs start late | GitHub schedules are best-effort. A delay of several minutes is normal. |
| `Invalid configuration: ...` | A YAML or regex error in `config.yaml`. The message names the key. |

Run the test suite any time with `python -m pytest -q`.
