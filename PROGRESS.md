# PROGRESS

## Status
- [x] **Phase 1: Foundation and filtering**
- [x] **Phase 2: Sources and notifications**
- [x] **Phase 3: Deployment and documentation** (106 tests passing, pyflakes clean, live `--dry-run` verified against Remotive, Remote OK and WWR; Jooble/Adzuna untested live, as no keys)

## Project tree
```
job-bot/
├── .github/workflows/jobs.yml        schedule, tests, state-branch DB, keepalive
├── jobbot/
│   ├── __init__.py
│   ├── models.py                     Job dataclass, URL/ID/dedupe-key helpers, html_to_text
│   ├── config.py                     DEFAULTS, load_config(), load_dotenv(), get_env()
│   ├── logging_setup.py              setup_logging()
│   ├── storage.py                    Storage (SQLite, read_only mode), LLMVerdict, dedupe_batch()
│   ├── filters.py                    JobFilter, FilterResult, filter_jobs()
│   ├── llm_check.py                  LLMChecker, make_checker(), run_llm_checks()
│   ├── http.py                       make_session(), HttpClient, HttpError, redact()
│   ├── app.py                        run() pipeline, print_jobs(), parse_args(), main()
│   ├── sources/
│   │   ├── __init__.py               discover_sources(), build_sources(), fetch_all(), ordered_names()
│   │   ├── base.py                   Source ABC, parse_date()
│   │   ├── remotive.py  remoteok.py  weworkremotely.py  jooble.py  adzuna.py
│   └── notifiers/
│       ├── __init__.py               build_notifiers(), notify_all()
│       ├── base.py                   Notifier ABC, NotifierError, formatting helpers, SOURCE_LABELS
│       ├── telegram.py               TelegramNotifier, build_digest_messages(), tg_len()
│       └── emailer.py                EmailNotifier, render_text(), render_html()
├── main.py                           thin entry point -> jobbot.app.main()
├── tests/
│   ├── conftest.py                   repo root on sys.path; `config`, `filter_cfg` fixtures
│   ├── test_filters.py               filters, dedupe, storage, LLM check, config
│   ├── test_notifiers.py             Telegram/email with mocked network, channel isolation
│   └── test_sources_and_app.py       source parsing (canned payloads), discovery, pipeline
├── data/                             SQLite DB (gitignored; created on first real run)
├── config.yaml, requirements.txt, requirements-dev.txt, .env.example, .gitignore
├── README.md                         setup, deployment, configuration, troubleshooting
└── PROGRESS.md
```

