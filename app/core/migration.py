# -*- coding: utf-8 -*-
"""轻量建表后迁移：为旧版数据库补齐新增列并归一化历史快照。

项目没有引入 Alembic，这里用 ADD COLUMN 做前向兼容（对已是最新结构的库为幂等无操作）：
- pending_crisis / last_resolution / last_expedition / expedition：状态机快照列，旧行补 NULL
- trade_order / last_trade：贸易救援订单与幂等凭据列，旧行补 NULL
- reputation：对外信誉，旧行统一从初始值 50 开始
- row_version：乐观锁版本号，旧行统一从 1 开始

补列后再做一次“旧存档归一化”（reconcile_old_saves），保证历史行加载进新引擎后
状态机可以唯一收敛：
- 已结束档案上悬而未决的危机/探索队/贸易订单一律清除，统一收敛到 ended
- 损坏/悬空的快照（成员全部不在档、目标居民失踪、JSON 残缺）不阻塞每日推进
- survivors 与实际存活居民数漂移时以居民表为准校正
"""
import json

from sqlalchemy import inspect, text


def _existing_columns(conn, table):
    try:
        return {c["name"] for c in inspect(conn).get_columns(table)}
    except Exception:
        return set()


def ensure_schema(engine):
    with engine.begin() as conn:
        columns = _existing_columns(conn, "game_sessions")
        if not columns:
            # 表尚未创建，create_all 会按最新模型建表，无需迁移
            return
        if "pending_crisis" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN pending_crisis JSON"))
        if "last_resolution" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_resolution JSON"))
        if "expedition" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN expedition JSON"))
        if "last_expedition" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_expedition JSON"))
        if "trade_order" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN trade_order JSON"))
        if "last_trade" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_trade JSON"))
        if "reputation" not in columns:
            # NOT NULL + 常量默认值：旧档案从未开展贸易，信誉从初始值 50 开始
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN reputation INTEGER NOT NULL DEFAULT 50")
            )
        if "row_version" not in columns:
            # NOT NULL + 常量默认值，存量行全部初始化为 1
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1")
            )
    # DDL 提交后再做数据归一化（SQLite 不允许在同一事务里混用 DDL 与行更新）
    reconcile_old_saves(engine)


def _loads(raw):
    """SQLite JSON 列读回可能是 str/dict/None；解析失败一律按损坏快照处理。"""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def reconcile_old_saves(engine):
    """旧存档快照归一化：见模块文档字符串。幂等，可重复执行。

    只有“确实发生了变化”的行才会被 UPDATE：快照本就规范时零写入，
    不会无谓 bump 乐观锁版本号。
    """
    inspector = inspect(engine)
    if "game_sessions" not in inspector.get_table_names():
        return 0
    resident_columns = _existing_columns(engine, "residents")
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, status, pending_crisis, expedition, trade_order, survivors "
                "FROM game_sessions"
            )
        ).all()
        updates = []
        for sid, status, crisis_raw, exp_raw, trade_raw, survivors in rows:
            old_crisis = _loads(crisis_raw)
            old_exp = _loads(exp_raw)
            old_trade = _loads(trade_raw)
            alive_count = None
            if {"id", "alive", "session_id"} <= resident_columns:
                alive_count = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM residents WHERE session_id = :sid AND alive = 1"
                    ),
                    {"sid": sid},
                ).scalar()

            if status == "running":
                # 运行中：只清理无法再被状态机处理的损坏/悬空快照
                new_crisis, crisis_changed = _clean_pending_crisis(conn, sid, old_crisis)
                new_exp, exp_changed = _clean_expedition(old_exp, conn, sid)
                new_trade, trade_changed = _clean_trade_order(old_trade, conn, sid)
            else:
                # 已结束：危机/探索队/贸易订单快照一律清空，阶段统一收敛到 ended
                new_crisis, crisis_changed = None, old_crisis is not None
                new_exp, exp_changed = None, old_exp is not None
                new_trade, trade_changed = None, old_trade is not None

            if crisis_changed or exp_changed or trade_changed or (
                alive_count is not None and alive_count != survivors
            ):
                updates.append(
                    {
                        "sid": sid,
                        "crisis": json.dumps(new_crisis, ensure_ascii=False)
                        if new_crisis is not None
                        else None,
                        "expedition": json.dumps(new_exp, ensure_ascii=False)
                        if new_exp is not None
                        else None,
                        "trade": json.dumps(new_trade, ensure_ascii=False)
                        if new_trade is not None
                        else None,
                        "survivors": alive_count if alive_count is not None else survivors,
                    }
                )
        for u in updates:
            conn.execute(
                text(
                    "UPDATE game_sessions SET pending_crisis = :crisis, "
                    "expedition = :expedition, trade_order = :trade, "
                    "survivors = :survivors WHERE id = :sid"
                ),
                u,
            )
    return len(updates)


