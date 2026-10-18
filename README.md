# painmap-reddit-reader

A small, read only Reddit client. It collects new posts and top comments from a configured list of subreddits into a local SQLite database, for later analysis on the same machine.

It uses the official Reddit Data API through [PRAW](https://praw.readthedocs.io/), registered as a personal "script" app.

## What it does

- Reads public posts and their top comments from about 200 trades, engineering, manufacturing, automotive, education, research, and small business communities listed in `subreddits.yaml`.
- Runs on a schedule by tier:
  - Tier 1: new posts and top comments, every 6 hours.
  - Tier 2: new posts and top comments, daily.
  - Tier 3: new posts only, weekly.
- The first run for a subreddit reads up to about 1,000 recent posts (posts only, within the request budget). Later runs read only posts newer than the last one seen.
- Top comments (default 10 per post, replies down to depth 2) are fetched once, when a post is between 24 hours and 7 days old.
- Offers a one time backfill of posts, up to one year back, limited to what the listing API returns (about 1,000 items per subreddit).
- Rechecks stored items daily and deletes any that were removed or deleted on Reddit (see Data handling).
- Marks a subreddit dead (and skips it) after 5 failed attempts in a row because it is private, banned, or missing. A dead subreddit is only collected again after a manual reset. Its stored posts and comments are deleted once it has been dead for 7 days.
- Detects a sitewide block. Before marking any subreddit dead it calls `/api/v1/me`. If that fails, or 5 or more subreddits answer 403 in one run, it stops, marks nothing dead, logs a "sitewide block" error, and refuses further runs until `reader reset-block` is run.

## What it does not do

- No writing of any kind: no posting, commenting, voting, or messaging.
- No scraping: no HTML pages, no RSS, no third party dumps. Only the official API through PRAW.
- No redistribution, publication, or sharing of collected data.
- No AI or LLM calls from this code, and no training of AI models on the data.
- No network traffic other than requests to the Reddit API.

## Data handling

- Everything is stored in a local SQLite file (WAL mode). Nothing is uploaded anywhere.
- Usernames are never stored. Authors are kept as a SHA256 hash with a secret salt. The salt comes from `READER_AUTHOR_SALT`, or is generated once into a separate file (mode 600), never inside the database or its folder.
- Content that is removed or deleted on Reddit is deleted locally, including its text. SQLite's `secure_delete` overwrites the deleted rows on disk, and the WAL file is emptied after each recheck.
- `reader recheck` runs daily. It checks stored posts and comments from the last 30 days, least recently checked first, through `reddit.info()` (100 items per request). It uses at most 20% of the request budget and starts only after that day's first tier 1 run has completed. Items older than 30 days are not rechecked.
- An item Reddit no longer returns at all is deleted once it has been missing in two rechecks at least 24 hours apart. If Reddit returns none of the items in a batch, that is treated as an API error and nothing is deleted.

## Request volume

At most 5,000 requests in any 24 hours across about 200 subreddits, well inside Reddit's published limits. PRAW handles the `X-Ratelimit-*` headers itself. On top of that the code stops a run, cleanly, when either of two limits is reached, whichever comes first:

- A hard budget of 5,000 requests in any rolling 24 hour window, counted from the timestamp of every request and shared by all runs. Every HTTP request counts, including retries and token refreshes; they pass through PRAW's documented `requestor_class` option.
- Reddit's own numbers from `reddit.auth.limits`: the run stops if Reddit reports fewer than 100 requests remaining in its window.

The next run resumes where the last one stopped. Tier 1 runs first and the recheck waits for it, so tier 1 gets priority. A 429 answer stops the whole run. A small token bucket also smooths bursts to about one request per second. Each run logs the requests it used, the requests in the last 24 hours, the budget, and Reddit's reported remaining and used counts.

When a run gets SIGTERM, it finishes the current item and is recorded as "interrupted". A run left "running" by a crash or power loss is marked "interrupted" when the next run starts.

## Purpose

Personal research into recurring problems that people describe in trades, engineering, and related work communities. The findings may inform a future product or business.

Collected data is analysed on my own machine with a locally hosted language model to find recurring problems people describe. It is never sent to a third party AI service, never used to train any model, and never shared or published. That analysis is not part of this repository.

## Setup

Requires Python 3.10 or newer and a Reddit app of type "script" whose redirect uri is `http://localhost:8080`.

```bash
pip install -e ".[dev]"
cp .env.example .env   # then fill in your credentials
```

Credentials are read from environment variables only (load `.env` with your shell or a systemd `EnvironmentFile`).

### Recommended: a read only refresh token

Set `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and `REDDIT_USERNAME`, then run:

```bash
reader auth
```

It prints a Reddit authorization address, asks you to paste back the address Reddit redirects to, and prints a refresh token. The token is requested with the `read` and `identity` scopes only: `read` to read posts and comments, and `identity` only so `/api/v1/me` can confirm the account works before a subreddit is marked dead. Put it in `REDDIT_REFRESH_TOKEN`; no password is needed.

### Fallback: password grant

If `REDDIT_REFRESH_TOKEN` is not set, the reader logs in with `REDDIT_USERNAME` and `REDDIT_PASSWORD`. A password login grants the app broader scopes than it needs. The code still only reads, but prefer the refresh token.

| Variable | Purpose |
| --- | --- |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | the script app's credentials |
| `REDDIT_USERNAME` | used in the user agent, and to log in |
| `REDDIT_REFRESH_TOKEN` | token from `reader auth` with `read` and `identity` only (recommended) |
| `REDDIT_PASSWORD` | password grant, used only if no refresh token is set |
| `READER_AUTHOR_SALT` | salt for author hashes (optional, see above) |
| `READER_SALT_FILE` | where a generated salt is kept (default `~/.config/painmap-reader/author_salt`) |
| `READER_DB` | database path (default `reader.db`) |

User agent: `linux:painmap-reddit-reader:v0.1 (by /u/<USERNAME>)`.

## Config

`subreddits.yaml` lists each subreddit with a `name`, a `tier` (1, 2 or 3) and a `field`.

## Usage

```bash
reader collect --tier 1        # collect due subreddits of a tier (repeat --tier, or omit for all)
reader backfill --subreddit X  # one time backfill of posts
reader recheck                 # daily slice: delete items removed on Reddit
reader auth                    # create a read only refresh token
reader status                  # counts, last run, dead subreddits, block, requests in 24 hours
reader reset-dead --subreddit X
reader reset-block             # allow runs again after a sitewide block
```

Example systemd units are in `deploy/`. They are examples only; nothing in the code installs them.

## Development

```bash
ruff check .
pytest
```

Tests run offline against fabricated fixtures in `tests/fixtures/`.

## License

MIT. See `LICENSE`.
