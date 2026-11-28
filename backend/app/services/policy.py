"""回退拨「在途跟随」口径——动工前已钉死，只此一套真源。

选定架构：**收口当刻以板规现档为准（在途跟现档）**。

* 唯一真源：`rollback_policy`（mode / hold_sec / version）。拨杆界面、在途单、校验屏、
  拧档流水四路都从它派生，不再各自存一份可被独立改写的「回退档观感」。
* 在途单（status=写闪中）：拧档时在同一笔事务内把回退档同步到所有在途单，并为每一单落一条
  `policy_follows` 跟随痕；`policy_flips` 记本次拧档的版本前后与随单数。读侧据此可重放
  「哪一单跟了哪一版」。
* 已收口单（status!=写闪中）：回退档在收口当刻冻结（`policy_frozen_at`），事后拧档**不得改写**。
  冻结档只读，校验屏按状态分列。
* 两臂不混：在途 = 跟现档（带可核痕）；已收口 = 跟冻结档（只读）。同一格既不许声称跟新档又不留痕。

本模块只负责回退档跟随；硬退与切槽抢点另题。
"""
from __future__ import annotations

import sqlite3


def read_policy(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT mode, hold_sec, version FROM rollback_policy WHERE id=1"
    ).fetchone()
    return {"mode": row["mode"], "hold_sec": row["hold_sec"], "version": row["version"]}


def _record_follow(
    conn: sqlite3.Connection,
    flip_id: int | None,
    order_id: int,
    from_mode: str | None,
    to_mode: str,
    outcome: str,
    at: str,
) -> None:
    """落一条跟随痕。测试会替换此函数以模拟「半截脏跟随」，验证整笔回滚。"""
    conn.execute(
        """
        INSERT INTO policy_follows(flip_id, order_id, from_mode, to_mode, outcome, at)
        VALUES (?,?,?,?,?,?)
        """,
        (flip_id, order_id, from_mode, to_mode, outcome, at),
    )


def in_flight_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # 拧档同步只认单据本身：不依赖 boards 联接，孤儿单也不得漏跟。
    return conn.execute(
        """
        SELECT id, policy_mode, policy_hold_sec, policy_version
        FROM flash_orders
        WHERE status = '写闪中'
        ORDER BY id
        """
    ).fetchall()


def apply_flip(conn: sqlite3.Connection, new_mode: str, new_hold_sec: int, at: str) -> dict:
    """拧档：改真源 + 记流水 + 同笔同步所有在途单并落跟随痕。调用方须已开写事务。

    返回 {changed, flip_id, affected, policy}。档位与 hold 都没变时不升版本、不落流水。
    """
    old = read_policy(conn)
    if old["mode"] == new_mode and int(old["hold_sec"]) == int(new_hold_sec):
        return {"changed": False, "flip_id": None, "affected": 0, "policy": old}

    new_version = int(old["version"]) + 1
    conn.execute(
        "UPDATE rollback_policy SET mode=?, hold_sec=?, version=? WHERE id=1",
        (new_mode, new_hold_sec, new_version),
    )
    cur = conn.execute(
        """
        INSERT INTO policy_flips(from_mode, to_mode, from_hold_sec, to_hold_sec,
                                 version_before, version_after, affected_orders, at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (old["mode"], new_mode, old["hold_sec"], new_hold_sec,
         old["version"], new_version, 0, at),
    )
    flip_id = cur.lastrowid

    inflight = in_flight_orders(conn)
    for o in inflight:
        conn.execute(
            "UPDATE flash_orders SET policy_mode=?, policy_hold_sec=?, policy_version=? WHERE id=?",
            (new_mode, new_hold_sec, new_version, o["id"]),
        )
        _record_follow(
            conn, flip_id, o["id"], o["policy_mode"] or old["mode"], new_mode, "跟随", at
        )
    conn.execute(
        "UPDATE policy_flips SET affected_orders=? WHERE id=?", (len(inflight), flip_id)
    )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("回退拨", "policy",
         f"拧档 {old['mode']}→{new_mode} (v{old['version']}→v{new_version})，随单 {len(inflight)}",
         at),
    )
    return {
        "changed": True,
        "flip_id": flip_id,
        "affected": len(inflight),
        "policy": {"mode": new_mode, "hold_sec": new_hold_sec, "version": new_version},
    }


def reconcile_for_close(conn: sqlite3.Connection, order: sqlite3.Row, at: str) -> dict:
    """收口当刻取板规现档为准。

    正常路径下拧档已把在途单同步到现档，此处读到即相等；若发现单内回退档与现档不一致
    （例如历史脏数据），补一条收口同步痕，绝不静默偷改。
    """
    pol = read_policy(conn)
    order_mode = order["policy_mode"]
    order_version = int(order["policy_version"] or 0)
    if order_mode != pol["mode"] or order_version != int(pol["version"]):
        _record_follow(
            conn, None, order["id"], order_mode or pol["mode"], pol["mode"], "收口同步", at
        )
    return pol


def list_flips(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    flips = conn.execute(
        "SELECT * FROM policy_flips ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for f in flips:
        follows = conn.execute(
            "SELECT id, order_id, from_mode, to_mode, outcome, at FROM policy_follows WHERE flip_id=? ORDER BY id",
            (f["id"],),
        ).fetchall()
        item = dict(f)
        item["跟随"] = [dict(r) for r in follows]
        out.append(item)
    return out


def follow_view(conn: sqlite3.Connection) -> list[dict]:
    """校验屏跟随格：在途按现档、已收口按冻结节分列，附跟随痕供重放。"""
    pol = read_policy(conn)
    rows = conn.execute(
        """
        SELECT o.id, o.board_id, b.name AS board_name, o.target_slot, o.status,
               o.policy_mode, o.policy_hold_sec, o.policy_version, o.policy_frozen_at,
               o.created_at
        FROM flash_orders o LEFT JOIN boards b ON b.id = o.board_id
        ORDER BY o.id DESC LIMIT 20
        """
    ).fetchall()
    out = []
    for r in rows:
        in_flight = r["status"] == "写闪中"
        synced = (
            in_flight
            and r["policy_mode"] == pol["mode"]
            and int(r["policy_version"] or 0) == int(pol["version"])
        )
        if in_flight:
            follow_state = "在途·跟现档" if synced else "在途·未同现档"
        elif r["policy_frozen_at"]:
            follow_state = "已收口·冻结节"
        else:
            follow_state = "已收口"
        traces = conn.execute(
            """
            SELECT id, flip_id, from_mode, to_mode, outcome, at
            FROM policy_follows WHERE order_id=? ORDER BY id
            """,
            (r["id"],),
        ).fetchall()
        item = dict(r)
        item["跟随态"] = follow_state
        item["同现档"] = bool(synced) if in_flight else None
        item["跟随痕"] = [dict(t) for t in traces]
        out.append(item)
    return out
