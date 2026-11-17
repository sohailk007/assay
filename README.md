# Assay

TrainifyHub lead validation. Evidence before contact.

An assay is the test that establishes what a metal really is before anyone
stamps it. UK hallmarking then adds a sponsor's mark and a date letter, so the
stamp says not just what the thing is but who vouched for it and when. That is
what this does to a company record.

Assay builds TrainifyHub's own UK construction lead list from the free
Companies House register, records a source URL and a date for every single
fact, and refuses to let anything reach a dialler or an inbox until a named
person has signed it off.

This is the Python implementation of the founder workbook dated 3 Nov 2026.
The 13 protocol stages, the 100-point scoring rules and the two-channel split
all come from that document.

## The one idea to hold onto

The system produces evidence, never permission.

It can tell you that a company exists, trades in roofing, and published a
phone number on its own contact page last month. It cannot tell you that you
are allowed to ring it. A person decides that, with their name attached, and
the database physically refuses to store an approval without one.

## Setup

Windows, from the project folder:

```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then open `.env` and set three things:

1. `COMPANIES_HOUSE_API_KEY` — free. Register an application at
   https://developer.company-information.service.gov.uk/ and create a REST
   API key. This is the only credential needed to run the research half.
2. `DATABASE_URL` — leave the SQLite line for the pilot, switch to Postgres
   when more than one person uses it.
3. `AUTHORISED_APPROVERS` — comma separated names. Until this has at least
   one name in it, nobody can approve anything, which is the correct
   starting state.

Create the tables:

```
python run.py init
```

## Running the pilot

Start from the 30 records already researched by hand:

```
python run.py import-workbook --path "TrainifyHub_Founder_ChatGPT_Lead_Validation_Architecture_v1_2026-11-03.xlsx"
```

Or pull a fresh cohort from the register:

```
python run.py intake --trade roofing --location birmingham --limit 50 --cohort pilot-100
python run.py identity
python run.py domain
python run.py score
python run.py route
```

`python run.py pipeline --trade roofing --location birmingham` runs all five
in sequence.

Each stage claims rows at one status and advances them, so every stage is
re-runnable on its own. If stage 4 dies halfway through, rerun stage 4.
Stages 1 to 3 are untouched.

### Screening

There is no free TPS or CTPS API. The registers are licensed and you buy
access through an approved reseller. Two routes are built in.

Manual, which is what the pilot should use because registration at tpsmydata
includes free test credits:

```
python run.py screen --export screening/to_screen.csv
```

Upload that file to the provider's bulk checker, save their result as
`screening/screened.csv` with columns `phone,tps,ctps`, then:

```
python run.py screen --import screening/screened.csv --receipt "tpsmydata bulk 2026-11-16 ref 12345"
```

The receipt string is what you produce if anyone ever asks who screened the
number and when. Put the provider, the date and their reference in it.

API, once volume justifies it: set `SCREENING_PROVIDER=tpsmydata` and
`TPSMYDATA_API_KEY` in `.env`. Verify the response parsing first with
`python run.py screen --provider tpsmydata --test 02079460000`.

Screening expires. `SCREENING_VALID_DAYS` defaults to 28, and an expired
result is treated as no result: the gate closes on its own, and a number that
appears on TPS at the next re-screen revokes its own approval automatically.

### The dashboard

```
python run.py serve
```

Then open http://127.0.0.1:8000

This is the only place outreach can be authorised. It has no login, so keep
it on localhost or put your own authentication in front of it before anyone
outside the compliance team can reach it.

### Export

```
python run.py export --out exports/pilot.xlsx
```

Writes the master register, the evidence ledger, both channel routes and the
pilot scorecard back into the workbook format the founder already reads.

## How it is put together

```
app/
  config.py                 settings from .env, the only place env is read
  models.py                 four tables, plus suppression and audit log
  db.py                     engine and session
  scoring.py                the 100-point rules from sheet 02
  approvals.py              the ONLY code path that can authorise outreach
  sources/
    companies_house.py      official register, rate limited
    website.py              domain discovery, verification, contact reading
  screening/
    base.py                 the provider contract
    manual.py               CSV round trip
    tpsmydata.py            API adapter
  stages/
    s00_import_workbook.py  load the existing 30 records
    s01_intake.py           pull a trade and geography
    s02_identity.py         re-check the live register
    s04_domain.py           stages 03 to 05 in one network pass
    s08_score.py            evidence confidence
    s09_route.py            the two gates
    s11_screen.py           TPS and CTPS
  web/app.py                review dashboard
  export/to_excel.py        back to the workbook
tests/test_gates.py         22 tests, no network needed
run.py                      CLI
```

### The four tables

`companies` is one row per company number. Never keyed on a name, because
two companies can share a name and one company can change its name.

`evidence` is one row per observed fact, each with its own source URL and
checked date. Nothing is overwritten: a changed value supersedes the old row,
so the ledger still shows what was believed before and when it changed.

`email_gate` and `call_gate` are separate tables on purpose. Email marketing
and live marketing calls are governed by different law, so they share no
clearance field. There is no `approved` column on `companies` and there never
should be. Clearing one route does not clear the other, and a test enforces
exactly that.

### Why approval is a database constraint

Application logic can be bypassed by a bug, a migration or a stray UPDATE.
So the rule lives in the schema:

```sql
CHECK (human_decision <> 'APPROVED'
       OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
```

and on the call gate additionally:

```sql
CHECK (human_decision <> 'APPROVED'
       OR (tps_result = 'CLEAR' AND ctps_result = 'CLEAR'
           AND screened_at IS NOT NULL))
```

Two tests do nothing but try to write an approval around `app/approvals.py`
and assert that the database rejects it.

Note for SQLite: `app/db.py` turns on `PRAGMA foreign_keys` per connection.
Without it SQLite ignores those constraints and they become decoration.

### What the score is not

The 100-point score measures how well evidenced a record is. It prioritises
research. It does not open either gate, and `s09_route.py` never reads it. An
85 does not authorise a call and a 30 does not forbid one.

The scoring is validated against the founder workbook: importing those 30
records and scoring them reproduces all 30 of the workbook's own totals
exactly, including the 62 for Tread Safe and the 5 for the company with an
active proposal to strike off.

### Three things that override everything

1. An adverse Companies House status, including an active proposal to strike
   off. Both gates hold.
2. An entry in the `suppression` table. It beats a clear screening result,
   permanently.
3. Screening older than `SCREENING_VALID_DAYS`, which counts as no screening.

## Deliberate limits

This code does not scrape LinkedIn for named contacts. That breaches their
terms and is the largest legal exposure in the whole plan. Buyer mapping is a
human field in the dashboard.

It does not guess email addresses. `firstname.lastname@` patterns are not
evidence. If a company does not publish a mailbox, the record has no mailbox.

It only marks a website as LINKED when the site itself carries the company
number. A matching name or a similar logo produces CANDIDATE and waits for a
person, which is what the protocol requires.

It respects `robots.txt` and waits `CRAWL_DELAY_SECONDS` between requests to
the same host.

## Tests

```
python -m pytest tests -q
```

22 tests, all offline. They cover the scoring against the workbook's own
numbers, both gates holding for the right reasons, TPS being irrelevant to
the email route, suppression beating a clear screen, expiry closing a gate
that was open, approval requiring a listed name, approving one channel
leaving the other untouched, and the database rejecting an approval written
around the approvals module.