def _resident_ids(conn, sid):
    return {
        row[0]
        for row in conn.execute(
            text("SELECT id FROM residents WHERE session_id = :sid"), {"sid": sid}
        ).all()
    }


def _clean_pending_crisis(conn, sid, crisis):
    """待处理危机快照结构残缺或绑定目标已不在档：清空以解除每日阶段的死锁。

    返回 (归一化快照, 是否发生变化)。
    """
    if crisis is None:
        return None, False
    if not isinstance(crisis, dict):
        return None, True
    if not crisis.get("event") or not isinstance(crisis.get("choices"), list):
        return None, True
    target_id = crisis.get("target_id")
    if target_id is not None and target_id not in _resident_ids(conn, sid):
        return None, True
    return crisis, False


def _clean_expedition(exp, conn, sid):
    """探索队快照结构残缺或成员全部不在档：清空（队伍无法恢复）。

    成员中夹杂已删除/重复编号时剔除；剔除后无人则整队清除。
    返回 (归一化快照, 是否发生变化)。
    """
    if exp is None:
        return None, False
    if not isinstance(exp, dict) or exp.get("status") != "away":
        return None, True
    raw_members = exp.get("members")
    if not isinstance(raw_members, list):
        return None, True
    known = _resident_ids(conn, sid)
    # 剔除悬空/重复编号，保持首次出现顺序
    members, seen = [], set()
    for m in raw_members:
        if m in known and m not in seen:
            seen.add(m)
            members.append(m)
    if not members:
        return None, True
    changed = members != raw_members
    cleaned = dict(exp)
    cleaned["members"] = members
    # 待处理遭遇绑定的目标若已不在队中，丢弃该遭遇（无法再结算单体效果）
    pending = cleaned.get("pending_encounter")
    if not isinstance(pending, dict):
        if pending is not None:
            changed = True
        cleaned["pending_encounter"] = None
    else:
        target_id = pending.get("target_id")
        if target_id is not None and target_id not in members:
            cleaned["pending_encounter"] = None
            changed = True
    return cleaned, changed


# 贸易订单合法状态链
_TRADE_STATUSES = {"reviewing", "transporting", "delivered", "failed", "rejected", "cancelled"}


def _clean_trade_order(order, conn, sid):
    """贸易订单快照归一化。

    - 结构残缺 / 非法状态：清空（无法恢复的订单）
    - 已收敛的终态（delivered/failed/rejected/cancelled）残留在列上：清空，
      统一以"无在谈订单"开始（结算结果早已回写资源/信誉）
    - 押运成员夹杂悬空/重复编号：剔除；剔除后无人且已在途则整单清除
    - 在途中事件绑定目标已不在队：丢弃该事件（无法再结算单体效果），
      订单本身保留，下一次推进正常运输/交付

    返回 (归一化快照, 是否发生变化)。
    """
    if order is None:
        return None, False
    if not isinstance(order, dict):
        return None, True
    status = order.get("status")
    if status not in _TRADE_STATUSES:
        return None, True
    if status in ("delivered", "failed", "rejected", "cancelled"):
        return None, True
    raw_escorts = order.get("escorts")
    if not isinstance(raw_escorts, list) or not order.get("token"):
        return None, True
    known = _resident_ids(conn, sid)
    escorts, seen = [], set()
    for m in raw_escorts:
        if m in known and m not in seen:
            seen.add(m)
            escorts.append(m)
    if not escorts:
        return None, True
    changed = escorts != raw_escorts
    cleaned = dict(order)
    cleaned["escorts"] = escorts
    pending = cleaned.get("pending_incident")
    if not isinstance(pending, dict):
        if pending is not None:
            changed = True
        cleaned["pending_incident"] = None
    else:
        target_id = pending.get("target_id")
        if target_id is not None and target_id not in escorts:
            cleaned["pending_incident"] = None
            changed = True
    return cleaned, changed
