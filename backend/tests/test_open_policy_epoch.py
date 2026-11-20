"""开账柜规世代（policy_epoch）钉定：核证、热更新、失败闭、邻键隔离。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app import db as db_mod
from app import seed as seed_mod
from app.modules import open_policy_epoch as epoch_mod
from app.modules import reclaim_deadline_materialize as deadline_mod


def _open(conn, occ_id, cell_id, opened_at, tracking="TN-EP", hours=None):
    conn.execute(
        "INSERT OR IGNORE INTO cells(id, cabinet_id, code, size_code, status) "
        "VALUES (?, 'CAB-A', ?, 'L', 'free')",
        (cell_id, cell_id[-2:]),
    )
    rec = deadline_mod.open_occupancy(
        conn,
        occupancy_id=occ_id,
        cell_id=cell_id,
        tracking_no=tracking,
        opened_at=opened_at,
        last_event_id=f"EV-{occ_id}",
        overdue_hours=hours,
    )
    conn.commit()
    return rec


def _set_hours(conn, hours):
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)", (str(hours),)
    )
    conn.commit()


def test_seed_pins_epoch_on_every_open_occupancy(fresh_db):
    rows = fresh_db.execute(
        "SELECT o.id, o.policy_epoch, o.overdue_hours_used, e.overdue_hours AS epoch_hours "
        "FROM occupancies o LEFT JOIN cabinet_policy_epochs e ON e.epoch=o.policy_epoch "
        "WHERE o.status='occupied'"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["policy_epoch"] is not None, f"{row['id']} 缺 policy_epoch"
        assert row["epoch_hours"] == row["overdue_hours_used"]


def test_open_pins_epoch_matching_live_generation(fresh_db):
    rec = _open(fresh_db, "OCC-T1", "CAB-A-04", "2026-11-18T00:00:00Z")
    assert rec["policy_epoch"] == 1
    assert rec["overdue_hours_used"] == 48
    stored = fresh_db.execute(
        "SELECT policy_epoch FROM occupancies WHERE id='OCC-T1'"
    ).fetchone()
    assert stored["policy_epoch"] == 1


def test_hot_update_keeps_old_epoch_and_pins_new(fresh_db):
    """夹具：先开占用，再热更新柜规 —— 旧占用 epoch 不变，新占用按新世代。"""
    old = _open(fresh_db, "OCC-OLD", "CAB-A-04", "2026-11-18T00:00:00Z")
    old_proof_before = epoch_mod.epoch_for(fresh_db, "OCC-OLD")

    _set_hours(fresh_db, 6)
    generation = epoch_mod.current_generation(fresh_db)
    fresh_db.commit()

    assert generation["policy_epoch"] > old["policy_epoch"]
    assert generation["overdue_hours"] == 6

    # 旧占用：自带 epoch 与 deadline 原样不动。
    old_proof = epoch_mod.epoch_for(fresh_db, "OCC-OLD")
    for key in (
        "policy_epoch",
        "epoch_overdue_hours",
        "reclaim_deadline_at",
        "overdue_hours_used",
        "epoch_pinned",
        "verified",
    ):
        assert old_proof[key] == old_proof_before[key], key
    assert old_proof["policy_epoch"] == old["policy_epoch"]
    assert old_proof["reclaim_deadline_at"] == "2026-11-20T00:00:00Z"
    assert old_proof["epoch_overdue_hours"] == 48
    assert old_proof["epoch_drift"] is True
    assert old_proof["verified"] is True

    # 新占用：按新世代落地。
    new = _open(fresh_db, "OCC-NEW", "CAB-B-02", "2026-11-18T00:00:00Z")
    assert new["policy_epoch"] == generation["policy_epoch"]
    assert new["overdue_hours_used"] == 6
    assert new["reclaim_deadline_at"] == "2026-11-18T06:00:00Z"


def test_proof_rejects_rewriting_pinned_epoch(fresh_db):
    _open(fresh_db, "OCC-T1", "CAB-A-04", "2026-11-18T00:00:00Z")
    _set_hours(fresh_db, 6)
    epoch_mod.current_generation(fresh_db)
    fresh_db.commit()
    for value in (2, 99):
        with pytest.raises(sqlite3.IntegrityError):
            fresh_db.execute(
                "UPDATE occupancies SET policy_epoch=? WHERE id='OCC-T1'", (value,)
            )
        fresh_db.rollback()
    row = fresh_db.execute("SELECT policy_epoch FROM occupancies WHERE id='OCC-T1'").fetchone()
    assert row["policy_epoch"] == 1


def test_certification_never_replays_with_live_generation(fresh_db):
    """核证读占用自带 epoch；现行世代变了也不得回填给旧占用。"""
    _open(fresh_db, "OCC-T1", "CAB-A-04", "2026-11-18T00:00:00Z")
    before = epoch_mod.epoch_for(fresh_db, "OCC-T1")

    _set_hours(fresh_db, 1)
    epoch_mod.current_generation(fresh_db)
    fresh_db.commit()

    after = epoch_mod.epoch_for(fresh_db, "OCC-T1")
    for key in ("policy_epoch", "reclaim_deadline_at", "overdue_hours_used", "epoch_overdue_hours"):
        assert after[key] == before[key], key
    assert after["live_policy_epoch"] != before["policy_epoch"]
    assert after["live_overdue_hours"] == 1


def test_reclaim_scan_carries_epoch_and_ignores_live(fresh_db):
    due, watermark = deadline_mod.due_for_reclaim(fresh_db, "2026-11-20T09:00:00Z")
    assert [r["occupancy_id"] for r in due] == ["OCC-CAB-A-02-E2"]
    assert due[0]["policy_epoch"] == 1
    assert due[0]["epoch_overdue_hours"] == 48

    _set_hours(fresh_db, 1)
    epoch_mod.current_generation(fresh_db)
    fresh_db.commit()
    again, again_wm = deadline_mod.due_for_reclaim(fresh_db, "2026-11-20T09:00:00Z")
    assert again == due
    assert again_wm == watermark


def test_open_without_epoch_is_rejected(fresh_db):
    conn = fresh_db
    conn.execute(
        "INSERT OR IGNORE INTO cells(id, cabinet_id, code, size_code, status) "
        "VALUES ('CAB-A-04', 'CAB-A', 'A04', 'L', 'free')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, "
            "reclaim_deadline_at, overdue_hours_used, policy_epoch, status) "
            "VALUES ('OCC-NOEPOCH', 'CAB-A-04', 'TN1', '2026-11-18T00:00:00Z', "
            "'2026-11-20T00:00:00Z', 48, NULL, 'occupied')"
        )
    conn.rollback()
    bad = conn.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE status='occupied' AND policy_epoch IS NULL"
    ).fetchone()["c"]
    assert bad == 0


def test_epoch_must_match_registered_hours(fresh_db):
    """epoch 与 hours 不一致（张冠李戴）同样被拒。"""
    fresh_db.execute(
        "INSERT OR IGNORE INTO cells(id, cabinet_id, code, size_code, status) "
        "VALUES ('CAB-A-04', 'CAB-A', 'A04', 'L', 'free')"
    )
    fresh_db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, "
            "reclaim_deadline_at, overdue_hours_used, policy_epoch, status) "
            "VALUES ('OCC-MISMATCH', 'CAB-A-04', 'TN1', '2026-11-18T00:00:00Z', "
            "'2026-11-18T06:00:00Z', 6, 1, 'occupied')"
        )
    fresh_db.rollback()
    assert fresh_db.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE id='OCC-MISMATCH'"
    ).fetchone()["c"] == 0


def test_half_open_failure_leaves_no_epochless_occupancy(fresh_db):
    """开账半截失败（deadline 算不出）须整笔回滚、无残留占用。"""
    fresh_db.execute(
        "INSERT OR IGNORE INTO cells(id, cabinet_id, code, size_code, status) "
        "VALUES ('CAB-A-04', 'CAB-A', 'A04', 'L', 'free')"
    )
    fresh_db.commit()
    before = fresh_db.execute("SELECT COUNT(*) AS c FROM occupancies").fetchone()["c"]
    with pytest.raises(ValueError):
        deadline_mod.open_occupancy(
            fresh_db,
            occupancy_id="OCC-HALF",
            cell_id="CAB-A-04",
            tracking_no="TN-HALF",
            opened_at="not-a-time",
        )
    fresh_db.rollback()
    after = fresh_db.execute("SELECT COUNT(*) AS c FROM occupancies").fetchone()["c"]
    assert after == before
    assert fresh_db.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE policy_epoch IS NULL"
    ).fetchone()["c"] == 0
    assert fresh_db.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE status='occupied' "
        "AND (reclaim_deadline_at IS NULL OR policy_epoch IS NULL)"
    ).fetchone()["c"] == 0


def test_epoch_registry_is_append_only(fresh_db):
    _set_hours(fresh_db, 6)
    epoch_mod.current_generation(fresh_db)
    fresh_db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute("UPDATE cabinet_policy_epochs SET overdue_hours=1 WHERE epoch=1")
    fresh_db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute("DELETE FROM cabinet_policy_epochs WHERE epoch=1")
    fresh_db.rollback()


def test_neighbor_open_does_not_touch_this_key(fresh_db):
    """邻键开账不串改本键 epoch。"""
    first = _open(fresh_db, "OCC-A", "CAB-A-04", "2026-11-18T00:00:00Z")
    snapshot = dict(
        fresh_db.execute(
            "SELECT policy_epoch, reclaim_deadline_at, overdue_hours_used "
            "FROM occupancies WHERE id='OCC-A'"
        ).fetchone()
    )
    _set_hours(fresh_db, 3)
    epoch_mod.current_generation(fresh_db)
    fresh_db.commit()
    _open(fresh_db, "OCC-B", "CAB-B-02", "2026-11-19T00:00:00Z")
    _open(fresh_db, "OCC-C", "CAB-B-01", "2026-11-19T01:00:00Z")
    again = dict(
        fresh_db.execute(
            "SELECT policy_epoch, reclaim_deadline_at, overdue_hours_used "
            "FROM occupancies WHERE id='OCC-A'"
        ).fetchone()
    )
    assert again == snapshot
    assert again["policy_epoch"] == first["policy_epoch"]


def test_ingest_carries_epoch_and_batch_failure_rolls_back(tmp_path, monkeypatch, fresh_db):
    """装载开账同样钉 epoch；批次内半截失败整批回滚，不留无 epoch 占用。"""
    from app.services import ingest as ingest_svc

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    good = json.loads(
        (db_mod.FIXTURES_DIR / "ingest_batch.json").read_text(encoding="utf-8")
    )
    (fixtures / "ingest_batch.json").write_text(json.dumps(good), encoding="utf-8")
    monkeypatch.setattr(ingest_svc, "FIXTURES_DIR", fixtures)
    report = ingest_svc.run_batch()
    assert report["accepted"] >= 1
    row = fresh_db.execute(
        "SELECT o.policy_epoch, e.overdue_hours FROM occupancies o "
        "JOIN cabinet_policy_epochs e ON e.epoch=o.policy_epoch "
        "WHERE o.id='OCC-CAB-A-03-E5'"
    ).fetchone()
    assert row is not None
    assert row["overdue_hours"] == 48

    # 半截失败：事件时间非法，开账抛错，整批不得提交。
    before = fresh_db.execute("SELECT COUNT(*) AS c FROM occupancies").fetchone()["c"]
    bad = {
        "events": [
            {
                "id": "E9",
                "cabinet_id": "CAB-A",
                "cell_id": "CAB-A-04",
                "event_type": "drop",
                "event_ts": "not-a-time",
                "tracking_no": "TN-BAD",
                "payload": {},
            }
        ]
    }
    (fixtures / "ingest_batch.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        ingest_svc.run_batch()
    fresh_db.rollback()
    assert fresh_db.execute("SELECT COUNT(*) AS c FROM occupancies").fetchone()["c"] == before
    assert fresh_db.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE id='OCC-CAB-A-04-E9'"
    ).fetchone()["c"] == 0


def test_api_policy_epoch_endpoints(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        one = client.get("/api/policy-epoch/OCC-CAB-A-02-E2")
        assert one.status_code == 200
        body = one.json()
        assert body["policy_epoch"] == 1
        assert body["epoch_pinned"] is True
        assert body["verified"] is True
        assert body["reclaim_deadline_at"] == "2026-11-20T09:00:00Z"

        missing = client.get("/api/policy-epoch/NOPE")
        assert missing.status_code == 404

        listing = client.get("/api/policy-epochs")
        assert listing.status_code == 200
        rows = listing.json()["rows"]
        assert rows and all(r["policy_epoch"] is not None for r in rows)

        # 改柜规后核证不回填：接口仍返回旧 epoch，live 仅作对照。
        assert client.put(
            "/api/settings/overdue-hours", json={"overdue_hours": 6}
        ).json()["policy_epoch"] == 2
        after = client.get("/api/policy-epoch/OCC-CAB-A-02-E2").json()
        assert after["policy_epoch"] == 1
        assert after["live_policy_epoch"] == 2
        assert after["reclaim_deadline_at"] == "2026-11-20T09:00:00Z"

        gens = client.get("/api/policy-epochs/generations").json()
        assert [g["overdue_hours"] for g in gens["generations"]] == [48, 6]
        assert gens["live"]["overdue_hours"] == 6


def test_migration_backfills_epoch_for_legacy_rows(tmp_path, monkeypatch):
    """存量库迁移：补列后旧开放占用也必须钉上 epoch，不得留空。"""
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
    seed_mod.init_db()
    row = conn.execute(
        "SELECT o.reclaim_deadline_at, o.overdue_hours_used, o.policy_epoch, "
        "e.overdue_hours AS epoch_hours FROM occupancies o "
        "LEFT JOIN cabinet_policy_epochs e ON e.epoch=o.policy_epoch WHERE o.id='OLD-1'"
    ).fetchone()
    conn.close()
    assert row["reclaim_deadline_at"] == "2026-11-18T10:00:00Z"
    assert row["overdue_hours_used"] == 10
    assert row["policy_epoch"] is not None
    assert row["epoch_hours"] == 10


def test_candidate_hours_collide_with_live_generation(fresh_db):
    """显式候选小时与现行世代不一致时拒绝开账，不得把旧世代钉给新占用。"""
    fresh_db.execute(
        "INSERT OR IGNORE INTO cells(id, cabinet_id, code, size_code, status) "
        "VALUES ('CAB-A-04', 'CAB-A', 'A04', 'L', 'free')"
    )
    fresh_db.commit()
    # 现行柜规是 48h；候选 6h 的世代尚不存在 → 对撞失败。
    with pytest.raises(ValueError):
        deadline_mod.open_occupancy(
            fresh_db,
            occupancy_id="OCC-COLLIDE",
            cell_id="CAB-A-04",
            tracking_no="TN-C",
            opened_at="2026-11-18T00:00:00Z",
            overdue_hours=6,
        )
    fresh_db.rollback()
    assert fresh_db.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE id='OCC-COLLIDE'"
    ).fetchone()["c"] == 0
    # 现行 48h 一致 → 正常开账，钉现行世代。
    rec = deadline_mod.open_occupancy(
        fresh_db,
        occupancy_id="OCC-MATCH",
        cell_id="CAB-A-04",
        tracking_no="TN-M",
        opened_at="2026-11-18T00:00:00Z",
        overdue_hours=48,
    )
    fresh_db.commit()
    assert rec["policy_epoch"] == epoch_mod.current_generation(fresh_db)["policy_epoch"]


def test_upgrade_replaces_old_trigger_definition(tmp_path, monkeypatch):
    """既有库升级：旧的 INSERT 守卫（无 epoch 子句）须被替换，不放行无 epoch 开账。"""
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings VALUES ('overdue_hours', '10');
        CREATE TABLE cells (id TEXT PRIMARY KEY, code TEXT);
        INSERT INTO cells VALUES ('C-1', 'A01');
        CREATE TABLE occupancies (
          id TEXT PRIMARY KEY,
          cell_id TEXT NOT NULL,
          tracking_no TEXT NOT NULL,
          opened_at TEXT NOT NULL,
          reclaim_deadline_at TEXT,
          overdue_hours_used INTEGER,
          closed_at TEXT,
          status TEXT NOT NULL DEFAULT 'occupied',
          last_event_id TEXT
        );
        CREATE TRIGGER occupancies_open_requires_deadline
        BEFORE INSERT ON occupancies
        FOR EACH ROW
        WHEN NEW.status='occupied'
         AND (NEW.reclaim_deadline_at IS NULL OR NEW.overdue_hours_used IS NULL)
        BEGIN SELECT RAISE(ABORT, 'old guard'); END;
        """
    )
    deadline_mod.ensure_schema(conn)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, "
            "reclaim_deadline_at, overdue_hours_used, policy_epoch, status) "
            "VALUES ('U-1', 'C-1', 'TN', '2026-11-18T00:00:00Z', "
            "'2026-11-18T10:00:00Z', 10, NULL, 'occupied')"
        )
    conn.rollback()
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE id='U-1'"
    ).fetchone()["c"] == 0
    conn.close()


def test_reopen_into_occupied_without_epoch_is_rejected(fresh_db):
    """UPDATE 路径把无 epoch 的已释放行改回开放也须被拒，堵住旁路。"""
    fresh_db.execute(
        "INSERT OR IGNORE INTO cells(id, cabinet_id, code, size_code, status) "
        "VALUES ('CAB-A-04', 'CAB-A', 'A04', 'L', 'free')"
    )
    # 历史行：已释放、无 deadline / epoch（INSERT 守卫只管 occupied 行）。
    fresh_db.execute(
        "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, "
        "reclaim_deadline_at, overdue_hours_used, policy_epoch, closed_at, status) "
        "VALUES ('OCC-LEGACY', 'CAB-A-04', 'TN-L', '2026-11-01T00:00:00Z', "
        "NULL, NULL, NULL, '2026-11-02T00:00:00Z', 'released')"
    )
    fresh_db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "UPDATE occupancies SET status='occupied' WHERE id='OCC-LEGACY'"
        )
    fresh_db.rollback()
    kept = fresh_db.execute(
        "SELECT status FROM occupancies WHERE id='OCC-LEGACY'"
    ).fetchone()
    assert kept["status"] == "released"
