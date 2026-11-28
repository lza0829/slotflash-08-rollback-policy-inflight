from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, write_tx
from app.services import policy as policy_svc
from app.services.common import now_iso

router = APIRouter(tags=["写闪单"])


class FlashBody(BaseModel):
    board_id: int
    target_slot: str = Field(pattern="^[AB]$")
    firmware_id: int


ORDER_COLS = """
    o.id, o.board_id, b.name AS board_name, o.target_slot, o.firmware_id,
    f.version AS firmware_version, o.status, o.note, o.created_at,
    o.policy_mode, o.policy_hold_sec, o.policy_version, o.policy_frozen_at
"""


@router.get("/写闪单")
def list_orders():
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT {ORDER_COLS}
            FROM flash_orders o
            LEFT JOIN boards b ON b.id = o.board_id
            LEFT JOIN firmwares f ON f.id = o.firmware_id
            ORDER BY o.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


@router.post("/写闪单")
def create_order(body: FlashBody):
    at = now_iso()
    with write_tx() as conn:
        board = conn.execute("SELECT * FROM boards WHERE id=?", (body.board_id,)).fetchone()
        fw = conn.execute("SELECT * FROM firmwares WHERE id=?", (body.firmware_id,)).fetchone()
        slot = conn.execute(
            "SELECT * FROM slots WHERE board_id=? AND slot_key=?",
            (body.board_id, body.target_slot),
        ).fetchone()
        if not board or not fw or not slot:
            raise HTTPException(400, "板卡/固件/槽位不存在")
        # 开单即带上当刻板规回退档（mode/hold/version）。在途期间随拧档同步。
        pol = policy_svc.read_policy(conn)
        cur = conn.execute(
            """
            INSERT INTO flash_orders(board_id, target_slot, firmware_id, status, note, created_at,
                                     policy_mode, policy_hold_sec, policy_version)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (body.board_id, body.target_slot, body.firmware_id, "写闪中", "底座占位写闪", at,
             pol["mode"], pol["hold_sec"], pol["version"]),
        )
        oid = cur.lastrowid
        conn.execute(
            "UPDATE slots SET write_state=?, firmware_id=? WHERE id=?",
            ("写闪中", body.firmware_id, slot["id"]),
        )
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("写闪", f"order-{oid}",
             f"开写闪 {board['name']}/{body.target_slot}，携回退档 {pol['mode']}@v{pol['version']}", at),
        )
        return dict(conn.execute("SELECT * FROM flash_orders WHERE id=?", (oid,)).fetchone())


@router.post("/写闪单/{oid}/收口")
def close_order(oid: int):
    at = now_iso()
    with write_tx() as conn:
        o = conn.execute("SELECT * FROM flash_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "写闪单不存在")
        if o["status"] != "写闪中":
            return dict(o)
        slot = conn.execute(
            "SELECT * FROM slots WHERE board_id=? AND slot_key=?",
            (o["board_id"], o["target_slot"]),
        ).fetchone()
        # 收口前置校验失败即整笔回滚：单保持「写闪中」，冻结节不落，跟随格不得回显冻结档。
        if not slot:
            raise HTTPException(409, "目标槽不存在，收口中止")
        # 收口当刻以板规现档为准；若单内档与现档不一致，落一条「收口同步」痕，不静默偷改。
        pol = policy_svc.reconcile_for_close(conn, o, at)
        conn.execute(
            """
            UPDATE flash_orders
            SET status=?, note=?, policy_mode=?, policy_hold_sec=?, policy_version=?, policy_frozen_at=?
            WHERE id=?
            """,
            ("已完成", "底座占位收口", pol["mode"], pol["hold_sec"], pol["version"], at, oid),
        )
        conn.execute(
            "UPDATE slots SET write_state=? WHERE board_id=? AND slot_key=?",
            ("完好", o["board_id"], o["target_slot"]),
        )
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("写闪收口", f"order-{oid}",
             f"收口为已完成，冻结回退档 {pol['mode']}@v{pol['version']}", at),
        )
        return dict(conn.execute("SELECT * FROM flash_orders WHERE id=?", (oid,)).fetchone())
