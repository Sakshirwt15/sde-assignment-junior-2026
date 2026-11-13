from __future__ import annotations

import csv
import json
from pathlib import Path


from solution.cli import main
from solution.validators import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    parse_timestamp_to_ist,
)

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


def test_phone_normalization():
    assert normalize_phone("9876543210") == "+919876543210"
    assert normalize_phone("+91 98765 43210") == "+919876543210"
    assert normalize_phone("09876543210") == "+919876543210"
    assert normalize_phone("91-98765-43210") == "+919876543210"
    assert normalize_phone("(+91) 6123456789") == "+916123456789"
    # Invalid numbers
    assert normalize_phone("5876543210") is None   # starts with 5
    assert normalize_phone("987654321") is None    # 9 digits
    assert normalize_phone("022-2345-6789") is None  # landline
    assert normalize_phone("+1 415 555 0134") is None
    assert normalize_phone("N/A") is None


def test_date_normalization():
    assert normalize_date("2026-10-15") == "2026-10-15"
    assert normalize_date("15/10/2026") == "2026-10-15"
    assert normalize_date("15-10-2026") == "2026-10-15"
    assert normalize_date("01-Dec-2026") == "2026-12-01"
    assert normalize_date("01-dec-2026") == "2026-12-01"
    # Impossible dates
    assert normalize_date("31/02/2026") is None
    assert normalize_date("2026-13-01") is None
    assert normalize_date("TBD") is None


def test_decimal_and_headers():
    assert normalize_decimal("₹12,500") == "12500.00"
    assert normalize_decimal("Rs. 4500.5") == "4500.50"
    assert normalize_decimal("INR 999.995") == "1000.00"
    assert normalize_decimal("-10") is None
    assert normalize_decimal("N/A") is None

    assert normalize_header("  Mobile__No  ") == "mobile no"
    assert normalize_header("Insured_Name") == "insured name"
    assert normalize_enum(" HI ", ["en", "hi"]) == "hi"
    assert normalize_enum("hindi", ["en", "hi"]) is None


def test_timestamp_ist_conversion():
    # 2026-10-05T19:00:00Z -> 2026-10-06 00:30:00+05:30 (next IST date!)
    _, ist_dt = parse_timestamp_to_ist("2026-10-05T19:00:00Z")
    assert ist_dt.strftime("%Y-%m-%d") == "2026-10-06"

    # Naive timestamp means IST
    _, ist_naive = parse_timestamp_to_ist("2026-10-03 10:01:00")
    assert ist_naive.isoformat() == "2026-10-03T10:01:00+05:30"


