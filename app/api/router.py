# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from ..core.database import get_db
from ..core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY
from ..models import GameSession, Resident, Facility
from ..services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    RESOURCE_KEYS,
    FACILITY_OUTPUT,
    FACILITY_COST,
    FACILITY_ZH,
    JOB_EFFICIENCY,
)
from ..schemas import (
    SessionCreate,
    SessionBrief,
    SessionDetail,
    AdvanceResult,
    CrisisChoice,
    ExpeditionSend,
    ExpeditionEncounterChoice,
    ExpeditionReturn,
    TradeApply,
    TradeReview,
    TradeResult,
    TradeOfferOut,
    JobAssign,
    BuildRequest,
    BuildableInfo,
    EngineConfig,
    Message,
)

router = APIRouter(prefix="/api")


# ---- 会话 ----
@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    rows = (
        db.query(GameSession)
        .order_by(GameSession.created_at.desc())
        .all()
    )
    return [SessionBrief.model_validate(r) for r in rows]


@router.post("/sessions", response_model=SessionDetail, status_code=201)
def create_session(body: SessionCreate, db: Session = Depends(get_db)):
    gs = GameSession(
        name=body.name,
        day=1,
        target_day=SURVIVAL_TARGET_DAY,
        status="running",
        resources=dict(INITIAL_RESOURCES),
        survivors=3,
        score=0,
        reputation=50,
        trade_orders=[],
    )
    db.add(gs)
    db.flush()
    # 初始三名幸存者
    for name, job in (("林粤", "engineer"), ("夏岚", "farmer"), ("老周", "general")):
        db.add(
            Resident(
                session_id=gs.id,
                name=name,
                job=job,
                health=90.0,
                morale=80.0,
                alive=1,
                joined_day=1,
            )
        )
    # 初始设施
    for cat in ("power", "farm", "water", "oxygen"):
        db.add(
            Facility(
                session_id=gs.id,
                name=FACILITY_ZH[cat],
                category=cat,
                level=1,
                status="active",
                built_day=1,
            )
        )
    db.commit()
    db.refresh(gs)
    return get_session_detail(gs, db)


