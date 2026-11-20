import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import db as db_mod
from app.modules import reclaim_deadline_materialize as deadline_mod
from app.services import ingest as ingest_svc


def _add_cell(conn, cell_id="CAB-A-04"):
    conn.execute(
        "INSERT OR IGNORE INTO cells(id, cabinet_id, code, size_code, status) "
        "VALUES (?, 'CAB-A', ?, 'L', 'free')",
        (cell_id, cell_id[-2:]),
    )
    conn.commit()


def _open(conn, occ_id, cell_id, opened_at, tracking="TN9", hours=None):
    _add_cell(conn, cell_id)
    opened = deadline_mod.open_occupancy(
        conn,
        occupancy_id=occ_id,
        cell_id=cell_id,
        tracking_no=tracking,
        opened_at=opened_at,
        last_event_id=f"EV-{occ_id}",
        overdue_hours=hours,
    )
    conn.commit()
    return opened


def test_seed_materializes_deadline_for_every_open_occupancy(fresh_db):
    rows = fresh_db.execute(
        "SELECT id, opened_at, reclaim_deadline_at, overdue_hours_used, status "
        "FROM occupancies WHERE status='occupied'"
    ).fetchall()
    assert rows, "夹具应开占用"
    for row in rows:
        assert row["reclaim_deadline_at"], f"{row['id']} 缺 deadline"
        expected = deadline_mod.format_ts(
            deadline_mod.parse_ts(row["opened_at"])
            + timedelta(hours=row["overdue_hours_used"])
        )
        assert row["reclaim_deadline_at"] == expected

    e2 = fresh_db.execute(
        "SELECT reclaim_deadline_at, overdue_hours_used FROM occupancies "
        "WHERE id='OCC-CAB-A-02-E2'"
    ).fetchone()
    assert e2["reclaim_deadline_at"] == "2026-11-20T09:00:00Z"
    assert e2["overdue_hours_used"] == 48


def test_ingest_open_carries_deadline_in_same_row(fresh_db):
    report = ingest_svc.run_batch()
    assert report["accepted"] >= 1
    row = fresh_db.execute(
        "SELECT opened_at, reclaim_deadline_at, overdue_hours_used "
        "FROM occupancies WHERE id='OCC-CAB-A-03-E5'"
    ).fetchone()
    assert row is not None
    assert row["opened_at"] == "2026-11-19T02:00:00Z"
    assert row["reclaim_deadline_at"] == "2026-11-21T02:00:00Z"
    assert row["overdue_hours_used"] == 48


def test_deadline_does_not_drift_when_hours_change(fresh_db):
    """开两笔占用后改柜规：旧 deadline 不动，新占用按新小时落地。"""
    old = _open(fresh_db, "OCC-T1", "CAB-A-04", "2026-11-18T00:00:00Z")
    assert old["reclaim_deadline_at"] == "2026-11-20T00:00:00Z"
    assert old["overdue_hours_used"] == 48

    fresh_db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', '6')"
    )
    fresh_db.commit()

    new = _open(fresh_db, "OCC-T2", "CAB-B-02", "2026-11-18T00:00:00Z")
    assert new["reclaim_deadline_at"] == "2026-11-18T06:00:00Z"
    assert new["overdue_hours_used"] == 6

    stored = fresh_db.execute(
        "SELECT reclaim_deadline_at, overdue_hours_used FROM occupancies WHERE id='OCC-T1'"
    ).fetchone()
    assert stored["reclaim_deadline_at"] == "2026-11-20T00:00:00Z"
    assert stored["overdue_hours_used"] == 48


def test_open_without_deadline_is_rejected(fresh_db):
    _add_cell(fresh_db)
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, "
            "reclaim_deadline_at, overdue_hours_used, status) "
            "VALUES ('OCC-BAD', 'CAB-A-04', 'TN1', '2026-11-18T00:00:00Z', NULL, NULL, 'occupied')"
        )
    fresh_db.rollback()
    # 失败闭：库里不得留下 open 但 deadline 为空的占用。
    bad = fresh_db.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE status='occupied' "
        "AND (reclaim_deadline_at IS NULL OR overdue_hours_used IS NULL)"
    ).fetchone()["c"]
    assert bad == 0


