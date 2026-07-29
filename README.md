# news-digest

A personal daily news digest: pulls RSS feeds, filters by keyword relevance,
has Claude pick and summarize the 8 most relevant stories, and emails the
result.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill in ANTHROPIC_API_KEY and RESEND_API_KEY
cp profile.example.py profile.py   # then edit profile.py with your own reader profile
```

`profile.py` is gitignored on purpose -- it holds who you are and what you
care about (the `about` text and keyword weights digest.py uses to curate
and rank stories), so it never ends up committed. `config.py` holds
everything not sensitive: the feed list, recency window, email settings,
etc.

## Usage

```bash
python main.py              # fetch -> filter -> curate -> send
python main.py --dry-run    # same, but prints instead of sending
python main.py --to a@b.com # override the recipient for this run
```

Every run is logged to `run.log` (timestamp, item count, estimated model
cost, success/failure).

Each stage is also runnable standalone for testing:

```bash
python fetch.py    # pull all feeds, print raw item counts
python filter.py   # fetch + keyword-filter, print what survives + near-misses
python digest.py   # fetch + filter + the Claude curation call, print the result
python send.py     # render the email template to _preview.html; optionally send a test email
```

## Files

| File | Purpose |
|---|---|
| `profile.py` | Your reader profile (gitignored -- copy from `profile.example.py`) |
| `config.py` | Feed list + global settings (recency window, email, thresholds) |
| `fetch.py` | Pulls every feed, normalizes entries |
| `filter.py` | Dedupes, drops stale items, scores by keyword relevance |
| `digest.py` | The Claude API call that curates and writes the final 8 items |
| `send.py` | Renders the HTML email and sends via Resend |
| `main.py` | Orchestrates the whole pipeline |
| `templates/digest.html` | The email template (Jinja2) |
