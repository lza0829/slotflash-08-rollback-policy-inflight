"""在途回退档跟随口径夹具。

覆盖：写闪中拧档 / 先拧档再收口 / 拧档与收口并发 / 半截脏跟随回滚 /
收口失败后冻结档是否回显 / 连拧两档时跟随痕如何落。

口径：在途跟现档（带痕），已收口跟冻结节（只读）。两臂分开验。
"""
from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每个用例一套独立 SQLite 库，重新加载模块避免 DB_PATH 串味。"""
    db_path = tmp_path / "slotflash.db"
    monkeypatch.setenv("SLOTFLASH_DB", str(db_path))

    import app.config
    import app.db
    import app.seed
    import app.services.policy
    import app.routers.policy
    import app.routers.orders
    import app.routers.align
    import app.main

    for m in (app.config, app.db, app.seed, app.services.policy,
              app.routers.policy, app.routers.orders, app.routers.align, app.main):
        importlib.reload(m)

    client = TestClient(app.main.app)
    yield client


def _policy(client):
    return client.get("/api/回退拨").json()


def _flip(client, mode, hold=20):
    r = client.post("/api/回退拨", json={"mode": mode, "hold_sec": hold})
    assert r.status_code == 200, r.text
    return r.json()


def _new_order(client, slot="B", board=1, fw=1):
    r = client.post("/api/写闪单", json={"board_id": board, "target_slot": slot, "firmware_id": fw})
    assert r.status_code == 200, r.text
    return r.json()


def _follow(client):
    return {r["id"]: r for r in client.get("/api/回退拨/跟随").json()}


def _flips(client):
    return client.get("/api/回退拨/流水").json()


def test_open_order_carries_current_policy(env):
    pol = _policy(env)
    assert pol["version"] == 0
    o = _new_order(env)
    assert o["policy_mode"] == pol["mode"]
    assert o["policy_version"] == pol["version"]
    assert o["policy_frozen_at"] is None


def test_flip_while_in_flight_follows_new_and_leaves_trace(env):
    o = _new_order(env)
    before = _policy(env)
    res = _flip(env, "挂缺口可核", 35)
    assert res["changed"] is True
    assert res["affected"] == 1

    grid = _follow(env)[o["id"]]
    assert grid["status"] == "写闪中"
    assert grid["跟随态"] == "在途·跟现档"
    assert grid["同现档"] is True
    assert grid["policy_mode"] == "挂缺口可核"
    assert grid["policy_version"] == before["version"] + 1
    # 跟随痕可重放
    assert any(t["outcome"] == "跟随" and t["to_mode"] == "挂缺口可核" for t in grid["跟随痕"])
    # 流水与随单互证
    top = _flips(env)[0]
    assert top["version_before"] == before["version"]
    assert top["version_after"] == before["version"] + 1
    assert top["affected_orders"] == 1
    assert top["跟随"][0]["order_id"] == o["id"]


def test_flip_then_close_freezes_new_policy(env):
    o = _new_order(env)
    _flip(env, "挂缺口可核", 35)
    closed = env.post(f"/api/写闪单/{o['id']}/收口").json()
    assert closed["status"] == "已完成"
    assert closed["policy_mode"] == "挂缺口可核"
    assert closed["policy_frozen_at"] is not None

    grid = _follow(env)[o["id"]]
    assert grid["跟随态"] == "已收口·冻结节"
    assert grid["policy_mode"] == "挂缺口可核"


def test_close_then_flip_does_not_rewrite_frozen(env):
    o = _new_order(env)
    old = _policy(env)
    closed = env.post(f"/api/写闪单/{o['id']}/收口").json()
    assert closed["policy_mode"] == old["mode"]

    res = _flip(env, "挂缺口可核", 35)
    assert res["affected"] == 0  # 无在途单

    row = [r for r in env.get("/api/写闪单").json() if r["id"] == o["id"]][0]
    assert row["policy_mode"] == old["mode"]  # 冻结节未被偷改
    assert row["policy_version"] == old["version"]
    grid = _follow(env)[o["id"]]
    assert grid["跟随态"] == "已收口·冻结节"


def test_double_flip_traces_each_time(env):
    o = _new_order(env)
    _flip(env, "挂缺口可核", 35)
    _flip(env, "失败硬退", 20)

    grid = _follow(env)[o["id"]]
    follows = [t for t in grid["跟随痕"] if t["outcome"] == "跟随"]
    assert len(follows) == 2
    assert [t["to_mode"] for t in follows] == ["挂缺口可核", "失败硬退"]
    assert grid["policy_mode"] == "失败硬退"
    assert grid["跟随态"] == "在途·跟现档"

    flips = _flips(env)
    assert len(flips) == 2
    # 最新的在前
    assert flips[0]["to_mode"] == "失败硬退"
    assert flips[1]["to_mode"] == "挂缺口可核"


def test_noop_flip_does_not_bump_version_or_trace(env):
    o = _new_order(env)
    pol = _policy(env)
    res = _flip(env, pol["mode"], pol["hold_sec"])
    assert res["changed"] is False
    assert res["affected"] == 0
    assert _policy(env)["version"] == pol["version"]
    assert _flips(env) == []
    assert _follow(env)[o["id"]]["跟随痕"] == []


def test_half_dirty_follow_rolls_back_whole_flip(env, monkeypatch):
    """模拟落跟随痕中途炸：拧档须整笔回滚，拨杆与在途单都不能半截。"""
    order = _new_order(env)
    before = _policy(env)

    import app.db as dbmod
    import app.services.policy as svc

    real = svc._record_follow
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("脏跟随注入")

    monkeypatch.setattr(svc, "_record_follow", boom)
    with pytest.raises(RuntimeError):
        with dbmod.write_tx() as conn:
            svc.apply_flip(conn, "挂缺口可核", 35, "2026-01-01T00:00:00Z")
    assert calls["n"] == 1

    # 整笔回滚：真源未动、无流水、单内档未改
    after = _policy(env)
    assert after == before
    assert _flips(env) == []
    grid = _follow(env)[order["id"]]
    assert grid["policy_mode"] == before["mode"]
    assert grid["policy_version"] == before["version"]
    assert grid["跟随痕"] == []


def test_close_failure_keeps_inflight_and_no_frozen_echo(env):
    """目标槽被删（收口前置失败）时整笔回滚：单仍写闪中，跟随格不得回显冻结档。"""
    o = _new_order(env)
    import app.db as dbmod

    with dbmod.write_tx() as conn:
        conn.execute("DELETE FROM slots WHERE board_id=1 AND slot_key='B'")

    r = env.post(f"/api/写闪单/{o['id']}/收口")
    assert r.status_code == 409

    grid = _follow(env)[o["id"]]
    assert grid["status"] == "写闪中"
    assert grid["policy_frozen_at"] is None
    assert grid["跟随态"] in ("在途·跟现档", "在途·未同现档")


def test_alignment_matches_lever_order_and_ledger(env):
    o = _new_order(env)
    _flip(env, "挂缺口可核", 35)
    snap = env.get("/api/校验").json()

    lever = snap["回退拨"]
    order = [r for r in snap["写闪近单"] if r["id"] == o["id"]][0]
    grid = [r for r in snap["跟随格"] if r["id"] == o["id"]][0]
    ledger = snap["拧档流水"][0]

    assert lever["mode"] == "挂缺口可核"
    assert order["policy_mode"] == lever["mode"]
    assert order["policy_version"] == lever["version"]
    assert grid["跟随态"] == "在途·跟现档"
    assert grid["policy_mode"] == lever["mode"]
    assert ledger["to_mode"] == lever["mode"]
    assert ledger["version_after"] == lever["version"]


def test_orphan_inflight_order_still_follows_flip(env):
    """孤儿在途单（板块已不存在）也不得漏跟：拧档只认单据本身。"""
    import app.db as dbmod

    conn = dbmod.connect()
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = OFF")  # 必须在 BEGIN 之前，模拟历史库里的孤儿单
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        """INSERT INTO flash_orders(board_id, target_slot, firmware_id, status, note, created_at,
                                    policy_mode, policy_hold_sec, policy_version)
           VALUES (99,'B',1,'写闪中','孤儿单','2026-01-01T00:00:00Z','失败硬退',20,0)"""
    )
    orphan_id = cur.lastrowid
    conn.execute("COMMIT")
    conn.close()

    res = _flip(env, "挂缺口可核", 35)
    assert res["affected"] == 1
    grid = _follow(env)[orphan_id]
    assert grid["跟随态"] == "在途·跟现档"
    assert grid["policy_mode"] == "挂缺口可核"


def test_concurrent_flip_and_close_serialize(env):
    """拧档与收口并发：IMMEDIATE 事务串行化，最终不出现拨杆与单不一致。"""
    o = _new_order(env)
    errors: list[BaseException] = []

    def do_flip():
        try:
            _flip(env, "挂缺口可核", 35)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def do_close():
        try:
            env.post(f"/api/写闪单/{o['id']}/收口")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=do_flip)
    t2 = threading.Thread(target=do_close)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors

    lever = _policy(env)
    row = [r for r in env.get("/api/写闪单").json() if r["id"] == o["id"]][0]
    grid = _follow(env)[o["id"]]
    if row["status"] == "已完成":
        # 收口先落定：冻结节固定为收口当刻的现档，事后拧档不得改写它。
        assert row["policy_frozen_at"] is not None
        assert row["policy_version"] <= lever["version"]
        assert grid["跟随态"] == "已收口·冻结节"
        # 拧档未改写冻结节：若版本滞后于现档，那是收口更早的正确结果。
        assert grid["policy_mode"] == row["policy_mode"]
    else:
        # 收口未成功，则单为在途且已跟随现档
        assert grid["跟随态"] == "在途·跟现档"
        assert grid["policy_mode"] == lever["mode"]
        assert grid["policy_version"] == lever["version"]
