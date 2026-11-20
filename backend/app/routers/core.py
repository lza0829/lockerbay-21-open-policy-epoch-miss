from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import open_policy_epoch as epoch_svc
from app.modules import reclaim_deadline_materialize as deadline_mod
from app.seed import overdue_hours
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/reclaim/deadlines")
def reclaim_deadlines(status: str | None = None):
    """deadline 核证只读接口：占用、opened_at、deadline、所用小时。"""
    conn = connect()
    try:
        return {
            "rows": deadline_mod.deadline_proofs(conn, status=status),
            "current_overdue_hours": overdue_hours(conn),
        }
    finally:
        conn.close()


@router.get("/reclaim/deadlines/{occupancy_id}")
def reclaim_deadline(occupancy_id: str):
    conn = connect()
    try:
        proof = deadline_mod.deadline_proof(conn, occupancy_id)
        if proof is None:
            raise HTTPException(404, "占用不存在")
        return proof
    finally:
        conn.close()


@router.get("/reclaim/due")
def reclaim_due(now: str | None = None):
    """回收扫描：只读已物化的 deadline；`now` 缺省取当前 UTC。"""
    now_iso = now or datetime.now(timezone.utc).strftime(deadline_mod.TS_FORMAT)
    try:
        deadline_mod.parse_ts(now_iso)
    except ValueError:
        raise HTTPException(400, "now 须为 ISO8601 时刻")
    conn = connect()
    try:
        rows, watermark = deadline_mod.due_for_reclaim(conn, now_iso)
        return {"now": now_iso, "rows": rows, "watermark": watermark, "count": len(rows)}
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"overdue_hours": overdue_hours(conn)}
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        # 柜规热更新在与改柜规同一事务内登记世代；旧占用锁定原世代不受影响。
        generation = epoch_svc.current_generation(conn)
        conn.commit()
        return {"overdue_hours": hours, "policy_epoch": generation["policy_epoch"]}
    finally:
        conn.close()

# --- plant routes ---
@router.get("/policy-epochs")
def policy_epoch_proofs(status: str | None = None):
    """epoch 核证只读列表：每笔占用自带的开账世代与一致性判定。

    只读占用行的 ``policy_epoch``；``live_*`` 仅作现行柜规对照，不参与回放。
    """
    conn = connect()
    try:
        return {
            "rows": epoch_svc.epoch_proofs(conn, status=status),
            "generations": epoch_svc.generations(conn),
        }
    finally:
        conn.close()


@router.get("/policy-epochs/generations")
def policy_epoch_generations():
    """柜规世代登记（append-only 只读档案）；只读，不追加。"""
    conn = connect()
    try:
        return {
            "live": epoch_svc.live_generation(conn),
            "generations": epoch_svc.generations(conn),
        }
    finally:
        conn.close()


@router.get("/policy-epoch/{occupancy_id}")
def policy_epoch(occupancy_id: str):
    conn = connect()
    try:
        proof = epoch_svc.epoch_for(conn, occupancy_id)
        if proof is None:
            raise HTTPException(404, "占用不存在")
        return proof
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "occupancy_id" in out and "occ_id" not in out:
        out["occ_id"] = out.get("occupancy_id")
    if "cell_id" in out and "locker_cell" not in out:
        out["locker_cell"] = out.get("cell_id")
    if "tracking_no" in out and "parcel_key" not in out:
        out["parcel_key"] = out.get("tracking_no")
    if "verified" not in out:
        out["verified"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True}
    if extra:
        data.update(extra)
    return data

