from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import hash_pii, mask_phone
from .validators import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    parse_timestamp_to_ist,
)


def _inc_metric(conn: sqlite3.Connection, key: str, delta: int = 1) -> None:
    if delta == 0:
        return
    conn.execute(
        """
        INSERT INTO ingestion_metrics (metric_key, metric_value)
        VALUES (?, ?)
        ON CONFLICT(metric_key) DO UPDATE SET metric_value = metric_value + excluded.metric_value
        """,
        (key, delta),
    )


def load_schemas_and_catalog(conn: sqlite3.Connection, schemas_dir: Path) -> dict[str, dict]:
    """Load workflow JSON schemas and dispositions.csv into memory and DB."""
    schemas: dict[str, dict] = {}
    for json_file in sorted(schemas_dir.glob("*.json")):
        data = json.loads(json_file.read_text(encoding="utf-8"))
        wf_id = data["workflow_id"]
        header_lookup: dict[str, str] = {}
        for field in data["fields"]:
            candidates = [field["name"]] + field.get("aliases", [])
            for cand in candidates:
                header_lookup[normalize_header(cand)] = field["name"]
        data["_header_lookup"] = header_lookup
        schemas[wf_id] = data

    disp_csv = schemas_dir / "dispositions.csv"
    if disp_csv.exists():
        with disp_csv.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                conn.execute(
                    """
                    INSERT INTO dispositions_catalog (workflow_id, code, label, is_terminal, is_success)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id, code) DO UPDATE SET
                        label = excluded.label,
                        is_terminal = excluded.is_terminal,
                        is_success = excluded.is_success
                    """,
                    (
                        row["workflow_id"].strip(),
                        row["code"].strip(),
                        row["label"].strip(),
                        1 if row["is_terminal"].strip().lower() == "true" else 0,
                        1 if row["is_success"].strip().lower() == "true" else 0,
                    ),
                )
    return schemas


def _cast_field(field_spec: dict, raw_val: str) -> tuple[bool, str | None]:
    """
    Returns (is_valid, normalized_value).
    raw_val is already stripped and non-empty.
    """
    ftype = field_spec["type"]
    if ftype == "string":
        return True, raw_val
    if ftype == "phone":
        norm = normalize_phone(raw_val)
        return (norm is not None), norm
    if ftype == "date":
        norm = normalize_date(raw_val)
        return (norm is not None), norm
    if ftype == "decimal":
        norm = normalize_decimal(raw_val)
        return (norm is not None), norm
    if ftype == "enum":
        norm = normalize_enum(raw_val, field_spec.get("values", []))
        return (norm is not None), norm
    return True, raw_val


