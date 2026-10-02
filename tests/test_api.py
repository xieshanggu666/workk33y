# -*- coding: utf-8 -*-
"""HTTP 层端到端测试：每日推进 / 危机 / 探索队遭遇 / 返程的前后端一致性与并发回放。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import Base, engine, SessionLocal
from app.models import GameSession
from app.services.engine import (
    BunkerEngine,
    FOOD, WATER, POWER, OXY,
    TRADE_INBOUND_POOL,
)
from tests.test_engine import make_session, ScriptedRand, FixedRand, TriggerRand  # noqa: F401


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def _seed(fn):
    """在独立 DB 会话里布置初始状态并提交，返回 (会话 id, *setup 返回值)。"""
    db = SessionLocal()
    try:
        gs = make_session(db)
        eng = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache"))
        ret = fn(db, gs, eng)
        db.commit()
        sid = gs.id
        if ret is None:
            return (sid,)
        if isinstance(ret, tuple):
            return (sid,) + ret
        return (sid, ret)
    finally:
        db.close()


def test_full_crisis_cycle(client):
    """挂起危机 → API 结算 200 → 危机清除。"""
    def setup(db, gs, eng):
        c = gs.pending_crisis = {
            "token": "tok-1", "event": "mutiny", "day": gs.day,
            "title": "t", "desc": "d", "needs_target": False,
            "target_id": None, "target_name": None,
            "choices": [{"key": "suppress", "label": "镇压", "hint": "", "targeted": False}],
        }
        return c
    sid, crisis = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/resolve", json={
        "event_key": "mutiny", "choice_key": "suppress", "target_id": None,
        "token": "tok-1",
    })
    assert r.status_code == 200
    assert r.json()["pending_crisis"] is None


def test_advance_blocked_while_crisis_pending(client):
    """危机待处理时推进一天 → 400，日期不前进。"""
    def setup(db, gs, eng):
        gs.pending_crisis = {"token": "t", "event": "mutiny", "choices": []}
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 400
    assert client.get(f"/api/sessions/{sid}").json()["day"] == 1


def test_expedition_full_flow_and_away_flag(client):
    """API 派遣 → 引擎行军挂遭遇 → API 结算遭遇 → API 返程；成员 away 标记前后一致。"""
    # 1) API 派遣
    r = client.post("/api/sessions", json={"name": "e2e"})
    sid = r.json()["id"]
    members = r.json()["residents"]
    mid = members[0]["id"]
    r = client.post(f"/api/sessions/{sid}/expedition/send", json={
        "member_ids": [mid], "supplies": {"food": 10, "water": 10},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["expedition"]["status"] == "away"
    # 离堡成员 away=1，在堡成员 away=0
    flags = {x["id"]: x["away"] for x in body["residents"]}
    assert flags[mid] == 1
    assert all(v == 0 for k, v in flags.items() if k != mid)
    team_token = body["expedition"]["token"]

    # 2) 直接用 API 推进一天（遭遇必然触发：随机由脚本固定，这里改走真实推进的前置布置）
    #    通过引擎在独立会话挂起遭遇，再用 API 验证推进响应里的 pending_event 别名
    db = SessionLocal()
    try:
        gs = db.get(GameSession, sid)
        enc0 = BunkerEngine(db, gs, rand=ScriptedRand(encounter_key="cache")).advance_day()
        assert enc0["event"] == "cache"
        db.commit()
    finally:
        db.close()
    state = client.get(f"/api/sessions/{sid}").json()
    assert state["expedition"]["pending_encounter"]["event"] == "cache"
    enc_token = state["expedition"]["pending_encounter"]["token"]

    # 3) API 结算遭遇
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "search_carefully", "token": enc_token,
    })
    assert r.status_code == 200
    assert r.json()["expedition"]["pending_encounter"] is None

    # 4) API 返程
    r = client.post(f"/api/sessions/{sid}/expedition/return", json={"token": team_token})
    assert r.status_code == 200
    assert r.json()["expedition"] is None
    # 成员归队
    assert all(x["away"] == 0 for x in r.json()["residents"])


def test_advance_response_pending_event_aliases_crisis(client, monkeypatch):
    """真实推进挂起遭遇时：响应里 pending_event 与兼容别名 crisis 同值。"""
    from app.services import engine as engine_mod

    class Scripted(ScriptedRand):
        pass

    # 让真实请求里的引擎也使用确定性随机（遭遇必触发）
    monkeypatch.setattr(engine_mod, "_rng", lambda: Scripted(encounter_key="cache"))

    r = client.post("/api/sessions", json={"name": "alias"})
    sid = r.json()["id"]
    mid = r.json()["residents"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/expedition/send", json={
        "member_ids": [mid], "supplies": {"food": 20, "water": 20},
    })
    assert r.status_code == 200
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 200
    body = r.json()
    assert body["pending_event"] is not None
    assert body["pending_event"]["event"] == "cache"
    # 兼容旧前端的 crisis 字段同值
    assert body["crisis"] == body["pending_event"]


def test_build_locked_during_pending_encounter(client):
    """探索遭遇挂起时建造设施 → 400（后端阶段守卫，不依赖前端禁用）。"""
    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        eng.advance_day()  # 挂起 cache 遭遇
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/build", json={"category": "med"})
    assert r.status_code == 400


def test_concurrent_crisis_resolve_loser_replays_200(client):
    """对方已结算同一危机：落败方重放同抉择 → 200 幂等回放，食物只扣一次。"""
    def setup(db, gs, eng):
        gs.pending_crisis = {
            "token": "tok", "event": "mutiny", "day": gs.day,
            "title": "", "desc": "", "needs_target": False,
            "target_id": None, "target_name": None,
            "choices": [{"key": "double_ration", "label": "", "hint": "", "targeted": False}],
        }
        # 对家先完成结算（食物 -20）并提交，留下 last_resolution
        eng.resolve_crisis("mutiny", "double_ration", token="tok")
        assert gs.resources[FOOD] == 280
    (sid,) = _seed(setup)
    # 落败方带相同负载重试
    r = client.post(f"/api/sessions/{sid}/resolve", json={
        "event_key": "mutiny", "choice_key": "double_ration",
        "target_id": None, "token": "tok",
    })
    assert r.status_code == 200
    assert r.json()["resources"]["food"] == 280  # 没有第二次扣减


def test_concurrent_expedition_return_loser_replays_200(client):
    """对方已完成返程：落败方带同一队伍 token 重试 → 200 回放，战利品只入库一次。"""
    team = {}

    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 10, WATER: 10})
        encounter = eng.advance_day()
        eng.resolve_expedition_encounter("search_carefully", token=encounter["token"])
        team["token"] = gs.expedition["token"]
        detail, replayed = eng.return_expedition(token=gs.expedition["token"])
        assert replayed is False
    (sid,) = _seed(setup)
    food_after = client.get(f"/api/sessions/{sid}").json()["resources"]["food"]
    r = client.post(f"/api/sessions/{sid}/expedition/return", json={"token": team["token"]})
    assert r.status_code == 200
    assert r.json()["expedition"] is None
    assert r.json()["resources"]["food"] == food_after  # 战利品未二次入库


def test_stale_encounter_token_rejected_409(client):
    """过期遭遇 token → 409，遭遇仍在档待正确处理。"""
    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        eng.advance_day()
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "search_carefully", "token": "stale",
    })
    assert r.status_code == 409
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["expedition"]["pending_encounter"] is not None


def test_expedition_send_requires_daily_phase(client):
    """遭遇挂起时派遣第二支队伍 → 400（状态机只允许 daily 阶段派遣）。"""
    def setup(db, gs, eng):
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        eng.advance_day()
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/send", json={
        "member_ids": [1], "supplies": {},
    })
    assert r.status_code == 400


def test_encounter_supply_exhaustion_converges_via_api(client):
    """遭遇把补给扣空：API 结算遭遇即完成返程收敛，会话回到无队伍状态。"""
    team = {}

    def setup(db, gs, eng):
        # weather·就地躲避：食物 -3 水 -3；1 人队带 4/4，行军消耗 1 后剩 3/3
        eng.rand = ScriptedRand(encounter_key="weather")
        eng.send_expedition([gs.residents[0].id], {FOOD: 4, WATER: 4})
        enc = eng.advance_day()
        team["token"] = enc["token"]
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "take_shelter", "token": team["token"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["expedition"] is None            # 当场收敛，无零补给僵尸队伍
    assert body["status"] == "running"
    assert all(x["away"] == 0 for x in body["residents"])
    # 同一遭遇请求重试：幂等回放 200，资源/人口不再变化
    food = body["resources"]["food"]
    survivors = body["survivors"]
    r2 = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "take_shelter", "token": team["token"],
    })
    assert r2.status_code == 200
    assert r2.json()["resources"]["food"] == food
    assert r2.json()["survivors"] == survivors


def test_concurrent_fatal_encounter_loser_replays_200(client):
    """致命遭遇已被另一请求结算并收敛：落败方凭遭遇 token 重试 → 200 回放。"""
    team = {}

    def setup(db, gs, eng):
        gs.residents[0].health = 5
        eng.rand = ScriptedRand(encounter_key="weather")
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        enc = eng.advance_day()
        team["token"] = enc["token"]
        # 对家先结算：全体健康 -6 杀死单人队 → 遭遇 + 返程收敛一次完成
        detail, replayed = eng.resolve_expedition_encounter(
            "push_through", token=enc["token"]
        )
        assert replayed is False
        assert gs.expedition is None
        assert gs.survivors == 2
    (sid,) = _seed(setup)
    survivors_after = client.get(f"/api/sessions/{sid}").json()["survivors"]
    assert survivors_after == 2
    # 落败方带相同负载重试
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "push_through", "token": team["token"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["expedition"] is None
    assert body["survivors"] == 2  # 人口没有第二次扣减


def test_fatal_encounter_wrong_choice_after_convergence_409(client):
    """收敛完成后用同 token 另一选项重试 → 409，不能回放成别人的结算。"""
    team = {}

    def setup(db, gs, eng):
        gs.residents[0].health = 5
        eng.rand = ScriptedRand(encounter_key="weather")
        eng.send_expedition([gs.residents[0].id], {FOOD: 20, WATER: 20})
        enc = eng.advance_day()
        team["token"] = enc["token"]
        eng.resolve_expedition_encounter("push_through", token=enc["token"])
    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/expedition/resolve", json={
        "choice_key": "take_shelter", "token": team["token"],
    })
    assert r.status_code == 409


# ---- 贸易救援模块 HTTP 端到端 ----

def test_session_detail_carries_reputation_and_orders(client):
    """新建档案：信誉初始 50、订单链为空。"""
    r = client.post("/api/sessions", json={"name": "trade"})
    assert r.status_code == 201
    body = r.json()
    assert body["reputation"] == 50
    assert body["trade_orders"] == []


def test_trade_offers_filtered_and_apply(client):
    """GET 可申请订单 → POST 申请生成 applied 订单。"""
    r = client.post("/api/sessions", json={"name": "trade"})
    sid = r.json()["id"]
    r = client.get(f"/api/sessions/{sid}/trade/offers")
    assert r.status_code == 200
    offers = r.json()
    assert any(o["key"] == "grain_deal" for o in offers)
    # 新档案信誉 50 看不到 50 门槛的军械废料（min_reputation=50 实际可见，50>=50）
    keys = {o["key"] for o in offers}
    assert "grain_deal" in keys

    mid = r  # noop
    residents = client.get(f"/api/sessions/{sid}").json()["residents"]
    r = client.post(f"/api/sessions/{sid}/trade/apply", json={
        "offer_key": "grain_deal", "escort_ids": [residents[0]["id"]],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["order"]["status"] == "applied"
    assert body["session"]["trade_orders"][0]["id"] == body["order"]["id"]


def test_trade_apply_unknown_offer_400(client):
    r = client.post("/api/sessions", json={"name": "t"})
    sid = r.json()["id"]
    r = client.post(f"/api/sessions/{sid}/trade/apply", json={"offer_key": "nope"})
    assert r.status_code == 400


def test_inbound_review_approve_and_replay(client):
    """外部申请 → API 批准托管押金 → 同负载重试幂等回放，押金只扣一次。"""

    def setup(db, gs, eng):
        order = eng._build_order(
            dict(next(o for o in TRADE_INBOUND_POOL if o["key"] == "in_water_short"),
                 party="锈河聚落"),
            origin="inbound", escorts=[],
        )
        gs.trade_orders = [order]
        return order["id"], order["token"]

    sid, order_id, token = _seed(setup)
    water_before = client.get(f"/api/sessions/{sid}").json()["resources"]["water"]
    r = client.post(f"/api/sessions/{sid}/trade/{order_id}/review", json={
        "approve": True, "token": token,
    })
    assert r.status_code == 200
    assert r.json()["order"]["status"] == "transporting"
    water_after = r.json()["session"]["resources"]["water"]
    assert water_after < water_before  # 押金已托管
    # 连点/并发落败：同负载重试 200 幂等回放，押金不二次扣减
    r2 = client.post(f"/api/sessions/{sid}/trade/{order_id}/review", json={
        "approve": True, "token": token,
    })
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["session"]["resources"]["water"] == water_after


def test_inbound_review_reject(client):
    from app.services.engine import TRADE_INBOUND_POOL

    def setup(db, gs, eng):
        tpl = dict(next(o for o in TRADE_INBOUND_POOL if o["key"] == "in_water_short"), party="x")
        order = eng._build_order(tpl, origin="inbound", escorts=[])
        gs.trade_orders = [order]
        return order["id"], order["token"]

    sid, order_id, token = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/trade/{order_id}/review", json={
        "approve": False, "token": token,
    })
    assert r.status_code == 200
    assert r.json()["order"]["status"] == "rejected"
    state = client.get(f"/api/sessions/{sid}").json()
    assert state["trade_orders"][0]["status"] == "rejected"
    # 拒绝外部求援信誉 -1（50 → 49）
    assert state["reputation"] == 49


def test_trade_review_stale_token_409(client):
    from app.services.engine import TRADE_INBOUND_POOL

    def setup(db, gs, eng):
        tpl = dict(next(o for o in TRADE_INBOUND_POOL if o["key"] == "in_water_short"), party="x")
        order = eng._build_order(tpl, origin="inbound", escorts=[])
        gs.trade_orders = [order]
        return order["id"]

    sid, order_id = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/trade/{order_id}/review", json={
        "approve": True, "token": "stale",
    })
    assert r.status_code == 409
    assert client.get(f"/api/sessions/{sid}").json()["trade_orders"][0]["status"] == "applied"


def test_dismiss_terminal_order(client):
    from app.services.engine import TRADE_INBOUND_POOL

    def setup(db, gs, eng):
        tpl = dict(next(o for o in TRADE_INBOUND_POOL if o["key"] == "in_water_short"), party="x")
        order = eng._build_order(tpl, origin="inbound", escorts=[])
        gs.trade_orders = [order]
        eng.review_trade(order["id"], False, token=order["token"])
        return order["id"]

    sid, order_id = _seed(setup)
    r = client.delete(f"/api/sessions/{sid}/trade/{order_id}")
    assert r.status_code == 200
    assert r.json()["trade_orders"] == []


def test_escort_serialized_with_away_kind(client):
    """在途押运员序列化 away=1 / away_kind=escort。"""
    from app.services.engine import TRADE_INBOUND_POOL

    def setup(db, gs, eng):
        tpl = dict(next(o for o in TRADE_INBOUND_POOL if o["key"] == "in_water_short"), party="x")
        order = eng._build_order(tpl, origin="inbound", escorts=[gs.residents[0].id])
        order["status"] = "transporting"
        gs.trade_orders = [order]

    (sid,) = _seed(setup)
    body = client.get(f"/api/sessions/{sid}").json()
    flags = {x["id"]: (x["away"], x["away_kind"]) for x in body["residents"]}
    rid = body["residents"][0]["id"]
    assert flags[rid] == (1, "escort")
    assert all(v == (0, None) for k, v in flags.items() if k != rid)


def test_bandit_failure_crisis_resolves_via_api(client, monkeypatch):
    """在途商队遇袭：推进挂起 trade_raid 危机 → API 结算后危机清除、订单 failed。"""
    from app.services import engine as engine_mod
    from app.services.engine import (
        TRADE_INBOUND_POOL, TRADE_STATUS_TRANSPORTING,
    )

    # 风险判定必失败(0<risk) + 盗匪类型(0<0.7)；危机/申请判定恒不触发(0.99)
    class BanditRand:
        def __init__(self):
            self.n = 0
        def random(self):
            self.n += 1
            return 0.0 if self.n <= 2 else 0.99
        def choice(self, seq):
            return seq[0]

    monkeypatch.setattr(engine_mod, "_rng", BanditRand)

    def setup(db, gs, eng):
        tpl = dict(next(o for o in TRADE_INBOUND_POOL if o["key"] == "in_water_short"), party="x")
        order = eng._build_order(tpl, origin="inbound", escorts=[gs.residents[0].id])
        order["status"] = TRADE_STATUS_TRANSPORTING
        order["travel_days"] = 1
        gs.trade_orders = [order]

    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 200
    body = r.json()
    crisis = body["session"]["pending_crisis"]
    assert crisis is not None and crisis["event"] == "trade_raid"
    order = body["session"]["trade_orders"][0]
    assert order["status"] == "failed"
    assert order["result"]["fail_reason"] == "bandit"
    # API 结算回写的危机
    r2 = client.post(f"/api/sessions/{sid}/resolve", json={
        "event_key": "trade_raid", "choice_key": "buy_off",
        "target_id": None, "token": crisis["token"],
    })
    assert r2.status_code == 200
    assert r2.json()["pending_crisis"] is None


def test_trade_apply_locked_during_crisis(client):
    """危机待处理阶段申请贸易 → 400（后端阶段守卫）。"""

    def setup(db, gs, eng):
        gs.pending_crisis = {"token": "t", "event": "mutiny", "choices": []}

    (sid,) = _seed(setup)
    r = client.post(f"/api/sessions/{sid}/trade/apply", json={"offer_key": "grain_deal"})
    assert r.status_code == 400
