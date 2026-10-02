# -*- coding: utf-8 -*-
"""末日地堡生存核心引擎。

资源守恒循环：
  每日净变化 = 设施产出 - 人口消耗 - 运营损耗
  产出受设施等级 + 人力资源(工程师/农夫加成) + 士气系数影响
"""

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..models import GameSession, Resident, Facility, EventLog
from ..core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY

import uuid

# 资源键
FOOD, WATER, POWER, OXY = "food", "water", "power", "oxygen"
RESOURCE_KEYS = [FOOD, WATER, POWER, OXY]

# 每日人均基础消耗
BASE_CONSUME = {FOOD: 1.5, WATER: 1.3, POWER: 1.0, OXY: 0.8}

# 设施基础产出（等级1）
FACILITY_OUTPUT = {
    "farm": {FOOD: 6.0, POWER: -1.5},   # 菜园产食物，耗电
    "water": {WATER: 7.0, POWER: -1.0}, # 净水器产水，耗电
    "power": {POWER: 8.0},              # 发电机产电
    "oxygen": {OXY: 6.0, POWER: -1.0},  # 水培/制氧耗电产氧
    "med": {},                          # 医疗：加速回复健康，微耗电
    "storage": {},                      # 仓库：降低损耗
}
FACILITY_LEVEL_SCALE = 1.6  # 升级产出按比例放大
FACILITY_COST = {  # 建造/升级消耗 builder cost
    1: {FOOD: 20, WATER: 10, POWER: 15},
    2: {FOOD: 35, WATER: 18, POWER: 28},
    3: {FOOD: 60, WATER: 30, POWER: 45},
}

# 岗位
JOB_EFFICIENCY = {"engineer": 1.25, "farmer": 1.3, "general": 1.0}

# 危机事件概率
CRISIS_DAY_CHANCE = 0.45

# 探索队系统
EXPEDITION_SUPPLY_PER_DAY = {FOOD: 1.0, WATER: 1.0}  # 每人每日消耗自带物资
EXPEDITION_MAX_DAYS = 7       # 最长探索天数，期满强制返程
EXPEDITION_ENCOUNTER_CHANCE = 0.85  # 每日行军遭遇概率
EXPEDITION_MAX_MEMBERS = 4    # 每支探索队上限

# 贸易救援系统：
# 状态链 申请/审核(applied) → 运输(transporting) → 交付(delivered)/失败回退(failed)/拒绝(rejected)
#   applied      —— 申请已发出等待审核：外部队伍向地堡申请（review_by=bunker，地堡管理者审核），
#                    或地堡主动申请（review_by=external，当日由外部聚落审核）
#   transporting —— 审核通过，商队在途，随每日推进行军并抽失败风险
#   delivered    —— 终态：送达交付，结果回写资源/居民/信誉/士气
#   failed       —— 终态：失败回退，押金按失败类型退还/没收，并可能回写为地堡危机
#   rejected     —— 终态：审核拒绝或申请超时，无资金往来
TRADE_STATUS_APPLIED = "applied"
TRADE_STATUS_TRANSPORTING = "transporting"
TRADE_STATUS_DELIVERED = "delivered"
TRADE_STATUS_FAILED = "failed"
TRADE_STATUS_REJECTED = "rejected"
TRADE_STATUS_ABORTED = "aborted"  # 终局强制中止（押金全额退回）
TRADE_TERMINAL_STATUSES = (
    TRADE_STATUS_DELIVERED,
    TRADE_STATUS_FAILED,
    TRADE_STATUS_REJECTED,
    TRADE_STATUS_ABORTED,
)
TRADE_ACTIVE_STATUSES = (TRADE_STATUS_APPLIED, TRADE_STATUS_TRANSPORTING)
TRADE_MAX_ESCORTS = 2          # 每张订单最多押运人数
TRADE_MAX_ACTIVE_ORDERS = 3    # 同时在途/待审订单上限
TRADE_INBOUND_CHANCE = 0.4     # 每日外部队伍主动申请贸易/救援的概率
TRADE_INBOUND_EXPIRE_DAYS = 3  # 外部申请的审核时限，超时自动判为拒绝
TRADE_INITIAL_REPUTATION = 50  # 新档案初始信誉

# 外部聚落审核（地堡主动申请）：信誉越高越容易接单
TRADE_ACCEPT_BASE = 0.55
TRADE_ACCEPT_REP_SLOPE = 0.006
# 商路风险（运输每日）：基础风险 - 信誉修正，再叠加订单固有风险
TRADE_RISK_BASE = 0.16
TRADE_RISK_REP_SLOPE = 0.0012


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _rng():
    """简单投影式随机数，便于测试时可注入 seed。"""
    import random
    return random.Random()


class BunkerEngineError(Exception):
    pass


class BunkerEngineConflict(BunkerEngineError):
    """并发冲突（乐观锁版本不匹配），HTTP 层映射为 409。"""


# 档案状态机阶段：
#   daily  —— 每日阶段，可建造/升级/调岗，可推进一天
#   crisis —— 危机阶段，存在待处理危机，除结算危机外拒绝一切推进与经营动作
#   ended  —— 终局（win/over），拒绝任何状态变更
PHASE_DAILY, PHASE_CRISIS, PHASE_ENDED = "daily", "crisis", "ended"
# 探索阶段：探索队在外且存在待处理遭遇，状态机拒绝一切经营/推进动作
PHASE_EXPEDITION = "expedition"


