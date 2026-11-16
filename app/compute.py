"""Interval computation: turn cumulative readings into account usage detail.

Pairing rule (from the specification):
  * Only two *adjacent* readings of the same account AND same meter may be
    subtracted, and only when the interval does not cross the account's
    meter-switch timestamp.
  * The difference is attributed to the UTC day of the later reading.
  * Reversed readings (later cumulative < earlier cumulative) and any reading
    that cannot be paired are flagged for review.

Meter service windows derive from the replacement sheet:
  * old meter: readings must be on or before ``switched_at``
  * new meter: readings must be on or after ``switched_at``
Readings outside the window cannot belong to that meter for that account and
are flagged for review.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .config import (
    REASON_CROSS_SWITCH,
    REASON_NO_REPLACEMENT,
    REASON_OUTSIDE_WINDOW,
    REASON_REVERSED,
    REASON_UNPAIRED,
)
from .ingestion import REASON_LABELS, now_utc_iso


@dataclass
class IntervalResult:
    account: str
    meter: str
    interval_start: Optional[str]
    interval_end: Optional[str]
    start_reading: Optional[float]
    end_reading: Optional[float]
    consumption_m3: Optional[float]
    attributed_day: Optional[str]
    status: str                       # "OK" or "REVIEW"
    review_reason: Optional[str]
    source_reading_ids: list[int] = field(default_factory=list)


class GenerationError(Exception):
    """A generation attempt failed; the previous active result stays intact."""


def _load_replacements(conn: sqlite3.Connection) -> dict[str, dict]:
    repl: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT account, old_meter, new_meter, switched_at FROM replacements"
    ):
        repl[row["account"]] = {
            "old_meter": row["old_meter"],
            "new_meter": row["new_meter"],
            "switched_at": row["switched_at"],
        }
    return repl


def _load_readings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT id, account, meter, read_at, reading_cum
                 FROM readings
             ORDER BY account, meter, read_at, id"""
        )
    )


def _review(row: sqlite3.Row, reason: str) -> IntervalResult:
    return IntervalResult(
        account=row["account"],
        meter=row["meter"],
        interval_start=None,
        interval_end=row["read_at"],
        start_reading=None,
        end_reading=row["reading_cum"],
        consumption_m3=None,
        attributed_day=None,
        status="REVIEW",
        review_reason=reason,
        source_reading_ids=[row["id"]],
    )


def _pair(prev: sqlite3.Row, curr: sqlite3.Row, reason_if_reversed: str | None) -> IntervalResult:
    diff = round(curr["reading_cum"] - prev["reading_cum"], 6)
    if diff < 0:
        # The later reading itself is the suspicious one and stays unpaired.
        return _review(curr, reason_if_reversed or REASON_REVERSED)
    return IntervalResult(
        account=curr["account"],
        meter=curr["meter"],
        interval_start=prev["read_at"],
        interval_end=curr["read_at"],
        start_reading=prev["reading_cum"],
        end_reading=curr["reading_cum"],
        consumption_m3=diff,
        attributed_day=curr["read_at"][:10],
        status="OK",
        review_reason=None,
        source_reading_ids=[prev["id"], curr["id"]],
    )


def _process_meter_group(group: list[sqlite3.Row], repl: dict,
                         meter: str) -> list[IntervalResult]:
    """Apply pairing rules to one (account, meter) reading group."""
    out: list[IntervalResult] = []
    switched_at = repl["switched_at"]
    if meter == repl["old_meter"]:
        in_window = lambda ts: ts <= switched_at
    elif meter == repl["new_meter"]:
        in_window = lambda ts: ts >= switched_at
    else:
        return [_review(r, REASON_NO_REPLACEMENT) for r in group]

    prev: sqlite3.Row | None = None
    for r in group:
        if not in_window(r["read_at"]):
            out.append(_review(r, REASON_OUTSIDE_WINDOW))
            # It cannot participate in any legitimate pair.
            prev = None
            continue
        if prev is None:
            out.append(_review(r, REASON_UNPAIRED))
            prev = r
            continue

        # A pair crosses the switch only when the switch instant lies
        # strictly inside the open interval (start, end).
        if prev["read_at"] < switched_at < r["read_at"]:
            # The earlier reading was already emitted as its own row; the
            # later one is held for review rather than cross-subtracted.
            out.append(_review(r, REASON_CROSS_SWITCH))
            prev = r
            continue

        paired = _pair(prev, r, REASON_REVERSED)
        out.append(paired)
        # Either way the later reading becomes the base of the next chain:
        # after a reversal it was itself emitted (unpaired REVIEW) and the
        # old chain must not continue through it.
        prev = r
    return out


