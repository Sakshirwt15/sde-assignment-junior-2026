from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


def generate_reports(conn: sqlite3.Connection, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load disposition catalog
    catalog: dict[tuple[str, str], dict] = {}
    for row in conn.execute("SELECT * FROM dispositions_catalog").fetchall():
        catalog[(row["workflow_id"], row["code"])] = {
            "is_terminal": bool(row["is_terminal"]),
            "is_success": bool(row["is_success"]),
        }

    # Load all valid leads
    leads_by_key: dict[tuple[str, str], dict] = {}
    for row in conn.execute("SELECT * FROM leads").fetchall():
        key = (row["workflow_id"], row["phone_hash"])
        leads_by_key[key] = {
            "workflow_id": row["workflow_id"],
            "phone_hash": row["phone_hash"],
            "phone_masked": row["phone_masked"],
            "external_ref": row["external_ref"],
            "batch_id": row["batch_id"],
        }

    # Load all ledger events
    events = conn.execute("SELECT * FROM interaction_ledger").fetchall()

    # Map call_id -> (workflow_id, phone_hash) from events that carry phone_hash
    call_to_lead_key: dict[str, tuple[str, str]] = {}
    for ev in events:
        if ev["call_id"] and ev["phone_hash"]:
            call_to_lead_key[ev["call_id"]] = (ev["workflow_id"], ev["phone_hash"])

    orphan_events = 0
    unknown_disposition_events = 0

    # Group valid, linked events by call_id
    # call_id -> dict of call attributes
    calls: dict[str, dict] = defaultdict(
        lambda: {
            "workflow_id": None,
            "phone_hash": None,
            "initiated_at_utc": None,
            "initiated_at_ist": None,
            "initiated_ist_date": None,
            "connected": False,
            "telephony_failed": False,
            "disp_sets": [],       # list of (occurred_at_utc, event_id, code)
            "disp_overrides": [],  # list of (occurred_at_utc, event_id, code)
        }
    )

    for ev in events:
        wf_id = ev["workflow_id"]
        call_id = ev["call_id"]
        etype = ev["event_type"]
        p_hash = ev["phone_hash"]

        # Resolve lead key
        if p_hash:
            lead_key = (wf_id, p_hash)
        elif call_id and call_id in call_to_lead_key:
            lead_key = call_to_lead_key[call_id]
        else:
            orphan_events += 1
            continue

        if lead_key not in leads_by_key:
            orphan_events += 1
            continue

        # Check unknown disposition
        if etype in {"disposition.set", "disposition.override"}:
            payload = json.loads(ev["payload_json"])
            code = str(payload.get("disposition_code", "")).strip()
            if (wf_id, code) not in catalog:
                unknown_disposition_events += 1
                continue

        if not call_id:
            continue

        c = calls[call_id]
        c["workflow_id"] = lead_key[0]
        c["phone_hash"] = lead_key[1]

        if etype == "call.initiated":
            # Keep earliest initiated if duplicate call.initiated existed with different event_id
            if c["initiated_at_utc"] is None or ev["occurred_at_utc"] < c["initiated_at_utc"]:
                c["initiated_at_utc"] = ev["occurred_at_utc"]
                c["initiated_at_ist"] = ev["occurred_at_ist"]
                c["initiated_ist_date"] = ev["ist_date"]
        elif etype == "call.connected":
            c["connected"] = True
        elif etype == "call.telephony_failed":
            c["telephony_failed"] = True
        elif etype == "disposition.set":
            payload = json.loads(ev["payload_json"])
            code = str(payload["disposition_code"]).strip()
            c["disp_sets"].append((ev["occurred_at_utc"], ev["event_id"], code))
        elif etype == "disposition.override":
            payload = json.loads(ev["payload_json"])
            code = str(payload["disposition_code"]).strip()
            c["disp_overrides"].append((ev["occurred_at_utc"], ev["event_id"], code))

    # Filter to valid calls (must have call.initiated present and linked)
    valid_calls_by_lead: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for call_id, c in calls.items():
        if c["initiated_at_utc"] is None:
            continue
        wf_id = c["workflow_id"]
        lead_key = (wf_id, c["phone_hash"])

        # Effective disposition: latest override by occurred_at, else latest set, else None
        eff_disp: str | None = None
        if c["disp_overrides"]:
            c["disp_overrides"].sort(key=lambda x: (x[0], x[1]))
            eff_disp = c["disp_overrides"][-1][2]
        elif c["disp_sets"]:
            c["disp_sets"].sort(key=lambda x: (x[0], x[1]))
            eff_disp = c["disp_sets"][-1][2]

        disp_meta = catalog.get((wf_id, eff_disp)) if eff_disp else None
        c["call_id"] = call_id
        c["effective_disposition"] = eff_disp
        c["is_terminal"] = disp_meta["is_terminal"] if disp_meta else False
        c["is_success"] = disp_meta["is_success"] if disp_meta else False

        valid_calls_by_lead[lead_key].append(c)

    # Sort calls per lead chronologically by call.initiated (tie-break by call_id)
    for lead_key in valid_calls_by_lead:
        valid_calls_by_lead[lead_key].sort(key=lambda x: (x["initiated_at_utc"], x["call_id"]))

    # ---------------------------------------------------------
    # Compute Lead Report & Batch Summary
    # ---------------------------------------------------------
    lead_report_rows = []
    batch_stats: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {
            "total_leads": 0,
            "yet_to_reach_out": 0,
            "in_progress_telephony_failed": 0,
            "in_progress_dialed": 0,
            "completed": 0,
        }
    )

    for lead_key, lead in leads_by_key.items():
        l_calls = valid_calls_by_lead.get(lead_key, [])
        total_calls = len(l_calls)
        connected_calls = sum(1 for c in l_calls if c["connected"])

        # Bucket logic (first matching):
        # 1. yet_to_reach_out: no calls
        # 2. completed: any call's effective disposition is terminal
        # 3. in_progress_telephony_failed: every call has a call.telephony_failed event
        # 4. in_progress_dialed: everything else
        if total_calls == 0:
            bucket = "yet_to_reach_out"
        elif any(c["is_terminal"] for c in l_calls):
            bucket = "completed"
        elif all(c["telephony_failed"] for c in l_calls):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"

        last_call_at = l_calls[-1]["initiated_at_ist"] if total_calls > 0 else ""

        # current_disposition: effective disposition of most recent call (by initiated time) that has one
        current_disp = ""
        for c in reversed(l_calls):
            if c["effective_disposition"]:
                current_disp = c["effective_disposition"]
                break

        ever_success = any(c["is_success"] for c in l_calls)

        lead_report_rows.append(
            {
                "workflow_id": lead["workflow_id"],
                "external_ref": lead["external_ref"],
                "batch_id": lead["batch_id"],
                "phone_masked": lead["phone_masked"],
                "bucket": bucket,
                "total_calls": total_calls,
                "connected_calls": connected_calls,
                "last_call_at": last_call_at,
                "current_disposition": current_disp,
                "ever_success": "true" if ever_success else "false",
            }
        )

        b_key = (lead["workflow_id"], lead["batch_id"])
        batch_stats[b_key]["total_leads"] += 1
        batch_stats[b_key][bucket] += 1

    # 1. Write batch_summary.csv (sorted by workflow_id, batch_id)
    with (out_dir / "batch_summary.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "batch_id",
                "total_leads",
                "yet_to_reach_out",
                "in_progress_telephony_failed",
                "in_progress_dialed",
                "completed",
            ]
        )
        for wf_id, batch_id in sorted(batch_stats.keys()):
            st = batch_stats[(wf_id, batch_id)]
            writer.writerow(
                [
                    wf_id,
                    batch_id,
                    st["total_leads"],
                    st["yet_to_reach_out"],
                    st["in_progress_telephony_failed"],
                    st["in_progress_dialed"],
                    st["completed"],
                ]
            )

    # 2. Write daily_overview.csv (sorted by workflow_id, date)
    daily_stats: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"total_calls": 0, "connected_calls": 0, "success_lead_hashes": set()}
    )
    for lead_key, l_calls in valid_calls_by_lead.items():
        for c in l_calls:
            d_key = (c["workflow_id"], c["initiated_ist_date"])
            daily_stats[d_key]["total_calls"] += 1
            if c["connected"]:
                daily_stats[d_key]["connected_calls"] += 1
            if c["is_success"]:
                daily_stats[d_key]["success_lead_hashes"].add(c["phone_hash"])

    with (out_dir / "daily_overview.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "date",
                "total_calls",
                "connected_calls",
                "connectivity_rate",
                "success_leads",
            ]
        )
        for wf_id, ist_date in sorted(daily_stats.keys()):
            st = daily_stats[(wf_id, ist_date)]
            tot = st["total_calls"]
            conn_c = st["connected_calls"]
            rate = f"{(conn_c / tot):.4f}" if tot > 0 else "0.0000"
            writer.writerow(
                [
                    wf_id,
                    ist_date,
                    tot,
                    conn_c,
                    rate,
                    len(st["success_lead_hashes"]),
                ]
            )

    # 3. Write lead_report.csv (sorted by workflow_id, external_ref)
    lead_report_rows.sort(key=lambda r: (r["workflow_id"], r["external_ref"]))
    with (out_dir / "lead_report.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "workflow_id",
                "external_ref",
                "batch_id",
                "phone_masked",
                "bucket",
                "total_calls",
                "connected_calls",
                "last_call_at",
                "current_disposition",
                "ever_success",
            ]
        )
        for r in lead_report_rows:
            writer.writerow(
                [
                    r["workflow_id"],
                    r["external_ref"],
                    r["batch_id"],
                    r["phone_masked"],
                    r["bucket"],
                    r["total_calls"],
                    r["connected_calls"],
                    r["last_call_at"],
                    r["current_disposition"],
                    r["ever_success"],
                ]
            )

    # 4. Write rejections.csv (sorted by workflow_id, batch_id, row_number)
    rej_rows = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
        """
    ).fetchall()
    rej_by_reason: dict[str, int] = defaultdict(int)
    with (out_dir / "rejections.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
        for r in rej_rows:
            writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])
            rej_by_reason[r["reason"]] += 1

    # 5. Write data_quality.json
    metrics = {
        row["metric_key"]: row["metric_value"]
        for row in conn.execute("SELECT metric_key, metric_value FROM ingestion_metrics").fetchall()
    }
    dq = {
        "lead_rows_total": metrics.get("lead_rows_total", 0),
        "lead_rows_rejected": metrics.get("lead_rows_rejected", 0),
        "rejections_by_reason": dict(sorted(rej_by_reason.items())),
        "duplicate_lead_rows_merged": metrics.get("duplicate_lead_rows_merged", 0),
        "field_warnings": metrics.get("field_warnings", 0),
        "events_total": metrics.get("events_total", 0),
        "malformed_events": metrics.get("malformed_events", 0),
        "duplicate_events_dropped": metrics.get("duplicate_events_dropped", 0),
        "orphan_events": orphan_events,
        "unknown_disposition_events": unknown_disposition_events,
    }
    with (out_dir / "data_quality.json").open("w", encoding="utf-8") as f:
        json.dump(dq, f, indent=2)
        f.write("\n")