## Key design decisions
### Phase 1
1. **Package layout**: code in `jobbot/`; `main.py` at repo root. Tests add the repo root to `sys.path` (no install step).
2. **Config**: `DEFAULTS` in `config.py`, `config.yaml` deep-merged over it (dicts merge, lists/scalars replace). `test_config_yaml_matches_builtin_defaults` fails if they drift, so **every new key goes in both**. `$JOBBOT_CONFIG` overrides the path. Secrets only via `get_env()` (blank = unset). `load_dotenv()` is a tiny built-in `.env` reader that never overrides real env vars.
3. **job_id** = first 20 hex of SHA-256 of the normalized URL (lowercase scheme/host, no trailing slash/fragment, tracking params removed, params sorted).
4. **Cross-source dedupe key** = normalized company + "|" + normalized title; stored in an extra indexed `dedupe_key` column of `seen_jobs`, so it works across runs too.
5. **Filter order** (docstring in `filters.py`): title exclusions → role match → exclusion phrases/regexes (beat allow keywords) → allow keyword in location (after masking "South Africa" etc.) → restricted region in location → strong allow keyword in description → only weak keyword in description = keep + needs_checking → nothing = keep + needs_checking.
6. **Additions beyond the brief** (all configurable): `title_exclude_keywords`, `restricted_location_terms`, `weak_description_keywords`, `exclude_patterns` (regex).
7. **Matching**: case-insensitive, whole-word, space/hyphen tolerant, HTML stripped. Known limit: plain phrases can't read context ("contact us only by email" trips "us only").
8. **LLM check** (off by default): official `anthropic` SDK, lazy import; model `claude-sonnet-4-6` (user's choice); JSON-schema structured output; verdicts cached in `llm_cache`; `max_checks_per_run` cap; fail-open.

### Phase 2
9. **Filter fixes found with live data**:
   - `allow_mask_phrases` (south/north/west africa) are removed before allow-matching, and those plus Nigeria/Egypt/Ghana/Morocco are in `restricted_location_terms`. Otherwise "South Africa only" passed as "africa".
   - `description_min_keyword_hits: 3`: a description-only role match needs 3 *different* role keywords (synonyms like full stack/full-stack count once). Remotive's "Remote Office Assistant" passed on one incidental "software".
10. **Remotive**: the public API currently ignores `category` and returns only ~10 mixed jobs, so `sources.remotive.categories` (Software Development, DevOps / Sysadmin) filters by each item's own `category` field. The `category` param is still sent in case the API honours it again.
11. **HTTP**: one `requests.Session` with urllib3 `Retry` (connect/read/status, 429/5xx, `backoff_factor`, honours Retry-After, max 60s backoff), configured timeout on every call, polite User-Agent. `HttpClient` raises `HttpError` with **secrets redacted** (`redact()` scrubs JOOBLE_API_KEY, ADZUNA_APP_ID/KEY and TELEGRAM_BOT_TOKEN values, which appear in request URLs).
12. **Sources**: auto-discovered (`pkgutil`) from `jobbot/sources/*.py`. Run order = order in `config.yaml` (first listing wins duplicates), then any others A-Z. A source not listed in config is enabled by default. `--source NAME` runs one source even if disabled. Missing `required_env` → warning + skip. Multi-request sources (WWR feeds, Adzuna countries) tolerate partial failure and only raise if *all* requests fail. `fetch_all` isolates each source (`stats[name] = None` on failure).
13. **Source specifics**: Remote OK skips element 0 and appends tags to the description; WWR splits "Company: Title" and uses `<region>` as location; Jooble POSTs `{keywords, location: "Kenya", page}` (note: this finds jobs *located* in Kenya, which may be on-site); Adzuna searches `countries: [gb, us]` and builds `location_text` from `display_name + area` so "London, UK" is rejected as UK-restricted unless the posting is open worldwide/Africa. In practice Adzuna will contribute few jobs.
14. **Notifications**:
    - `notify_all` isolates channels; an enabled channel with missing env vars counts as failed. No jobs → nothing sent unless `send_empty_notice`.
    - **Jobs are saved as seen only if at least one channel delivered** (user decision). If all fail, they're retried next run; exit code 1.
    - Telegram: HTML parse mode with escaping; ≤ `telegram_digest_threshold` jobs → one message each, more → numbered digest split under 4096 UTF-16 units with "part i/n". ~1.1s pacing between messages; 429 → sleeps Telegram's `retry_after` (capped 60s); 5xx/connection errors → backoff; other 4xx → fail fast. Partial delivery counts as failure (rare duplicate on retry accepted for v1).
    - Email: one digest per run, `multipart/alternative` (plain + HTML cards with "needs checking" badge and reason), subject "N new Kenya-eligible remote software jobs – DD Mon YYYY". Port 465 → `SMTP_SSL`, else STARTTLS. Auth errors fail fast (Gmail App Password hint); other SMTP/socket errors retry 3×. Module named `emailer.py` to avoid shadowing stdlib `email`.
    - Both name the source (Remotive and Remote OK API terms require attribution + link back to their URL).
15. **Pipeline** (`app.run`): build sources → fetch_all → filter_jobs (+LLM) → dedupe_batch → storage.filter_new → sort (confirmed first, newest first) → cap `max_jobs_per_run` (the rest stay unseen and go out next run) → notify → mark_seen.
16. **--dry-run** opens storage `read_only=True`: no DB/dir created, no seen-marking, no LLM-cache writes, nothing sent; prints the list to stdout. Logs go to stderr.
17. **Exit codes**: 0 ok; 1 every source failed, or every channel failed; 2 bad config / unknown `--source`. Useful for GitHub Actions failure emails.
18. **First real run** will send up to 50 jobs (live dry run today found 84 new), then the remainder next run.

## Interfaces (stable)
```python
# models.py
Job(title, company, url, location_text="", description="", source="", posted_date=None,
    job_id="", needs_checking=False, check_reason="", tags=[]); Job.dedupe_key
html_to_text(raw) -> str; make_job_id(url) -> str; make_dedupe_key(company, title) -> str

# config.py
load_config(path=None) -> Config   # .log_level .storage .http .filters .notifications .sources
Config.source_settings(name) -> dict; Config.source_enabled(name) -> bool
load_dotenv(path=".env"); get_env(name, default=None) -> str | None; ConfigError

# storage.py
Storage(db_path, read_only=False): filter_new(jobs), mark_seen(jobs) -> int, is_seen(job), seen_count(),
    get_llm_verdict(job_id), save_llm_verdict(job_id, verdict)
dedupe_batch(jobs) -> list[Job]

# filters.py
filter_jobs(jobs, cfg: FilterConfig, storage=None, checker=None) -> list[Job]

# http.py
HttpClient(http_cfg, session=None): request(), get_json(url, params), post_json(url, payload), get_bytes(url)
make_session(http_cfg, retry_statuses=(429,500,502,503,504)); HttpError(msg, status_code); redact(text)

# sources
class Source(ABC): name: ClassVar[str]; required_env: ClassVar[tuple[str, ...]]
    __init__(settings: dict, http: HttpClient); fetch() -> list[Job]; setting(key, default); env(name)
discover_sources() -> dict[str, type[Source]]; build_sources(config, http, only=None) -> list[Source]
fetch_all(sources) -> (list[Job], dict[str, int | None])

# notifiers
class Notifier(ABC): name; required_env; send(jobs); send_empty_notice(); missing_env()
build_notifiers(config) -> list[Notifier]; notify_all(notifiers, jobs, send_empty_notice=False) -> dict[str, bool]

# app.py
run(config, dry_run=False, only_source=None, notifiers=None, http=None) -> RunResult
main(argv=None) -> int     # CLI: --dry-run --seed --source NAME --config PATH --list-sources -v
run(..., seed=False)       # seed: mark all new jobs seen without sending (not combinable with dry_run)
```

Env vars: `TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465), SMTP_USER, SMTP_PASSWORD, EMAIL_FROM (default SMTP_USER), EMAIL_TO (comma-separated), JOOBLE_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY, ANTHROPIC_API_KEY, JOBBOT_CONFIG, LOG_LEVEL`.

Run command for CI: `python main.py` (DB at `data/jobs.db` from `storage.db_path`).

### Phase 3
19. **Workflow** (`.github/workflows/jobs.yml`): cron `0 5 * * *` and `0 13 * * *` (08:00/16:00 Nairobi, UTC+3, no DST) plus `workflow_dispatch` with inputs `dry_run`, `source`, `seed`. `concurrency: job-alerts` with `cancel-in-progress: false`. Python 3.12, pip cache, **tests run before the bot** (failure = bot doesn't run). Permissions `contents: write`, `actions: write`.
20. **DB persistence = orphan `state` branch**, not actions/cache (cache entries are immutable per key, evicted after 7 days unused or under quota pressure, branch-scoped; losing them means repeat floods). Restore: `git fetch --depth=1 origin state` + `git show FETCH_HEAD:jobs.db`. Save: fresh temp repo, one commit, **force-push**, so the branch holds only the latest DB and history doesn't grow. Save runs even if the bot exited 1 (safe: seen-marking only happens after delivery) but not on dry runs or cancellation. Simulated locally against a bare repo.
21. **Keepalive**: on scheduled runs, `gh api --method PUT repos/$REPO/actions/workflows/jobs.yml/enable` (the same API call the community keepalive-workflow action uses), `continue-on-error`. No dummy commits, no third-party action.
22. **`--seed`** flag (and workflow input): marks every current new job as seen without sending, uncapped, to skip the first-run backlog.

## Manual steps remaining (for the user)
1. `git init`, commit, create a GitHub repo, push (this folder is not a git repo yet). `.venv/` and `.env` are git-ignored.
2. Create the Telegram bot and get the chat ID; create a Gmail App Password (README sections 2-3).
3. Optional: Jooble / Adzuna / Anthropic keys (README section 4).
4. Add GitHub Secrets (README section 5 table).
5. Settings → Actions → General → Workflow permissions → **Read and write**.
6. Actions → Job alerts → Run workflow: first `dry_run`, then optionally `seed`, then a normal run to confirm delivery.
7. Optional: tune `config.yaml` after a week of alerts (`title_exclude_keywords`, `exclude_phrases`, enable the LLM check). Consider `claude-sonnet-5-5` (newer, cheaper) for `filters.llm.model`.

## Possible future work
- Telegram partial-delivery tracking (currently a mid-batch failure resends the whole batch next run).
- Jooble: try `keywords: remote software developer` if on-site Nairobi jobs are unwanted.
- Pruning very old `seen_jobs` rows (not needed at current volumes: ~100 KB/month).
