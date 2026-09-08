"""
P-Card Audit Console — backend.

Two capabilities:
  1. /api/ask     natural-language question -> SQL -> results
  2. /api/search  keyword search of Description or Vendor, by year

The API key is read from the GEMINI_API_KEY environment variable and is
never written to source. See README.md.
"""

import os
import re
import sqlite3

try:
    from dotenv import load_dotenv
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(APP_DIR, ".env"))
except Exception:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

import requests
from flask import Flask, jsonify, request, send_from_directory

DB_PATH = os.environ.get("PCARD_DB", os.path.join(APP_DIR, "pcards.db"))
API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
ROW_CAP = 500

app = Flask(__name__, static_folder="static", static_url_path="")


# --------------------------------------------------------------- database ---

def connect():
    """Read-only connection. The audit console must never mutate evidence."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.create_function("REGEXP", 2, lambda p, s: 0)  # disable regexp
    return con


SCHEMA = """
Table: pcards
  Year                   INTEGER  calendar year of the transaction
  Month                  INTEGER  1-12
  FullName               TEXT     cardholder, e.g. 'Employee 51914657'
  ID                     INTEGER  row id within the cardholder's set
  AgencyNumber           INTEGER  always 1000
  AgencyName             TEXT     always 'OKLAHOMA STATE UNIVERSITY'
  CardholderLastName     TEXT
  CardholderFirstInitial TEXT
  Description            TEXT     merchant line-item text, often 'GENERAL PURCHASE'
  Amount                 REAL     positive = charge, negative = credit/return
  Vendor                 TEXT     merchant name
  TransactionDate        TEXT     'M/D/YYYY 0:00:00' -- does NOT sort as text
  PostedDate             TEXT     'M/D/YYYY 0:00:00'
  MCC                    TEXT     merchant category code description

Rows: 489,178 covering 2010-2014. 2,021 cardholders, 17,513 vendors.
"""

DATE_HINT = """
TransactionDate is text in M/D/YYYY format and will not sort correctly.
To sort or compare dates, build an ISO string:
  printf('%04d-%02d-%02d',
      CAST(substr(TransactionDate, instr(TransactionDate,' ')-4, 4) AS INTEGER),
      CAST(substr(TransactionDate, 1, instr(TransactionDate,'/')-1) AS INTEGER),
      CAST(substr(TransactionDate, instr(TransactionDate,'/')+1,
           instr(substr(TransactionDate, instr(TransactionDate,'/')+1),'/')-1) AS INTEGER))
The Year and Month columns already match TransactionDate, so prefer
`WHERE Year = 2014` over parsing the date.
"""

CONTROLS = """
Relevant OSU P-Card limits: $50,000 per cycle credit limit, $5,000 single
transaction limit, $10,000 monthly limit without written justification.
Splitting a purchase to evade the $5,000 limit is prohibited.
"""


# ---------------------------------------------------------- SQL guardrails ---

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|"
    r"DETACH|PRAGMA|VACUUM|REINDEX|GRANT|COMMIT|BEGIN)\b",
    re.I,
)


def validate_sql(sql):
    """Return cleaned SQL or raise ValueError. Read-only, single statement."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ValueError("The model returned an empty query.")
    if ";" in sql:
        raise ValueError("Only one statement can run at a time.")
    if FORBIDDEN.search(sql):
        raise ValueError("Only SELECT queries are allowed in this console.")
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
        raise ValueError("The query must start with SELECT or WITH.")
    if not re.search(r"\bLIMIT\b", sql, re.I):
        sql += f" LIMIT {ROW_CAP}"
    return sql


def run_sql(sql):
    con = connect()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(ROW_CAP)]
        return cols, rows
    finally:
        con.close()