def ingest_drop(conn: sqlite3.Connection, drop_dir: Path, schemas_dir: Path) -> None:
    drop_dir = drop_dir.resolve()
    drop_key = str(drop_dir)

    # Idempotency guard at drop level: re-running ingest on the exact same drop changes nothing.
    already = conn.execute(
        "SELECT 1 FROM ingested_drops WHERE drop_path = ?", (drop_key,)
    ).fetchone()
    if already:
        return

    schemas = load_schemas_and_catalog(conn, schemas_dir)
    drop_name = drop_dir.name

    # Counters for this drop
    lead_rows_total = 0
    lead_rows_rejected = 0
    duplicate_lead_rows_merged = 0
    field_warnings = 0

    # 1. Ingest leads in workflow_id order, then filename order, then row order
    leads_root = drop_dir / "leads"
    if leads_root.exists():
        for wf_dir in sorted([p for p in leads_root.iterdir() if p.is_dir()], key=lambda p: p.name):
            wf_id = wf_dir.name
            if wf_id not in schemas:
                continue
            schema = schemas[wf_id]
            fields = schema["fields"]
            header_lookup = schema["_header_lookup"]
            ref_field_name = next(f["name"] for f in fields if f.get("reference"))

            for csv_file in sorted(wf_dir.glob("*.csv"), key=lambda p: p.name):
                batch_id = csv_file.stem
                with csv_file.open("r", encoding="utf-8", newline="") as f:
                    reader = csv.reader(f)
                    raw_headers = next(reader, None)
                    if raw_headers is None:
                        continue
                    col_to_field: dict[int, str] = {}
                    for idx, h in enumerate(raw_headers):
                        norm_h = normalize_header(h)
                        if norm_h in header_lookup:
                            col_to_field[idx] = header_lookup[norm_h]

                    row_num = 0
                    for raw_row in reader:
                        # Skip blank rows (every cell empty or whitespace) and do not count
                        if all(cell.strip() == "" for cell in raw_row):
                            continue
                        row_num += 1
                        lead_rows_total += 1

                        mapped_raw: dict[str, str | None] = {fld["name"]: None for fld in fields}
                        for idx, cell in enumerate(raw_row):
                            if idx in col_to_field:
                                val = cell.strip()
                                mapped_raw[col_to_field[idx]] = val if val != "" else None

                        # Validate fields in schema order
                        rejection_reason: str | None = None
                        row_warnings = 0
                        parsed_values: dict[str, str | None] = {}

                        for fld in fields:
                            fname = fld["name"]
                            raw_val = mapped_raw.get(fname)
                            required = bool(fld.get("required", False))

                            if raw_val is None:
                                if required:
                                    rejection_reason = f"missing_required:{fname}"
                                    break
                                parsed_values[fname] = None
                                continue

                            is_valid, norm_val = _cast_field(fld, raw_val)
                            if not is_valid:
                                if fname == "phone":
                                    rejection_reason = "invalid_phone"
                                    break
                                if required:
                                    rejection_reason = f"invalid_{fname}"
                                    break
                                # Optional field present but invalid -> store null and count warning
                                parsed_values[fname] = None
                                row_warnings += 1
                            else:
                                parsed_values[fname] = norm_val

                        if rejection_reason is not None:
                            lead_rows_rejected += 1
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO rejections
                                (drop_name, workflow_id, batch_id, row_number, reason)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (drop_name, wf_id, batch_id, row_num, rejection_reason),
                            )
                            continue

                        # Valid row
                        field_warnings += row_warnings
                        canonical_phone = parsed_values["phone"]
                        assert canonical_phone is not None
                        p_hash = hash_pii(canonical_phone)
                        p_mask = mask_phone(canonical_phone)
                        name_hash = hash_pii(parsed_values["customer_name"] or "")
                        ext_ref = parsed_values[ref_field_name] or ""

                        # Non-PII non-primary fields stored in extra_fields_json
                        extra = {
                            fld["name"]: parsed_values[fld["name"]]
                            for fld in fields
                            if fld["name"] not in {"phone", "customer_name", ref_field_name}
                        }

                        existing = conn.execute(
                            """
                            SELECT customer_name_hash, external_ref, extra_fields_json
                            FROM leads
                            WHERE workflow_id = ? AND phone_hash = ?
                            """,
                            (wf_id, p_hash),
                        ).fetchone()

                        if existing is None:
                            # Apply defaults for null fields on initial creation?
                            # Wait: spec says defaults apply after merging, to fields that are still null.
                            # We store nulls during ingestion and apply defaults on read/report (or update after merge).
                            conn.execute(
                                """
                                INSERT INTO leads (
                                    workflow_id, phone_hash, phone_masked,
                                    customer_name_hash, external_ref, batch_id, extra_fields_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    wf_id,
                                    p_hash,
                                    p_mask,
                                    name_hash,
                                    ext_ref,
                                    batch_id,
                                    json.dumps(extra, sort_keys=True),
                                ),
                            )
                        else:
                            duplicate_lead_rows_merged += 1
                            merged_extra = json.loads(existing["extra_fields_json"])
                            for k, v in extra.items():
                                if v is not None:
                                    merged_extra[k] = v
                            new_name_hash = name_hash if parsed_values["customer_name"] is not None else existing["customer_name_hash"]
                            new_ext_ref = ext_ref if parsed_values[ref_field_name] is not None else existing["external_ref"]
                            conn.execute(
                                """
                                UPDATE leads
                                SET customer_name_hash = ?,
                                    external_ref = ?,
                                    extra_fields_json = ?
                                WHERE workflow_id = ? AND phone_hash = ?
                                """,
                                (
                                    new_name_hash,
                                    new_ext_ref,
                                    json.dumps(merged_extra, sort_keys=True),
                                    wf_id,
                                    p_hash,
                                ),
                            )

    # Apply schema defaults to any still-null fields across leads
    for wf_id, schema in schemas.items():
        defaults = {
            fld["name"]: fld["default"]
            for fld in schema["fields"]
            if "default" in fld and fld["name"] not in {"phone", "customer_name"}
        }
        if not defaults:
            continue
        rows = conn.execute(
            "SELECT phone_hash, extra_fields_json FROM leads WHERE workflow_id = ?", (wf_id,)
        ).fetchall()
        for r in rows:
            extra = json.loads(r["extra_fields_json"])
            changed = False
            for k, dval in defaults.items():
                if extra.get(k) is None:
                    extra[k] = dval
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE leads SET extra_fields_json = ? WHERE workflow_id = ? AND phone_hash = ?",
                    (json.dumps(extra, sort_keys=True), wf_id, r["phone_hash"]),
                )

    # 2. Ingest events into append-only interaction_ledger
    events_total = 0
    malformed_events = 0
    duplicate_events_dropped = 0

    events_root = drop_dir / "events"
    if events_root.exists():
        for jsonl_file in sorted(events_root.glob("*.jsonl"), key=lambda p: p.name):
            with jsonl_file.open("r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    events_total += 1
                    try:
                        ev = json.loads(stripped)
                    except json.JSONDecodeError:
                        malformed_events += 1
                        continue
                    if not isinstance(ev, dict) or not ev.get("event_id"):
                        malformed_events += 1
                        continue

                    event_id = str(ev["event_id"]).strip()
                    event_type = str(ev.get("event_type", "")).strip()
                    workflow_id = str(ev.get("workflow_id", "")).strip()
                    call_id = str(ev["call_id"]).strip() if ev.get("call_id") else None
                    raw_phone = ev.get("phone")
                    norm_phone = normalize_phone(raw_phone) if raw_phone is not None else None
                    p_hash = hash_pii(norm_phone) if norm_phone else None

                    try:
                        utc_dt, ist_dt = parse_timestamp_to_ist(str(ev["occurred_at"]))
                    except Exception:
                        malformed_events += 1
                        continue

                    payload_json = json.dumps(ev.get("payload") or {}, sort_keys=True)

                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO interaction_ledger (
                            event_id, event_type, workflow_id, call_id, phone_hash,
                            occurred_at_utc, occurred_at_ist, ist_date, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            event_type,
                            workflow_id,
                            call_id,
                            p_hash,
                            utc_dt.isoformat(),
                            ist_dt.isoformat(),
                            ist_dt.strftime("%Y-%m-%d"),
                            payload_json,
                        ),
                    )
                    if cur.rowcount == 0:
                        duplicate_events_dropped += 1

    _inc_metric(conn, "lead_rows_total", lead_rows_total)
    _inc_metric(conn, "lead_rows_rejected", lead_rows_rejected)
    _inc_metric(conn, "duplicate_lead_rows_merged", duplicate_lead_rows_merged)
    _inc_metric(conn, "field_warnings", field_warnings)
    _inc_metric(conn, "events_total", events_total)
    _inc_metric(conn, "malformed_events", malformed_events)
    _inc_metric(conn, "duplicate_events_dropped", duplicate_events_dropped)

    conn.execute(
        "INSERT INTO ingested_drops (drop_path, ingested_at) VALUES (?, ?)",
        (drop_key, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
