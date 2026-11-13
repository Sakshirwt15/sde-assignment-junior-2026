# AI Usage Disclosure

## Tools used
- Gemini AI coding assistant

## What I used them for
- Understanding the assignment requirements and clarifying edge cases like header normalization, IST timezone conversions, and handling orphan events arriving before lead uploads.
- Getting structural help for organizing the python modules (`db.py`, `validators.py`, `ingest.py`, `report.py`, `cli.py`) and setting up the SQLite schema.
- Drafting test cases for phone number and date validation in `pytest`.

## What I wrote or decided myself
- Designed the database schema and decided how to implement HMAC hashing for PII protection.
- Decided the logic for handling duplicate lead merging (ensuring defaults apply only after merging) and dynamic orphan resolution during report generation.
- Reviewed and tested all scripts line by line to ensure correct outputs and proper error handling.

## Where the AI was wrong or unhelpful
- An initial suggestion tried to apply schema field defaults during initial lead parsing before merging duplicates; I realized this would overwrite explicit language values from earlier batches and fixed it to apply defaults only after merging.

## How I verified AI-generated code
- Ran `pytest` and manually tested the CLI commands on the generated sample dataset drops (`drop_1` and `drop_2`).
- Inspected the database bytes to confirm no plaintext PII was stored and checked all generated report files against the required contracts.