"""开账柜规世代钉定（open_policy_epoch）。

背景：到期点（``reclaim_deadline_at``）在开账时已物化，但开账当时的柜规
世代若不被钉死，柜规热更新后核证页 / 回收扫描会用「现行世代」重新解释旧
占用，口径分叉；半截开账失败还可能留下没有世代归属的开放占用。

落库形态（二选一，此处择「占用行不可变列」）：

- ``occupancies.policy_epoch``：开账时与到期点同一条 INSERT 落库的柜规世代
  号；本模块的 ``cabinet_policy_epochs`` 只是「柜规世代登记表」（世代 →
  当时 overdue_hours 的只读档案），不是占用与世代的绑定存储。

提交形态（二选一，此处择「开账同事务写到期点并钉 epoch，缺一回滚」）：

- 占用行 INSERT 一次写全 ``reclaim_deadline_at`` / ``overdue_hours_used`` /
  ``policy_epoch``，任一缺失或世代对不上即由触发器 ABORT；调用方事务整体
  回滚，不留半截开放占用，也不留孤立的世代登记。

约束：

- 成功开账不得 epoch 空：INSERT 守卫要求 occupied 行的 epoch 非空且登记表
  中存在同世代、同小时数；
- 热更新不得改写旧占用：占用侧 epoch 与 deadline 一样由 BEFORE UPDATE 触发器
  锁定；世代登记表 append-only，不可改不可删；
- 核证 / 回收只认占用自带 epoch：``epoch_for`` / ``epoch_proofs`` 只读占用行
  的 ``policy_epoch``，现行世代仅作 ``live_*`` 对照展示，绝不回填；
- 邻键开账不串改本键：epoch 存在占用行上，开 A 键不会触碰 B 键。
"""

from __future__ import annotations

from datetime import datetime, timezone

STATUS = "active"

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REGISTRY_TABLE = "cabinet_policy_epochs"
DEFAULT_HOURS_FALLBACK = 48

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS cabinet_policy_epochs (
  epoch INTEGER PRIMARY KEY,
  overdue_hours INTEGER NOT NULL CHECK (overdue_hours >= 1),
  created_at TEXT NOT NULL
)
"""

REGISTRY_GUARDS = (
    """
    CREATE TRIGGER IF NOT EXISTS cabinet_policy_epochs_no_update
    BEFORE UPDATE ON cabinet_policy_epochs
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, '柜规世代登记不可改写');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS cabinet_policy_epochs_no_delete
    BEFORE DELETE ON cabinet_policy_epochs
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, '柜规世代登记不可删除');
    END
    """,
)


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def _live_hours(conn) -> int:
    """现行柜规小时（只读；延迟导入避免与物化模块循环依赖）。"""
    from app.modules.reclaim_deadline_materialize import read_overdue_hours

    return read_overdue_hours(conn)


def _live_raw(conn):
    """现行柜规原始值（settings 行）；缺行时 None。"""
    row = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    return None if row is None else row["value"]


def _latest(conn):
    return conn.execute(
        "SELECT epoch, overdue_hours, created_at FROM cabinet_policy_epochs "
        "ORDER BY epoch DESC LIMIT 1"
    ).fetchone()


def _append_generation(conn, hours: int) -> int:
    """追加一个柜规世代（append-only）；返回新世代号。迁移路径专用。"""
    conn.execute(
        "INSERT INTO cabinet_policy_epochs(epoch, overdue_hours, created_at) "
        "SELECT COALESCE(MAX(epoch), 0) + 1, ?, ? FROM cabinet_policy_epochs",
        (int(hours), _now_ts()),
    )
    row = conn.execute("SELECT MAX(epoch) AS epoch FROM cabinet_policy_epochs").fetchone()
    return int(row["epoch"])


def _append_if_still_live(conn, hours: int) -> bool:
    """仅当 settings 仍等于 ``hours`` 时追加世代（对撞失败则 0 行，调用方重试）。

    写入语句本身会拿到写锁；把「现行柜规仍是我读到的值」作为 INSERT …
    SELECT 的过滤条件，可避免热更新并发交错时登记一个从未在用的世代。
    settings 缺行 / 非法值时与 ``read_overdue_hours`` 的默认值口径对齐。
    """
    raw = _live_raw(conn)
    try:
        effective = int(raw) if raw is not None else DEFAULT_HOURS_FALLBACK
    except (TypeError, ValueError):
        effective = DEFAULT_HOURS_FALLBACK
    if effective != int(hours):
        return False
    if raw is None:
        where = "(SELECT value FROM settings WHERE key='overdue_hours') IS NULL OR " \
                "(SELECT value FROM settings WHERE key='overdue_hours') = ?"
        params = (int(hours), _now_ts(), str(int(hours)))
    else:
        where = "(SELECT value FROM settings WHERE key='overdue_hours') = ?"
        params = (int(hours), _now_ts(), raw)
    cur = conn.execute(
        "INSERT INTO cabinet_policy_epochs(epoch, overdue_hours, created_at) "
        "SELECT COALESCE(MAX(epoch), 0) + 1, ?, ? FROM cabinet_policy_epochs "
        "WHERE " + where,
        params,
    )
    return cur.rowcount > 0


def current_generation(conn) -> dict:
    """读取现行柜规世代；若 settings 与登记表最新世代不一致则追加新世代。

    任何路径（柜规热更新接口、直接改 settings、存量迁移）改动了柜规小时数，
    下一次开账都会在这里与现行世代「对撞」，对不上就追加，保证开账钉到的
    世代与当时柜规一致。追加语句以「settings 仍是读到的值」为条件，热更新
    并发交错时 0 行写入即重读重试，不会登记未在用的世代。
    """
    for _ in range(16):
        hours = _live_hours(conn)
        latest = _latest(conn)
        if latest is not None and int(latest["overdue_hours"]) == hours:
            return {
                "policy_epoch": int(latest["epoch"]),
                "overdue_hours": int(latest["overdue_hours"]),
                "created_at": latest["created_at"],
            }
        if not _append_if_still_live(conn, hours):
            # 柜规在我们读取之后又被改动了：重读现行值再对撞。
            continue
    raise RuntimeError("柜规世代无法钉定：settings 与世代登记持续对撞")


def epoch_for_hours(conn, overdue_hours) -> int:
    """取与给定小时数一致的世代号；没有则按该小时数追加一个世代。"""
    from app.modules.reclaim_deadline_materialize import validate_hours

    hours = validate_hours(overdue_hours)
    row = conn.execute(
        "SELECT epoch FROM cabinet_policy_epochs WHERE overdue_hours=? "
        "ORDER BY epoch DESC LIMIT 1",
        (hours,),
    ).fetchone()
    if row is not None:
        return int(row["epoch"])
    return _append_generation(conn, hours)


def _legacy_hours(conn) -> list[int]:
    """存量占用用过的小时数（按最早开账排序），供迁移世代登记。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(occupancies)").fetchall()}
    if "overdue_hours_used" not in cols:
        return []
    rows = conn.execute(
        "SELECT overdue_hours_used AS hours, MIN(opened_at) AS first_opened "
        "FROM occupancies WHERE overdue_hours_used IS NOT NULL "
        "GROUP BY overdue_hours_used ORDER BY first_opened, overdue_hours_used"
    ).fetchall()
    return [int(row["hours"]) for row in rows if int(row["hours"]) >= 1]