class BunkerEngine:
    def __init__(self, db: Session, session: GameSession, rand=None):
        self.db = db
        self.session = session
        self.rand = rand or _rng()

    # ---- 状态机 ----
    @property
    def phase(self):
        if self.session.status != "running":
            return PHASE_ENDED
        if self.session.pending_crisis:
            return PHASE_CRISIS
        exp = self.session.expedition
        if exp and exp.get("status") == "away" and exp.get("pending_encounter"):
            return PHASE_EXPEDITION
        return PHASE_DAILY

    def _require_phase(self, phase, message):
        if self.phase != phase:
            raise BunkerEngineError(message)

    # ---- 资源查询 ----
    def get_resources(self):
        return self.session.resources or {k: 0 for k in RESOURCE_KEYS}

    def _set_resource(self, key, val):
        # 复制后整体回写，确保 JSON 列的变更被 SQLAlchemy 追踪并落库
        res = dict(self.session.resources or {k: 0 for k in RESOURCE_KEYS})
        res[key] = round(max(0.0, val), 1)
        self.session.resources = res

    def _add_resource(self, key, delta):
        res = self.session.resources or {k: 0 for k in RESOURCE_KEYS}
        cur = res.get(key, 0.0)
        nxt = max(0.0, cur + delta)
        new_res = dict(res)
        new_res[key] = round(nxt, 1)
        self.session.resources = new_res
        return nxt

    # ---- 设施 ----
    def facility_output(self, facility: Facility):
        base = FACILITY_OUTPUT.get(facility.category, {})
        mult = FACILITY_LEVEL_SCALE ** (facility.level - 1)
        out = {k: v * mult for k, v in base.items()}
        # 农夫/工程师提升产出设施
        if facility.category in ("farm", "oxygen") and self.job_count("farmer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("farmer")
        if facility.category == "power" and self.job_count("engineer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("engineer")
        return out

    def job_count(self, job):
        away = self._away_resident_ids()
        return sum(1 for r in self.session.residents if r.alive and r.job == job and r.id not in away)

    def active_facilities(self):
        return [f for f in self.session.facilities if f.status == "active"]

    # ---- 外出人员追踪 ----
    def _away_resident_ids(self):
        """当前离堡执行任务的居民编号（探索队 + 贸易押运）；无在外任务时为空集。

        探索队与商队押运的居民统一暂停地堡生产、不消耗地堡口粮、不参与
        地堡危机效果，两类任务的“离堡”口径一致。
        """
        ids = set()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            ids.update(exp.get("members", []))
        for order in self._active_orders():
            ids.update(order.get("escorts", []))
        return ids

    def _away_kind(self, resident_id):
        """居民当前的外出类型：'expedition' / 'escort' / None。"""
        exp = self.session.expedition
        if exp and exp.get("status") == "away" and resident_id in exp.get("members", []):
            return "expedition"
        for order in self._active_orders():
            if order.get("status") == TRADE_STATUS_TRANSPORTING and resident_id in order.get("escorts", []):
                return "escort"
        return None

    def _away_residents(self):
        """探索队编制内的全部居民（含已阵亡，用于返程结算）。"""
        ids = self._away_resident_ids()
        return [r for r in self.session.residents if r.id in ids]

    def _in_bunker_residents(self):
        """地堡内存活居民（排除探索队成员）。"""
        away = self._away_resident_ids()
        return [r for r in self.session.residents if r.alive and r.id not in away]

    def _in_bunker_count(self):
        return len(self._in_bunker_residents())

    # ---- 每日推进 ----
    def advance_day(self):
        # 终局或存在待处理抉择（危机/探索遭遇）时都不能推进：抉择不可被"再点一天"跳过
        self._require_phase(PHASE_DAILY, "存在待处理抉择，必须先完成才能推进")
        self.session.day += 1
        # 在任何产出/探索队结算之前快照当日终局裁决：抵达目标日立即胜利；
        # 若推进前已全线枯竭，随后的当日产出或探索队带回的战利品/余粮
        # 都不得把败局“救回”——终局在当天只收敛一次
        pre_verdict = self._end_verdict()
        self._apply_production_and_consumption()
        self._apply_health_morale()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            # 探索队在外出差：地堡按在堡人口结算，探索队消耗自带物资、行军并触发遭遇
            self._apply_expedition_travel(exp, pre_verdict=pre_verdict)
            # 强制返程（补给耗尽/期满/全员失联）会清除探索队状态：
            # 此时不得再用旧 exp 触发遭遇，否则会把已结算的队伍恢复成"在外"
            if self.session.expedition is None:
                # 贸易救援订单与探索队并行：即便队伍当天已收敛，商队仍正常推进
                crisis = self._advance_trade_pipeline(pre_verdict=pre_verdict)
                self._check_end(forced_verdict=pre_verdict)
                return crisis
            # 终局优先：抵达目标日胜利，或地堡因在堡匮乏/人口归零失败时，
            # 在外队伍先安全返程（战利品入库、剩余物资归还、幸存者归队），
            # 再统一收敛到 ended——绝不在 ended 档案上留下无法处理的"僵尸队伍"
            if pre_verdict is not None or self._end_conditions_met():
                self._settle_expedition(
                    self.session.expedition, reason="终局已至，探索队返程"
                )
                crisis = self._advance_trade_pipeline(pre_verdict=pre_verdict)
                self._check_end(forced_verdict=pre_verdict)
                return crisis
            # 贸易救援订单先行：商队遇袭会挂起地堡危机（盗匪截击），
            # 当天探索队不再触发遭遇——地堡先处理危机，次日再行军
            crisis = self._advance_trade_pipeline(pre_verdict=pre_verdict)
            if crisis is not None:
                return crisis
            # 探索队行军中：触发遭遇（替代地堡危机），遭遇挂起后进入 expedition 阶段
            return self._maybe_trigger_expedition_encounter(self.session.expedition)
        # 终局优先：抵达目标日或全面崩溃直接结算结局，不再凭空挂起一个
        # 永远无法处理的危机（统一每日推进 → 危机处理 → 终局的流转）
        if self._check_end(forced_verdict=pre_verdict):
            return None
        # 贸易救援订单：运输在途的商队行军/抽风险（失败可能挂起地堡危机），
        # 并可能收到外部聚落发来的新申请
        crisis = self._advance_trade_pipeline()
        if crisis is not None:
            return crisis
        return self._maybe_trigger_crisis()

    def _end_conditions_met(self):
        """只判定终局条件、不写终局状态（用于终局前的探索队返程收敛）。"""
        return self._end_verdict() is not None

    def _end_verdict(self):
        """当前状态对应的终局裁决：返回 None（未终局）或 (win, reason)。

        纯判定、不写状态。返程结算在战利品/余粮入库前先快照一次裁决，
        保证“全线枯竭”的败局不会被随后入库的战利品抬过阈值而“复活”，
        终局状态只收敛一次且与结算路径（主动返程/强制返程/遭遇收敛）无关。
        """
        if self.session.day >= self.session.target_day:
            return True, f"坚持到第{self.session.day}天，末日阴影散去，幸存者们走向了新生。"
        if self.session.survivors <= 0:
            return False, "所有幸存者都已逝去，地堡陷入永恒的寂静。"
        res = self.get_resources()
        if all(res.get(k, 0) <= 1 for k in RESOURCE_KEYS):
            return False, "食物、水源、电力和氧气全线枯竭，地堡无法再维系生命。"
        return None

    def _apply_production_and_consumption(self):
        # 离堡人员不消耗地堡物资（吃自带口粮），地堡消耗只计在堡人口
        pop = self._in_bunker_count()
        # 士气系数(在堡人员平均士气)：低士气降低产出；探索队在外不参与地堡生产
        avg_morale = self.avg_morale(in_bunker_only=True)
        morale_factor = 0.6 + 0.4 * (avg_morale / 100.0)

        # 消耗
        consume = {}
        for k in RESOURCE_KEYS:
            consume[k] = BASE_CONSUME[k] * pop

        # 产出（累计设施净产）
        prod = {k: 0.0 for k in RESOURCE_KEYS}
        for f in self.active_facilities():
            for k, v in self.facility_output(f).items():
                prod[k] += v * morale_factor

        # 应用净变化（消耗优先，产出后）
        for k in RESOURCE_KEYS:
            net = prod.get(k, 0.0) - consume[k]
            self._add_resource(k, net)

        # 日志
        self._log(
            "update",
            f"第{self.session.day}天 · 生存更新",
            f"人口{pop}，食物净变{round(consume[FOOD]-prod[FOOD],1):+}、水{round(consume[WATER]-prod[WATER],1):+}、电力{round(consume[POWER]-prod[POWER],1):+}、氧气{round(consume[OXY]-prod[OXY],1):+}",
            decision="例行更新",
        )

    def _apply_health_morale(self):
        res = self.get_resources()
        away = self._away_resident_ids()
        # 资源不足影响（仅作用于在堡居民；探索队吃自带物资，不受地堡短缺波及）
        for r in self.session.residents:
            if not r.alive:
                continue
            if r.id in away:
                continue
            morale = r.morale
            # 资源不足影响
            for k, name in ((FOOD, "食物"), (WATER, "水源"), (OXY, "氧气"), (POWER, "电力")):
                if res.get(k, 0) <= 15:
                    morale -= 2.0
            # 医疗站回复 + 保持士气
            if self.has_category("med"):
                if r.health < 100:
                    r.health = _clamp(r.health + 1.2)
            # 低健康拖累士气
            if r.health < 30:
                morale -= 3.0
            # 士气自然衰减/恢复向基准 75
            if morale < 75:
                morale += 0.5
            elif morale > 80:
                morale -= 0.3
            r.morale = _clamp(morale)
        # 去除最严重短缺导致的死亡
        self._apply_starvation_deaths()

    def has_category(self, cat):
        return any(f.category == cat and f.status == "active" for f in self.session.facilities)

    def _apply_starvation_deaths(self):
        res = self.get_resources()
        critical = [k for k in RESOURCE_KEYS if res.get(k, 0) <= 0]
        if not critical:
            return
        # 每日最多因匮乏死 1 人，依次从在堡最弱居民开始（探索队不在堡内，不参与地堡匮乏判定）
        alive = self._in_bunker_residents()
        if not alive:
            return
        weakest = min(alive, key=lambda r: r.health)
        weakest.alive = 0
        weakest.health = 0
        self.session.survivors -= 1
        self._log("crisis", "生存危机：资源耗尽", f"{weakest.name} 因匮乏失去生命。", decision="自然事件")

    def avg_morale(self, in_bunker_only=False):
        """平均士气。

        in_bunker_only=True（地堡设施产出加成）只统计在堡存活居民：
        探索队在外时其士气不参与地堡生产结算；终局评分等全局口径仍统计全体存活者。
        """
        if in_bunker_only:
            alive = self._in_bunker_residents()
        else:
            alive = [r for r in self.session.residents if r.alive]
        if not alive:
            return 0.0
        return sum(r.morale for r in alive) / len(alive)

    def _log(self, etype, title, detail, decision=None):
        self.db.add(
            EventLog(
                session_id=self.session.id,
                day=self.session.day,
                event_type=etype,
                title=title,
                detail=detail,
                decision=decision,
            )
        )

    # ---- 危机轮盘 ----

    @staticmethod
    def _effect_scope(effect):
        """健康/士气效果的作用域：'single' 仅目标本人，'all' 全体存活者。

        数字简写默认为全体；单体效果须显式声明
        {"value": -5, "target": "single"}。
        """
        if isinstance(effect, dict):
            return effect.get("target", "all")
        return "all"

    @staticmethod
    def _effect_value(effect):
        return effect["value"] if isinstance(effect, dict) else effect

    def _event_needs_target(self, event):
        """事件是否存在只作用于单个居民的决策；只有这类事件才随机目标。"""
        for c in event["choices"]:
            effects = c.get("effects", {})
            for stat in ("health", "morale"):
                if stat in effects and self._effect_scope(effects[stat]) == "single":
                    return True
        return False

    def _maybe_trigger_crisis(self):
        if self.rand.random() > CRISIS_DAY_CHANCE:
            return None
        event = self.rand.choice(CRISIS_POOL)
        crisis = self._build_crisis(event)
        # 待处理危机整体写入存档：事件、目标、选项与一次性 token 一起绑定，
        # 刷新页面后凭档案即可恢复同一个决策
        self.session.pending_crisis = crisis
        return crisis

    def _build_crisis(self, event):
        # 仅当事件存在单体效果的决策时才抽取受影响居民；
        # 全体事件不产生目标，前端也无从回传 target_id。
        # 目标只从"在堡存活居民"中抽取：探索队外出期间不受地堡危机波及
        needs_target = self._event_needs_target(event)
        alive = self._in_bunker_residents()
        target = self.rand.choice(alive) if needs_target and alive else None
        return {
            "token": uuid.uuid4().hex,  # 本次待处理危机的一次性凭据
            "event": event["key"],
            "day": self.session.day,
            "title": event["title"],
            "desc": event["desc"],
            "needs_target": needs_target,
            "target_id": target.id if target else None,
            "target_name": target.name if target else None,
            "choices": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "hint": c.get("hint", ""),
                    "targeted": self._choice_targeted(c),
                }
                for c in event["choices"]
            ],
        }

    @classmethod
    def _choice_targeted(cls, choice):
        """该决策是否含只作用于目标本人的健康/士气效果。"""
        effects = choice.get("effects", {})
        return any(
            cls._effect_scope(effects[stat]) == "single"
            for stat in ("health", "morale")
            if stat in effects
        )

    def _ensure_running(self):
        """结算边界：游戏结束后拒绝一切状态变更。"""
        if self.session.status != "running":
            raise BunkerEngineError("游戏已结束，无法执行该操作")

    def _require_daily_phase(self, action):
        """经营/推进类动作只允许在每日阶段执行。"""
        self._ensure_running()
        if self.phase == PHASE_CRISIS:
            raise BunkerEngineError(f"存在待处理危机，必须先完成抉择才能{action}")
        if self.phase == PHASE_EXPEDITION:
            raise BunkerEngineError(f"存在待处理探索遭遇，必须先完成抉择才能{action}")

    def _pending_event(self):
        """取出当前待处理危机对应的事件定义；存档损坏时视为无法结算。"""
        pending = self.session.pending_crisis
        if not pending:
            return None, None
        event_key = pending.get("event")
        event = next((e for e in CRISIS_POOL if e["key"] == event_key), None)
        if event is None:
            raise BunkerEngineError("待处理危机已失效，请刷新档案后重试")
        return pending, event

    @staticmethod
    def _matches_resolution(rec, event_key, choice_key, target_id, day=None):
        """判断落败/重试请求是否就是上一次已完成的那次结算（幂等回放）。

        除事件/选项/目标外还核对危机发生日，避免不同天的同类型危机被误重放；
        day 为 None（调用方拿不到上下文）时退化为不校验天数。
        """
        if not rec or rec.get("event") != event_key or rec.get("choice") != choice_key:
            return False
        if day is not None and rec.get("day") is not None and rec.get("day") != day:
            return False
        return (rec.get("target_id") or None) == (target_id or None)

    def _resolve_target(self, target_id, required):
        """统一解析目标居民。

        - required=True（所选决策含单体效果）：必须显式给出目标，且目标归属
          当前档案并存活；跨档案编号、不存在、已故或缺席一律报错。
        - required=False（全体/资源类决策）：忽略客户端传入的目标，返回 None，
          效果按全体结算，前端回传谁都不会把全体效果收窄成单体。
        """
        if not required:
            return None
        if target_id is None:
            raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
        target = next((r for r in self.session.residents if r.id == target_id), None)
        if target is None:
            raise BunkerEngineError("目标居民不存在或不属于当前档案")
        if not target.alive:
            raise BunkerEngineError("目标居民已故，无法作为效果目标")
        return target

    def resolve_crisis(self, event_key, choice_key, target_id=None, token=None):
        """结算待处理危机。

        结算必须命中档案里唯一的待处理危机：事件、选项、单体目标都与存档绑定，
        既不能凭空伪造一场危机（无待处理危机时拒绝），也不能重复结算
        （结算后待处理危机被清除并留下幂等凭据，重放只返回上次结果）。
        返回 (detail, replayed)：replayed=True 表示这是重复请求，未再次施加效果。
        """
        self._ensure_running()
        pending, event = self._pending_event()

        # 已有同一危机（事件/选项/目标/发生日一致）的结算记录：
        # 重复提交（含并发落败方）只回放，不二次结算
        pending_day = pending.get("day") if pending else None
        if self._matches_resolution(
            self.session.last_resolution, event_key, choice_key, target_id, day=pending_day
        ):
            return self.session.last_resolution.get("detail", ""), True

        if pending is None:
            raise BunkerEngineError("当前没有待处理的危机，无法结算")

        # 事件必须与存档中的待处理危机一致：不能用 A 事件的请求去结算 B
        if event_key != pending.get("event"):
            raise BunkerEngineError("危机事件与当前待处理事件不符")
        # token 用于区分“同一危机上一次的旧点击”与刷新后恢复的当前决策；
        # 旧客户端/旧档案没有 token 时退化为仅按事件匹配
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该危机决策已过期，请按当前危机重新选择")

        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")

        effects = choice.get("effects", {})

        # 作用域由所选决策的效果声明决定，客户端传入的 target_id 不能改变它：
        # 单体效果必须携带有效目标，全体效果一律忽略客户端目标
        targeted = self._choice_targeted(choice)
        if targeted:
            # 目标与待处理危机绑定：不能用任意/其他居民编号替换事件目标
            bound_id = pending.get("target_id")
            if target_id is None:
                raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
            if bound_id is not None and target_id != bound_id:
                raise BunkerEngineError("目标居民与本次危机指定的幸存者不符")
        # 在应用任何效果前完成目标校验，保证失败时档案状态不发生部分变更
        target = self._resolve_target(target_id, required=targeted)

        detail_parts = []

        # 资源效果
        for k, v in effects.get("resources", {}).items():
            self._add_resource(k, v)
            detail_parts.append(f"{RESOURCE_ZH.get(k,k)} {v:+.0f}")
        # 健康/士气效果：single 只作用于目标本人，all 作用于在堡全体存活者
        # （探索队外出期间不参与地堡危机结算，与每日短缺/生产口径一致）
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                pool = [target]
                scope = f"仅{target.name}"
            else:
                pool = self._in_bunker_residents()
                scope = "全体"
            for r in pool:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")
        if "add_resident" in effects:
            self._add_resident(effects["add_resident"])
            detail_parts.append(f"加入新幸存者 {effects['add_resident']}")
        if effects.get("trap"):
            detail_parts.append("（不良后果）")

        # 日志与实际结算同一作用域：单体写名，全体写明“全体幸存者”
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", event["title"], f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])

        # 清除待处理危机并记下幂等凭据——无论后续是否终局，本危机都已结算
        self.session.pending_crisis = None
        self.session.last_resolution = {
            "token": pending.get("token"),
            "event": event["key"],
            "choice": choice["key"],
            "target_id": target.id if targeted else None,
            "day": pending.get("day"),
            "detail": detail,
        }
        self._check_end()
        return detail, False

    def reconcile_stale_resolution(self, event_key, choice_key, target_id, token=None):
        """并发落败（版本冲突）后核对：若对方提交的是同一次结算则安全回放。

        返回 (detail, replayed)；请求与任何已知结算都对不上时抛 409，
        由调用方提示“危机状态已变化”，杜绝并发重复结算。
        """
        rec = self.session.last_resolution
        if self._matches_resolution(rec, event_key, choice_key, target_id) and (
            token is None or not rec.get("token") or token == rec.get("token")
        ):
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("危机状态已被其他请求更新，请刷新后重试")

    def _add_resident(self, name):
        r = Resident(
            session_id=self.session.id,
            name=name,
            job="general",
            health=70.0,
            morale=60.0,
            alive=1,
            joined_day=self.session.day,
        )
        self.db.add(r)
        self.session.survivors += 1

    # ---- 探索队 ----
    def _random_survivor_name(self):
        import random
        surnames = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许")
        givens = list("伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英华玉萍红斌")
        return random.choice(surnames) + random.choice(givens)

    def send_expedition(self, member_ids, supplies):
        """派遣探索队：选择在堡居民并分配自带物资，队伍出发后暂停地堡生产。

        离堡人员不参与设施产出、不消耗地堡口粮；行军消耗自带物资，
        途中遭遇由玩家抉择，返程时统一结算战利品与伤亡。
        """
        self._ensure_running()
        if self.phase != PHASE_DAILY:
            raise BunkerEngineError("当前状态无法派遣探索队")
        if self.session.expedition:
            raise BunkerEngineError("已有探索队在外，无法同时派遣第二支队伍")
        if not member_ids:
            raise BunkerEngineError("必须选择至少一名居民参加探索队")
        if len(set(member_ids)) != len(member_ids):
            raise BunkerEngineError("同一名居民不能重复编入探索队")
        if len(member_ids) > EXPEDITION_MAX_MEMBERS:
            raise BunkerEngineError(f"探索队最多 {EXPEDITION_MAX_MEMBERS} 人")
        # 校验队员：必须是在堡存活居民
        members = []
        for mid in member_ids:
            r = next((x for x in self.session.residents if x.id == mid), None)
            if not r or not r.alive:
                raise BunkerEngineError("队员不存在或已故，无法参加探索队")
            if r.id in self._away_resident_ids():
                raise BunkerEngineError(f"{r.name} 已在探索队中")
            members.append(r)
        # 校验并扣除自带物资
        supply_cost = {}
        for k, v in (supplies or {}).items():
            if k not in RESOURCE_KEYS:
                raise BunkerEngineError(f"未知物资 {k}")
            if v < 0:
                raise BunkerEngineError("物资数量不能为负")
            supply_cost[k] = float(v)
        if not self._can_afford(supply_cost):
            raise BunkerEngineError("物资不足，无法派遣")
        for k, v in supply_cost.items():
            self._add_resource(k, -v)
        # 写入探索队快照（含一次性 token，刷新后恢复同一支队伍）
        exp = {
            "token": uuid.uuid4().hex,
            "status": "away",
            "started_day": self.session.day,
            "members": [r.id for r in members],
            "supplies": dict(supply_cost),
            "travel_days": 0,
            "encounters_resolved": 0,
            "pending_encounter": None,
            "loot": {},
            "casualties": [],
        }
        self.session.expedition = dict(exp)
        names = "、".join(r.name for r in members)
        self._log("system", "探索队出发", f"{names} 携带物资外出探索。", decision="派遣探索队")
        return exp

    def _apply_expedition_travel(self, exp, pre_verdict=None):
        """探索队每日行军：消耗自带物资、累计天数，触发强制返程判定。

        pre_verdict 为当日推进开始时快照的终局裁决（如已全线枯竭），
        透传给返程结算，避免行军/入库把当日败局“救回”。
        """
        alive_members = [r for r in self._away_residents() if r.alive]
        if not alive_members:
            # 全员失联：强制返程（无人生还）
            self._settle_expedition(exp, reason="探索队全员失联", pre_verdict=pre_verdict)
            return
        exp["travel_days"] = exp.get("travel_days", 0) + 1
        # 消耗自带口粮（按存活人数；阵亡者不再消耗）
        n = len(alive_members)
        supplies = exp.get("supplies", {})
        for k in (FOOD, WATER):
            cost = EXPEDITION_SUPPLY_PER_DAY[k] * n
            supplies[k] = round(max(0.0, supplies.get(k, 0.0) - cost), 1)
        exp["supplies"] = supplies
        # 物资耗尽或达到最长探索天数：当日强制返程，补给不得出现负值快照
        if supplies.get(FOOD, 0) <= 0 or supplies.get(WATER, 0) <= 0:
            self._settle_expedition(
                exp, reason="补给耗尽，探索队被迫返程", pre_verdict=pre_verdict
            )
            return
        if exp["travel_days"] >= EXPEDITION_MAX_DAYS:
            self._settle_expedition(
                exp, reason="探索期满，探索队返程", pre_verdict=pre_verdict
            )
            return
        # 整体回写，确保 JSON 列变更被追踪并落库
        self.session.expedition = dict(exp)

    def _maybe_trigger_expedition_encounter(self, exp):
        """每日行军后概率触发遭遇；已有待处理遭遇时不重复触发。"""
        if exp.get("pending_encounter"):
            return exp["pending_encounter"]
        if self.rand.random() > EXPEDITION_ENCOUNTER_CHANCE:
            return None
        event = self.rand.choice(EXPEDITION_ENCOUNTERS)
        encounter = self._build_expedition_encounter(event, exp)
        exp["pending_encounter"] = encounter
        # 整体回写，确保 JSON 列变更被追踪并落库
        self.session.expedition = dict(exp)
        return encounter

    def _build_expedition_encounter(self, event, exp):
        alive_members = [r for r in self._away_residents() if r.alive]
        # 仅当存在单体健康效果的决策时才随机目标队员
        needs_target = any(
            self._choice_targeted(c) for c in event["choices"]
        )
        target = self.rand.choice(alive_members) if needs_target and alive_members else None
        return {
            "token": uuid.uuid4().hex,
            "event": event["key"],
            "day": self.session.day,
            "title": event["title"],
            "desc": event["desc"],
            "needs_target": needs_target,
            "target_id": target.id if target else None,
            "target_name": target.name if target else None,
            "choices": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "hint": c.get("hint", ""),
                    "targeted": self._choice_targeted(c),
                }
                for c in event["choices"]
            ],
        }

    # 探索队动作类型（用于档案级幂等凭据 last_expedition）
    _EXP_ACT_ENCOUNTER = "encounter"
    _EXP_ACT_RETURN = "return"

    @staticmethod
    def _matches_expedition(rec, action, token, exp_token=None, choice_key=None):
        """判断落败/重试请求是否就是上一次已完成的那次探索队动作（幂等回放）。

        - 遭遇：action=encounter，token=遭遇 token，再核对选项
        - 返程：action=return，exp_token=队伍 token（返程凭据挂在队伍上）
        """
        if not rec or rec.get("action") != action:
            return False
        if token is not None and rec.get("token") and token != rec["token"]:
            return False
        if action == "return" and exp_token is not None and rec.get("exp_token") and exp_token != rec["exp_token"]:
            return False
        if choice_key is not None and rec.get("choice") is not None and choice_key != rec["choice"]:
            return False
        return True

    def _last_expedition_replay(self, action, token, exp_token=None, choice_key=None):
        """命中档案级幂等记录则返回 (detail, True)，否则返回 (None, False)。"""
        rec = self.session.last_expedition
        if self._matches_expedition(rec, action, token, exp_token=exp_token, choice_key=choice_key):
            return rec.get("detail", ""), True
        return None, False

    def _encounter_settlement_replay(self, token, choice_key=None):
        """遭遇请求命中“由该遭遇直接触发的返程结算”凭据时安全回放。

        遭遇结算后若队伍当场收敛（补给耗尽/全员阵亡/终局），档案级凭据会被
        返程记录覆盖，但该记录仍挂着本次遭遇的一次性 token。并发落败或连点
        凭 token 命中这里：回放返程结算明细，绝不二次入库战利品。
        """
        rec = self.session.last_expedition
        if not token or not rec or rec.get("action") != self._EXP_ACT_RETURN:
            return None, False
        if rec.get("token") != token:
            return None, False
        if choice_key is not None and rec.get("choice") is not None and choice_key != rec["choice"]:
            return None, False
        return rec.get("detail", ""), True

    def _remember_expedition(self, action, token, detail, exp_token=None, choice_key=None):
        """把已完成的探索队动作写入档案级幂等凭据。

        队伍随后可能被清除（返程）或继续在外（遭遇），凭据独立保存在档案上，
        使并发落败/连点请求在队伍消失后仍能被识别并安全回放。
        """
        self.session.last_expedition = {
            "action": action,
            "token": token,
            "exp_token": exp_token,
            "choice": choice_key,
            "day": self.session.day,
            "detail": detail,
        }

    def resolve_expedition_encounter(self, choice_key, token=None):
        """处理探索队途中遭遇：抉择影响队员健康/士气、物资与战利品。

        结算必须命中央档案里唯一的待处理遭遇：事件、选项、单体目标都与存档绑定，
        token 用于识别过期/重复请求；结算后待处理遭遇被清除。
        返回 (detail, replayed)：replayed=True 表示重复/并发落败请求，未再次施加效果。
        """
        self._ensure_running()
        # 幂等回放优先：遭遇结算后、下一个探索队动作前的连点/并发落败只回放。
        # 若档案级凭据已被后续动作（如返程）覆盖，说明遭遇所属状态已前进，
        # 落到下方的“无在外队伍/无待处理遭遇”分支并按 409 拒绝
        replay = self._last_expedition_replay(
            self._EXP_ACT_ENCOUNTER, token, choice_key=choice_key
        )
        if replay[0] is not None:
            return replay
        # 遭遇已直接触发队伍收敛（补给耗尽/全员阵亡/终局）：凭据已被返程记录
        # 覆盖，但记录上仍挂着本次遭遇 token，命中则回放返程明细而非 409
        converged = self._encounter_settlement_replay(token, choice_key=choice_key)
        if converged[0] is not None:
            return converged
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            # 携带遭遇凭据却找不到在外队伍：队伍已被其他请求召回，状态已前进
            if token:
                raise BunkerEngineConflict("探索队状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有在外的探索队")
        pending = exp.get("pending_encounter")
        if not pending:
            # 队伍仍在但该遭遇已被其他请求结算：重复请求安全拒绝并引导刷新
            if token:
                raise BunkerEngineConflict("该遭遇已被处理，请刷新后重试")
            raise BunkerEngineError("当前没有待处理的探索遭遇")
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该遭遇决策已过期，请刷新后重试")
        event_key = pending.get("event")
        event = next((e for e in EXPEDITION_ENCOUNTERS if e["key"] == event_key), None)
        if not event:
            raise BunkerEngineError("探索遭遇已失效，请刷新档案后重试")
        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")
        effects = choice.get("effects", {})
        # 单体目标校验：必须是队内存活队员，且与待处理遭遇绑定
        targeted = self._choice_targeted(choice)
        target = None
        if targeted:
            bound_id = pending.get("target_id")
            if bound_id is None:
                raise BunkerEngineError("该决策需要指定一名队员作为目标")
            target = next((r for r in self._away_residents() if r.id == bound_id), None)
            if not target or not target.alive:
                raise BunkerEngineError("目标队员不在队中或已故，无法作为效果目标")
        # 在应用任何效果前完成校验，保证失败时档案状态不发生部分变更
        detail_parts = []
        exp.setdefault("casualties", [])
        alive_members = [r for r in self._away_residents() if r.alive]
        # 战利品（单独累计，返程时统一入库）
        loot = exp.get("loot", {})
        for k, v in effects.get("loot", {}).items():
            loot[k] = round(loot.get(k, 0.0) + v, 1)
            detail_parts.append(f"战利品 {RESOURCE_ZH.get(k, k)} +{v:g}")
        exp["loot"] = loot
        # 物资损失（从探索队自带物资中扣除，不为负）
        supplies = exp.get("supplies", {})
        for k, v in effects.get("supply_loss", {}).items():
            supplies[k] = round(max(0.0, supplies.get(k, 0.0) - v), 1)
            detail_parts.append(f"物资损失 {RESOURCE_ZH.get(k, k)} -{v:g}")
        exp["supplies"] = supplies
        # 健康/士气：单体作用于目标队员，全体作用于队内存活者
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                pool = [target]
                scope = f"仅{target.name}"
            else:
                pool = alive_members
                scope = "全体队员"
            for r in pool:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")
        # 全部效果施加完毕后统一收敛伤亡：健康归零即阵亡。单体/全体效果
        # （以及遭遇前已濒死、被本次效果带过零点的队员）在同一次扫描中处理，
        # 保证人口只扣一次、casualties 不重不漏
        casualties = exp.get("casualties", [])
        for r in alive_members:
            if r.health <= 0 and r.alive:
                r.alive = 0
                r.health = 0
                if r.id not in casualties:
                    casualties.append(r.id)
                    self.session.survivors = max(0, self.session.survivors - 1)
        exp["casualties"] = casualties
        # 偶遇幸存者加入队伍
        if effects.get("add_resident"):
            name = self._random_survivor_name()
            self.db.flush()
            r = Resident(
                session_id=self.session.id, name=name, job="general",
                health=60.0, morale=50.0, alive=1, joined_day=self.session.day,
            )
            self.db.add(r)
            self.db.flush()  # 取得新居民 id
            exp["members"].append(r.id)
            self.session.survivors += 1
            detail_parts.append(f"新幸存者 {name} 加入队伍")
        # 日志与实际结算同一作用域
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", f"探索遭遇·{event['title']}", f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])
        # 清除待处理遭遇、写入档案级幂等凭据，队伍继续在外行军
        enc_token = pending.get("token")
        exp["pending_encounter"] = None
        exp["encounters_resolved"] = exp.get("encounters_resolved", 0) + 1
        self.session.expedition = dict(exp)
        self._remember_expedition(
            self._EXP_ACT_ENCOUNTER, enc_token, detail,
            exp_token=exp.get("token"), choice_key=choice["key"],
        )
        # 遭遇结算后立即收敛，不把“零补给 / 全员阵亡 / 人口归零”的队伍留给下一步：
        #   1) 全员阵亡 —— 无人生还的队伍不能继续行军（僵尸队伍）
        #   2) 自带补给耗尽 —— 无需再等一次“推进一天”，当场被迫返程
        #   3) 人口归零/抵达目标日等终局 —— 先安全返程再收敛到 ended
        # 返程结算内部会在战利品入库前快照终局裁决，收敛只会发生一次
        alive_after = [r for r in self._away_residents() if r.alive]
        supplies_after = exp.get("supplies", {})
        supplies_out = supplies_after.get(FOOD, 0) <= 0 or supplies_after.get(WATER, 0) <= 0
        settle_reason = None
        if not alive_after:
            settle_reason = "探索队全员失联"
        elif supplies_out:
            settle_reason = "补给耗尽，探索队被迫返程"
        elif self._end_conditions_met():
            settle_reason = "终局已至，探索队返程"
        if settle_reason is not None:
            return_detail, _ = self._settle_expedition(
                self.session.expedition, reason=settle_reason,
                enc_token=enc_token, enc_choice=choice["key"],
            )
            # 遭遇响应同时承载遭遇效果与当场返程结算；返程凭据记录同一份
            # 明细，保证该遭遇的连点/并发落败回放结果逐字一致
            detail = f"{detail}；队伍返程：{return_detail}"
            self.session.last_expedition["detail"] = detail
            return detail, False
        return detail, False

    def reconcile_stale_expedition(self, action, token=None, choice_key=None, exp_token=None):
        """并发落败（版本冲突）后核对：若对方提交的是同一次探索队动作则安全回放。

        对不上任何已知结算时抛 409，由调用方提示刷新，杜绝并发重复结算。
        """
        rec = self.session.last_expedition
        if action == self._EXP_ACT_ENCOUNTER:
            ok = self._matches_expedition(rec, action, token, choice_key=choice_key)
            if not ok:
                # 遭遇已直接触发队伍收敛（返程凭据覆盖了遭遇凭据）：
                # 凭遭遇 token 回放那次返程结算，落败方同样拿到 200 而非 409；
                # 对不上任何已知结算（token/选项不符）则落入统一的 409
                settled = self._encounter_settlement_replay(token, choice_key=choice_key)
                if settled[0] is not None:
                    return settled
        else:
            ok = self._matches_expedition(rec, action, token, exp_token=exp_token)
        if ok:
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("探索队状态已被其他请求更新，请刷新后重试")

    def return_expedition(self, token=None):
        """玩家主动召回探索队：结算战利品入库、伤亡扣减、剩余物资归还。

        返程必须命中央档案里唯一的在外探索队；队伍 token 用于识别过期/重复请求。
        结算后探索队状态被清除并在档案上留下幂等凭据，重复提交只回放。
        返回 (detail, replayed)。
        """
        self._ensure_running()
        # 幂等回放优先：返程后队伍已清除，凭据仍在档案上可识别连点/并发落败请求
        replay = self._last_expedition_replay(
            self._EXP_ACT_RETURN, None, exp_token=token
        )
        if replay[0] is not None:
            return replay
        # 返程属于地堡经营动作：危机/遭遇待处理阶段一律锁定（幂等回放除外）
        self._require_daily_phase("召回探索队")
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            # 队伍已不在外：通常是上一次返程已完成。携带不匹配 token 的请求
            # 属于过期/串档，明确报 409；完全无凭据时才按“无队伍”处理
            rec = self.session.last_expedition
            if token and rec and rec.get("action") == self._EXP_ACT_RETURN:
                raise BunkerEngineConflict("探索队状态已过期，请刷新后重试")
            raise BunkerEngineError("当前没有在外的探索队")
        if exp.get("pending_encounter"):
            raise BunkerEngineError("探索队还有未处理的遭遇，无法返程")
        if token is not None and exp.get("token") and token != exp["token"]:
            raise BunkerEngineConflict("探索队状态已过期，请刷新后重试")
        return self._settle_expedition(exp, reason="探索队安全返程")

    def _settle_expedition(self, exp, reason, enc_token=None, enc_choice=None, pre_verdict=None):
        """结算探索队返程：战利品入库、剩余自带物资归还、伤亡扣减。

        幂等：以队伍 token 为凭据写入档案级 last_expedition，重复调用只回放，
        不二次发放战利品。enc_token/enc_choice 非空表示本次返程由某次遭遇
        抉择直接触发（补给耗尽/全员失联/终局收敛），返程凭据同时挂住该
        遭遇的一次性 token，使该遭遇的连点/并发落败请求也能安全回放。
        pre_verdict 为状态变更前快照的终局裁决（如行军日推进开始时已枯竭），
        优先于本方法内部快照。返回 (detail, replayed)。
        """
        exp_token = exp.get("token")
        rec = self.session.last_expedition
        if rec and rec.get("action") == self._EXP_ACT_RETURN and rec.get("exp_token") == exp_token:
            return rec.get("detail", ""), True
        members = self._away_residents()
        dead_members = [r for r in members if not r.alive]
        # 终局裁决在战利品/余粮入库前快照：全线枯竭的败局不得被随后入库的
        # 战利品抬过阈值而“复活”，终局状态与本结算只收敛一次（主动返程、
        # 补给耗尽强制返程、遭遇收敛各路径口径一致）；调用方（行军日推进）
        # 在产出前快照的更早裁决同样优先
        verdict = pre_verdict if pre_verdict is not None else self._end_verdict()
        # 战利品入库
        loot = exp.get("loot", {})
        loot_parts = [f"{RESOURCE_ZH.get(k, k)} +{v:g}" for k, v in loot.items() if v > 0]
        for k, v in loot.items():
            if v > 0:
                self._add_resource(k, v)
        # 剩余自带物资归还地堡（行军消耗已先行扣减，只归还正值余额）
        supplies = exp.get("supplies", {})
        supply_parts = [f"剩余{RESOURCE_ZH.get(k, k)} +{round(v, 1):g}" for k, v in supplies.items() if v > 0]
        for k, v in supplies.items():
            if v > 0:
                self._add_resource(k, v)
        # 伤亡（阵亡队员已在遭遇结算时扣减过 survivors，此处不再重复扣减）
        casualty_names = [r.name for r in dead_members]
        # 组装日志
        detail_parts = []
        if loot_parts:
            detail_parts.append("战利品：" + "、".join(loot_parts))
        if supply_parts:
            detail_parts.append("归还物资：" + "、".join(supply_parts))
        if casualty_names:
            detail_parts.append(f"殉职：{'、'.join(casualty_names)}")
        else:
            detail_parts.append("全员平安归来")
        detail = "；".join(detail_parts)
        self._log("system", f"探索队返程（{reason}）", detail, decision="返程结算")
        # 先写档案级幂等凭据，再清除探索队状态：凭据在队伍消失后依然可查
        self._remember_expedition(
            self._EXP_ACT_RETURN, enc_token, detail,
            exp_token=exp_token, choice_key=enc_choice,
        )
        self.session.expedition = None
        # 用入库前快照收敛终局；无预设败局时再按结算后状态正常判定
        self._check_end(forced_verdict=verdict)
        return detail, False


    # ==================================================================
    # 贸易救援模块：申请 → 审核 → 运输 → 交付/失败回退 的完整状态链
    # ==================================================================
    def _orders(self):
        """订单列表（读取便捷；写回统一走 _save_orders 保证 JSON 落库）。"""
        return self.session.trade_orders or []

    def _save_orders(self, orders):
        # 整体回写并显式标记 JSON 列为脏：列表内的订单是普通 dict，
        # 原地改 status/timeline/result 不会被 MutableList 自动追踪，必须 flag 一次，
        # 保证“把列表中的订单原地改判”这种写法也能落库
        self.session.trade_orders = list(orders)
        flag_modified(self.session, "trade_orders")

    def _get_order(self, order_id):
        return next((o for o in self._orders() if o.get("id") == order_id), None)

    def _active_orders(self):
        """未到终态的订单（申请/审核中 + 运输在途）。"""
        return [o for o in self._orders() if o.get("status") in TRADE_ACTIVE_STATUSES]

    def _escort_resident_ids(self):
        """当前正在商队押运（仅在途订单）的居民编号。"""
        ids = set()
        for o in self._orders():
            if o.get("status") == TRADE_STATUS_TRANSPORTING:
                ids.update(o.get("escorts", []))
        return ids

    # ---- 可申请订单（由模板池 + 当前信誉筛选） ----
    def available_offers(self):
        """当前信誉下可申请的贸易/救援模板列表（外部聚落提供给地堡）。"""
        rep = self.session.reputation or 0
        return [
            self._public_offer(o)
            for o in TRADE_OFFERS
            if rep >= o.get("min_reputation", 0)
        ]

    @staticmethod
    def _public_offer(o):
        return {
            "key": o["key"],
            "title": o["title"],
            "kind": o["kind"],          # trade 贸易 / rescue 救援
            "desc": o.get("desc", ""),
            "payment": dict(o.get("payment", {})),   # 地堡支付（审核通过时托管）
            "reward": dict(o.get("reward", {})),     # 送达后入库的回报
            "travel_days": o.get("travel_days", 2),
            "risk": o.get("risk", 0.1),
            "morale_bonus": o.get("morale_bonus", 0),
            "rep_bonus": o.get("rep_bonus", 2),
            "add_survivor": 1 if o.get("add_survivor") else 0,
            "bonus_health": o.get("bonus_health", 0),
            "max_escorts": TRADE_MAX_ESCORTS,
            "min_reputation": o.get("min_reputation", 0),
        }

    # ---- 申请 ----
    def apply_trade(self, offer_key, escort_ids=None):
        """地堡管理者主动向外部聚落申请贸易/救援订单。

        申请写入即进入 applied(审核) 阶段（review_by=external）：不立即扣款，
        当日每日推进时由外部聚落审核——通过则托管押金并进入运输，
        拒绝（信誉不足等）则订单判为 rejected。可选押运员（最多 2 人）。
        """
        self._require_daily_phase("申请贸易救援")
        offer = next((o for o in TRADE_OFFERS if o["key"] == offer_key), None)
        if offer is None:
            raise BunkerEngineError("未知贸易订单")
        rep = self.session.reputation or 0
        if rep < offer.get("min_reputation", 0):
            raise BunkerEngineError("信誉不足，该聚落暂不愿与你方交易")
        if len(self._active_orders()) >= TRADE_MAX_ACTIVE_ORDERS:
            raise BunkerEngineError(f"同时进行的订单已达上限（{TRADE_MAX_ACTIVE_ORDERS} 单）")
        escorts = self._validate_escorts(escort_ids or [])
        order = self._build_order(offer, origin="outbound", escorts=escorts)
        orders = self._orders()
        orders.append(order)
        self._save_orders(orders)
        names = "、".join(self._resident_name(i) for i in escorts) if escorts else "无押运"
        self._log(
            "trade", "贸易申请已发出",
            f"{offer['title']}：等待「{order['party']}」审核（押运：{names}）。",
            decision="申请贸易救援",
        )
        return order

    def _validate_escorts(self, escort_ids):
        """校验押运员：本档案存活、在堡、未参加探索队/其他商队、不重复、上限 2 人。"""
        if not escort_ids:
            return []
        if len(escort_ids) > TRADE_MAX_ESCORTS:
            raise BunkerEngineError(f"每张订单最多 {TRADE_MAX_ESCORTS} 名押运员")
        if len(set(escort_ids)) != len(escort_ids):
            raise BunkerEngineError("同一名居民不能重复担任押运员")
        busy = self._away_resident_ids() | self._escort_resident_ids()
        escorts = []
        for rid in escort_ids:
            r = next((x for x in self.session.residents if x.id == rid), None)
            if not r or not r.alive:
                raise BunkerEngineError("押运员不存在或已故")
            if r.id in busy:
                raise BunkerEngineError(f"{r.name} 正在执行探索/押运任务，无法离堡")
            escorts.append(r.id)
            busy.add(r.id)
        return escorts

    def _resident_name(self, rid):
        r = next((x for x in self.session.residents if x.id == rid), None)
        return r.name if r else f"#{rid}"

    def _build_order(self, offer, origin, escorts, party=None):
        """依据模板构造一张订单快照（applied 阶段）。

        origin=inbound  外部队伍主动向地堡申请，review_by=bunker（管理者审核）
        origin=outbound 地堡主动申请，review_by=external（外部聚落审核）
        """
        now = self.session.day
        review_by = "bunker" if origin == "inbound" else "external"
        return {
            "id": uuid.uuid4().hex[:12],
            "token": uuid.uuid4().hex,       # 审核/拒绝动作的一次性凭据
            "offer_key": offer["key"],
            "title": offer["title"],
            "kind": offer["kind"],
            "origin": origin,                # inbound 外部申请 / outbound 地堡申请
            "review_by": review_by,
            "party": party or offer.get("party", "外部聚落"),
            "status": TRADE_STATUS_APPLIED,
            "created_day": now,
            "expire_day": now + TRADE_INBOUND_EXPIRE_DAYS if origin == "inbound" else None,
            "payment": dict(offer.get("payment", {})),
            "reward": dict(offer.get("reward", {})),
            "travel_days": offer.get("travel_days", 2),
            "elapsed_days": 0,
            "risk": offer.get("risk", 0.1),
            "morale_bonus": offer.get("morale_bonus", 0),
            "rep_bonus": offer.get("rep_bonus", 2),
            "add_survivor": 1 if offer.get("add_survivor") else 0,
            "bonus_health": offer.get("bonus_health", 0),
            "escorts": list(escorts),
            "timeline": [{"day": now, "stage": TRADE_STATUS_APPLIED,
                          "note": "外部队伍求援" if origin == "inbound" else "地堡发出申请"}],
            "result": None,
        }

    # ---- 审核（地堡管理者）：批准/拒绝外部队伍的申请 ----
    _TRADE_ACT_REVIEW = "review"

    def _matches_trade(self, rec, order_id, token, decision=None):
        if not rec or rec.get("order_id") != order_id:
            return False
        if token is not None and rec.get("token") and token != rec["token"]:
            return False
        if decision is not None and rec.get("decision") != decision:
            return False
        return True

    def review_trade(self, order_id, approve, token=None):
        """地堡管理者审核外部队伍的贸易/救援申请（review_by=bunker）。

        批准：托管押金（扣资源）→ 进入运输；拒绝：订单判 rejected。
        返回 (order, replayed)：replayed=True 表示重复/并发落败请求安全回放。
        """
        self._ensure_running()
        rec = self.session.last_trade
        if rec and self._matches_trade(rec, order_id, token):
            return self._get_order(order_id) or rec.get("order_snapshot"), True
        self._require_daily_phase("审核贸易申请")
        order = self._get_order(order_id)
        if order is None:
            raise BunkerEngineError("订单不存在")
        if order.get("status") != TRADE_STATUS_APPLIED or order.get("review_by") != "bunker":
            raise BunkerEngineError("该订单已不在待审核状态")
        if token is not None and order.get("token") and token != order["token"]:
            raise BunkerEngineConflict("该贸易申请已过期，请按当前订单重新操作")
        decision = "approve" if approve else "reject"
        if not approve:
            self._mark_rejected(order, reason="地堡管理者拒绝了本次交易", rep_delta=-1)
            detail = f"已拒绝「{order['party']}」的{self._kind_zh(order['kind'])}申请"
            self._log("trade", "拒绝贸易申请", f"{order['title']}：{detail}。", decision="拒绝")
        else:
            payment = order.get("payment", {})
            if not self._can_afford(payment):
                raise BunkerEngineError("资源不足，无法托管这笔交易的押金")
            for k, v in payment.items():
                self._add_resource(k, -v)
            order["status"] = TRADE_STATUS_TRANSPORTING
            order["timeline"].append({"day": self.session.day, "stage": TRADE_STATUS_TRANSPORTING,
                                      "note": f"管理者批准，押金已托管，{order['party']}商队启程"})
            self._log("trade", "贸易申请已批准",
                      f"{order['title']}：托管{self._fmt_costs(payment)}，商队启程（预计 {order['travel_days']} 天）。",
                      decision="批准")
            detail = f"已批准「{order['party']}」的{self._kind_zh(order['kind'])}申请"
        self._save_orders(self._orders())
        self._remember_trade(self._TRADE_ACT_REVIEW, order, decision, token or order.get("token"), detail)
        return order, False

    def reconcile_stale_trade(self, order_id, token, decision):
        """并发落败（版本冲突）后核对：同一张订单同一动作则安全回放，否则 409。"""
        rec = self.session.last_trade
        if self._matches_trade(rec, order_id, token, decision=decision):
            return rec.get("order_snapshot"), True
        raise BunkerEngineConflict("贸易订单状态已被其他请求更新，请刷新后重试")

    def _remember_trade(self, action, order, decision, token, detail):
        self.session.last_trade = {
            "action": action,
            "order_id": order.get("id"),
            "offer_key": order.get("offer_key"),
            "decision": decision,
            "token": token,
            "day": self.session.day,
            "detail": detail,
            "order_snapshot": dict(order),
        }

    def _mark_rejected(self, order, reason, rep_delta=0):
        order["status"] = TRADE_STATUS_REJECTED
        order["result"] = {"reason": reason}
        order["timeline"].append({"day": self.session.day, "stage": TRADE_STATUS_REJECTED, "note": reason})
        if rep_delta:
            self._adjust_reputation(rep_delta)

    # ---- 归档终态订单 ----
    def dismiss_trade(self, order_id):
        """归档一张已结束（交付/失败/拒绝）的订单，从活跃列表中移除（大事记仍有记录）。"""
        self._ensure_running()
        order = self._get_order(order_id)
        if order is None:
            raise BunkerEngineError("订单不存在")
        if order.get("status") not in TRADE_TERMINAL_STATUSES:
            raise BunkerEngineError("订单尚未结束，无法归档")
        orders = [o for o in self._orders() if o.get("id") != order_id]
        self._save_orders(orders)

    # ---- 每日推进：外部审核/超时、商队行军、风险判定、新申请 ----
    def _advance_trade_pipeline(self, pre_verdict=None):
        """推进全部贸易救援订单一天。

        顺序：①外部申请超时判拒绝 ②外部聚落审核地堡的申请 ③在途商队行军/抽风险
        ④外部聚落可能发来新申请。返回挂起的地堡危机（商队遇袭）或 None。
        pre_verdict 非 None（推进前已终局）时只做订单中止，不抽风险、不发新申请。
        """
        orders = self._orders()
        pending_crisis = None
        for order in orders:
            status = order.get("status")
            if status == TRADE_STATUS_APPLIED:
                if order.get("review_by") == "bunker":
                    # 外部队伍的申请逾期未处理：自动判拒绝（信誉不受损）
                    if order.get("expire_day") is not None and self.session.day >= order["expire_day"]:
                        self._mark_rejected(order, reason="审核超时，外部队伍已离开")
                        self._log("trade", "贸易申请超时",
                                  f"{order['title']}：{order['result']['reason']}。", decision="自动拒绝")
                elif pre_verdict is None:
                    # 地堡的申请：由外部聚落审核
                    self._review_by_external(order)
            elif status == TRADE_STATUS_TRANSPORTING:
                if pre_verdict is not None:
                    self._abort_order(order, reason="终局已至，商队中止行程")
                else:
                    crisis = self._travel_order(order)
                    if crisis is not None and pending_crisis is None:
                        pending_crisis = crisis
        # 订单可能在循环中被改判/中止，统一整体回写
        if orders:
            self._save_orders(orders)
        if pre_verdict is None and pending_crisis is None and self.session.survivors > 0:
            self._maybe_inbound_offer()
        return pending_crisis

    def _review_by_external(self, order):
        """外部聚落审核地堡的申请：信誉决定接单概率；通过则托管押金并发车。"""
        accept_chance = _clamp(
            TRADE_ACCEPT_BASE + TRADE_ACCEPT_REP_SLOPE * (self.session.reputation or 0), 0.05, 0.97
        )
        if self.rand.random() > accept_chance:
            self._mark_rejected(
                order, reason=f"「{order['party']}」对此次交易缺乏信任，拒绝了申请", rep_delta=0
            )
            self._log("trade", "外部聚落拒绝交易",
                      f"{order['title']}：{order['result']['reason']}。", decision="外部拒绝")
            return
        payment = order.get("payment", {})
        if not self._can_afford(payment):
            # 审核期间物资被挪作他用：无法托管，订单搁置回 applied，等待管理者补资源
            order["result"] = {"reason": "押金不足，等待物资补齐后再次审核"}
            self._log("trade", "贸易押金不足",
                      f"{order['title']}：托管{self._fmt_costs(payment)}的物资不足，订单暂缓。",
                      decision="审核暂缓")
            return
        for k, v in payment.items():
            self._add_resource(k, -v)
        order["status"] = TRADE_STATUS_TRANSPORTING
        order["timeline"].append(
            {"day": self.session.day, "stage": TRADE_STATUS_TRANSPORTING,
             "note": f"「{order['party']}」接受申请，押金已托管，商队启程"}
        )
        self._log("trade", "贸易申请获批准",
                  f"{order['title']}：托管{self._fmt_costs(payment)}，商队启程（预计 {order['travel_days']} 天）。",
                  decision="外部批准")

    def _travel_order(self, order):
        """在途商队行军一天：抽失败风险，抵达则交付结算。返回挂起的危机或 None。"""
        order["elapsed_days"] = order.get("elapsed_days", 0) + 1
        risk = _clamp(order.get("risk", 0.1) - TRADE_RISK_REP_SLOPE * (self.session.reputation or 0), 0.02, 0.6)
        if self.rand.random() < risk:
            # 失败类型：多数为盗匪截击（回退一半押金并挂地堡危机），少数为恶劣天气（全退，不挂危机）
            bandit = self.rand.random() < 0.7
            if bandit:
                return self._fail_order(order, "bandit")
            return self._fail_order(order, "storm")
        if order["elapsed_days"] >= order.get("travel_days", 1):
            self._deliver_order(order)
        else:
            order["timeline"].append(
                {"day": self.session.day, "stage": TRADE_STATUS_TRANSPORTING,
                 "note": f"商队在途（第 {order['elapsed_days']}/{order['travel_days']} 天），一路平安"}
            )
        return None

    # ---- 交付：结果回写资源 / 居民 / 信誉 / 士气 ----
    def _deliver_order(self, order):
        reward = order.get("reward", {})
        parts = [f"{RESOURCE_ZH.get(k, k)} +{v:g}" for k, v in reward.items() if v > 0]
        for k, v in reward.items():
            if v > 0:
                self._add_resource(k, v)
        # 救援订单：被救幸存者加入地堡（flush 取得 id，与探索队偶遇幸存者口径一致）
        added_name = None
        if order.get("add_survivor"):
            added_name = self._random_survivor_name()
            self.db.add(Resident(
                session_id=self.session.id, name=added_name, job="general",
                health=65.0, morale=60.0, alive=1, joined_day=self.session.day,
            ))
            self.db.flush()
            self.session.survivors += 1
            parts.append(f"幸存者 {added_name} 获救加入")
        # 全体存活居民士气（在堡 + 押运归队都受益）
        morale_bonus = order.get("morale_bonus", 0)
        health_bonus = order.get("bonus_health", 0)
        if morale_bonus or health_bonus:
            for r in self.session.residents:
                if not r.alive:
                    continue
                if morale_bonus:
                    r.morale = _clamp(r.morale + morale_bonus)
                if health_bonus:
                    r.health = _clamp(r.health + health_bonus)
            if morale_bonus:
                parts.append(f"全体士气 {morale_bonus:+.0f}")
            if health_bonus:
                parts.append(f"全体健康 {health_bonus:+.0f}")
        rep_bonus = order.get("rep_bonus", 2)
        self._adjust_reputation(rep_bonus)
        parts.append(f"信誉 {rep_bonus:+d}")
        detail = "，".join(parts) if parts else "交易完成"
        order["status"] = TRADE_STATUS_DELIVERED
        order["result"] = {
            "detail": detail,
            "reward": dict(reward),
            "rep_delta": rep_bonus,
            "morale_bonus": morale_bonus,
            "added_resident": added_name,
        }
        order["timeline"].append({"day": self.session.day, "stage": TRADE_STATUS_DELIVERED, "note": detail})
        kind_zh = self._kind_zh(order["kind"])
        self._log(
            "trade", f"{kind_zh}订单交付",
            f"{order['title']}（{order['party']}）：{detail}。",
            decision="交付成功",
        )

    # ---- 失败回退：押金退还/没收、士气、押运员受伤、危机回写 ----
    def _fail_order(self, order, reason_key):
        payment = order.get("payment", {})
        escorts = [next((r for r in self.session.residents if r.id == i), None)
                   for i in order.get("escorts", [])]
        escorts = [r for r in escorts if r and r.alive]
        if reason_key == "bandit":
            refund_rate, rep_delta, morale_delta, escort_hp = 0.5, -8, -8, -15
            reason = "盗匪截击商队"
        else:
            refund_rate, rep_delta, morale_delta, escort_hp = 1.0, -3, -5, -5
            reason = "辐射风暴吞没商路"
        # 押金按比例退还（盗匪截走一半；风暴全退但白跑一趟）
        refund = {}
        refund_parts = []
        for k, v in payment.items():
            back = round(v * refund_rate, 1)
            refund[k] = back
            if back > 0:
                self._add_resource(k, back)
                refund_parts.append(f"{RESOURCE_ZH.get(k, k)} +{back:g}")
        # 全体存活居民士气受挫
        if morale_delta:
            for r in self.session.residents:
                if r.alive:
                    r.morale = _clamp(r.morale + morale_delta)
        # 押运员受伤（健康归零即殉职，人口扣减）
        hurt_names, dead_names = [], []
        for r in escorts:
            r.health = _clamp(r.health + escort_hp)
            if r.health <= 0 and r.alive:
                r.alive = 0
                r.health = 0
                self.session.survivors = max(0, self.session.survivors - 1)
                dead_names.append(r.name)
            else:
                hurt_names.append(r.name)
        self._adjust_reputation(rep_delta)
        parts = [f"押金退还：{'、'.join(refund_parts)}" if refund_parts else "押金全部损失",
                 f"信誉 {rep_delta:+d}", f"全体士气 {morale_delta:+.0f}"]
        if dead_names:
            parts.append(f"押运殉职：{'、'.join(dead_names)}")
        if hurt_names:
            parts.append(f"押运受伤：{'、'.join(hurt_names)}")
        detail = "，".join(parts)
        order["status"] = TRADE_STATUS_FAILED
        order["result"] = {
            "fail_reason": reason_key,
            "reason": reason,
            "detail": detail,
            "refund": refund,
            "rep_delta": rep_delta,
            "morale_delta": morale_delta,
            "casualties": dead_names,
        }
        order["timeline"].append({"day": self.session.day, "stage": TRADE_STATUS_FAILED, "note": f"{reason}：{detail}"})
        self._log("trade", f"{self._kind_zh(order['kind'])}失败：{reason}",
                  f"{order['title']}（{order['party']}）：{detail}。", decision="失败回退")
        # 回写危机流程：盗匪截击挂起一个地堡危机（玩家抉择），风暴仅结算不挂危机
        if reason_key == "bandit":
            return self._build_trade_raid_crisis(order)
        return None

    def _build_trade_raid_crisis(self, order):
        """商队遇袭回写为待处理地堡危机（复用危机状态机与幂等回放链路）。"""
        event = next(e for e in CRISIS_POOL if e["key"] == "trade_raid")
        crisis = self._build_crisis(event)
        crisis["source"] = "trade"
        crisis["order_id"] = order["id"]
        crisis["order_title"] = order["title"]
        crisis["desc"] = (
            f"「{order['party']}」前往交付 {order['title']} 的商队在归路上遭盗匪截击，"
            "匪徒尾随至地堡气闸外，正试图破门抢夺仓储。"
        )
        self.session.pending_crisis = crisis
        return crisis

    # ---- 终局/中止 ----
    def _abort_active_orders(self, reason):
        orders = self._orders()
        changed = False
        for order in orders:
            if order.get("status") in TRADE_ACTIVE_STATUSES:
                self._abort_order(order, reason=reason)
                changed = True
        if changed:
            self._save_orders(orders)

    def _abort_order(self, order, reason):
        """中止在途/待审订单：已托管押金全额退回，不动信誉/士气，押运员直接归队。"""
        refunded = []
        for k, v in order.get("payment", {}).items():
            # 仅 transporting 阶段押金已托管；applied 阶段尚未扣款
            if order.get("status") == TRADE_STATUS_TRANSPORTING and v > 0:
                self._add_resource(k, v)
                refunded.append(f"{RESOURCE_ZH.get(k, k)} +{v:g}")
        order["status"] = TRADE_STATUS_ABORTED
        order["result"] = {"reason": reason, "refund_detail": "、".join(refunded)}
        order["timeline"].append({"day": self.session.day, "stage": TRADE_STATUS_ABORTED, "note": reason})

    # ---- 外部队伍主动申请（inbound） ----
    def _maybe_inbound_offer(self):
        """每日概率收到外部聚落的贸易/救援申请，进入待管理者审核状态。"""
        if self.rand.random() > TRADE_INBOUND_CHANCE:
            return
        if self.session.pending_crisis:
            return
        if len(self._active_orders()) >= TRADE_MAX_ACTIVE_ORDERS:
            return
        template = self.rand.choice(TRADE_INBOUND_POOL)
        party = self.rand.choice(TRADE_PARTIES)
        offer = dict(template, party=party)
        order = self._build_order(offer, origin="inbound", escorts=[])
        orders = self._orders()
        orders.append(order)
        self._save_orders(orders)
        self._log(
            "trade", "外部队伍求援",
            f"「{party}」发来{self._kind_zh(order['kind'])}申请：{order['title']}，"
            f"请在 {TRADE_INBOUND_EXPIRE_DAYS} 天内审核。",
            decision="收到申请",
        )

    def _adjust_reputation(self, delta):
        self.session.reputation = int(_clamp((self.session.reputation or TRADE_INITIAL_REPUTATION) + delta, 0, 100))

    @staticmethod
    def _kind_zh(kind):
        return "救援" if kind == "rescue" else "贸易"

    @staticmethod
    def _fmt_costs(costs):
        parts = [f"{RESOURCE_ZH.get(k, k)} {v:g}" for k, v in (costs or {}).items() if v]
        return "、".join(parts) if parts else "无押金"


    # ---- 扩建 ----
    def build_facility(self, category):
        self._require_daily_phase("建造设施")
        cost = FACILITY_COST[1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法建造")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f = Facility(
            session_id=self.session.id,
            name=FACILITY_ZH.get(category, category),
            category=category,
            level=1,
            status="active",
            built_day=self.session.day,
        )
        self.db.add(f)
        self.db.flush()  # 让新设施立即反映到 session.facilities 集合
        self._log("system", "设施扩建", f"建造了{FACILITY_ZH.get(category, category)}。", decision="扩建")
        return f

    def upgrade_facility(self, facility_id):
        self._require_daily_phase("升级设施")
        f = next((x for x in self.session.facilities if x.id == facility_id), None)
        if not f:
            raise BunkerEngineError("设施不存在")
        if f.level >= max(FACILITY_COST.keys()):
            raise BunkerEngineError("已达最高等级")
        cost = FACILITY_COST[f.level + 1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法升级")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f.level += 1
        self._log("system", "设施升级", f"{FACILITY_ZH.get(f.category, f.category)} 提升到 Lv.{f.level}。", decision="升级")
        return f

    def _can_afford(self, cost):
        res = self.get_resources()
        return all(res.get(k, 0) >= v for k, v in cost.items())

    # ---- 任务分配（重分配岗位）----
    def set_job(self, resident_id, job):
        self._require_daily_phase("调整岗位")
        if job not in JOB_EFFICIENCY:
            raise BunkerEngineError("未知岗位")
        r = next((x for x in self.session.residents if x.id == resident_id), None)
        if not r or not r.alive:
            raise BunkerEngineError("居民不存在或已故")
        if r.id in self._away_resident_ids():
            raise BunkerEngineError("探索队中的居民无法调整岗位")
        r.job = job

    # ---- 结局判定 ----
    def _check_end(self, forced_verdict=None):
        """判定并落终局状态。

        forced_verdict 为状态变更（如返程战利品入库）前快照的裁决：一旦在
        变更前已满足终局（尤其是全线枯竭），即便变更后资源回升也照样收敛，
        保证终局判定单调、不被中途入库的物资“救回”。返回是否处于终局。
        """
        if self.session.status != "running":
            return True
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        if verdict is None:
            return False
        win, reason = verdict
        self._finish(win=win, reason=reason)
        return True

    def _finish(self, win, reason):
        # 幂等：终局只结算一次。重复调用（多路径收敛）直接返回，
        # 不重算分数、不重复写结局日志
        if self.session.status != "running":
            return
        # 终局收敛前中止一切在途贸易救援订单：押金全额退回、押运员归队，
        # 不把悬而未决的订单/离堡押运员带进 ended
        self._abort_active_orders(reason="终局已至，商队中止行程")
        self.session.status = "win" if win else "over"
        # 进入终局后不存在悬而未决的抉择/在外队伍，状态机统一收敛到 ended
        self.session.pending_crisis = None
        self.session.expedition = None
        alive = [r for r in self.session.residents if r.alive]
        # 计分：幸存者 * 天数 * 士气系数
        morale = self.avg_morale()
        score = int(self.session.survivors * self.session.day * (0.5 + morale / 200.0))
        self.session.score = score
        self.session.outcome = {"win": win, "reason": reason, "survivors": len(alive), "day": self.session.day}
        self._log("system", "游戏结束", reason, decision="结局")


RESOURCE_ZH = {"food": "食物", "water": "水源", "power": "电力", "oxygen": "氧气"}
FACILITY_ZH = {"farm": "穹顶菜园", "water": "净水器", "power": "发电机", "oxygen": "水培制氧", "med": "医疗舱", "storage": "仓储区"}


# ============ 危机事件池（决策树） ============
CRISIS_POOL = [
    {
        "key": "radstorm",
        "title": "辐射风暴来袭",
        "desc": "一场强辐射风暴正在逼近地堡。派工程师抢修屏蔽层，或让所有人避难并停电。",
        "choices": [
            {
                "key": "shield_repair",
                "label": "抢修屏蔽层",
                "hint": "消耗少量电力，成功则平安，失败有人员受伤",
                "effects": {"resources": {"power": -8}},
            },
            {
                "key": "shutdown",
                "label": "全员断电避难",
                "hint": "所有设施停摆一天，电力下降，无人员风险",
                "effects": {"resources": {"power": -15, "food": -5, "water": -4}},
            },
        ],
    },
    {
        "key": "mutiny",
        "title": "地堡内讧",
        "desc": "因食物分配不公，一部分人情绪失控，要求重新分配口粮。",
        "choices": [
            {
                "key": "double_ration",
                "label": "加倍发放食物",
                "hint": "士气+20，但食物储备大减",
                "effects": {"resources": {"food": -20}, "morale": 20},
            },
            {
                "key": "suppress",
                "label": "严令镇压",
                "hint": "食物不变，但士气大降",
                "effects": {"morale": -15},
            },
        ],
    },
    {
        "key": "leak",
        "title": "氧气泄漏",
        "desc": "水培舱密封圈老化，氧气正在泄漏。",
        "choices": [
            {
                "key": "emergency_repair",
                "label": "紧急封堵",
                "hint": "消耗食物与电力，防止气体外泄",
                "effects": {"resources": {"food": -6, "power": -6}},
            },
            {
                "key": "vent",
                "label": "先泄压再修",
                "hint": "氧气大降但更省资源",
                "effects": {"resources": {"oxygen": -20, "power": -3}},
            },
        ],
    },
    {
        "key": "sick",
        "title": "疫病袭来",
        "desc": "一名幸存者出现不明高热，可能是污染引发的疾病。",
        "choices": [
            {
                "key": "quarantine",
                "label": "隔离治疗",
                "hint": "该居民卸下工作，健康缓慢回复",
                "effects": {"resources": {"food": -4}, "health": {"value": -5, "target": "single"}},
            },
            {
                "key": "public_health",
                "label": "全员消毒",
                "hint": "消耗电力与水源消毒，保护大家",
                "effects": {"resources": {"power": -6, "water": -8}},
            },
        ],
    },
    {
        "key": "raid",
        "title": "盗匪袭扰",
        "desc": "地堡外传来敲击声，一伙流民试图破门而入抢夺物资。",
        "choices": [
            {
                "key": "defend",
                "label": "武装抵抗",
                "hint": "能耗物资，可能有人受伤，但守住粮食",
                "effects": {"resources": {"food": -2, "power": -4}, "health": {"value": -8, "target": "single"}},
            },
            {
                "key": "bribe",
                "label": "分粮和解",
                "hint": "交出部分食物换取平安",
                "effects": {"resources": {"food": -18}},
            },
        ],
    },
    {
        "key": "scavenge",
        "title": "发现物资舱",
        "desc": "侦察队在地堡深处发现一间废弃补给舱，但已部分损坏。",
        "choices": [
            {
                "key": "crack_open",
                "label": "强制开启",
                "hint": "可能获得大量补给，也可能毁坏",
                "effects": {"resources": {"food": 12, "water": 8}},
            },
            {
                "key": "careful",
                "label": "小心拆解",
                "hint": "稳定获得少量补给",
                "effects": {"resources": {"food": 6, "water": 5, "power": 3}},
            },
        ],
    },
    {
        "key": "blizzard",
        "title": "暴雪封门",
        "desc": "极寒暴雪掩盖了地堡入口，通风与采能都受影响。",
        "choices": [
            {
                "key": "burn_fuel",
                "label": "燃烧燃料保温",
                "hint": "消耗食物(燃料)维持温度",
                "effects": {"resources": {"food": -10}},
            },
            {
                "key": "huddle",
                "label": "集中避寒",
                "hint": "士气下降，但省下燃料",
                "effects": {"morale": -10},
            },
        ],
    },
    {
        "key": "trade_raid",
        "title": "商路盗匪截击",
        "desc": "外部商队归路上遭盗匪截击，匪徒尾随至地堡气闸外，试图破门抢夺仓储。",
        "choices": [
            {
                "key": "ambush",
                "label": "设伏反打",
                "hint": "消耗电力设伏，一名队员可能受伤，击退盗匪并缴获物资",
                "effects": {"resources": {"power": -6, "food": 10, "water": 6}, "health": {"value": -10, "target": "single"}},
            },
            {
                "key": "buy_off",
                "label": "破财消灾",
                "hint": "交出部分食物与电力换盗匪退去，无人受伤",
                "effects": {"resources": {"food": -12, "power": -8}},
            },
        ],
    },
]


# ============ 探索队遭遇池（外出探索途中的遭遇决策树） ============
# 与地堡危机相互独立：探索队在外时，每日行军触发的是探索遭遇而非地堡危机。
# 效果键：
#   loot       —— 战利品，单独累计，返程时统一入库
#   supply_loss —— 从探索队自带物资中扣除
#   health/morale —— 队员健康/士气（single 仅作用于目标队员，all 作用于全体队员）
#   add_resident —— 有幸存者加入队伍
EXPEDITION_ENCOUNTERS = [
    {
        "key": "cache",
        "title": "废弃补给点",
        "desc": "探索队在一处废墟中发现半埋的废弃补给箱，外观尚可辨认。",
        "choices": [
            {
                "key": "search_carefully",
                "label": "仔细搜索",
                "hint": "耗时但可能获得更多物资",
                "effects": {"loot": {FOOD: 8, WATER: 6}},
            },
            {
                "key": "grab_quickly",
                "label": "快速搜刮",
                "hint": "安全但收获有限",
                "effects": {"loot": {FOOD: 4, WATER: 3}},
            },
        ],
    },
    {
        "key": "beast",
        "title": "异兽袭击",
        "desc": "一头变异巨兽从废墟中窜出，挡住了去路。",
        "choices": [
            {
                "key": "fight",
                "label": "武装驱赶",
                "hint": "可能有人受伤，但能保住物资并缴获战利品",
                "effects": {"health": {"value": -12, "target": "single"}, "loot": {FOOD: 5}},
            },
            {
                "key": "flee",
                "label": "绕道撤退",
                "hint": "损失部分物资，但无人受伤",
                "effects": {"supply_loss": {FOOD: 6, WATER: 4}, "morale": -5},
            },
        ],
    },
    {
        "key": "weather",
        "title": "恶劣天气",
        "desc": "辐射尘暴骤起，能见度极低，探索队被迫寻找掩体。",
        "choices": [
            {
                "key": "take_shelter",
                "label": "就地躲避",
                "hint": "消耗一日物资，士气下降",
                "effects": {"supply_loss": {FOOD: 3, WATER: 3}, "morale": -8},
            },
            {
                "key": "push_through",
                "label": "冒雨前进",
                "hint": "可能生病，但不耽误行程",
                "effects": {"health": -6, "morale": -3},
            },
        ],
    },
    {
        "key": "survivors",
        "title": "偶遇幸存者",
        "desc": "探索队遇到一群流离失所的幸存者，他们请求加入地堡。",
        "choices": [
            {
                "key": "accept",
                "label": "接纳加入",
                "hint": "新增一名幸存者，但消耗更多补给",
                "effects": {"add_resident": True, "supply_loss": {FOOD: 4, WATER: 3}},
            },
            {
                "key": "trade",
                "label": "交换物资",
                "hint": "用自带物资换取情报与小份补给",
                "effects": {"supply_loss": {FOOD: 3}, "loot": {POWER: 5}, "morale": 3},
            },
            {
                "key": "refuse",
                "label": "拒绝并离开",
                "hint": "保持警惕，安然离开",
                "effects": {"morale": -2},
            },
        ],
    },
    {
        "key": "ruins",
        "title": "废墟探索",
        "desc": "一座保存较完整的废弃建筑矗立在眼前，隐约有物资的气息。",
        "choices": [
            {
                "key": "deep_explore",
                "label": "深入探索",
                "hint": "高风险高回报，可能有重大伤亡",
                "effects": {"loot": {FOOD: 12, WATER: 8, POWER: 6}, "health": {"value": -15, "target": "single"}},
            },
            {
                "key": "outer_search",
                "label": "外围搜索",
                "hint": "安全获得少量物资",
                "effects": {"loot": {FOOD: 5, WATER: 4}},
            },
        ],
    },
    {
        "key": "lost",
        "title": "迷路",
        "desc": "复杂的废墟巷道让探索队迷失了方向，补给在不知不觉中消耗。",
        "choices": [
            {
                "key": "retrace",
                "label": "凭记忆折返",
                "hint": "消耗额外物资寻找归路",
                "effects": {"supply_loss": {FOOD: 5, WATER: 4}, "morale": -5},
            },
            {
                "key": "climb_high",
                "label": "登高辨认",
                "hint": "冒险登高，可能有意外收获",
                "effects": {"loot": {FOOD: 3}, "health": -4, "morale": 2},
            },
        ],
    },
    {
        "key": "airdrop",
        "title": "空投补给",
        "desc": "一架老旧的运输机残骸旁，探索队发现了未被开启的空投舱。",
        "choices": [
            {
                "key": "open_carefully",
                "label": "小心开启",
                "hint": "稳定获得补给",
                "effects": {"loot": {FOOD: 6, WATER: 6, POWER: 4, OXY: 4}},
            },
            {
                "key": "force_open",
                "label": "强行破开",
                "hint": "可能获得更多，也可能损坏物资",
                "effects": {"loot": {FOOD: 10, WATER: 8, POWER: 6}, "supply_loss": {OXY: 3}},
            },
        ],
    },
    {
        "key": "trap",
        "title": "陷阱",
        "desc": "探索队触发了一处老旧的捕兽夹，一名队员被夹住。",
        "choices": [
            {
                "key": "free_carefully",
                "label": "小心解救",
                "hint": "可能加重伤势，但能保全物资",
                "effects": {"health": {"value": -10, "target": "single"}},
            },
            {
                "key": "force_free",
                "label": "强行挣脱",
                "hint": "伤势更重，但不耽误行程",
                "effects": {"health": {"value": -18, "target": "single"}, "supply_loss": {FOOD: 2}},
            },
        ],
    },
]


# ============ 贸易救援模板池 ============
# 地堡管理者可主动申请（outbound）：payment 审核通过后托管，reward 送达后入库。
# 字段：
#   kind          trade 贸易 / rescue 救援
#   payment       地堡支付的押金（外部商队启程时扣除，失败按类型退还）
#   reward        送达交付后入库的回报
#   travel_days   运输天数；risk 订单固有风险（再随信誉下调）
#   morale_bonus  交付成功时全体存活居民士气变化；bonus_health 同理作用于健康
#   rep_bonus     交付成功的信誉奖励；min_reputation 申请所需信誉门槛
#   add_survivor  救援订单：交付时是否有获救幸存者加入地堡
TRADE_PARTIES = ["锈河聚落", "铁壁哨站", "灰岩营地", "北岭车队"]


TRADE_OFFERS = [
    {
        "key": "grain_deal",
        "title": "以水换粮",
        "kind": "trade",
        "party": "锈河聚落",
        "desc": "锈河聚落水源告急，愿意用囤积的罐头换取地堡净化水。",
        "payment": {WATER: 25},
        "reward": {FOOD: 40},
        "travel_days": 2, "risk": 0.12,
        "morale_bonus": 4, "rep_bonus": 3, "min_reputation": 0,
    },
    {
        "key": "cell_battery",
        "title": "燃料电池包",
        "kind": "trade",
        "party": "铁壁哨站",
        "desc": "铁壁哨站拆解军用设备得到燃料电池，只收食物作为硬通货。",
        "payment": {FOOD: 30},
        "reward": {POWER: 45},
        "travel_days": 3, "risk": 0.16,
        "morale_bonus": 3, "rep_bonus": 3, "min_reputation": 0,
    },
    {
        "key": "oxygen_capsule",
        "title": "压缩氧气舱",
        "kind": "trade",
        "party": "灰岩营地",
        "desc": "灰岩营地封存着工业级压缩氧气舱，需要电力与水源一并交换。",
        "payment": {POWER: 20, WATER: 20},
        "reward": {OXY: 45},
        "travel_days": 3, "risk": 0.18,
        "morale_bonus": 4, "rep_bonus": 4, "min_reputation": 30,
    },
    {
        "key": "medic_escort",
        "title": "护送医疗队",
        "kind": "rescue",
        "party": "北岭车队",
        "desc": "北岭车队的医疗队被困中转站，出资请求地堡派人护送他们安全抵达。",
        "payment": {FOOD: 15},
        "reward": {POWER: 15, WATER: 15},
        "travel_days": 2, "risk": 0.2,
        "morale_bonus": 8, "rep_bonus": 6, "min_reputation": 20,
        "bonus_health": 6,
    },
    {
        "key": "rescue_family",
        "title": "营救幸存者一家",
        "kind": "rescue",
        "party": "锈河聚落",
        "desc": "锈河聚落附近的避难站失联，一名幸存者带着孩子等待接应，获救者愿加入地堡。",
        "payment": {FOOD: 12, WATER: 12},
        "reward": {FOOD: 8},
        "travel_days": 3, "risk": 0.22,
        "morale_bonus": 10, "rep_bonus": 8, "min_reputation": 35,
        "add_survivor": True,
    },
    {
        "key": "ammo_scrap",
        "title": "军械废料",
        "kind": "trade",
        "party": "铁壁哨站",
        "desc": "一批战前军械废料可拆成发电部件，但路途盗匪猖獗，只与信誉过硬的地堡交易。",
        "payment": {FOOD: 35, WATER: 15},
        "reward": {POWER: 35, OXY: 15},
        "travel_days": 4, "risk": 0.24,
        "morale_bonus": 5, "rep_bonus": 5, "min_reputation": 50,
    },
    {
        "key": "greenhouse_seeds",
        "title": "穹顶菜种",
        "kind": "trade",
        "party": "灰岩营地",
        "desc": "灰岩营地保存着一批耐寒菜种，能显著充实地堡粮仓，交易条件不菲。",
        "payment": {POWER: 30, WATER: 20},
        "reward": {FOOD: 55},
        "travel_days": 3, "risk": 0.14,
        "morale_bonus": 6, "rep_bonus": 4, "min_reputation": 40,
    },
]


# 外部队伍主动发来的申请（inbound）：地堡支付押金救援/收购，送达获得回报。
# 结构与 TRADE_OFFERS 相同，party 由每日随机选取。
TRADE_INBOUND_POOL = [
    {
        "key": "in_refugees",
        "title": "难民求庇",
        "kind": "rescue",
        "desc": "一小队难民在地表发讯，愿用最后的物资换取地堡庇护，其中一人可加入地堡。",
        "payment": {FOOD: 10},
        "reward": {WATER: 18, POWER: 8},
        "travel_days": 2, "risk": 0.15,
        "morale_bonus": 7, "rep_bonus": 5,
        "add_survivor": True,
    },
    {
        "key": "in_water_short",
        "title": "邻堡购水",
        "kind": "trade",
        "desc": "附近聚落净水器损毁，愿以罐头高价求购地堡的净化水。",
        "payment": {WATER: 20},
        "reward": {FOOD: 34},
        "travel_days": 2, "risk": 0.12,
        "morale_bonus": 3, "rep_bonus": 3,
    },
    {
        "key": "in_stranded_medic",
        "title": "接应流浪医生",
        "kind": "rescue",
        "desc": "一名流浪医生被困废弃诊所，承诺得救后为地堡义诊并分享药品。",
        "payment": {FOOD: 8, WATER: 8},
        "reward": {POWER: 10, OXY: 10},
        "travel_days": 2, "risk": 0.18,
        "morale_bonus": 8, "rep_bonus": 6,
        "bonus_health": 8,
    },
    {
        "key": "in_generator_parts",
        "title": "发电机零件求售",
        "kind": "trade",
        "desc": "一支车队带来稀缺的发电机零件，只要地堡愿意先垫付水粮作为运输押金。",
        "payment": {FOOD: 18, WATER: 12},
        "reward": {POWER: 40},
        "travel_days": 3, "risk": 0.17,
        "morale_bonus": 4, "rep_bonus": 4,
    },
]