def compute_intervals(conn: sqlite3.Connection) -> list[IntervalResult]:
    replacements = _load_replacements(conn)
    readings = _load_readings(conn)

    results: list[IntervalResult] = []
    # Group readings by (account, meter), preserving global order.
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in readings:
        groups.setdefault((r["account"], r["meter"]), []).append(r)

    for (account, meter), group in groups.items():
        repl = replacements.get(account)
        if repl is None:
            # No replacement sheet row for this account: meter ownership cannot
            # be confirmed, so every reading is held for review rather than
            # silently producing consumption.
            results.extend(_review(r, REASON_NO_REPLACEMENT) for r in group)
            continue
        results.extend(_process_meter_group(group, repl, meter))

    results.sort(key=lambda x: (x.account, x.meter,
                                x.interval_end or "", x.source_reading_ids))
    return results


def generate_generation(conn: sqlite3.Connection) -> dict:
    """Build a full result set from all committed inputs.

    Runs under one serialised transaction so an interleaved upload can never
    be half-included. On any failure the transaction is rolled back and the
    previously active generation remains the downloadable, active result.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        reading_batch_ids = [
            str(r["id"])
            for r in conn.execute(
                "SELECT id FROM upload_batches WHERE batch_type='reading' AND status='committed' ORDER BY id"
            )
        ]
        repl_row = conn.execute(
            "SELECT id FROM upload_batches WHERE batch_type='replacement' AND status='committed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        repl_batch_id = repl_row["id"] if repl_row else None

        if not reading_batch_ids:
            raise GenerationError("尚无任何已提交的读数，无法生成结果")

        results = compute_intervals(conn)

        cur = conn.execute(
            """INSERT INTO generations
                   (status, status_detail, reading_batch_ids, replacement_batch_id,
                    created_at, superseded_at)
               VALUES ('active', ?, ?, ?, ?, NULL)""",
            (None, ",".join(reading_batch_ids),
             repl_batch_id, now_utc_iso()),
        )
        generation_id = int(cur.lastrowid)
        conn.executemany(
            """INSERT INTO result_rows
                   (generation_id, account, meter, interval_start, interval_end,
                    start_reading, end_reading, consumption_m3, attributed_day,
                    status, review_reason, source_reading_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    generation_id, x.account, x.meter, x.interval_start,
                    x.interval_end, x.start_reading, x.end_reading,
                    x.consumption_m3, x.attributed_day, x.status,
                    x.review_reason, ",".join(str(i) for i in x.source_reading_ids),
                )
                for x in results
            ],
        )
        # Any older active result is superseded only after the new one is fully built.
        conn.execute(
            """UPDATE generations SET status='expired', superseded_at=?
               WHERE status='active' AND id<>?""",
            (now_utc_iso(), generation_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    counts = {"OK": 0, "REVIEW": 0}
    for x in results:
        counts[x.status] += 1
    return {
        "generation_id": generation_id,
        "total_rows": len(results),
        "ok_rows": counts["OK"],
        "review_rows": counts["REVIEW"],
        "reading_batch_ids": reading_batch_ids,
        "replacement_batch_id": repl_batch_id,
    }


def reason_label(code: str | None) -> str | None:
    if code is None:
        return None
    return REASON_LABELS.get(code, code)
