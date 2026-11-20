
STATUS = "partial"


def epoch_for(conn, occupancy_id: str) -> dict:
    occ = conn.execute(
        "SELECT id, reclaim_deadline_at, opened_at FROM occupancies WHERE id=?",
        (occupancy_id,),
    ).fetchone()
    if not occ:
        return {"occupancy_id": occupancy_id, "policy_epoch": None}
    # 读现行柜规世代，不钉开账当时
    cur = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    return {
        "occupancy_id": occupancy_id,
        "reclaim_deadline_at": occ["reclaim_deadline_at"] if "reclaim_deadline_at" in occ.keys() else None,
        "policy_epoch": None,
        "live_hours": int(cur["value"]) if cur else None,
    }