def ensure_schema(conn) -> None:
    """补齐不可变 policy_epoch 列、世代登记表与守卫（幂等）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(occupancies)").fetchall()}
    if cols and "policy_epoch" not in cols:
        conn.execute("ALTER TABLE occupancies ADD COLUMN policy_epoch INTEGER")

    conn.execute(REGISTRY_DDL)
    for ddl in REGISTRY_GUARDS:
        conn.execute(ddl)

    if conn.execute("SELECT COUNT(*) AS c FROM cabinet_policy_epochs").fetchone()["c"] == 0:
        # 存量占用先登记其历史小时数，再登记现行柜规，世代按时间大致有序。
        for hours in _legacy_hours(conn):
            _append_generation(conn, hours)
    current_generation(conn)


def _expected_deadline(opened_at: str, hours: int):
    from app.modules.reclaim_deadline_materialize import materialize_deadline

    try:
        return materialize_deadline(opened_at, hours)
    except (TypeError, ValueError):
        return None


def _proof_from_row(row, live_epoch, live_hours) -> dict:
    epoch = row["policy_epoch"]
    epoch_hours = row["epoch_hours"]
    deadline = row["reclaim_deadline_at"]
    pinned = epoch is not None
    resolvable = epoch_hours is not None
    hours_match = bool(resolvable and row["overdue_hours_used"] == epoch_hours)
    expected = _expected_deadline(row["opened_at"], int(epoch_hours)) if resolvable else None
    deadline_matches = bool(deadline is not None and expected is not None and deadline == expected)
    epoch_is_live = bool(pinned and live_epoch is not None and int(epoch) == int(live_epoch))

    return {
        "occupancy_id": row["id"],
        "cell_id": row["cell_id"],
        "tracking_no": row["tracking_no"],
        "opened_at": row["opened_at"],
        "reclaim_deadline_at": deadline,
        "overdue_hours_used": row["overdue_hours_used"],
        "status": row["status"],
        # 占用自带的开账世代：核证只认它，不拿 live 世代替换。
        "policy_epoch": epoch,
        "epoch_overdue_hours": int(epoch_hours) if resolvable else None,
        "epoch_created_at": row["epoch_created_at"],
        "epoch_pinned": pinned,
        "epoch_resolvable": resolvable,
        "epoch_hours_match": hours_match,
        "deadline_matches_epoch": deadline_matches,
        "deadline_missing": deadline is None,
        "verified": bool(pinned and resolvable and hours_match and deadline_matches),
        # 现行世代仅作对照：核证 / 回收不得用它回放旧占用。
        "live_policy_epoch": int(live_epoch) if live_epoch is not None else None,
        "live_overdue_hours": int(live_hours) if live_hours is not None else None,
        "live_hours": int(live_hours) if live_hours is not None else None,
        "epoch_is_live": epoch_is_live,
        "epoch_drift": bool(pinned and live_epoch is not None and not epoch_is_live),
    }


_PROOF_SELECT = (
    "SELECT o.id, o.cell_id, o.tracking_no, o.opened_at, o.reclaim_deadline_at, "
    "o.overdue_hours_used, o.status, o.policy_epoch, "
    "e.overdue_hours AS epoch_hours, e.created_at AS epoch_created_at "
    "FROM occupancies o "
    "LEFT JOIN cabinet_policy_epochs e ON e.epoch = o.policy_epoch"
)


def live_epoch(conn):
    """现行柜规对应的已登记世代；settings 已改但尚未登记时为 None（不回填）。

    只读：不追加世代。开账时由 ``current_generation`` 负责在对撞时追加。
    """
    latest = _latest(conn)
    hours = _live_hours(conn)
    if latest is None or int(latest["overdue_hours"]) != hours:
        return None
    return int(latest["epoch"])


def live_generation(conn) -> dict:
    """只读现行柜规世代视图：epoch 为 None 表示现行柜规尚未登记世代。"""
    epoch = live_epoch(conn)
    latest = _latest(conn)
    return {
        "policy_epoch": epoch,
        "overdue_hours": _live_hours(conn),
        "registered": epoch is not None,
        "latest_registered_epoch": int(latest["epoch"]) if latest is not None else None,
        "created_at": latest["created_at"] if epoch is not None and latest is not None else None,
    }


def epoch_proofs(conn, status: str | None = None) -> list[dict]:
    """只读核证列表：逐占用给出钉定世代与一致性判定。"""
    live_ep = live_epoch(conn)
    live_hours = _live_hours(conn)
    sql = _PROOF_SELECT
    params: tuple = ()
    if status:
        sql += " WHERE o.status=?"
        params = (status,)
    sql += " ORDER BY o.opened_at DESC, o.id DESC"
    return [
        _proof_from_row(row, live_ep, live_hours)
        for row in conn.execute(sql, params).fetchall()
    ]


def epoch_for(conn, occupancy_id: str):
    """只读核证单笔占用：返回占用自带的开账世代，绝不读取即回填现行世代。"""
    row = conn.execute(_PROOF_SELECT + " WHERE o.id=?", (occupancy_id,)).fetchone()
    if row is None:
        return None
    return _proof_from_row(row, live_epoch(conn), _live_hours(conn))


def generations(conn) -> list[dict]:
    """只读读出柜规世代登记表（append-only 档案）。"""
    rows = conn.execute(
        "SELECT epoch, overdue_hours, created_at FROM cabinet_policy_epochs ORDER BY epoch"
    ).fetchall()
    return [
        {
            "policy_epoch": int(row["epoch"]),
            "overdue_hours": int(row["overdue_hours"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def pin_legacy(conn) -> None:
    """给存量开放 / 已释放占用按已物化小时数补钉世代（幂等，仅补 NULL）。

    迁移路径专用：存量行没有开账当时的世代证据，只能按它已物化的
    ``overdue_hours_used``（或由 deadline 反推）登记并钉定；此后与正常开账
    一样不可改写。``NULL`` 行不会再被核证 / 回收当作「无世代占用」放过。
    """
    from app.modules.reclaim_deadline_materialize import parse_ts

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(occupancies)").fetchall()}
    if "policy_epoch" not in cols:
        return
    epoch_col = "policy_epoch"
    hours_col = "overdue_hours_used" if "overdue_hours_used" in cols else None
    deadline_col = "reclaim_deadline_at" if "reclaim_deadline_at" in cols else None

    sql = "SELECT id, opened_at, " + (f"{hours_col} AS hours, " if hours_col else "NULL AS hours, ")
    sql += f"{deadline_col} AS deadline" if deadline_col else "NULL AS deadline"
    sql += f" FROM occupancies WHERE {epoch_col} IS NULL"

    live_hours = _live_hours(conn)
    for row in conn.execute(sql).fetchall():
        hours = row["hours"]
        if hours is None and row["deadline"]:
            delta = parse_ts(row["deadline"]) - parse_ts(row["opened_at"])
            hours = int(round(delta.total_seconds() / 3600))
        if hours is None or int(hours) < 1:
            hours = live_hours
        hours = int(hours)
        epoch = epoch_for_hours(conn, hours)
        conn.execute(
            f"UPDATE occupancies SET {epoch_col}=? WHERE id=? AND {epoch_col} IS NULL",
            (epoch, row["id"]),
        )
