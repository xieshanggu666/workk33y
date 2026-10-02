# -*- coding: utf-8 -*-
from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class SessionCreate(BaseModel):
    name: str = "末日地堡档案"


class SessionBrief(BaseModel):
    id: int
    name: str
    day: int
    target_day: int
    status: str
    survivors: int
    score: int

    class Config:
        from_attributes = True


class ResidentOut(BaseModel):
    id: int
    name: str
    job: str
    job_zh: Optional[str] = None
    health: float
    morale: float
    alive: int
    away: int = 0
    # 外出任务类型：expedition 探索队 / escort 商队押运 / None
    away_kind: Optional[str] = None
    joined_day: int

    class Config:
        from_attributes = True


class FacilityOut(BaseModel):
    id: int
    name: str
    category: str
    level: int
    status: str
    built_day: int

    class Config:
        from_attributes = True


class LogOut(BaseModel):
    id: int
    day: int
    event_type: str
    title: str
    detail: str
    decision: Optional[str] = None

    class Config:
        from_attributes = True


class SessionDetail(BaseModel):
    id: int
    name: str
    day: int
    target_day: int
    status: str
    resources: Dict[str, float]
    survivors: int
    score: int
    outcome: Optional[Dict[str, Any]] = None
    # 待处理危机快照：刷新/重进档案后前端据此恢复决策弹层
    pending_crisis: Optional[Dict[str, Any]] = None
    # 探索队状态快照：在外行军/遭遇/返程，刷新后恢复同一支队伍
    expedition: Optional[Dict[str, Any]] = None
    # 对外信誉（0-100）：影响外部接单概率、商路风险与可申请订单
    reputation: int = 50
    # 贸易救援订单链：申请/审核/运输/交付/失败回退
    trade_orders: List[Dict[str, Any]] = []
    residents: List[ResidentOut] = []
    facilities: List[FacilityOut] = []
    logs: List[LogOut] = []


class AdvanceResult(BaseModel):
    session: SessionDetail
    # 本次推进挂起的待处理抉择：可能是地堡危机，也可能是探索队遭遇，
    # 前端统一据 session.pending_crisis / session.expedition.pending_encounter 渲染
    pending_event: Optional[Dict[str, Any]] = None
    # 兼容旧字段名（旧前端读取 crisis）；构造方保证与 pending_event 同值
    crisis: Optional[Dict[str, Any]] = None


class CrisisChoice(BaseModel):
    event_key: str
    choice_key: str
    target_id: Optional[int] = None
    # 待处理危机的一次性凭据，用于识别过期/并发的旧请求；旧客户端可省略
    token: Optional[str] = None


class ExpeditionSend(BaseModel):
    """派遣探索队：选择在堡居民与自带物资。"""
    member_ids: List[int]
    supplies: Dict[str, float] = {}


class ExpeditionEncounterChoice(BaseModel):
    """处理探索队途中遭遇。"""
    choice_key: str
    # 待处理遭遇的一次性凭据，用于识别过期/重复请求
    token: Optional[str] = None


class ExpeditionReturn(BaseModel):
    """召回探索队。"""
    token: Optional[str] = None


# ---- 贸易救援 ----
class TradeApply(BaseModel):
    """地堡主动向外部聚落申请贸易/救援订单。"""
    offer_key: str
    escort_ids: List[int] = []


class TradeReview(BaseModel):
    """审核外部队伍发来的贸易/救援申请。"""
    approve: bool
    token: Optional[str] = None


class TradeResult(BaseModel):
    """贸易动作返回：最新档案 + 本次操作的订单快照（便于前端定位/提示）。"""
    session: "SessionDetail"
    order: Optional[Dict[str, Any]] = None
    replayed: bool = False
    detail: Optional[str] = None


class JobAssign(BaseModel):
    job: str


class BuildRequest(BaseModel):
    category: str


class BuildableInfo(BaseModel):
    category: str
    name: str
    cost: Dict[str, float]
    level_scale: float


class TradeOfferOut(BaseModel):
    """可申请的贸易/救援模板（由信誉门槛筛选后下发）。"""
    key: str
    title: str
    kind: str
    desc: str = ""
    payment: Dict[str, float] = {}
    reward: Dict[str, float] = {}
    travel_days: int = 2
    risk: float = 0.1
    morale_bonus: int = 0
    rep_bonus: int = 2
    add_survivor: int = 0
    bonus_health: int = 0
    max_escorts: int = 2
    min_reputation: int = 0


class EngineConfig(BaseModel):
    resources: Dict[str, float]
    facility_costs: Dict[int, Dict[str, float]]
    facility_names: Dict[str, str]
    job_options: List[str]
    status: str


class Message(BaseModel):
    detail: str = "ok"


# TradeResult.session 前向引用 SessionDetail，需在模块加载后完成绑定
TradeResult.model_rebuild()