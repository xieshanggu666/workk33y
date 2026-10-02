# -*- coding: utf-8 -*-
"""贸易救援模块测试：申请 → 审核 → 运输 → 交付 / 失败回退 的完整状态链。

覆盖：
- 主动申请(outbound)：外部审核接单/拒绝、押金托管、在途运输、交付回写资源/居民/信誉/士气
- 外部申请(inbound)：管理者批准/拒绝、审核幂等回放、超时自动拒绝
- 失败回退：盗匪（半额退款 + 押运送伤员 + 回写地堡危机）/ 风暴（全额退款）
- 押运员离堡口径与探索队一致；终局中止订单押金全退；订单上限/信誉门槛
- 每日推进与探索队/地堡危机的交互
"""
import pytest

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession, Resident, EventLog
from app.core.config import SURVIVAL_TARGET_DAY
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    CRISIS_POOL,
    TRADE_OFFERS,
    TRADE_INBOUND_POOL,
    TRADE_STATUS_APPLIED,
    TRADE_STATUS_TRANSPORTING,
    TRADE_STATUS_DELIVERED,
    TRADE_STATUS_FAILED,
    TRADE_STATUS_REJECTED,
    TRADE_STATUS_ABORTED,
    TRADE_MAX_ACTIVE_ORDERS,
    TRADE_INITIAL_REPUTATION,
    FOOD,
    WATER,
    POWER,
    OXY,
)
from tests.test_engine import make_session, FixedRand  # noqa: F401


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def make_trade_session(db, residents=3, resources=None, reputation=50):
    gs = make_session(db, residents=residents, resources=resources)
    gs.reputation = reputation
    gs.trade_orders = []
    db.commit()
    db.refresh(gs)
    return gs


class ThresholdRand:
    """固定阈值随机：

    - random() 恒为 0.5：外部审核 0.5 < 接单概率(≈0.85) → 接单；
      运输风险 0.5 > 风险(≤0.25) → 安全；地堡危机 0.5 > 0.45 → 不触发；
      外部申请 0.5 > 0.4 → 不收到新申请
    - choice() 取首项
    """

    def random(self):
        return 0.5

    def choice(self, seq):
        return seq[0]


class ScriptedValues:
    """按顺序返回预设随机值，用尽后返回 tail。"""

    def __init__(self, values, tail=0.9, choice_key=None):
        self.values = list(values)
        self.tail = tail
        self.choice_key = choice_key

    def random(self):
        return self.values.pop(0) if self.values else self.tail

    def choice(self, seq):
        if self.choice_key:
            for item in seq:
                if isinstance(item, dict) and item.get("key") == self.choice_key:
                    return item
        return seq[0]


GRAIN = "grain_deal"  # 支付水25 → 食物40，运输2天


def _offer(key):
    return next(o for o in TRADE_OFFERS if o["key"] == key)


def _inject_inbound(eng, template_key=None, origin="inbound", status=None,
                    escorts=None, party="锈河聚落"):
    """直接在档案上挂一张订单（绕过随机申请，用于精确构造状态）。"""
    pool = TRADE_INBOUND_POOL if origin == "inbound" else TRADE_OFFERS
    tpl = dict(next(o for o in pool if o["key"] == (template_key or pool[0]["key"])))
    tpl["party"] = party
    order = eng._build_order(tpl, origin=origin, escorts=escorts or [])
    if status:
        order["status"] = status
    orders = list(eng.session.trade_orders or [])
    orders.append(order)
    eng.session.trade_orders = orders
    return order


def _escrow(order, gs):
    """模拟批准时已托管押金。"""
    for k, v in order.get("payment", {}).items():
        gs.resources = dict(gs.resources)
        gs.resources[k] = round(gs.resources[k] - v, 1)


# ---- 可申请订单 / 信誉门槛 ----

