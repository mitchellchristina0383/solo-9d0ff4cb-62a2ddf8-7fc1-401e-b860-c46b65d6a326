"""CSV parsing, validation and transactional ingestion."""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import (
    REASON_CROSS_SWITCH,
    REASON_NO_REPLACEMENT,
    REASON_OUTSIDE_WINDOW,
    REASON_REVERSED,
    REASON_UNPAIRED,
)

READING_HEADERS = ["account", "meter", "read_at", "reading_cum"]
REPLACEMENT_HEADERS = ["account", "old_meter", "new_meter", "switched_at"]


class BatchRejected(Exception):
    """Raised when an upload fails validation; nothing is committed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unparsable timestamp {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_iso_utc(dt: datetime) -> str:
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ReadingRow:
    account: str
    meter: str
    read_at: str
    reading_cum: float


@dataclass(frozen=True)
class ReplacementRow:
    account: str
    old_meter: str
    new_meter: str
    switched_at: str


def _open_csv(text: str):
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader]
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    if not rows:
        raise BatchRejected("CSV 文件为空")
    return rows


def _check_headers(header: list[str], expected: list[str]):
    cleaned = [h.strip() for h in header]
    if cleaned != expected:
        raise BatchRejected(
            f"表头必须为 {','.join(expected)}，实际为 {','.join(cleaned) if cleaned else '(空)'}"
        )


def parse_readings_csv(text: str) -> list[ReadingRow]:
    rows = _open_csv(text)
    _check_headers(rows[0], READING_HEADERS)
    parsed: list[ReadingRow] = []
    seen: dict[tuple[str, str, str], float] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(READING_HEADERS):
            raise BatchRejected(f"第 {line_no} 行字段数应为 {len(READING_HEADERS)}，实际为 {len(row)}")
        account, meter, raw_ts, raw_val = (c.strip() for c in row)
        if not account or not meter:
            raise BatchRejected(f"第 {line_no} 行 account/meter 不能为空")
        try:
            ts = format_iso_utc(parse_iso_utc(raw_ts))
        except ValueError as exc:
            raise BatchRejected(f"第 {line_no} 行时刻无效: {exc}") from exc
        try:
            value = float(raw_val)
        except ValueError as exc:
            raise BatchRejected(f"第 {line_no} 行累计读数不是数字: {raw_val!r}") from exc
        if value < 0:
            raise BatchRejected(f"第 {line_no} 行累计读数不能为负: {value}")
        key = (account, meter, ts)
        if key in seen:
            if seen[key] != value:
                raise BatchRejected(
                    f"第 {line_no} 行与批内更早的同表同时刻读数异值（{seen[key]} != {value}），整批拒收"
                )
            continue  # identical retransmission inside the batch: count once
        seen[key] = value
        parsed.append(ReadingRow(account, meter, ts, value))
    if not parsed:
        raise BatchRejected("CSV 不含任何有效数据行")
    return parsed


def parse_replacements_csv(text: str) -> list[ReplacementRow]:
    rows = _open_csv(text)
    _check_headers(rows[0], REPLACEMENT_HEADERS)
    parsed: list[ReplacementRow] = []
    seen_accounts: set[str] = set()
    meter_owner: dict[str, str] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(REPLACEMENT_HEADERS):
            raise BatchRejected(f"第 {line_no} 行字段数应为 {len(REPLACEMENT_HEADERS)}，实际为 {len(row)}")
        account, old_meter, new_meter, raw_ts = (c.strip() for c in row)
        if not all((account, old_meter, new_meter, raw_ts)):
            raise BatchRejected(f"第 {line_no} 行存在空字段")
        if old_meter == new_meter:
            raise BatchRejected(f"第 {line_no} 行旧表与新表不能相同: {old_meter}")
        if account in seen_accounts:
            raise BatchRejected(f"第 {line_no} 行账户 {account} 在本批中出现多条换表记录（每户一条）")
        try:
            ts = format_iso_utc(parse_iso_utc(raw_ts))
        except ValueError as exc:
            raise BatchRejected(f"第 {line_no} 行切换时刻无效: {exc}") from exc
        for meter in (old_meter, new_meter):
            owner = meter_owner.get(meter)
            if owner is not None and owner != account:
                raise BatchRejected(
                    f"第 {line_no} 行表号 {meter} 已归属账户 {owner}，同一表号不能跨户使用"
                )
            meter_owner[meter] = account
        seen_accounts.add(account)
        parsed.append(ReplacementRow(account, old_meter, new_meter, ts))
    if not parsed:
        raise BatchRejected("CSV 不含任何有效数据行")
    return parsed


def _record_batch(
    conn: sqlite3.Connection,
    batch_type: str,
    filename: str,
    status: str,
    row_count: int,
    reject_reason: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO upload_batches (batch_type, filename, status, row_count, reject_reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (batch_type, filename, status, row_count, reject_reason, now_utc_iso()),
    )
    return int(cur.lastrowid)


def _expire_active_generation(conn: sqlite3.Connection) -> None:
    """Any newly committed input supersedes the currently active generation."""
    conn.execute(
        """UPDATE generations
              SET status = 'expired', superseded_at = ?
            WHERE status = 'active'""",
        (now_utc_iso(),),
    )


def _record_rejected_batch(conn: sqlite3.Connection, batch_type: str,
                           filename: str, reason: str) -> None:
    """Persist the rejection itself as an audit record (never partial data)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _record_batch(conn, batch_type, filename, "rejected", 0, reason)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ingest_readings(conn: sqlite3.Connection, filename: str, text: str) -> dict:
    rows = parse_readings_csv(text)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = {
            (r["account"], r["meter"], r["read_at"]): r["reading_cum"]
            for r in conn.execute(
                "SELECT account, meter, read_at, reading_cum FROM readings"
            )
        }
        duplicates = 0
        for row in rows:
            key = (row.account, row.meter, row.read_at)
            if key in existing:
                if existing[key] != row.reading_cum:
                    raise BatchRejected(
                        f"表 {row.meter} 在 {row.read_at} 已有读数 {existing[key]}，"
                        f"本批为 {row.reading_cum}，异值整批拒收"
                    )
                duplicates += 1
        batch_id = _record_batch(conn, "reading", filename, "committed", len(rows), None)
        conn.executemany(
            """INSERT INTO readings (batch_id, account, meter, read_at, reading_cum, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (batch_id, r.account, r.meter, r.read_at, r.reading_cum, now_utc_iso())
                for r in rows
                if (r.account, r.meter, r.read_at) not in existing
            ],
        )
        _expire_active_generation(conn)
        conn.commit()
    except BatchRejected as exc:
        conn.rollback()
        _record_rejected_batch(conn, "reading", filename, exc.reason)
        raise
    except Exception:
        conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "rows": len(rows),
        "duplicates_skipped": duplicates,
        "inserted": len(rows) - duplicates,
    }


def ingest_replacements(conn: sqlite3.Connection, filename: str, text: str) -> dict:
    rows = parse_replacements_csv(text)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Snapshot replacement: the full upload replaces every prior replacement row.
        conn.execute("DELETE FROM replacements")
        batch_id = _record_batch(conn, "replacement", filename, "committed", len(rows), None)
        conn.executemany(
            """INSERT INTO replacements (batch_id, account, old_meter, new_meter, switched_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (batch_id, r.account, r.old_meter, r.new_meter, r.switched_at, now_utc_iso())
                for r in rows
            ],
        )
        _expire_active_generation(conn)
        conn.commit()
    except BatchRejected as exc:
        conn.rollback()
        _record_rejected_batch(conn, "replacement", filename, exc.reason)
        raise
    except Exception:
        conn.rollback()
        raise
    return {"batch_id": batch_id, "rows": len(rows), "inserted": len(rows)}


REASON_LABELS = {
    REASON_UNPAIRED: "无法配对读数（缺少同户同表的前一笔相邻读数）",
    REASON_REVERSED: "累计读数倒退（后笔小于前笔）",
    REASON_CROSS_SWITCH: "相邻读数跨越换表切换时刻，禁止相减",
    REASON_OUTSIDE_WINDOW: "读数超出该表在本户的服务窗口",
    REASON_NO_REPLACEMENT: "该账户无换表记录，表归属无法确认",
}