def test_end_to_end_idempotency_orphans_overrides_and_buckets(tmp_path: Path):
    db_path = tmp_path / "test.db"

    # Create drop_1:
    # - Lead L1 (9876543210): gets call_1 with disposition.set=callback_requested
    # - Lead L2 (8876543210): gets call_2 with telephony_failed -> bucket in_progress_telephony_failed
    # - Lead L3 (7876543210): no calls -> bucket yet_to_reach_out
    # - Lead L4 (6876543210): gets call_3 connected, no terminal disposition -> in_progress_dialed
    # - Event for Lead L5 (9123456789) arrives BEFORE Lead L5 is uploaded (orphan in drop_1!)
    drop1 = tmp_path / "drop_1"
    (drop1 / "leads" / "motor_renewal").mkdir(parents=True)
    (drop1 / "events").mkdir(parents=True)

    with (drop1 / "leads" / "motor_renewal" / "batch_001.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["customer_name", "phone", "policy_number", "premium_amount"])
        w.writerow(["Aarav Sharma", "9876543210", "POL-1", "₹10,000"])
        w.writerow(["", "", "", ""])  # blank row -> ignored
        w.writerow(["Diya Nair", "08876543210", "POL-2", "invalid_amt"])  # warning, valid lead
        w.writerow(["Rohan Das", "+91 78765 43210", "POL-3", "5000"])
        w.writerow(["Priya Iyer", "91-68765-43210", "POL-4", "6000"])
        w.writerow(["Bad Phone", "12345", "POL-BAD", "1000"])  # rejected: invalid_phone

    events_d1 = [
        # L1 call_1
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-01T10:00:00+05:30", "phone": "9876543210", "payload": {}},
        {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-01T10:00:10+05:30", "phone": "9876543210", "payload": {}},
        {"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-01T10:02:00+05:30", "phone": "9876543210",
         "payload": {"disposition_code": "callback_requested"}},
        # L2 call_2 (telephony failed)
        {"event_id": "e4", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c2", "occurred_at": "2026-10-01T11:00:00+05:30", "phone": "8876543210", "payload": {}},
        {"event_id": "e5", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal",
         "call_id": "c2", "occurred_at": "2026-10-01T11:00:02+05:30", "phone": "8876543210",
         "payload": {"reason": "sip_503"}},
        # L4 call_3 (connected, non-terminal)
        {"event_id": "e6", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c3", "occurred_at": "2026-10-01T12:00:00+05:30", "phone": "6876543210", "payload": {}},
        {"event_id": "e7", "event_type": "call.connected", "workflow_id": "motor_renewal",
         "call_id": "c3", "occurred_at": "2026-10-01T12:00:05+05:30", "phone": "6876543210", "payload": {}},
        # Early event for L5 (arrives before L5 lead row!)
        {"event_id": "e8", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c5", "occurred_at": "2026-10-01T13:00:00+05:30", "phone": "9123456789", "payload": {}},
        {"event_id": "e9", "event_type": "call.connected", "workflow_id": "motor_renewal",
         "call_id": "c5", "occurred_at": "2026-10-01T13:00:10+05:30", "phone": "9123456789", "payload": {}},
        {"event_id": "e10", "event_type": "disposition.set", "workflow_id": "motor_renewal",
         "call_id": "c5", "occurred_at": "2026-10-01T13:02:00+05:30", "phone": "9123456789",
         "payload": {"disposition_code": "renewed"}},
    ]
    with (drop1 / "events" / "events_0001.jsonl").open("w", encoding="utf-8") as f:
        for ev in events_d1:
            f.write(json.dumps(ev) + "\n")
        f.write("{corrupt_json_line\n")

    main(["--db", str(db_path), "--schemas", str(SCHEMAS_DIR), "ingest", str(drop1)])
    # Re-ingesting drop_1 must be completely idempotent
    main(["--db", str(db_path), "--schemas", str(SCHEMAS_DIR), "ingest", str(drop1)])

    rep1 = tmp_path / "rep1"
    main(["--db", str(db_path), "--schemas", str(SCHEMAS_DIR), "report", "--out", str(rep1)])

    dq1 = json.loads((rep1 / "data_quality.json").read_text(encoding="utf-8"))
    assert dq1["lead_rows_total"] == 5
    assert dq1["lead_rows_rejected"] == 1
    assert dq1["field_warnings"] == 1
    assert dq1["malformed_events"] == 1
    assert dq1["orphan_events"] == 3  # e8, e9, e10 are orphans right now

    # Create drop_2:
    # - Uploads Lead L5 (9123456789) -> resolves those 3 orphan events!
    # - Duplicate row for L1 -> merged_duplicates == 1, batch_id remains batch_001
    # - Supervisor override on c1 (no phone!) -> changes effective disposition to "renewed" (terminal + success)
    drop2 = tmp_path / "drop_2"
    (drop2 / "leads" / "motor_renewal").mkdir(parents=True)
    (drop2 / "events").mkdir(parents=True)

    with (drop2 / "leads" / "motor_renewal" / "batch_003.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Insured Name", "Mobile No", "Policy No"])
        w.writerow(["Kavya Menon", "9123456789", "POL-5"])
        w.writerow(["Aarav Sharma", "9876543210", "POL-1"])  # duplicate lead

    events_d2 = [
        # Duplicate e1 from drop_1
        events_d1[0],
        # Override on c1 (no phone field!)
        {"event_id": "e11", "event_type": "disposition.override", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-02T09:00:00+05:30",
         "payload": {"disposition_code": "renewed", "overridden_by": "sup_1", "reason": "qa"}},
    ]
    with (drop2 / "events" / "events_1001.jsonl").open("w", encoding="utf-8") as f:
        for ev in events_d2:
            f.write(json.dumps(ev) + "\n")

    main(["--db", str(db_path), "--schemas", str(SCHEMAS_DIR), "ingest", str(drop2)])
    rep2 = tmp_path / "rep2"
    main(["--db", str(db_path), "--schemas", str(SCHEMAS_DIR), "report", "--out", str(rep2)])

    dq2 = json.loads((rep2 / "data_quality.json").read_text(encoding="utf-8"))
    assert dq2["orphan_events"] == 0  # L5 arrived, orphans resolved!
    assert dq2["duplicate_events_dropped"] == 1
    assert dq2["duplicate_lead_rows_merged"] == 1

    # Verify lead_report.csv buckets and overrides
    with (rep2 / "lead_report.csv").open("r", encoding="utf-8") as f:
        leads_out = {r["external_ref"]: r for r in csv.DictReader(f)}

    assert leads_out["POL-1"]["bucket"] == "completed"
    assert leads_out["POL-1"]["current_disposition"] == "renewed"
    assert leads_out["POL-1"]["ever_success"] == "true"
    assert leads_out["POL-1"]["batch_id"] == "batch_001"  # first batch preserved
    assert leads_out["POL-1"]["phone_masked"] == "+91******3210"

    assert leads_out["POL-2"]["bucket"] == "in_progress_telephony_failed"
    assert leads_out["POL-3"]["bucket"] == "yet_to_reach_out"
    assert leads_out["POL-4"]["bucket"] == "in_progress_dialed"
    assert leads_out["POL-5"]["bucket"] == "completed"
    assert leads_out["POL-5"]["ever_success"] == "true"

    # Verify no plaintext PII in sqlite file bytes
    db_bytes = db_path.read_bytes()
    assert b"9876543210" not in db_bytes
    assert b"Aarav Sharma" not in db_bytes
    assert b"Kavya Menon" not in db_bytes
