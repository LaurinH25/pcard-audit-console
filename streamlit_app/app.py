"""
P-Card Audit Console — Streamlit build.

Same two capabilities as the Flask app (../app.py) and the static GitHub
Pages build (../docs/index.html), rebuilt for Streamlit Community Cloud:

  1. Ask a question    natural-language question -> SQL -> results
  2. Prohibited purchases   keyword search of Description or Vendor, by year

Unlike the GitHub Pages build, the Gemini API key lives server-side here
(Streamlit secrets), so visitors use the app without needing their own key.
The key is never written to source and is read only from st.secrets or the
GEMINI_API_KEY environment variable. See ../README.md.
"""

import os
import re
import sqlite3
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

DB_PATH = os.environ.get("PCARD_DB", str(REPO_ROOT / "pcards.db"))
ROW_CAP = 500


def get_secret(name, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


MODEL = get_secret("GEMINI_MODEL", "gemini-3.6-flash")

st.set_page_config(page_title="P-Card Audit Console", page_icon="🗂️", layout="wide")


# --------------------------------------------------------------- database ---

@st.cache_resource
def get_connection():
    """Read-only connection, shared across reruns. Never mutate evidence."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


@st.cache_data
def get_years():
    con = get_connection()
    rows = con.execute(
        "SELECT Year, COUNT(*) n FROM pcards GROUP BY Year ORDER BY Year DESC"
    ).fetchall()
    return [(r["Year"], r["n"]) for r in rows]


def run_sql(sql):
    con = get_connection()
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [list(r) for r in cur.fetchmany(ROW_CAP)]
    return cols, rows


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


def ask_gemini(question, api_key):
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
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            headers={"content-type": "application/json", "x-goog-api-key": api_key},
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": question}]}],
                "generationConfig": {"maxOutputTokens": 4096, "temperature": 0},
            },
            timeout=60,
        )
    except requests.RequestException:
        raise RuntimeError("Could not reach the language model. Try again.")

    if resp.status_code != 200:
        raise RuntimeError(f"Language model returned {resp.status_code}.")

    payload = resp.json()
    candidates = payload.get("candidates", [])
    text = "".join(
        p.get("text", "")
        for c in candidates
        for p in c.get("content", {}).get("parts", [])
    )
    finish = candidates[0].get("finishReason") if candidates else None
    if finish == "MAX_TOKENS":
        raise RuntimeError(
            "The model ran out of output budget before finishing the SQL. "
            "Try a shorter or simpler question."
        )
    if not text.strip():
        raise RuntimeError("The model returned no SQL. Try rephrasing the question.")

    sql = re.sub(r"^```(?:sql)?|```$", "", text.strip(), flags=re.M).strip()
    return validate_sql(sql)


def money_format_df(cols, rows):
    df = pd.DataFrame(rows, columns=cols)
    money_cols = [c for c in cols if re.search(r"amount|total|spend|paid", c, re.I)]
    fmt = {c: "${:,.2f}".format for c in money_cols if pd.api.types.is_numeric_dtype(df[c])}
    return df.style.format(fmt) if fmt else df


# ------------------------------------------------------------------- state ---

if "question" not in st.session_state:
    st.session_state["question"] = ""
if "kw_desc" not in st.session_state:
    st.session_state["kw_desc"] = ""
if "kw_vend" not in st.session_state:
    st.session_state["kw_vend"] = ""


# --------------------------------------------------------------------- ui ---

years = get_years()
total_txns = sum(n for _, n in years)
year_values = sorted(y for y, _ in years)

st.title("P-Card Audit Console")
st.caption("Oklahoma State University · internal audit")
st.caption(f"{total_txns:,} transactions · {year_values[0]}–{year_values[-1]}")

tab_ask, tab_dash = st.tabs(["Ask a question", "Prohibited purchases"])

with tab_ask:
    st.subheader("Ask a question in plain English")
    st.markdown(
        "Your question is translated into a SQLite query, run against the "
        "transaction file, and returned with the query shown so you can "
        "verify what was actually asked. **Read the SQL before you rely on "
        "the numbers** — a question that reads clearly can still translate "
        "into the wrong filter."
    )

    st.text_area(
        "Your question",
        placeholder="Which cardholders spent more than $50,000 in 2014?",
        key="question",
    )

    def use_preset_question(preset):
        st.session_state["question"] = preset

    ask_presets = [
        "Which cardholders spent more than $50,000 in 2014?",
        "Show every 2014 transaction over $5,000",
        "Top 20 vendors by total 2014 spending",
        "Which cardholders bought from liquor stores in 2014?",
    ]
    chip_cols = st.columns(len(ask_presets))
    for col, preset in zip(chip_cols, ask_presets):
        col.button(preset, key=f"ask-chip-{preset}", use_container_width=True,
                   on_click=use_preset_question, args=(preset,))

    run_clicked = st.button("Run question", type="primary")

    if run_clicked:
        api_key = get_secret("GEMINI_API_KEY")
        question = st.session_state["question"].strip()
        if not api_key:
            st.error(
                "No API key is configured on the server. This is the app "
                "owner's setup issue, not yours — nothing you can fix here."
            )
        elif not question:
            st.warning("Type a question first.")
        else:
            with st.spinner("Running…"):
                try:
                    sql = ask_gemini(question, api_key)
                    st.session_state["ask_sql"] = sql
                    st.session_state["ask_error"] = None
                    try:
                        cols, rows = run_sql(sql)
                        st.session_state["ask_result"] = (cols, rows)
                    except sqlite3.Error as e:
                        st.session_state["ask_result"] = None
                        st.session_state["ask_error"] = (
                            f"The generated query did not run: {e}. Try rephrasing."
                        )
                except RuntimeError as e:
                    st.session_state["ask_sql"] = None
                    st.session_state["ask_result"] = None
                    st.session_state["ask_error"] = str(e)

    if st.session_state.get("ask_sql"):
        st.markdown("**Query that ran**")
        st.code(st.session_state["ask_sql"], language="sql")

    if st.session_state.get("ask_error"):
        st.error(st.session_state["ask_error"])

    if st.session_state.get("ask_result"):
        cols, rows = st.session_state["ask_result"]
        truncated = len(rows) >= ROW_CAP
        if rows:
            st.caption(f"{len(rows)} row{'s' if len(rows) != 1 else ''}{' (capped at 500)' if truncated else ''}")
            st.dataframe(money_format_df(cols, rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No rows matched. Try rephrasing, or widen the year.")

with tab_dash:
    st.subheader("Search for prohibited purchases")
    st.markdown(
        "OSU prohibits P-Card use for alcohol, cash advances, decorations, "
        "donations and sponsorships, gasoline, gift cards, insurance, late "
        "fees, postage, moving expenses, personal purchases, individual "
        "memberships, salaries and benefits, and service or incentive "
        "awards. Two search routes are offered because these purchases "
        "hide in different places."
    )
    st.markdown(
        "1. Pick the year you are auditing.\n"
        "2. Search the **description** for what was bought — this catches "
        "line-item text such as “gift card”, and is the better "
        "route for alcohol, awards and decorations.\n"
        "3. Search the **vendor** for who was paid — this catches "
        "merchants such as USPS or a liquor store even when the "
        "description is only “GENERAL PURCHASE”, which is the "
        "case for two thirds of all rows.\n"
        "4. Review the results, then pull the receipt and the business "
        "purpose recorded in Works for anything you flag. A hit is a "
        "question, not a finding."
    )

    year = st.selectbox("Year", options=sorted(year_values, reverse=True),
                         index=sorted(year_values, reverse=True).index(2014) if 2014 in year_values else 0)

    def run_search(field, keyword):
        if not keyword.strip():
            st.session_state["dash_error"] = "Enter a keyword to search for."
            st.session_state["dash_result"] = None
            return
        sql = (
            f"SELECT TransactionDate, PostedDate, FullName, Vendor, Description, "
            f"Amount, MCC FROM pcards WHERE Year = ? AND UPPER({field}) LIKE ? "
            f"ORDER BY Amount DESC LIMIT {ROW_CAP}"
        )
        con = get_connection()
        cur = con.execute(sql, (year, f"%{keyword.strip().upper()}%"))
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        total = round(sum(r[cols.index("Amount")] for r in rows), 2)
        st.session_state["dash_error"] = None
        st.session_state["dash_result"] = (field, keyword.strip(), cols, rows, total)

    desc_presets = ["gift card", "gift certificate", "alcohol", "wine", "beer", "flowers",
                    "decoration", "donation", "sponsorship", "membership", "dues", "award",
                    "plaque", "late fee", "insurance", "postage", "gasoline", "fuel", "moving"]
    vend_presets = ["usps", "post office", "pitney", "liquor", "wine", "spirits", "atm", "cash",
                    "florist", "flowers", "shell", "exxon", "conoco", "country club", "fitness",
                    "united way", "red cross", "moving", "u-haul"]

    def use_preset_search(field, state_key, word):
        st.session_state[state_key] = word
        run_search(field, word)

    with st.container(border=True):
        st.markdown("**Description search** — searches the merchant's line-item text for what was purchased.")
        c1, c2 = st.columns([4, 1])
        c1.text_input("Keyword in description", placeholder="gift card", key="kw_desc")
        if c2.button("Search descriptions", use_container_width=True):
            run_search("Description", st.session_state["kw_desc"])
        chip_rows = [desc_presets[i:i + 7] for i in range(0, len(desc_presets), 7)]
        for row in chip_rows:
            cols = st.columns(len(row))
            for col, word in zip(cols, row):
                col.button(word, key=f"desc-chip-{word}", use_container_width=True,
                           on_click=use_preset_search, args=("Description", "kw_desc", word))

    with st.container(border=True):
        st.markdown("**Vendor search** — searches the merchant name for who was paid.")
        c1, c2 = st.columns([4, 1])
        c1.text_input("Keyword in vendor name", placeholder="usps", key="kw_vend")
        if c2.button("Search vendors", use_container_width=True):
            run_search("Vendor", st.session_state["kw_vend"])
        chip_rows = [vend_presets[i:i + 7] for i in range(0, len(vend_presets), 7)]
        for row in chip_rows:
            cols = st.columns(len(row))
            for col, word in zip(cols, row):
                col.button(word, key=f"vend-chip-{word}", use_container_width=True,
                           on_click=use_preset_search, args=("Vendor", "kw_vend", word))

    if st.session_state.get("dash_error"):
        st.error(st.session_state["dash_error"])

    if st.session_state.get("dash_result"):
        field, keyword, cols, rows, total = st.session_state["dash_result"]
        if not rows:
            st.caption(f"No {field.lower()} in {year} contains “{keyword}”.")
        else:
            truncated = len(rows) >= ROW_CAP
            st.warning(
                f"{len(rows)}{'+' if truncated else ''} transaction{'s' if len(rows) != 1 else ''} "
                f"matched “{keyword}”, totalling ${total:,.2f} — review each before "
                f"treating it as an exception."
            )
            st.dataframe(money_format_df(cols, rows), use_container_width=True, hide_index=True)

st.divider()
st.caption(
    "Every hit is a question, not a finding. Legitimate explanations exist for most "
    "flagged transactions — laboratory alcohol reagents, bulk agricultural fuel, "
    "institutional memberships coded to charitable merchant categories. Pull the "
    "receipt and the business purpose recorded in Works before treating anything "
    "as an exception."
)
