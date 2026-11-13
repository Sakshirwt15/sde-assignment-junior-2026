# Submission

## Candidate
- **Name:** Sakshi Rawat
- **Assignment chosen:** Assignment A (Data — Interaction Ledger & Reporting Pipeline)
- **Time spent (honest estimate):** ~7 hours

## How to run
```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Generate synthetic data (do not commit data/, reports/, or *.db)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Run the pipeline incrementally on both drops and generate reports
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/

# 5. Run the test suite
pytest
```

## Design
I kept the codebase clean and modular inside `solution/` so each part has a clear responsibility:
- **`solution/db.py`**: Sets up the SQLite database (`ledger.db`) in WAL mode and handles PII hashing and masking.
- **`solution/validators.py`**: Cleans messy CSV inputs—normalizes headers, parses 6 Indian phone number formats into `+91XXXXXXXXXX`, handles day-first dates, cleans currency symbols, and converts timestamps to IST.
- **`solution/ingest.py`**: Reads lead CSVs and event JSONL files in deterministic order (`workflow_id` -> filename -> row order) and loads them into the database without duplicating already-processed drops or events.
- **`solution/report.py`**: Links calls to leads, figures out the effective disposition (applying supervisor overrides if present), classifies leads into buckets, and generates all 5 required output files.

### Database Schema & PII Protection
- **`ingested_drops`**: Tracks which drop folders have already been processed so re-running `ingest` is completely safe and idempotent.
- **`leads`**: Stores unique leads keyed by `(workflow_id, phone_hash)`. To follow the PII rule strictly, I never save raw names or phone numbers in the database. Instead, I hash them using deterministic HMAC-SHA256 (`PII_PEPPER`) so I can still join events to leads, and I store `phone_masked` (`+91******XXXX`) for `lead_report.csv`.
- **`interaction_ledger`**: An append-only table for all call events (`event_id` as Primary Key). Supervisor overrides (`disposition.override`) are inserted as new rows rather than updating old rows.
- **`rejections` & `dispositions_catalog`**: Stores rejected row details (without any PII) and the workflow disposition rules.

## Decisions & assumptions
| Decision | Why I made this call | Alternative considered |
|---|---|---|
| **SQLite in WAL mode (`ledger.db`)** | Needs zero external setup for anyone running my repo, while still giving full ACID transactions and indexed SQL queries. | PostgreSQL via Docker (good for heavy production, but SQLite avoids extra container startup steps). |
| **HMAC-SHA256 hashing + masked phone column** | Guarantees zero plaintext PII at rest while keeping lead-to-event matching fast and deterministic. | Symmetric encryption (unnecessary because the reports never need to decrypt back to the full phone number). |
| **Linking orphan events at `report` time** | Events in `drop_1` can arrive before their lead is uploaded in `drop_2`. By storing all valid events in the ledger and resolving links during `report`, late-arriving leads automatically connect to their earlier events. | Moving orphans to a separate quarantine table and re-checking them on every ingest (more complex state to maintain). |
| **Applying defaults after merging duplicates** | If a later duplicate row has an empty `preferred_language`, applying the default (`en`) too early would overwrite an explicit language (`hi` or `ta`) from an earlier batch. Applying defaults after merging prevents this bug. | Applying defaults immediately while parsing each row. |
| **Tie-breaking same-second dispositions by `event_id`** | If two disposition events have the exact same `occurred_at` timestamp, sorting by `(occurred_at, event_id)` keeps the result 100% deterministic. | Relying on database insertion order. |

## Failure modes considered
- **Running `ingest` twice on the same drop:** Prevented by checking `ingested_drops` first and using `INSERT OR IGNORE` on `event_id` and `rejections`. Re-running produces the exact same database state and identical report files.
- **Dirty & messy inputs:** Blank CSV rows are skipped without affecting row numbers; corrupt JSONL lines are counted under `malformed_events`; impossible dates (`31/02/2026`), negative amounts, and bad phone numbers are caught cleanly without crashing.
- **Overrides without phone numbers:** `disposition.override` events only carry `call_id` (no phone), so I map `call_id` to `(workflow_id, phone_hash)` using the other events from that same call.
- **What is not handled:** Multiple processes running `ingest` concurrently on the exact same SQLite file at the same millisecond (in production, I would use PostgreSQL with row-level locks or a job queue).

## Testing
- Wrote automated tests in `solution/tests/test_pipeline.py` (runnable via `pytest`) covering:
  - All 6 Indian phone number formats and invalid numbers (landlines, wrong length, wrong starting digit).
  - Day-first date formats, leap/impossible dates, currency strings (`₹`, `Rs.`, `INR`), and messy headers.
  - UTC-to-IST midnight rollover (`19:00 UTC` becoming the next calendar day in IST).
  - Full multi-drop end-to-end flow: idempotent re-ingestion, cross-drop orphan resolution, supervisor overrides, bucket classification rules, and inspecting raw `ledger.db` bytes to verify zero plaintext PII.
- Also tested the pipeline on the generated dataset (`--seed 42 --scale small`) and checked all 5 output files.

## Results
- On `--seed 42 --scale small` (2,057 lead rows and 8,745 event lines across 2 drops):
  - `ingest` for both drops finishes in ~0.2 seconds.
  - `report` generates all 5 files in ~0.05 seconds.
  - Final database state: 1,892 valid deduplicated leads, 8,500 valid ledger events, and 108 rejected rows logged cleanly.

## If I had another day
1. Add a `--db-url` flag to support PostgreSQL alongside SQLite for concurrent multi-worker ingestion.
2. Push the report grouping and effective-disposition queries into SQL window functions (`ROW_NUMBER() OVER (...)`) so `--scale large` (140k+ leads) uses minimal RAM.
3. Store SHA-256 checksums of ingested CSV/JSONL files in `ingested_drops` to alert if a file inside an already-ingested folder was modified.