def test_deadline_is_immutable_after_materialization(fresh_db):
    _open(fresh_db, "OCC-T1", "CAB-A-04", "2026-11-18T00:00:00Z")
    for column, value in (
        ("reclaim_deadline_at", "2026-11-19T00:00:00Z"),
        ("overdue_hours_used", 1),
        ("opened_at", "2026-11-01T00:00:00Z"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            fresh_db.execute(
                f"UPDATE occupancies SET {column}=? WHERE id='OCC-T1'", (value,)
            )
        fresh_db.rollback()

    row = fresh_db.execute(
        "SELECT opened_at, reclaim_deadline_at, overdue_hours_used "
        "FROM occupancies WHERE id='OCC-T1'"
    ).fetchone()
    assert dict(row) == {
        "opened_at": "2026-11-18T00:00:00Z",
        "reclaim_deadline_at": "2026-11-20T00:00:00Z",
        "overdue_hours_used": 48,
    }


def test_release_still_allowed_on_materialized_row(fresh_db):
    _open(fresh_db, "OCC-T1", "CAB-A-04", "2026-11-18T00:00:00Z")
    fresh_db.execute(
        "UPDATE occupancies SET closed_at='2026-11-18T12:00:00Z', status='released' "
        "WHERE id='OCC-T1'"
    )
    fresh_db.commit()
    row = fresh_db.execute(
        "SELECT status, closed_at, reclaim_deadline_at FROM occupancies WHERE id='OCC-T1'"
    ).fetchone()
    assert row["status"] == "released"
    assert row["reclaim_deadline_at"] == "2026-11-20T00:00:00Z"


def test_invalid_hours_fail_open_instead_of_storing_bad_deadline(fresh_db):
    for bad in (0, -3, "abc", True):
        with pytest.raises(ValueError):
            _open(fresh_db, f"OCC-BAD-{bad}", "CAB-A-04", "2026-11-18T00:00:00Z", hours=bad)
    assert fresh_db.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE id LIKE 'OCC-BAD-%'"
    ).fetchone()["c"] == 0


def test_deadline_view_is_read_only_and_immutable(fresh_db):
    before = deadline_mod.deadline_proof(fresh_db, "OCC-CAB-A-02-E2")
    assert before["reclaim_deadline_at"] == "2026-11-20T09:00:00Z"
    assert before["overdue_hours_used"] == 48
    assert before["deadline_missing"] is False

    fresh_db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', '2')"
    )
    fresh_db.commit()

    after = deadline_mod.deadline_proof(fresh_db, "OCC-CAB-A-02-E2")
    assert after == before, "核证只读，不应随柜规变化重算"


def test_migration_backfills_legacy_rows(tmp_path, monkeypatch):
    """存量库缺列时补列并按迁移时柜规物化，不留缺列 open。"""
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings VALUES ('overdue_hours', '10');
        CREATE TABLE occupancies (
          id TEXT PRIMARY KEY,
          cell_id TEXT NOT NULL,
          tracking_no TEXT NOT NULL,
          opened_at TEXT NOT NULL,
          closed_at TEXT,
          status TEXT NOT NULL DEFAULT 'occupied',
          last_event_id TEXT
        );
        INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, status)
          VALUES ('OLD-1', 'C-1', 'TN-OLD', '2026-11-18T00:00:00Z', 'occupied');
        """
    )
    deadline_mod.ensure_schema(conn)
    conn.commit()
    row = conn.execute(
        "SELECT reclaim_deadline_at, overdue_hours_used FROM occupancies WHERE id='OLD-1'"
    ).fetchone()
    conn.close()
    assert row["reclaim_deadline_at"] == "2026-11-18T10:00:00Z"
    assert row["overdue_hours_used"] == 10


def test_due_scan_reads_materialized_deadline_only(fresh_db):
    """回收扫描只比已落地 deadline；改柜规不改变扫描结果。"""
    rows, watermark = deadline_mod.due_for_reclaim(fresh_db, "2026-11-20T09:00:00Z")
    due_ids = [r["occupancy_id"] for r in rows]
    # E1 已释放（E3 取件），E4 截止 10:30 未到；只剩 E2 到期。
    assert due_ids == ["OCC-CAB-A-02-E2"]
    assert all(r["status"] == "occupied" for r in rows)
    assert watermark == "2026-11-20T09:00:00Z"

    fresh_db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', '1')"
    )
    fresh_db.commit()
    again, _ = deadline_mod.due_for_reclaim(fresh_db, "2026-11-20T09:00:00Z")
    assert [r["occupancy_id"] for r in again] == due_ids, "扫描不得按新柜规重算"


def test_api_due_endpoint(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        body = client.get("/api/reclaim/due", params={"now": "2026-11-20T09:00:00Z"}).json()
        assert body["count"] == 1
        assert [r["occupancy_id"] for r in body["rows"]] == ["OCC-CAB-A-02-E2"]

        bad = client.get("/api/reclaim/due", params={"now": "not-a-time"})
        assert bad.status_code == 400


def test_api_deadline_endpoints(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        listing = client.get("/api/reclaim/deadlines", params={"status": "occupied"})
        assert listing.status_code == 200
        body = listing.json()
        assert body["rows"]
        sample = next(r for r in body["rows"] if r["occupancy_id"] == "OCC-CAB-A-02-E2")
        assert sample["opened_at"] == "2026-11-18T09:00:00Z"
        assert sample["reclaim_deadline_at"] == "2026-11-20T09:00:00Z"
        assert sample["overdue_hours_used"] == 48

        one = client.get("/api/reclaim/deadlines/OCC-CAB-A-02-E2")
        assert one.status_code == 200
        assert one.json()["reclaim_deadline_at"] == "2026-11-20T09:00:00Z"

        missing = client.get("/api/reclaim/deadlines/NOPE")
        assert missing.status_code == 404


def test_api_settings_change_does_not_rewrite_existing_deadline(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        assert client.put("/api/settings/overdue-hours", json={"overdue_hours": 6}).status_code == 200
        row = client.get("/api/reclaim/deadlines/OCC-CAB-A-02-E2").json()
        assert row["reclaim_deadline_at"] == "2026-11-20T09:00:00Z"
        assert row["overdue_hours_used"] == 48

        # 新开一笔（夹具 E5，opened 2026-11-19T02:00:00Z）按 6 小时落地。
        client.post("/api/ingest/run", json={})
        fresh = client.get("/api/reclaim/deadlines/OCC-CAB-A-03-E5").json()
        assert fresh["reclaim_deadline_at"] == "2026-11-19T08:00:00Z"
        assert fresh["overdue_hours_used"] == 6
