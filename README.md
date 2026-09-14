# P-Card Audit Console

An internal-audit tool for Oklahoma State University purchasing-card transactions.
Built for the EY Analytics Mindset P-card case, Part IV.

Two tabs:

- **Ask a question** — type a question in plain English, get results back along with
  the SQL that was actually run, so you can verify the query before relying on the numbers.
- **Prohibited purchases** — search transaction descriptions or vendor names, by year,
  for the categories OSU prohibits on a P-Card.

## Protect your API key

The key is read from the `GEMINI_API_KEY` environment variable. It is never written
to source and `.env` is listed in `.gitignore`. **Do not commit a key.** If one is ever
pushed, revoke it in [Google AI Studio](https://aistudio.google.com/apikey) immediately —
rewriting Git history is not enough, because the key is already exposed in forks, caches
and clones.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit .env and add your key
export $(grep -v '^#' .env | xargs)

python app.py                 # http://localhost:5000
```

Place `pcards.db` in the project root, or point `PCARD_DB` at it.

## The database and GitHub

`pcards.db` is about 98 MB. It's tracked with **Git LFS** (`git lfs track "*.db"`,
committed via `.gitattributes`) instead of being stored directly in the repo's normal
history. Cloning the repo pulls the real file automatically as long as Git LFS is
installed (`git lfs install`) — no extra setup needed on the cloning end. Watch the
free LFS storage/bandwidth quota (1 GiB/month) if the repo gets cloned a lot.

## Deploy

The app runs under gunicorn via the included `Procfile` and works on Render, Railway,
Fly.io or any similar host. Set two environment variables in the host's dashboard:

| Variable | Value |
|---|---|
| `GEMINI_API_KEY` | your key |
| `PCARD_DB` | path to the database on the deployed filesystem |

Optionally set `GEMINI_MODEL` (defaults to `gemini-3.6-flash`).

## Static build (GitHub Pages, `docs/`)

`docs/index.html` is a second, self-contained version of this app that runs entirely
in the browser — no Flask, no server at all. This is the version GitHub Pages serves,
since Pages can only host static files and cannot run Python.

How it differs from the Flask app:

- **Database** — queried client-side with [sql.js](https://sql.js.org/) (SQLite
  compiled to WebAssembly). The page downloads `pcards.db` once per visit directly
  from this repo's Git LFS storage via `media.githubusercontent.com` (the endpoint
  that resolves LFS pointers to the real file; GitHub Pages itself and
  `raw.githubusercontent.com` both serve only the ~130-byte LFS pointer, not the
  database, so the static page fetches from the LFS media host explicitly).
- **"Ask a question"** — since there's no server, there's nowhere to keep a shared
  secret. Each visitor pastes their own Gemini key into the page; it's kept only in
  that browser's `localStorage` and sent directly from the browser to Google's API.
  It is never bundled into the page, never committed, and never seen by anyone but
  that visitor. "Prohibited purchases" needs no key at all.
- Both tabs otherwise behave identically to the Flask version — same schema prompt,
  same SQL guardrails (`validateSql()` mirrors `validate_sql()`), same queries.

To enable: repo **Settings → Pages → Source: Deploy from a branch → Branch: `main`,
folder: `/docs`**. The live URL is `https://<username>.github.io/<repo>/`.

## How the natural-language tab works

The question goes to the Google Gemini API (`generateContent`) with a system prompt
containing the table schema, a note that `TransactionDate` is text in `M/D/YYYY` format
and will not sort correctly, and the OSU spending limits. The model returns SQL only.

Before anything runs, `validate_sql()` enforces:

- the statement begins with `SELECT` or `WITH`;
- no `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `PRAGMA`, `ATTACH` or
  similar keywords appear;
- only one statement is present, so a `;` cannot smuggle in a second;
- a `LIMIT` is appended if the model omitted one.

The connection itself opens read-only (`file:...?mode=ro`), so a write could not
succeed even if it slipped through the text checks.

The generated SQL is always displayed above the results. Treat it as part of the
output — a question that reads clearly can still translate into the wrong filter,
and in an audit context an unverified number is worse than no number.

## Files

```
app.py              Flask backend: /api/ask, /api/search, /api/years
static/index.html   single-page frontend (Flask version)
docs/index.html     static, client-side version served by GitHub Pages
requirements.txt    Flask, requests, gunicorn
Procfile            gunicorn entry point for deployment
.env.example        template — copy to .env, never commit .env
.gitignore          excludes .env and *.db (pcards.db itself is allow-listed back in)
.gitattributes       tracks *.db with Git LFS
```

## A note on results

Every hit is a question, not a finding. Legitimate explanations exist for most flagged
transactions — laboratory alcohol reagents, bulk agricultural fuel, institutional
memberships coded to charitable merchant categories. Pull the receipt and the business
purpose recorded in Works before treating anything as an exception.