def test_available_offers_filtered_by_reputation(db):
    gs = make_trade_session(db, reputation=0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    keys = {o["key"] for o in eng.available_offers()}
    # 信誉 0：只能看到无门槛订单；min_reputation=50 的军械废料不可见
    assert "grain_deal" in keys
    assert "ammo_scrap" not in keys
    gs.reputation = 60
    keys = {o["key"] for o in eng.available_offers()}
    assert "ammo_scrap" in keys


def test_apply_unknown_offer_rejected(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.apply_trade("nope")


def test_apply_below_reputation_gate_rejected(db):
    gs = make_trade_session(db, reputation=0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    with pytest.raises(BunkerEngineError):
        eng.apply_trade("ammo_scrap")  # 需要信誉 50


def test_apply_creates_applied_order_without_payment(db):
    """主动申请：进入 applied(外部审核) 阶段，暂不扣押金。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    water_before = gs.resources[WATER]
    order = eng.apply_trade(GRAIN, escort_ids=[gs.residents[0].id])
    assert order["status"] == TRADE_STATUS_APPLIED
    assert order["review_by"] == "external"
    assert order["origin"] == "outbound"
    assert order["escorts"] == [gs.residents[0].id]
    assert gs.resources[WATER] == water_before  # 审核通过前不扣款
    assert len(gs.trade_orders) == 1
    # applied 阶段押运员不离堡（尚未启程）
    assert gs.residents[0].id not in eng._escort_resident_ids()


def test_apply_respects_active_order_cap(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    for _ in range(TRADE_MAX_ACTIVE_ORDERS):
        eng.apply_trade(GRAIN)
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(GRAIN)


def test_apply_rejects_busy_escort(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    # 探索队占用
    eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(GRAIN, escort_ids=[gs.residents[0].id])
    # 超员/重复
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(GRAIN, escort_ids=[gs.residents[1].id, gs.residents[2].id, gs.residents[1].id])
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(GRAIN, escort_ids=[gs.residents[1].id, gs.residents[2].id, gs.residents[0].id])


def test_apply_blocked_in_crisis_phase(db):
    gs = make_trade_session(db)
    from tests.test_engine import TriggerRand, arm_crisis
    eng = BunkerEngine(db, gs, rand=TriggerRand())
    eng.advance_day()  # 触发地堡危机
    assert eng.phase == "crisis"
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(GRAIN)


# ---- 主动申请：外部审核 + 运输 + 交付 ----

def test_outbound_full_success_chain(db):
    """申请 →（推进）外部接单托管押金 → 在途 → 交付回写。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = eng.apply_trade(GRAIN, escort_ids=[gs.residents[0].id])
    water0, rep0 = gs.resources[WATER], gs.reputation

    # day2：外部审核接单 → transporting，押金托管；当日不在途
    eng.advance_day()
    o = eng._get_order(order["id"])
    assert gs.day == 2
    assert o["status"] == TRADE_STATUS_TRANSPORTING
    assert o["elapsed_days"] == 0
    # 押金 25 已托管；押运员离堡后在堡 2 人，当日水净产出照常：
    # 士气系数 0.92 → 净水产出 7*0.92=6.44，消耗 1.3*2=2.6，净 +3.8
    assert gs.resources[WATER] == round(water0 - 25 + 3.8, 1)
    # 在途押运员离堡：不参与地堡生产/口粮
    assert gs.residents[0].id in eng._away_resident_ids()
    assert eng._away_kind(gs.residents[0].id) == "escort"

    # day3：在途行军 1/2，平安
    eng.advance_day()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_TRANSPORTING and o["elapsed_days"] == 1

    # day4：抵达交付（在堡 2 人，农场当日另产食物，故校验净增包含回报 +40）
    eng.advance_day()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_DELIVERED
    assert o["result"]["reward"][FOOD] == 40
    # 交付结果回写：本次订单奖励 40 已入库（相对 day1 初始值，净增 ≥ 40）
    assert gs.resources[FOOD] >= 340
    # 直接核对结果明细记录的入库量
    assert f"食物 +40" in o["result"]["detail"]
    assert gs.reputation == rep0 + 3
    # 全体士气 +4（含押运归队者）
    assert all(r.morale == 84 for r in gs.residents if r.alive)
    # 押运员归队
    assert gs.residents[0].id not in eng._away_resident_ids()
    assert [t["stage"] for t in o["timeline"]] == [
        TRADE_STATUS_APPLIED, TRADE_STATUS_TRANSPORTING,
        TRADE_STATUS_TRANSPORTING, TRADE_STATUS_DELIVERED,
    ]


def test_external_reject_marks_order_rejected_without_payment(db):
    """外部审核拒绝：订单 rejected，无任何资金往来。"""
    gs = make_trade_session(db)
    # random=0.99 > 接单概率 → 拒绝；同时 > 风险/危机/inbound 阈值
    eng = BunkerEngine(db, gs, rand=ScriptedValues([], tail=0.99))
    order = eng.apply_trade(GRAIN)
    water_before = gs.resources[WATER]
    eng.advance_day()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_REJECTED
    assert "拒绝" in o["result"]["reason"]
    # 押金未动：3 人在堡、士气系数 0.92 → 水净变化 7*0.92 - 1.3*3 = +2.5
    assert gs.resources[WATER] == round(water_before + 2.5, 1)
    assert gs.reputation == TRADE_INITIAL_REPUTATION  # 被外部拒绝不掉信誉


def test_external_review_deferred_when_escrow_unaffordable(db):
    """审核通过但托管时物资已不足：订单搁置回 applied，不报错不扣款，次日可再审。"""
    gs = make_trade_session(db)
    # day2 接单（0.01 必接单）；接单判定后资源被外部改空
    eng = BunkerEngine(db, gs, rand=ScriptedValues([0.01], tail=0.99))
    order = eng.apply_trade(GRAIN)
    gs.resources = {FOOD: 0, WATER: 0, POWER: 0, OXY: 0}
    eng._advance_trade_pipeline()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_APPLIED  # 押金不足，暂缓
    assert "不足" in o["result"]["reason"]


def test_rescue_delivery_adds_survivor_and_health_bonus(db):
    """救援订单交付：获救者加入地堡、人口+1、健康加成，且提交后真实落库。"""
    gs = make_trade_session(db, resources={FOOD: 999, WATER: 999, POWER: 999, OXY: 999})
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = eng.apply_trade("rescue_family")  # travel 3, add_survivor
    for _ in range(4):
        eng.advance_day()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_DELIVERED
    assert gs.survivors == 4
    assert o["result"]["added_resident"]
    new = max(r.id for r in gs.residents)
    assert any(r.id == new and r.alive for r in gs.residents)
    # 提交后用全新会话读取：获救居民必须真实落库（历史 bug：只 bump 计数未持久化行）
    db.commit()
    sid = gs.id
    db2 = SessionLocal()
    try:
        reloaded = db2.get(GameSession, sid)
        assert reloaded.survivors == 4
        assert sum(1 for r in reloaded.residents if r.alive) == 4
        newcomers = [r for r in reloaded.residents if r.joined_day == reloaded.day]
        assert len(newcomers) == 1
        assert newcomers[0].job == "general"
    finally:
        db2.close()


# ---- 外部申请(inbound)：管理者审核 ----

def test_inbound_approve_escrows_and_departs(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")  # 支付水20
    water_before = gs.resources[WATER]
    o, replayed = eng.review_trade(order["id"], True, token=order["token"])
    assert replayed is False
    assert o["status"] == TRADE_STATUS_TRANSPORTING
    assert gs.resources[WATER] == round(water_before - 20, 1)
    assert gs.last_trade["order_id"] == order["id"]
    assert gs.last_trade["decision"] == "approve"


def test_inbound_reject_marks_rejected(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    rep_before = gs.reputation
    water_before = gs.resources[WATER]
    o, _ = eng.review_trade(order["id"], False, token=order["token"])
    assert o["status"] == TRADE_STATUS_REJECTED
    assert gs.resources[WATER] == water_before  # 拒绝不扣款
    assert gs.reputation == rep_before - 1      # 拒绝外部求援：信誉 -1


def test_review_unknown_order_rejected(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    with pytest.raises(BunkerEngineError):
        eng.review_trade("nope", True)


def test_review_wrong_origin_rejected(db):
    """地堡主动申请(review_by=external)不能走管理者审核接口。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, GRAIN, origin="outbound")
    with pytest.raises(BunkerEngineError):
        eng.review_trade(order["id"], True, token=order["token"])


def test_review_approve_unaffordable_rejected_without_partial_change(db):
    """批准时押金不足：报错且订单仍停在 applied、资源不变。"""
    gs = make_trade_session(db, resources={FOOD: 1, WATER: 1, POWER: 1, OXY: 1})
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    with pytest.raises(BunkerEngineError):
        eng.review_trade(order["id"], True, token=order["token"])
    assert eng._get_order(order["id"])["status"] == TRADE_STATUS_APPLIED
    assert gs.resources[WATER] == 1


def test_review_stale_token_conflict(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    with pytest.raises(BunkerEngineConflict):
        eng.review_trade(order["id"], True, token="stale-token")
    assert eng._get_order(order["id"])["status"] == TRADE_STATUS_APPLIED


def test_review_is_idempotent_on_duplicate(db):
    """连点/并发落败：同一审核动作第二次安全回放，押金只托管一次。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    o1, r1 = eng.review_trade(order["id"], True, token=order["token"])
    water_after = gs.resources[WATER]
    o2, r2 = eng.review_trade(order["id"], True, token=order["token"])
    assert r1 is False and r2 is True
    assert gs.resources[WATER] == water_after
    assert o2["status"] == TRADE_STATUS_TRANSPORTING


def test_review_other_decision_after_done_is_conflict(db):
    """已批准后改用拒绝重试：不得回放成批准，409。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    eng.review_trade(order["id"], True, token=order["token"])
    with pytest.raises(BunkerEngineConflict):
        eng.reconcile_stale_trade(order["id"], order["token"], "reject")


def test_inbound_order_auto_rejected_after_expiry(db):
    """外部申请逾期未审核：推进时自动判拒绝，不扣信誉。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    rep_before = gs.reputation
    gs.day = order["expire_day"]
    eng._advance_trade_pipeline()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_REJECTED
    assert "超时" in o["result"]["reason"]
    assert gs.reputation == rep_before


# ---- 失败回退 ----

def _fail_setup(db, rolls, travel_days=1):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ScriptedValues(rolls, tail=0.99))
    order = _inject_inbound(
        eng, "in_water_short",
        status=TRADE_STATUS_TRANSPORTING,
        escorts=[gs.residents[0].id],
    )
    order["travel_days"] = travel_days
    _escrow(order, gs)
    return gs, eng, order


def test_bandit_failure_half_refund_escorts_hurt_and_crisis(db):
    """盗匪截击：押金半退、信誉/士气下降、押运受伤，并回写地堡危机。"""
    gs, eng, order = _fail_setup(db, rolls=[0.0, 0.0])  # 0.0<risk 失败；0.0<0.7 盗匪
    water_after_escrow = gs.resources[WATER]
    crisis = eng._advance_trade_pipeline()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_FAILED
    assert o["result"]["fail_reason"] == "bandit"
    # 押金 20 半额退还 10
    assert gs.resources[WATER] == round(water_after_escrow + 10, 1)
    assert gs.reputation == 42
    # 全体士气 -8
    assert all(r.morale == 72 for r in gs.residents if r.alive)
    # 押运员健康 -15（90 → 75），未阵亡
    assert gs.residents[0].health == 75 and gs.residents[0].alive == 1
    # 回写地堡危机
    assert crisis is not None
    assert crisis["event"] == "trade_raid"
    assert crisis["source"] == "trade"
    assert crisis["order_id"] == order["id"]
    assert eng.phase == "crisis"


def test_storm_failure_full_refund_no_crisis(db):
    """风暴失败：押金全额退还、无危机挂起。"""
    gs, eng, order = _fail_setup(db, rolls=[0.0, 0.99])  # 失败；0.99≥0.7 风暴
    water_after_escrow = gs.resources[WATER]
    crisis = eng._advance_trade_pipeline()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_FAILED
    assert o["result"]["fail_reason"] == "storm"
    assert gs.resources[WATER] == round(water_after_escrow + 20, 1)  # 全退
    assert crisis is None
    assert eng.phase == "daily"
    assert gs.reputation == 47  # -3


def test_bandit_failure_can_kill_escort_and_reduce_population(db):
    """押运员健康不足：盗匪伤害殉职，人口扣减，仍回写危机。"""
    gs, eng, order = _fail_setup(db, rolls=[0.0, 0.0])
    gs.residents[0].health = 10  # 10 - 15 → 殉职
    crisis = eng._advance_trade_pipeline()
    assert gs.residents[0].alive == 0
    assert gs.survivors == 2
    assert gs.residents[0].name in eng._get_order(order["id"])["result"]["casualties"]
    assert crisis is not None and crisis["event"] == "trade_raid"


def test_trade_raid_crisis_resolves_via_crisis_flow(db):
    """回写的商路盗匪危机走标准危机结算链，结算后回到 daily。"""
    gs, eng, order = _fail_setup(db, rolls=[0.0, 0.0])
    crisis = eng._advance_trade_pipeline()
    food_before = gs.resources[FOOD]
    # 破财消灾：食物 -12 电力 -8
    detail, replayed = eng.resolve_crisis(
        "trade_raid", "buy_off", token=crisis["token"]
    )
    assert replayed is False
    assert gs.pending_crisis is None
    assert eng.phase == "daily"
    assert gs.resources[FOOD] == round(food_before - 12, 1)
    # 幂等回放
    detail2, replay2 = eng.resolve_crisis(
        "trade_raid", "buy_off", token=crisis["token"]
    )
    assert replay2 is True
    assert gs.resources[FOOD] == round(food_before - 12, 1)


def test_failure_during_daily_advance_blocks_next_day(db):
    """失败经每日推进发生时：危机挂起，日期不再前进，必须先抉择。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short", status=TRADE_STATUS_TRANSPORTING,
                            escorts=[gs.residents[0].id])
    order["travel_days"] = 1
    _escrow(order, gs)
    # 让风险判定失败：advance_day 内随机序列包含生产后的多次调用，
    # 直接把引擎随机换成“风险必失败+盗匪”的脚本并推进
    eng.rand = ScriptedValues([0.0, 0.0], tail=0.99)
    crisis = eng.advance_day()
    assert crisis is not None and crisis["event"] == "trade_raid"
    day = gs.day
    eng.rand = ThresholdRand()
    with pytest.raises(BunkerEngineError):
        eng.advance_day()
    assert gs.day == day


# ---- 押运员离堡口径 ----

def test_escorts_excluded_from_bunker_consumption_and_crisis(db):
    """在途押运员与探索队一致：不消耗地堡口粮、不吃地堡危机全体效果。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short", status=TRADE_STATUS_TRANSPORTING,
                            escorts=[gs.residents[0].id])
    _escrow(order, gs)
    assert eng._in_bunker_count() == 2
    # 挂一个内讧（全体士气），押运员不应被波及
    from tests.test_engine import arm_crisis
    crisis = arm_crisis(eng, "mutiny")
    morale_before = gs.residents[0].morale
    eng.resolve_crisis("mutiny", "suppress", token=crisis["token"])
    assert gs.residents[0].morale == morale_before      # 押运中，不吃 -15
    assert gs.residents[1].morale == 65
    # 押运中不能调岗
    with pytest.raises(BunkerEngineError):
        eng.set_job(gs.residents[0].id, "farmer")


def test_escort_cannot_join_expedition(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    _inject_inbound(eng, "in_water_short", status=TRADE_STATUS_TRANSPORTING,
                    escorts=[gs.residents[0].id])
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([gs.residents[0].id], {FOOD: 5, WATER: 5})


def test_two_orders_cannot_share_escort(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    _inject_inbound(eng, "in_water_short", status=TRADE_STATUS_TRANSPORTING,
                    escorts=[gs.residents[0].id])
    # 第一张在途占 1 人，第二张最多 2 人但不能重复占用
    order2 = _inject_inbound(eng, "in_generator_parts")
    with pytest.raises(BunkerEngineError):
        eng._validate_escorts([gs.residents[0].id, gs.residents[1].id])
    # 其余两人可押运
    assert eng._validate_escorts([gs.residents[1].id, gs.residents[2].id]) == [
        gs.residents[1].id, gs.residents[2].id
    ]


# ---- 外部队伍主动申请（随机 inbound）----

def test_daily_advance_may_receive_inbound_offer(db):
    """推进日随机收到外部申请：进入 applied(bunker) 待审，带过期日。

    无在途订单的推进日，trade pipeline 只有一次随机（inbound 判定），
    地堡危机判定紧随其后再抽一次。
    """
    gs = make_trade_session(db)
    eng = BunkerEngine(
        db, gs,
        # 0.1≤0.4 收到外部申请 → choice 取首个聚落/模板；0.99>0.45 当日不触发地堡危机
        rand=ScriptedValues([0.1, 0.99], tail=0.99),
    )
    crisis = eng.advance_day()
    assert crisis is None
    active = [o for o in gs.trade_orders if o["status"] == TRADE_STATUS_APPLIED
              and o["review_by"] == "bunker"]
    assert len(active) == 1
    order = active[0]
    assert order["expire_day"] == gs.day + 3
    assert order["origin"] == "inbound"


# ---- 终局 / 中止 ----

def test_endgame_aborts_transporting_order_with_full_refund(db):
    gs = make_trade_session(db, resources={FOOD: 999, WATER: 999, POWER: 999, OXY: 999})
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short", status=TRADE_STATUS_TRANSPORTING,
                            escorts=[gs.residents[0].id])
    water_after_escrow = gs.resources[WATER]
    gs.day = SURVIVAL_TARGET_DAY
    eng._check_end()
    o = eng._get_order(order["id"])
    assert o["status"] == TRADE_STATUS_ABORTED
    assert gs.resources[WATER] == round(water_after_escrow + 20, 1)  # 押金全额退回
    assert gs.status == "win"
    # 押运员归队（away 集合为空）
    assert eng._away_resident_ids() == set()


def test_endgame_aborts_pending_applied_order(db):
    """applied 阶段终局：订单中止，无押金可退（尚未托管）。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    gs.day = SURVIVAL_TARGET_DAY
    gs.resources = {FOOD: 999, WATER: 999, POWER: 999, OXY: 999}
    eng._check_end()
    assert eng._get_order(order["id"])["status"] == TRADE_STATUS_ABORTED


def test_dismiss_terminal_order(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    with pytest.raises(BunkerEngineError):
        eng.dismiss_trade(order["id"])  # 未结束不能归档
    eng.review_trade(order["id"], False, token=order["token"])
    eng.dismiss_trade(order["id"])
    assert eng._get_order(order["id"]) is None


def test_trade_actions_rejected_after_game_end(db):
    gs = make_trade_session(db)
    gs.status = "over"
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(GRAIN)
    order = _inject_inbound(eng, "in_water_short")
    with pytest.raises(BunkerEngineError):
        eng.review_trade(order["id"], True, token=order["token"])


# ---- 信誉机制 ----

def test_reputation_clamped(db):
    gs = make_trade_session(db, reputation=99)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    eng._adjust_reputation(10)
    assert gs.reputation == 100
    eng._adjust_reputation(-200)
    assert gs.reputation == 0


def test_reputation_lowers_risk_and_raises_acceptance(db):
    """高信誉：接单概率更高、在途风险更低（通过公式间接验证阈值方向）。"""
    from app.services.engine import TRADE_ACCEPT_BASE, TRADE_ACCEPT_REP_SLOPE, TRADE_RISK_BASE, TRADE_RISK_REP_SLOPE
    accept_low = TRADE_ACCEPT_BASE + TRADE_ACCEPT_REP_SLOPE * 0
    accept_high = TRADE_ACCEPT_BASE + TRADE_ACCEPT_REP_SLOPE * 100
    risk_low = TRADE_RISK_BASE - TRADE_RISK_REP_SLOPE * 0
    risk_high = TRADE_RISK_BASE - TRADE_RISK_REP_SLOPE * 100
    assert accept_high > accept_low
    assert risk_high < risk_low


# ---- 日志 ----

def test_trade_lifecycle_logs_written(db):
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = eng.apply_trade(GRAIN)
    for _ in range(3):
        eng.advance_day()
    db.commit()
    logs = db.query(EventLog).filter_by(session_id=gs.id).all()
    titles = " ".join(l.title for l in logs)
    assert "贸易申请" in titles
    assert "贸易申请获批准" in titles
    assert "订单交付" in titles


# ---- 并发：乐观锁只允许一次审核落库 ----

def test_concurrent_trade_review_only_one_escrows(db):
    """两个独立会话并发批准同一张外部申请：押金只托管一次。"""
    from sqlalchemy.orm.exc import StaleDataError

    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    db.commit()
    sid, oid, token = gs.id, order["id"], order["token"]

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        ga, gb = db_a.get(GameSession, sid), db_b.get(GameSession, sid)
        BunkerEngine(db_a, ga).review_trade(oid, True, token=token)
        db_a.commit()
        BunkerEngine(db_b, gb).review_trade(oid, True, token=token)
        with pytest.raises(StaleDataError):
            db_b.commit()
        db_b.rollback()

        final = db_a.get(GameSession, sid)
        # 押金只扣一次（水 300 → 280，不含日常生产：本测试没有推进）
        assert final.resources[WATER] == 280
        assert final.trade_orders[0]["status"] == TRADE_STATUS_TRANSPORTING
    finally:
        db_a.close()
        db_b.close()


def test_concurrent_trade_review_loser_reconciles(db):
    """落败会话在刷新看到对方结果后：reconcile 凭同一凭据安全回放，不再扣款。"""
    gs = make_trade_session(db)
    eng = BunkerEngine(db, gs, rand=ThresholdRand())
    order = _inject_inbound(eng, "in_water_short")
    db.commit()
    sid, oid, token = gs.id, order["id"], order["token"]

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        BunkerEngine(db_a, db_a.get(GameSession, sid)).review_trade(oid, True, token=token)
        db_a.commit()
        # 落败方刷新到对方已落库的结果（含 last_trade 凭据）后做 reconcile
        gb = db_b.get(GameSession, sid)
        db_b.refresh(gb)
        snap, replayed = BunkerEngine(db_b, gb).reconcile_stale_trade(oid, token, "approve")
        assert replayed is True
        assert snap["status"] == TRADE_STATUS_TRANSPORTING
        # 不同动作（reject）不得回放成批准
        with pytest.raises(BunkerEngineConflict):
            BunkerEngine(db_b, gb).reconcile_stale_trade(oid, token, "reject")
        water_final = db_a.get(GameSession, sid).resources[WATER]
        assert gb.resources[WATER] == water_final
    finally:
        db_a.close()
        db_b.close()


def test_trade_and_expedition_coexist(db):
    """探索队与商队可同时在外：探索队不吃商队风险，商队遇袭仍挂地堡危机。"""
    # 探索队在途（无遭遇）+ 商队在途遇袭：当天挂 trade_raid 而非探索遭遇
    gs = make_trade_session(db)
    from tests.test_engine import ScriptedRand
    eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
    # 先派探索队
    eng.send_expedition([gs.residents[1].id], {FOOD: 20, WATER: 20})
    # 再放一张在途商队（押运另一人）
    order = _inject_inbound(
        eng, "in_water_short", status=TRADE_STATUS_TRANSPORTING,
        escorts=[gs.residents[0].id],
    )
    order["travel_days"] = 1
    _escrow(order, gs)
    # 推进日随机序列：行军无遭遇(0.99) → 商队风险失败(0.0) → 盗匪(0.0)
    # 注：pipeline 在遭遇判定之前执行，前两个值被商队风险/失败类型消费，
    # 0.99 是 pipeline 末尾的新申请判定
    eng.rand = ScriptedValues([0.0, 0.0, 0.99], tail=0.99)
    crisis = eng.advance_day()
    assert crisis is not None and crisis["event"] == "trade_raid"
    # 探索队仍在在外（未触发遭遇、未返程）
    assert gs.expedition is not None and gs.expedition["status"] == "away"
    assert gs.expedition["pending_encounter"] is None
    assert eng.phase == "crisis"
