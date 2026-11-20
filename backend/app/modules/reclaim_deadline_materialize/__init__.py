"""开账物化回收截止时刻（reclaim_deadline_materialize）。

落库形态（二选一，此处择「占用不可变列」）：

- ``occupancies.reclaim_deadline_at``：drop 开占用时，由 ``opened_at`` 与
  当时柜规 ``overdue_hours`` 算出的绝对时刻，随占用同一条 INSERT 落库；
- ``occupancies.overdue_hours_used``：物化时采用的柜规小时数，供核证对账；
- ``occupancies.policy_epoch``：物化时柜规的世代号（由 ``open_policy_epoch``
  登记），与 deadline 同一条 INSERT 落库，事后不可改写。

约束：

- 失败闭：开占用与写 deadline / 钉柜规世代同一条 INSERT / 同一事务；``CHECK``
  与 BEFORE INSERT 触发器兜底，禁止「占用已 open 但 deadline 或 epoch 为空」；
  半截失败由调用方事务整体回滚，不留无 epoch 的开放占用。
- 不可变：已落地的 deadline / epoch（及 opened_at、overdue_hours_used）由
  BEFORE UPDATE 触发器锁定，事后改柜规只能影响之后新开的占用。
- 只读：回收 / 核证读该列本身，不在查询时用逾期小时临时相加，也不拿现行
  柜规世代回放旧占用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

STATUS = "active"

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_OVERDUE_HOURS = 48

INSERT_SQL = """
INSERT INTO occupancies(
  id, cell_id, tracking_no, opened_at, reclaim_deadline_at,
  overdue_hours_used, policy_epoch, closed_at, status, last_event_id
) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'occupied', ?)
"""

INSERT_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS occupancies_open_requires_deadline
BEFORE INSERT ON occupancies
FOR EACH ROW
WHEN NEW.status = 'occupied'
 AND (
      NEW.reclaim_deadline_at IS NULL
   OR NEW.overdue_hours_used IS NULL
   OR NEW.policy_epoch IS NULL
   OR NOT EXISTS (
        SELECT 1 FROM cabinet_policy_epochs e
        WHERE e.epoch = NEW.policy_epoch
          AND e.overdue_hours = NEW.overdue_hours_used
      )
 )
BEGIN
  SELECT RAISE(ABORT, '开占用必须同笔物化 reclaim_deadline_at 并钉定 policy_epoch');
END
"""

IMMUTABLE_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS occupancies_deadline_immutable
BEFORE UPDATE OF opened_at, reclaim_deadline_at, overdue_hours_used ON occupancies
FOR EACH ROW
WHEN OLD.reclaim_deadline_at IS NOT NULL
 AND (
      NEW.reclaim_deadline_at IS NOT OLD.reclaim_deadline_at
   OR NEW.overdue_hours_used IS NOT OLD.overdue_hours_used
   OR NEW.opened_at IS NOT OLD.opened_at
 )
BEGIN
  SELECT RAISE(ABORT, 'reclaim_deadline 已物化，不可改写');
END
"""

EPOCH_IMMUTABLE_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS occupancies_policy_epoch_immutable
BEFORE UPDATE OF policy_epoch ON occupancies
FOR EACH ROW
WHEN OLD.policy_epoch IS NOT NULL
 AND NEW.policy_epoch IS NOT OLD.policy_epoch
BEGIN
  SELECT RAISE(ABORT, 'policy_epoch 已钉定，不可改写');
END
"""

OCCUPY_REQUIRES_EPOCH_SQL = """
CREATE TRIGGER IF NOT EXISTS occupancies_occupy_requires_epoch
BEFORE UPDATE OF status ON occupancies
FOR EACH ROW
WHEN NEW.status = 'occupied'
 AND (NEW.reclaim_deadline_at IS NULL OR NEW.policy_epoch IS NULL)
BEGIN
  SELECT RAISE(ABORT, '开放占用必须带 reclaim_deadline_at 与 policy_epoch');
END
"""

