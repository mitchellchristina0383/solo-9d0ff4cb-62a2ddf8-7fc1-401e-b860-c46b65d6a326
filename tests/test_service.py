"""End-to-end tests for the water usage service."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("WATER_DATA_DIR", tmp)
    monkeypatch.setenv("WATER_DB_PATH", os.path.join(tmp, "test.db"))
    # Import after env vars are set; rebuild app with isolated DB.
    import importlib
    import app.config as config
    import app.database as database
    import app.main as main
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def _files(c, kind, content, name="x.csv"):
    return c.post(
        f"/api/uploads/{kind}",
        files={"file": (name, content.encode("utf-8"), "text/csv")},
    )


REPL = (
    "account,old_meter,new_meter,switched_at\n"
    "A1,OLD1,NEW1,2026-01-04T12:00:00Z\n"
    "A2,OLD2,NEW2,2026-01-04T00:00:00Z\n"
)

READINGS = (
    "account,meter,read_at,reading_cum\n"
    "A1,OLD1,2026-01-01T00:00:00Z,100\n"
    "A1,OLD1,2026-01-02T00:00:00Z,110\n"
    "A1,OLD1,2026-01-03T00:00:00Z,125.5\n"
    "A1,NEW1,2026-01-05T00:00:00Z,0\n"
    "A1,NEW1,2026-01-06T00:00:00Z,7\n"
    "A1,NEW1,2026-01-07T00:00:00Z,6\n"          # reversed
    "A2,OLD2,2026-01-01T08:00:00Z,200\n"
    "A2,OLD2,2026-01-02T08:00:00Z,208\n"
    "A2,NEW2,2026-01-04T12:00:00Z,0\n"
    "A2,NEW2,2026-01-05T12:00:00Z,15\n"
)


def _by_key(rows):
    return {(r["account"], r["meter"], r["interval_end"]): r for r in rows}


def _gen(client):
    resp = client.post("/api/generations")
    assert resp.status_code == 200, resp.text
    return resp.json()["detail"]["generation_id"]


def _rows(client, gid):
    return client.get(f"/api/generations/{gid}/rows?limit=10000").json()["rows"]


def test_health_and_empty_state(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/api/generations").json()["generations"] == []
    assert client.get("/api/generations/latest/download").status_code == 404


def test_full_flow_pairing_day_attribution_and_review(client):
    assert _files(client, "replacements", REPL).status_code == 200
    assert _files(client, "readings", READINGS).status_code == 200
    gid = _gen(client)
    rows = _rows(client, gid)
    k = _by_key(rows)

    # Normal pairs: difference attributed to the UTC day of the later reading.
    r = k[("A1", "OLD1", "2026-01-02T00:00:00Z")]
    assert r["status"] == "OK" and r["consumption_m3"] == 10.0
    assert r["attributed_day"] == "2026-01-02"
    assert r["source_reading_ids"]  # source ids populated

    r = k[("A1", "OLD1", "2026-01-03T00:00:00Z")]
    assert r["status"] == "OK" and r["consumption_m3"] == 15.5
    assert r["attributed_day"] == "2026-01-03"

    # First reading of each meter is unpaired -> REVIEW.
    assert k[("A1", "OLD1", "2026-01-01T00:00:00Z")]["status"] == "REVIEW"
    assert k[("A1", "OLD1", "2026-01-01T00:00:00Z")]["review_reason"] == "unpaired_reading"
    assert k[("A1", "NEW1", "2026-01-05T00:00:00Z")]["review_reason"] == "unpaired_reading"

    # Reversed reading flagged, no consumption.
    rev = k[("A1", "NEW1", "2026-01-07T00:00:00Z")]
    assert rev["status"] == "REVIEW"
    assert rev["review_reason"] == "reversed_reading"
    assert rev["consumption_m3"] is None

    # The pair before the reversed one is still fine.
    assert k[("A1", "NEW1", "2026-01-06T00:00:00Z")]["consumption_m3"] == 7.0

    # A2: old-meter second pair OK; new meter first unpaired, second OK.
    assert k[("A2", "OLD2", "2026-01-02T08:00:00Z")]["consumption_m3"] == 8.0
    assert k[("A2", "NEW2", "2026-01-05T12:00:00Z")]["consumption_m3"] == 15.0


def test_same_timestamp_same_value_counts_once(client):
    _files(client, "replacements", REPL)
    base = (
        "account,meter,read_at,reading_cum\n"
        "A1,OLD1,2026-01-01T00:00:00Z,100\n"
        "A1,OLD1,2026-01-02T00:00:00Z,110\n"
    )
    r1 = _files(client, "readings", base)
    assert r1.json()["detail"]["inserted"] == 2
    # Re-upload identical content: all duplicates, zero new rows.
    r2 = _files(client, "readings", base)
    body = r2.json()["detail"]
    assert body["inserted"] == 0 and body["duplicates_skipped"] == 2

    gid = _gen(client)
    rows = _rows(client, gid)
    assert len([r for r in rows if r["meter"] == "OLD1"]) == 2  # no double counting


def test_same_timestamp_different_value_whole_batch_rejected(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n")
    # Conflicting value against committed data -> 422, nothing inserted.
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\n"
                  "A1,OLD1,2026-01-01T00:00:00Z,999\n"
                  "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    assert resp.status_code == 422
    batches = client.get("/api/batches").json()["batches"]
    rejected = [b for b in batches if b["status"] == "rejected"]
    assert len(rejected) == 1 and rejected[0]["reject_reason"]
    # The second row of the rejected batch must not exist.
    gid = _gen(client)
    rows = _rows(client, gid)
    assert all(r["interval_end"] != "2026-01-02T00:00:00Z" for r in rows)


def test_conflict_inside_same_batch_rejected(client):
    _files(client, "replacements", REPL)
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\n"
                  "A1,OLD1,2026-01-01T00:00:00Z,100\n"
                  "A1,OLD1,2026-01-01T00:00:00Z,101\n")
    assert resp.status_code == 422
    batches = client.get("/api/batches").json()["batches"]
    assert all(b["status"] != "committed" for b in batches if b["batch_type"] == "reading")


def test_crossing_switchover_pair_flagged(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A3,O3,N3,2026-01-10T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A3,O3,2026-01-09T00:00:00Z,10\n"
           "A3,O3,2026-01-11T00:00:00Z,30\n"          # after switch on old meter
           "A3,N3,2026-01-09T00:00:00Z,0\n"           # before switch on new meter
           "A3,N3,2026-01-11T00:00:00Z,5\n")
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    assert k[("A3", "O3", "2026-01-11T00:00:00Z")]["review_reason"] == "outside_meter_service_window"
    assert k[("A3", "N3", "2026-01-09T00:00:00Z")]["review_reason"] == "outside_meter_service_window"
    # The surviving in-window readings are unpaired, never cross-subtracted.
    assert k[("A3", "O3", "2026-01-09T00:00:00Z")]["review_reason"] == "unpaired_reading"
    assert k[("A3", "N3", "2026-01-11T00:00:00Z")]["review_reason"] == "unpaired_reading"


def test_interval_exactly_spanning_switch_is_flagged(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A4,O4,N4,2026-01-10T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A4,O4,2026-01-09T00:00:00Z,10\n"
           "A4,O4,2026-01-10T00:00:00Z,12\n"          # exactly at switch: in window
           "A4,O4,2026-01-10T12:00:00Z,40\n")         # outside window -> pair blocked
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    # Pair ending exactly at the switch timestamp is allowed (does not cross).
    assert k[("A4", "O4", "2026-01-10T00:00:00Z")]["consumption_m3"] == 2.0
    assert k[("A4", "O4", "2026-01-10T12:00:00Z")]["review_reason"] == "outside_meter_service_window"


def test_late_reading_expires_previous_generation(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)
    gens = client.get("/api/generations").json()["generations"]
    assert gens[0]["id"] == g1 and gens[0]["status"] == "active"

    # Late reading arrives -> previous generation must be expired.
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2025-12-31T00:00:00Z,90\n")
    gens = client.get("/api/generations").json()["generations"]
    assert next(g for g in gens if g["id"] == g1)["status"] == "expired"

    g2 = _gen(client)
    rows = _rows(client, g2)
    k = _by_key(rows)
    # New earliest reading becomes the unpaired one; old first reading now pairs.
    assert k[("A1", "OLD1", "2025-12-31T00:00:00Z")]["review_reason"] == "unpaired_reading"
    assert k[("A1", "OLD1", "2026-01-01T00:00:00Z")]["consumption_m3"] == 10.0
    assert k[("A1", "OLD1", "2026-01-02T00:00:00Z")]["consumption_m3"] == 10.0
    # Old generation remains downloadable.
    assert client.get(f"/api/generations/{g1}/download").status_code == 200


def test_replacement_correction_expires_and_changes_window(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A5,O5,N5,2026-01-10T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A5,O5,2026-01-08T00:00:00Z,10\n"
           "A5,O5,2026-01-09T00:00:00Z,13\n")
    g1 = _gen(client)
    assert _rows(client, g1)[1]["consumption_m3"] == 3.0

    # Correction: switch actually happened earlier; Jan 9 reading now out of window.
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A5,O5,N5,2026-01-08T12:00:00Z\n")
    gens = client.get("/api/generations").json()["generations"]
    assert next(g for g in gens if g["id"] == g1)["status"] == "expired"
    g2 = _gen(client)
    k = _by_key(_rows(client, g2))
    assert k[("A5", "O5", "2026-01-09T00:00:00Z")]["review_reason"] == "outside_meter_service_window"


def test_generation_without_readings_fails_but_keeps_old_result(client):
    _files(client, "replacements", REPL)
    # No readings at all -> business failure.
    resp = client.post("/api/generations")
    assert resp.status_code == 409

    # Now create a valid active generation.
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)
    assert client.get("/api/generations").json()["generations"][0]["status"] == "active"

    # Simulate a failing generation (no committed readings reachable): force error
    # by breaking compute via monkeypatched loader on the app module.
    import app.main as main
    original = main.generate_generation

    def boom(conn):
        raise main.GenerationError("模拟生成失败")

    main.generate_generation = boom
    resp = client.post("/api/generations")
    main.generate_generation = original
    assert resp.status_code == 409

    gens = client.get("/api/generations").json()["generations"]
    old = next(g for g in gens if g["id"] == g1)
    assert old["status"] == "expired"      # marked expired, still downloadable
    assert "失败" in old["status_detail"]
    dl = client.get(f"/api/generations/{g1}/download")
    assert dl.status_code == 200 and "text/csv" in dl.headers["content-type"]
    assert dl.headers["X-Generation-Status"] == "expired"


def test_download_csv_contents(client):
    _files(client, "replacements", REPL)
    _files(client, "readings", READINGS)
    gid = _gen(client)
    dl = client.get(f"/api/generations/{gid}/download")
    text = dl.text
    header = text.splitlines()[0]
    assert "source_reading_ids" in header
    assert "attributed_utc_day" in header
    assert "review_reason" in header
    # Every result line references the generation and has an OK/REVIEW status.
    for line in text.splitlines()[1:]:
        assert line.split(",")[0] == str(gid)
    assert "REVIEW" in text and "reversed_reading" in text


def test_bad_csv_headers_and_rows_rejected(client):
    resp = _files(client, "readings", "a,b,c\n1,2,3\n")
    assert resp.status_code == 422 and "表头" in resp.json()["detail"]
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\nA1,OLD1,not-a-time,10\n")
    assert resp.status_code == 422 and "时刻" in resp.json()["detail"]
    resp = _files(client, "replacements",
                  "account,old_meter,new_meter,switched_at\n"
                  "A1,X,X,2026-01-01T00:00:00Z\n")
    assert resp.status_code == 422


def test_rejected_replacement_keeps_previous_snapshot(client):
    good = (
        "account,old_meter,new_meter,switched_at\n"
        "A5,O5,N5,2026-01-10T00:00:00Z\n")
    assert _files(client, "replacements", good).status_code == 200
    # Invalid sheet (duplicate account) must be rejected without touching data.
    bad = (
        "account,old_meter,new_meter,switched_at\n"
        "A5,X1,Y1,2026-01-01T00:00:00Z\n"
        "A5,X2,Y2,2026-01-02T00:00:00Z\n")
    assert _files(client, "replacements", bad).status_code == 422
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A5,O5,2026-01-08T00:00:00Z,10\n"
           "A5,O5,2026-01-09T00:00:00Z,13\n")
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    # Original switch (Jan 10) still in force: pair is valid.
    assert k[("A5", "O5", "2026-01-09T00:00:00Z")]["consumption_m3"] == 3.0


def test_duplicate_account_in_replacement_sheet_rejected(client):
    resp = _files(client, "replacements",
                  "account,old_meter,new_meter,switched_at\n"
                  "A1,O1,N1,2026-01-01T00:00:00Z\n"
                  "A1,O2,N2,2026-01-02T00:00:00Z\n")
    assert resp.status_code == 422 and "每户一条" in resp.json()["detail"]


class _Row(dict):
    """Minimal sqlite3.Row stand-in supporting key access."""
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_cross_switch_branch_direct_unit():
    """Unit tests for the per-meter pairing rules around the switch instant."""
    from app.compute import _process_meter_group

    def group(*spec):
        return [_Row(id=i, account="A7", meter="O7", read_at=ts, reading_cum=val)
                for i, (ts, val) in enumerate(spec, start=1)]

    def repl(switch):
        return {"old_meter": "O7", "new_meter": "N7", "switched_at": switch}

    # 1) Switch strictly inside an old-meter pair: the later reading is past
    #    the window, so the window guard blocks the pair (production path).
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10), ("2026-01-12T00:00:00Z", 30)),
        repl("2026-01-10T12:00:00Z"), "O7")
    assert [r.review_reason for r in out] == [
        "unpaired_reading", "outside_meter_service_window"]

    # 2) Pair ending exactly at the switch boundary is allowed (no strict cross).
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10), ("2026-01-10T12:00:00Z", 30)),
        repl("2026-01-10T12:00:00Z"), "O7")
    assert out[0].review_reason == "unpaired_reading"
    assert out[1].status == "OK" and out[1].consumption_m3 == 20.0

    # 3) The cross-switch predicate itself is a strict open-interval test.
    t1, switch, t2 = ("2026-01-10T00:00:00Z",
                      "2026-01-10T12:00:00Z",
                      "2026-01-11T00:00:00Z")
    assert t1 < switch < t2           # would trigger crosses_switchover
    assert not (t1 < t2 < switch)     # readings both before switch: no cross

    # 4) Window boundary followed by an out-of-window reading resets the chain.
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10),
              ("2026-01-10T00:00:00Z", 12),
              ("2026-01-10T00:00:01Z", 30)),
        repl("2026-01-10T00:00:00Z"), "O7")
    assert out[0].review_reason == "unpaired_reading"
    assert out[1].status == "OK" and out[1].consumption_m3 == 2.0
    assert out[2].review_reason == "outside_meter_service_window"

    # 5) Unknown meter for the account: every row held, no replacement record.
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10)),
        repl("2026-01-10T00:00:00Z"), "MYSTERY")
    assert out[0].review_reason == "no_replacement_record"


def test_zero_consumption_equal_readings_are_ok(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A8,O8,N8,2026-02-01T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A8,O8,2026-01-01T00:00:00Z,50\n"
           "A8,O8,2026-01-02T00:00:00Z,50\n"     # equal -> zero usage, still OK
           "A8,O8,2026-01-03T00:00:00Z,51\n")
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    assert k[("A8", "O8", "2026-01-02T00:00:00Z")]["consumption_m3"] == 0.0
    assert k[("A8", "O8", "2026-01-02T00:00:00Z")]["status"] == "OK"
    assert k[("A8", "O8", "2026-01-03T00:00:00Z")]["consumption_m3"] == 1.0


def test_naive_timestamp_treated_as_utc(client):
    _files(client, "replacements", REPL)
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\n"
                  "A1,OLD1,2026-01-01 00:00:00,100\n"
                  "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    assert resp.status_code == 200
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    assert k[("A1", "OLD1", "2026-01-02T00:00:00Z")]["consumption_m3"] == 10.0


def test_meter_without_replacement_record_is_review(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A9,UNKNOWN,2026-01-01T00:00:00Z,5\n"
           "A9,UNKNOWN,2026-01-02T00:00:00Z,9\n")
    gid = _gen(client)
    rows = [r for r in _rows(client, gid) if r["account"] == "A9"]
    assert all(r["status"] == "REVIEW" for r in rows)
    assert all(r["review_reason"] == "no_replacement_record" for r in rows)
