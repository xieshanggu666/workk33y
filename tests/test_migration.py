# -*- coding: utf-8 -*-
"""旧存档兼容迁移测试：补列 + 历史快照归一化（reconcile_old_saves）。"""
import json

import pytest
from sqlalchemy import text

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession, Resident, Facility
from app.core.migration import ensure_schema, reconcile_old_saves
from app.services.engine import BunkerEngine
from app.services.engine import FOOD, WATER, POWER, OXY
from tests.test_engine import make_session, FixedRand, TriggerRand  # noqa: F401


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def test_backfills_last_expedition_column(db):
    """缺列旧表经 ensure_schema 后补齐 last_expedition，旧行为 NULL。"""
    gs = make_session(db)
    db.commit()
    db.expire_all()
    db.execute(text("ALTER TABLE game_sessions RENAME TO gs_old"))
    db.execute(text(
        "CREATE TABLE game_sessions ("
        "id INTEGER PRIMARY KEY, name VARCHAR(64), day INTEGER, target_day INTEGER, "
        "status VARCHAR(16), resources JSON, survivors INTEGER, outcome JSON, "
        "score INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))
    db.execute(text(
        "INSERT INTO game_sessions SELECT id,name,day,target_day,status,resources,"
        "survivors,outcome,score,created_at,updated_at FROM gs_old"
    ))
    db.execute(text("DROP TABLE gs_old"))
    db.commit()

    ensure_schema(engine)
    db.expire_all()
    row = db.query(GameSession).first()
    assert row.last_expedition is None
    assert row.pending_crisis is None


def test_running_save_with_corrupt_crisis_is_unblocked(db):
    """运行中档案挂着结构损坏/目标失踪的危机：归一化后清空，回到可推进的每日阶段。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    # 目标居民编号不存在于任何档案
    db.execute(
        text("UPDATE game_sessions SET pending_crisis = :c WHERE id = :sid"),
        {
            "c": json.dumps({"event": "sick", "choices": [{"key": "x"}], "target_id": 999999}),
            "sid": sid,
        },
    )
    db.commit()

    n = reconcile_old_saves(engine)
    assert n == 1
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.pending_crisis is None
    eng = BunkerEngine(db, fixed, rand=FixedRand())
    assert eng.phase == "daily"
    eng.advance_day()  # 不再被损坏快照死锁
    db.commit()


def test_ended_save_keeps_no_hanging_snapshots(db):
    """已结束档案即便残留危机/探索队快照，归一化后也一律收敛到 ended。"""
    gs = make_session(db)
    gs.status = "over"
    db.commit()
    sid = gs.id
    db.execute(
        text("UPDATE game_sessions SET pending_crisis = :c, expedition = :e WHERE id = :sid"),
        {
            "c": json.dumps({"event": "mutiny", "choices": [{"key": "x"}]}),
            "e": json.dumps({"status": "away", "members": [], "token": "t"}),
            "sid": sid,
        },
    )
    db.commit()

    reconcile_old_saves(engine)
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.pending_crisis is None
    assert fixed.expedition is None
    assert BunkerEngine(db, fixed).phase == "ended"


def test_expedition_with_all_missing_members_is_cleared(db):
    """探索队成员全部不在档（悬空快照）：整队清除，档案可继续推进。"""
    gs = make_session(db)
    db.commit()
    sid = gs.id
    db.execute(
        text("UPDATE game_sessions SET expedition = :e WHERE id = :sid"),
        {"e": json.dumps({"status": "away", "members": [424242, 525252], "token": "t"}),
         "sid": sid},
    )
    db.commit()

    reconcile_old_saves(engine)
    db.expire_all()
    assert db.get(GameSession, sid).expedition is None


def test_expedition_partial_dangling_member_is_pruned(db):
    """队伍中夹杂一个已删除编号：剔除悬空编号，保留有效成员与待处理遭遇。"""
    gs = make_session(db)
    valid = gs.residents[0].id
    db.commit()
    sid = gs.id
    exp = {
        "status": "away",
        "token": "team-token",
        "members": [valid, 999999],
        "travel_days": 1,
        "pending_encounter": {"token": "enc", "event": "cache", "target_id": None},
    }
    db.execute(
        text("UPDATE game_sessions SET expedition = :e WHERE id = :sid"),
        {"e": json.dumps(exp), "sid": sid},
    )
    db.commit()

    reconcile_old_saves(engine)
    db.expire_all()
    fixed = db.get(GameSession, sid).expedition
    assert fixed["members"] == [valid]
    assert fixed["pending_encounter"]["token"] == "enc"


def test_survivors_count_drift_reconciled(db):
    """survivors 与实际存活人数漂移（旧 bug）：以居民表为准校正。"""
    gs = make_session(db)
    gs.survivors = 9  # 实际只有 3 名存活居民
    db.commit()
    sid = gs.id

    reconcile_old_saves(engine)
    db.expire_all()
    assert db.get(GameSession, sid).survivors == 3


def test_valid_running_save_left_untouched(db):
    """结构完好、目标在档的运行中快照不被误清理。"""
    gs = make_session(db)
    target = gs.residents[0].id
    crisis = {"event": "sick", "choices": [{"key": "quarantine"}], "target_id": target}
    gs.pending_crisis = crisis
    db.commit()
    sid = gs.id

    assert reconcile_old_saves(engine) == 0
    db.expire_all()
    assert db.get(GameSession, sid).pending_crisis["target_id"] == target


def test_reconcile_is_idempotent_no_rewrite(db):
    """重复执行归一化：第二次零更新，且不 bump 乐观锁版本号。"""
    gs = make_session(db)
    gs.survivors = 7  # 制造漂移，第一次需要校正
    db.commit()
    sid = gs.id

    assert reconcile_old_saves(engine) == 1
    db.expire_all()
    version_after_first = db.get(GameSession, sid).row_version

    assert reconcile_old_saves(engine) == 0
    db.expire_all()
    fixed = db.get(GameSession, sid)
    assert fixed.survivors == 3
    assert fixed.row_version == version_after_first  # 幂等运行不产生额外写入