# (触发器名, 建表语句)：ensure_schema 以 DROP + CREATE 重建，确保升级时
# 旧定义（无 epoch 子句）被替换而不是被 IF NOT EXISTS 沿用。
_OCCUPANCY_GUARDS = (
    ("occupancies_open_requires_deadline", INSERT_GUARD_SQL),
    ("occupancies_deadline_immutable", IMMUTABLE_GUARD_SQL),
    ("occupancies_policy_epoch_immutable", EPOCH_IMMUTABLE_GUARD_SQL),
    ("occupancies_occupy_requires_epoch", OCCUPY_REQUIRES_EPOCH_SQL),
)


def validate_hours(overdue_hours) -> int:
    """柜规小时须为正整数；非法值让开账失败，而不是落一个坏 deadline。"""
    if isinstance(overdue_hours, bool):
        raise ValueError("overdue_hours 须为正整数")
    try:
        hours = int(overdue_hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("overdue_hours 须为正整数") from exc
    if hours < 1:
        raise ValueError("overdue_hours 须为 >= 1 的整数")
    return hours


def read_overdue_hours(conn) -> int:
    """读取现行柜规（只在开账 / 迁移时读，回收与核证不再读它算截止）。"""
    row = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    if row is None:
        return DEFAULT_OVERDUE_HOURS
    try:
        return validate_hours(row["value"])
    except ValueError:
        return DEFAULT_OVERDUE_HOURS


def parse_ts(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def materialize_deadline(opened_at: str, overdue_hours) -> str:
    """由 opened_at 与当时柜规算出绝对截止时刻（唯一计算入口）。"""
    hours = validate_hours(overdue_hours)
    return format_ts(parse_ts(opened_at) + timedelta(hours=hours))


def open_occupancy(
    conn,
    *,
    occupancy_id: str,
    cell_id: str,
    tracking_no: str,
    opened_at: str,
    last_event_id: str | None = None,
    overdue_hours=None,
):
    """开占用：在调用方事务内，一条 INSERT 同时落占用、deadline 与柜规世代。

    ``overdue_hours`` 不传时读当前柜规并取现行世代；传入时视为候选世代，
    须与现行柜规世代「对撞」一致，不一致即失败（调用方回滚重试），绝不把
    旧世代悄悄钉到新开占用上。两条路径都保证三者同笔落库，不存在中间态。
    """
    from app.modules import open_policy_epoch as epoch_mod

    candidate = validate_hours(overdue_hours) if overdue_hours is not None else None
    generation = epoch_mod.current_generation(conn)
    hours = generation["overdue_hours"]
    if candidate is not None and candidate != hours:
        raise ValueError(
            f"候选柜规世代对撞：候选 {candidate}h 与现行世代 "
            f"{generation['policy_epoch']} ({hours}h) 不一致，拒绝开账"
        )
    if candidate is None:
        candidate = hours
    epoch = generation["policy_epoch"]
    opened = format_ts(parse_ts(opened_at))
    deadline = materialize_deadline(opened, candidate)
    conn.execute(
        INSERT_SQL,
        (occupancy_id, cell_id, tracking_no, opened, deadline, candidate, epoch, last_event_id),
    )
    return {
        "id": occupancy_id,
        "cell_id": cell_id,
        "tracking_no": tracking_no,
        "opened_at": opened,
        "reclaim_deadline_at": deadline,
        "overdue_hours_used": candidate,
        "policy_epoch": epoch,
        "status": "occupied",
    }


def ensure_schema(conn) -> None:
    """补齐不可变列与守卫（幂等）；存量行按迁移时柜规补物化并钉世代。"""
    from app.modules import open_policy_epoch as epoch_mod

    # 世代登记表与 policy_epoch 列必须先于下文物化守卫就位。
    epoch_mod.ensure_schema(conn)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(occupancies)").fetchall()}
    if "reclaim_deadline_at" not in cols:
        conn.execute("ALTER TABLE occupancies ADD COLUMN reclaim_deadline_at TEXT")
    if "overdue_hours_used" not in cols:
        conn.execute("ALTER TABLE occupancies ADD COLUMN overdue_hours_used INTEGER")

    hours = read_overdue_hours(conn)
    legacy = conn.execute(
        "SELECT id, opened_at FROM occupancies WHERE reclaim_deadline_at IS NULL"
    ).fetchall()
    for row in legacy:
        conn.execute(
            "UPDATE occupancies SET reclaim_deadline_at=?, overdue_hours_used=? WHERE id=?",
            (materialize_deadline(row["opened_at"], hours), hours, row["id"]),
        )

    # 守卫用 DROP + CREATE 重建：升级既有库时旧触发器的定义（缺 epoch 子句）
    # 必须被替换，CREATE TRIGGER IF NOT EXISTS 会静默沿用旧定义。
    for name, ddl in _OCCUPANCY_GUARDS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_occupancies_reclaim_deadline "
        "ON occupancies(status, reclaim_deadline_at)"
    )

    # 存量行（含刚补物化的）按其物化小时数钉定世代，杜绝无 epoch 开放占用。
    epoch_mod.pin_legacy(conn)


_PROOF_COLUMNS = (
    "o.id, o.cell_id, o.tracking_no, o.opened_at, o.reclaim_deadline_at, "
    "o.overdue_hours_used, o.policy_epoch, o.status, "
    "e.overdue_hours AS epoch_overdue_hours"
)
_PROOF_FROM = (
    " FROM occupancies o "
    "LEFT JOIN cabinet_policy_epochs e ON e.epoch = o.policy_epoch"
)


def due_for_reclaim(conn, now_iso: str):
    """回收扫描：只比已落地的 deadline，不读柜规、不临时相加、不按现行世代回放。

    返回 ``(rows, watermark)``；watermark 供增量扫描续跑。每行自带开账世代。
    """
    now = format_ts(parse_ts(now_iso))
    rows = conn.execute(
        "SELECT " + _PROOF_COLUMNS + _PROOF_FROM + " "
        "WHERE o.status='occupied' AND o.reclaim_deadline_at IS NOT NULL "
        "AND o.reclaim_deadline_at <= ? ORDER BY o.reclaim_deadline_at, o.id",
        (now,),
    ).fetchall()
    watermark = max((row["reclaim_deadline_at"] for row in rows), default=None)
    return [_proof(row) for row in rows], watermark


def deadline_proofs(conn, status: str | None = None):
    """只读核证列表：占用、opened_at、deadline、所用小时与开账世代。"""
    sql = "SELECT " + _PROOF_COLUMNS + _PROOF_FROM
    params: tuple = ()
    if status:
        sql += " WHERE o.status=?"
        params = (status,)
    sql += " ORDER BY o.opened_at DESC, o.id DESC"
    return [_proof(row) for row in conn.execute(sql, params).fetchall()]


def deadline_proof(conn, occupancy_id: str):
    row = conn.execute(
        "SELECT " + _PROOF_COLUMNS + _PROOF_FROM + " WHERE o.id=?",
        (occupancy_id,),
    ).fetchone()
    return _proof(row) if row else None


def _proof(row) -> dict:
    deadline = row["reclaim_deadline_at"]
    hours = row["overdue_hours_used"]
    if hours is None and deadline:
        # 仅兼容早期存量：deadline 已落地但未记小时时，由绝对时刻反推。
        delta = parse_ts(deadline) - parse_ts(row["opened_at"])
        hours = int(round(delta.total_seconds() / 3600))
    epoch = row["policy_epoch"] if "policy_epoch" in row.keys() else None
    epoch_hours = row["epoch_overdue_hours"] if "epoch_overdue_hours" in row.keys() else None
    return {
        "occupancy_id": row["id"],
        "cell_id": row["cell_id"],
        "tracking_no": row["tracking_no"],
        "opened_at": row["opened_at"],
        "reclaim_deadline_at": deadline,
        "overdue_hours_used": hours,
        "policy_epoch": epoch,
        "epoch_overdue_hours": epoch_hours,
        "epoch_pinned": epoch is not None,
        "status": row["status"],
        "deadline_missing": deadline is None,
    }