@router.get("/sessions/{sid}", response_model=SessionDetail)
def get_session(sid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    return get_session_detail(gs, db)


def _serialize_resident(r, away_info=None):
    away_kind = (away_info or {}).get(r.id)
    return {
        "id": r.id,
        "name": r.name,
        "job": r.job,
        "job_zh": JOB_ZH.get(r.job, r.job),
        "health": r.health,
        "morale": r.morale,
        "alive": r.alive,
        "away": 1 if away_kind else 0,
        "away_kind": away_kind,
        "joined_day": r.joined_day,
    }


def get_session_detail(gs, db):
    # 外出居民编号：探索队成员 + 商队押运员，用于标注居民"探索中/押运中"状态
    away_ids = set()
    if gs.expedition and gs.expedition.get("status") == "away":
        away_ids = set(gs.expedition.get("members", []))
    escort_ids = set()
    for o in (gs.trade_orders or []):
        if o.get("status") == "transporting":
            escort_ids.update(o.get("escorts", []))
    away_info = {rid: "expedition" for rid in away_ids}
    for rid in escort_ids - away_ids:
        away_info[rid] = "escort"
    residents = [
        _serialize_resident(r, away_info)
        for r in gs.residents
    ]
    facilities = [
        {
            "id": f.id,
            "name": f.name,
            "category": f.category,
            "level": f.level,
            "status": f.status,
            "built_day": f.built_day,
        }
        for f in gs.facilities
    ]
    logs = [
        {
            "id": l.id,
            "day": l.day,
            "event_type": l.event_type,
            "title": l.title,
            "detail": l.detail,
            "decision": l.decision,
        }
        for l in gs.logs
    ]
    return SessionDetail(
        id=gs.id,
        name=gs.name,
        day=gs.day,
        target_day=gs.target_day,
        status=gs.status,
        resources={k: gs.resources.get(k, 0) for k in RESOURCE_KEYS},
        survivors=gs.survivors,
        score=gs.score,
        outcome=gs.outcome,
        pending_crisis=gs.pending_crisis,
        expedition=gs.expedition,
        reputation=gs.reputation if gs.reputation is not None else 50,
        trade_orders=list(gs.trade_orders or []),
        residents=residents,
        facilities=facilities,
        logs=logs,
    )


# ---- 游戏动作 ----
def _run_mutation(db, gs, action):
    """统一执行经营类状态变更。

    - 业务校验失败（BunkerEngineError）→ 400
    - 乐观锁版本冲突（并发请求已先行落库，StaleDataError）→ 409，
      落败方不产生任何效果，避免重复推进/重复扣费
    """
    eng = BunkerEngine(db, gs)
    try:
        action(eng)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        db.rollback()
        db.refresh(gs)
        raise HTTPException(409, "档案已被其他请求更新，请刷新后重试")


@router.post("/sessions/{sid}/advance", response_model=AdvanceResult)
def advance(sid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        crisis = eng.advance_day()
        db.commit()
        db.refresh(gs)
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发/重复的“推进一天”落败：另一个请求已经推进过，这里幂等回放
        # 当前状态（含可能已挂起的待处理危机/探索遭遇），绝不再多推进一天
        db.rollback()
        db.refresh(gs)
        crisis = gs.pending_crisis
    # 探索队在外时推进可能挂起的是遭遇而非地堡危机：把当前任一待处理抉择带回，
    # 前端据此恢复对应弹层（落败回放与正常返回保持同一口径）
    if crisis is None and gs.expedition and gs.expedition.get("pending_encounter"):
        crisis = gs.expedition["pending_encounter"]
    # pending_event 为语义准确的新字段；crisis 为兼容旧前端的同值别名
    return AdvanceResult(session=get_session_detail(gs, db), pending_event=crisis, crisis=crisis)


@router.post("/sessions/{sid}/resolve", response_model=SessionDetail)
def resolve_crisis(sid: int, body: CrisisChoice, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.resolve_crisis(
            body.event_key, body.choice_key, body.target_id, token=body.token
        )
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复结算：版本不匹配说明对方已先落库。核对是否同一次抉择：
        # 相同则幂等回放当前状态（效果只结算一次），否则 409 拒绝
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_resolution(
                body.event_key, body.choice_key, body.target_id, token=body.token
            )
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


# ---- 探索队 ----
@router.post("/sessions/{sid}/expedition/send", response_model=SessionDetail)
def send_expedition(sid: int, body: ExpeditionSend, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.send_expedition(body.member_ids, body.supplies))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/expedition/resolve", response_model=SessionDetail)
def resolve_expedition(sid: int, body: ExpeditionEncounterChoice, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.resolve_expedition_encounter(body.choice_key, token=body.token)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复结算：版本不匹配说明对方已先落库。核对是否同一次遭遇抉择：
        # 相同则幂等回放当前状态（效果只结算一次），否则 409 拒绝
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_expedition(
                "encounter", token=body.token, choice_key=body.choice_key
            )
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/expedition/return", response_model=SessionDetail)
def return_expedition(sid: int, body: ExpeditionReturn, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    try:
        eng.return_expedition(token=body.token)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复返程：版本不匹配说明对方已先落库。核对是否同一支队伍：
        # 相同则幂等回放（战利品只结算一次，队伍已清除也能识别），否则 409
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        try:
            replay_eng.reconcile_stale_expedition("return", exp_token=body.token)
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    return get_session_detail(gs, db)


# ---- 贸易救援 ----
@router.get("/sessions/{sid}/trade/offers", response_model=list[TradeOfferOut])
def list_trade_offers(sid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    return [TradeOfferOut(**o) for o in eng.available_offers()]


@router.post("/sessions/{sid}/trade/apply", response_model=TradeResult)
def apply_trade(sid: int, body: TradeApply, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    order = None

    def _action(eng):
        nonlocal order
        order = eng.apply_trade(body.offer_key, body.escort_ids)

    _run_mutation(db, gs, _action)
    return TradeResult(session=get_session_detail(gs, db), order=order, replayed=False)


@router.post("/sessions/{sid}/trade/{order_id}/review", response_model=TradeResult)
def review_trade(sid: int, order_id: str, body: TradeReview, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    eng = BunkerEngine(db, gs)
    order = None
    try:
        order, replayed = eng.review_trade(order_id, body.approve, token=body.token)
        db.commit()
        db.refresh(gs)
    except BunkerEngineConflict as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except BunkerEngineError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except StaleDataError:
        # 并发的重复审核：版本不匹配说明对方已先落库。核对同一订单同一动作：
        # 相同则幂等回放（押金只托管一次），否则 409 拒绝
        db.rollback()
        db.refresh(gs)
        replay_eng = BunkerEngine(db, gs)
        decision = "approve" if body.approve else "reject"
        try:
            order, replayed = replay_eng.reconcile_stale_trade(
                order_id, body.token, decision
            )
        except BunkerEngineConflict as e:
            raise HTTPException(409, str(e))
    detail = (gs.last_trade or {}).get("detail") if gs.last_trade else None
    return TradeResult(
        session=get_session_detail(gs, db),
        order=order,
        replayed=bool(replayed),
        detail=detail,
    )


@router.delete("/sessions/{sid}/trade/{order_id}", response_model=SessionDetail)
def dismiss_trade(sid: int, order_id: str, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.dismiss_trade(order_id))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/build", response_model=SessionDetail)
def build(sid: int, body: BuildRequest, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    if body.category not in FACILITY_OUTPUT:
        raise HTTPException(400, "未知设施类别")
    _run_mutation(db, gs, lambda eng: eng.build_facility(body.category))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/upgrade/{fid}", response_model=SessionDetail)
def upgrade(sid: int, fid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.upgrade_facility(fid))
    return get_session_detail(gs, db)


@router.post("/sessions/{sid}/resident/{rid}/job", response_model=SessionDetail)
def set_job(sid: int, rid: int, body: JobAssign, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    _run_mutation(db, gs, lambda eng: eng.set_job(rid, body.job))
    return get_session_detail(gs, db)


@router.delete("/sessions/{sid}", response_model=Message)
def delete_session(sid: int, db: Session = Depends(get_db)):
    gs = db.get(GameSession, sid)
    if not gs:
        raise HTTPException(404, "档案不存在")
    db.delete(gs)
    db.commit()
    return Message(detail="已删除")


# ---- 配置信息 ----
@router.get("/config", response_model=EngineConfig)
def get_config():
    return EngineConfig(
        resources=dict(INITIAL_RESOURCES),
        facility_costs=FACILITY_COST,
        facility_names=FACILITY_ZH,
        job_options=list(JOB_EFFICIENCY.keys()),
        status="running",
    )


@router.get("/buildings", response_model=list)
def list_buildable():
    return [
        BuildableInfo(
            category=k,
            name=FACILITY_ZH[k],
            cost=FACILITY_COST[1],
            level_scale=1.6,
        )
        for k in ("farm", "water", "power", "oxygen", "med", "storage")
    ]


JOB_ZH = {"engineer": "工程师", "farmer": "农民", "general": "杂工"}