from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services import policy as policy_svc

router = APIRouter(tags=["校验屏"])


@router.get("/校验")
def align_snapshot():
    with db() as conn:
        slots = conn.execute(
            """
            SELECT s.board_id, b.name AS board_name, s.slot_key, s.role, s.write_state,
                   s.boot_color, b.active_slot
            FROM slots s JOIN boards b ON b.id=s.board_id
            ORDER BY s.board_id, s.slot_key
            """
        ).fetchall()
        boots = conn.execute(
            "SELECT board_id, color, fail_streak, pending_slot FROM boot_chain"
        ).fetchall()
        orders = conn.execute(
            """
            SELECT id, board_id, target_slot, status, policy_mode, policy_hold_sec,
                   policy_version, policy_frozen_at
            FROM flash_orders ORDER BY id DESC LIMIT 20
            """
        ).fetchall()
        policy = policy_svc.read_policy(conn)
        return {
            "闪槽投影": [dict(r) for r in slots],
            "启动链投影": [dict(r) for r in boots],
            "写闪近单": [dict(r) for r in orders],
            "回退拨": policy,
            # 校验屏跟随格与拧档流水：与拨杆、在途单互证；闪槽页可作旁证。
            "跟随格": policy_svc.follow_view(conn),
            "拧档流水": policy_svc.list_flips(conn),
        }
