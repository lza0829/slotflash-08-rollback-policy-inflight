from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, write_tx
from app.services import policy as policy_svc
from app.services.common import now_iso

router = APIRouter(tags=["回退拨"])


class PolicyBody(BaseModel):
    # 拨杆只有两档；控件旁不再弹问答，口径已在 services/policy.py 钉死。
    mode: str = Field(pattern="^(失败硬退|挂缺口可核)$")
    hold_sec: int = Field(default=20, ge=0)


@router.get("/回退拨")
def get_policy():
    with db() as conn:
        return policy_svc.read_policy(conn)


@router.post("/回退拨")
def set_policy(body: PolicyBody):
    at = now_iso()
    with write_tx() as conn:
        result = policy_svc.apply_flip(conn, body.mode, body.hold_sec, at)
        # 四路对齐：返回本次拧档后的真源、是否变更、随单数与流水号。
        return {
            "policy": result["policy"],
            "changed": result["changed"],
            "flip_id": result["flip_id"],
            "affected": result["affected"],
        }


@router.get("/回退拨/流水")
def list_flips():
    """拧档流水：每次拧档的版本前后、随单数，以及逐单跟随痕（可重放）。"""
    with db() as conn:
        return policy_svc.list_flips(conn)


@router.get("/回退拨/跟随")
def follow_grid():
    """校验屏跟随格：在途按现档、已收口按冻结节分列。"""
    with db() as conn:
        return policy_svc.follow_view(conn)