# ------------------------------------------------------------------ routes ---

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/years")
def years():
    con = connect()
    try:
        rows = con.execute(
            "SELECT Year, COUNT(*) n FROM pcards GROUP BY Year ORDER BY Year DESC"
        ).fetchall()
        return jsonify([{"year": r["Year"], "count": r["n"]} for r in rows])
    finally:
        con.close()


@app.post("/api/search")
def search():
    """Keyword search of Description or Vendor for one year."""
    body = request.get_json(silent=True) or {}
    field = body.get("field")
    keyword = (body.get("keyword") or "").strip()
    year = body.get("year")

    if field not in ("Description", "Vendor"):
        return jsonify(error="Choose either the description or the vendor field."), 400
    if not keyword:
        return jsonify(error="Enter a keyword to search for."), 400
    try:
        year = int(year)
    except (TypeError, ValueError):
        return jsonify(error="Choose a year to search."), 400

    sql = (
        f"SELECT TransactionDate, PostedDate, FullName, Vendor, Description, "
        f"Amount, MCC FROM pcards WHERE Year = ? AND UPPER({field}) LIKE ? "
        f"ORDER BY Amount DESC LIMIT {ROW_CAP}"
    )
    con = connect()
    try:
        cur = con.execute(sql, (year, f"%{keyword.upper()}%"))
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
    finally:
        con.close()

    total = sum(r[cols.index("Amount")] for r in rows)
    return jsonify(
        columns=cols,
        rows=rows,
        truncated=len(rows) >= ROW_CAP,
        total=round(total, 2),
    )


@app.post("/api/ask")
def ask():
    """Natural-language question -> SQL -> results."""
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify(error="Ask a question about the P-card data."), 400
    api_key = os.environ.get("GEMINI_API_KEY") or API_KEY
    if not api_key:
        return jsonify(
            error="No API key is configured. Put your Google AI (Gemini) API key in .env (GEMINI_API_KEY=...) and restart."
        ), 503

    model = os.environ.get("GEMINI_MODEL", MODEL)

    system = (
        "You translate an internal auditor's question into one SQLite SELECT "
        "statement against the P-card transaction table below. Reply with the "
        "SQL only: no prose, no markdown fences, no trailing semicolon.\n"
        f"{SCHEMA}\n{DATE_HINT}\n{CONTROLS}\n"
        "Rules: read-only SELECT or WITH only. Always include a LIMIT of 500 "
        "or fewer. Round money with ROUND(x, 2). If the question names no "
        "year, filter to Year = 2014."
    )

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={
                "content-type": "application/json",
                "x-goog-api-key": api_key,
            },
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": question}]}],
                "generationConfig": {
                    "maxOutputTokens": 4096,
                    "temperature": 0,
                },
            },
            timeout=60,
        )
    except requests.RequestException:
        return jsonify(error="Could not reach the language model. Try again."), 502

    if resp.status_code != 200:
        return jsonify(error=f"Language model returned {resp.status_code}."), 502

    payload = resp.json()
    candidates = payload.get("candidates", [])
    text = "".join(
        p.get("text", "")
        for c in candidates
        for p in c.get("content", {}).get("parts", [])
    )
    finish = candidates[0].get("finishReason") if candidates else None
    if finish == "MAX_TOKENS":
        return jsonify(
            error="The model ran out of output budget before finishing the SQL. Try a shorter or simpler question."
        ), 502
    if not text.strip():
        return jsonify(error="The model returned no SQL. Try rephrasing the question."), 502
    sql = re.sub(r"^```(?:sql)?|```$", "", text.strip(), flags=re.M).strip()

    try:
        sql = validate_sql(sql)
    except ValueError as e:
        return jsonify(error=str(e), sql=sql), 400

    try:
        cols, rows = run_sql(sql)
    except sqlite3.Error as e:
        return jsonify(
            error=f"The generated query did not run: {e}. Try rephrasing.",
            sql=sql,
        ), 400

    return jsonify(sql=sql, columns=cols, rows=rows, truncated=len(rows) >= ROW_CAP)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
