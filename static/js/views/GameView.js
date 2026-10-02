/* 末日地堡生存 —— 主游戏界面 */
window.GameView = {
  props: ["sid", "onExit"],
  data() {
    return {
      s: null,
      crisis: null,
      loading: false,
      error: "",
      tab: "overview",
      config: null,
      buildings: [],
      selectedJob: {},
      showExpeditionDialog: false,
      expMembers: [],
      expSupplies: { food: 0, water: 0 },
      // 贸易救援
      offers: [],
      showTradeDialog: false,
      tradeOfferKey: "",
      tradeEscorts: [],
    };
  },
  created() { this.init(); },
  methods: {
    async init() {
      this.error = "";
      try {
        const [s, cfg, bld] = await Promise.all([
          Api.get(`/api/sessions/${this.sid}`),
          Api.get("/api/config"),
          Api.get("/api/buildings"),
        ]);
        this.s = s; this.config = cfg; this.buildings = bld;
        // 待处理危机已随存档持久化：刷新/重进档案后恢复同一个决策弹层
        this.crisis = s.pending_crisis || null;
      } catch (e) { this.error = e.message; }
    },
    async loadSession() {
      this.s = await Api.get(`/api/sessions/${this.sid}`);
      // 以服务端为准恢复待处理危机（并发落败回放时也可能带回）
      this.crisis = this.s.pending_crisis || null;
    },
    async advance() {
      this.error = "";
      if (this.s.status !== "running" || this.actionLocked) return;
      this.loading = true;
      try {
        const r = await Api.post(`/api/sessions/${this.sid}/advance`);
        this.s = r.session;
        // 两个抉择弹层统一只从服务端会话快照派生（不读返回里的 pending_event/crisis）：
        // 该字段可能是地堡危机也可能是探索遭遇，直接复用会把遭遇渲染成危机。
        // 地堡危机 → s.pending_crisis；探索遭遇 → s.expedition.pending_encounter（模板另判）
        this.crisis = this.s.pending_crisis || null;
      } catch (e) {
        this.error = e.message;
        // 并发落败等 409 场景：拉取最新状态，避免覆盖掉已挂起的抉择
        await this.loadSession();
      }
      finally { this.loading = false; }
    },
    async resolve(c) {
      this.error = "";
      this.loading = true;
      try {
        // 目标语义以后端下发的 c.targeted 为准：
        // 仅单体决策回传 target_id；全体决策显式传 null，
        // 避免危机事件的随机目标被无条件带回、把全体效果收窄成一人。
        // token 绑定本次待处理危机：重复/并发请求由后端识别为同一次结算
        const body = {
          event_key: this.crisis.event,
          choice_key: c.key,
          target_id: c.targeted ? this.crisis.target_id : null,
          token: this.crisis.token,
        };
        this.s = await Api.post(`/api/sessions/${this.sid}/resolve`, body);
        this.crisis = this.s.pending_crisis || null;
      } catch (e) {
        this.error = e.message;
        // 409（过期/并发）或危机已被其他标签页结算：刷新为最新状态
        await this.loadSession();
      }
      finally { this.loading = false; }
    },
    async build(cat) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/build`, { category: cat });
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    async upgrade(fid) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/upgrade/${fid}`);
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    async assignJob(rid, job) {
      this.error = "";
      if (this.actionLocked) return;
      try {
        this.s = await Api.post(`/api/sessions/${this.sid}/resident/${rid}/job`, { job });
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
    },
    async loadSessionOn409(e) {
      // 409（并发落败/状态过期）统一以服务端为准，防止旧标签页继续按过期状态操作
      if (e && e.status === 409) await this.loadSession();
    },
    setJobSel(rid, job) { this.selectedJob[rid] = job; },
    // ---- 探索队 ----
    openExpeditionDialog() {
      this.error = "";
      this.expMembers = [];
      this.expSupplies = { food: 0, water: 0 };
      this.showExpeditionDialog = true;
    },
    toggleMember(id) {
      const i = this.expMembers.indexOf(id);
      if (i >= 0) this.expMembers.splice(i, 1);
      else {
        if (this.expMembers.length >= 4) { this.error = "探索队最多 4 人"; return; }
        this.expMembers.push(id);
      }
    },
    async sendExpedition() {
      this.error = "";
      if (!this.expMembers.length) { this.error = "必须选择至少一名居民"; return; }
      if (this.actionLocked) return;
      this.loading = true;
      try {
        const supplies = {};
        for (const k of ["food", "water"]) {
          const v = Number(this.expSupplies[k]) || 0;
          if (v > 0) supplies[k] = v;
        }
        this.s = await Api.post(`/api/sessions/${this.sid}/expedition/send`, {
          member_ids: this.expMembers,
          supplies,
        });
        this.showExpeditionDialog = false;
      } catch (e) { this.error = e.message; }
      finally { this.loading = false; }
    },
    async resolveExpeditionEncounter(c) {
      this.error = "";
      this.loading = true;
      try {
        const body = { choice_key: c.key, token: this.s.expedition.pending_encounter.token };
        this.s = await Api.post(`/api/sessions/${this.sid}/expedition/resolve`, body);
      } catch (e) { this.error = e.message; await this.loadSession(); }
      finally { this.loading = false; }
    },
    async returnExpedition() {
      this.error = "";
      this.loading = true;
      try {
        const body = { token: this.s.expedition.token };
        this.s = await Api.post(`/api/sessions/${this.sid}/expedition/return`, body);
      } catch (e) { this.error = e.message; await this.loadSession(); }
      finally { this.loading = false; }
    },
    // ---- 贸易救援 ----
    async loadOffers() {
      try { this.offers = await Api.get(`/api/sessions/${this.sid}/trade/offers`); }
      catch (e) { this.error = e.message; }
    },
    async openTradeTab() {
      this.tab = "trade";
      await this.loadOffers();
    },
    openTradeDialog(key) {
      this.tradeOfferKey = key;
      this.tradeEscorts = [];
      this.showTradeDialog = true;
    },
    toggleTradeEscort(id) {
      const i = this.tradeEscorts.indexOf(id);
      if (i >= 0) this.tradeEscorts.splice(i, 1);
      else {
        if (this.tradeEscorts.length >= 2) { this.error = "每张订单最多 2 名押运员"; return; }
        this.tradeEscorts.push(id);
      }
    },
    async submitTradeApply() {
      this.error = "";
      if (this.actionLocked) return;
      this.loading = true;
      try {
        this.s = (await Api.post(`/api/sessions/${this.sid}/trade/apply`, {
          offer_key: this.tradeOfferKey,
          escort_ids: this.tradeEscorts,
        })).session;
        this.showTradeDialog = false;
      } catch (e) { this.error = e.message; }
      finally { this.loading = false; }
    },
    async reviewOrder(o, approve) {
      this.error = "";
      if (this.actionLocked) return;
      this.loading = true;
      try {
        this.s = (await Api.post(`/api/sessions/${this.sid}/trade/${o.id}/review`, {
          approve, token: o.token,
        })).session;
      } catch (e) { this.error = e.message; await this.loadSessionOn409(e); }
      finally { this.loading = false; }
    },
    async dismissOrder(o) {
      this.error = "";
      this.loading = true;
      try {
        this.s = await Api.del(`/api/sessions/${this.sid}/trade/${o.id}`);
      } catch (e) { this.error = e.message; }
      finally { this.loading = false; }
    },
    orderEscortNames(o) {
      return (o.escorts || []).map(id => {
        const r = this.s.residents.find(x => x.id === id);
        return r ? r.name : "?";
      }).join("、") || "无押运";
    },
    tradeStatusZh(st) {
      return ({
        applied: "申请/审核",
        transporting: "运输在途",
        delivered: "已交付",
        failed: "失败回退",
        rejected: "已拒绝",
        aborted: "已中止",
      })[st] || st;
    },
    expMemberNames() {
      if (!this.s || !this.s.expedition) return "";
      const ids = this.s.expedition.members || [];
      return ids.map(id => {
        const r = this.s.residents.find(x => x.id === id);
        return r ? r.name : "?";
      }).join("、");
    },
    expLootText() {
      if (!this.s || !this.s.expedition) return "";
      const loot = this.s.expedition.loot || {};
      const parts = [];
      for (const k of ["food", "water", "power", "oxygen"]) {
        if (loot[k] > 0) parts.push(`${{food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k]}+${Math.round(loot[k])}`);
      }
      return parts.join("、") || "暂无";
    },
    resPct(k) {
      const cap = { food: 300, water: 300, power: 200, oxygen: 200 };
      const c = cap[k] || 100;
      return Math.min(100, Math.round((this.s.resources[k] / c) * 100));
    },
    clazz(st) {
      return st === "win" ? "win" : st === "over" ? "over" : "running";
    },
    fmt(v) { return v == null ? "-" : Math.round(v); },
  },
  computed: {
    alive() { return this.s ? this.s.residents.filter(r => r.alive) : []; },
    expPending() {
      return !!(this.s && this.s.expedition && this.s.expedition.pending_encounter);
    },
    // 抉择锁：地堡危机或探索遭遇待处理时，推进与一切经营动作统一禁用
    actionLocked() {
      return !!(this.crisis || this.expPending);
    },
    pendingTitle() {
      if (this.crisis) return "请先处理当前危机";
      if (this.expPending) return "请先处理探索遭遇";
      return "";
    },
    inBunkerAlive() {
      return this.s ? this.s.residents.filter(r => r.alive && !r.away) : [];
    },
    expMemberCount() {
      // 只统计档案中仍在编制内的成员，兼容旧快照里夹杂已移除编号的情况
      if (!this.s || !this.s.expedition) return 0;
      const ids = this.s.expedition.members || [];
      return ids.filter(id => this.s.residents.some(r => r.id === id)).length;
    },
    // ---- 贸易救援 ----
    tradeActiveOrders() {
      return (this.s?.trade_orders || []).filter(o => ["applied", "transporting"].includes(o.status));
    },
    tradeHistoryOrders() {
      // 终态订单逆序展示（最新在前）
      return (this.s?.trade_orders || [])
        .filter(o => ["delivered", "failed", "rejected", "aborted"].includes(o.status))
        .slice().reverse();
    },
    inboundPendingOrders() {
      return (this.s?.trade_orders || []).filter(
        o => o.status === "applied" && o.review_by === "bunker"
      );
    },
  },
  template: `
  <div v-if="s" class="game" :class="clazz(s.status)">
    <!-- 顶栏 -->
    <header class="game-top">
      <div class="brand">末日地堡<i class="bar"></i></div>
      <div class="day">{{ s.day }}<small>/{{ s.target_day }} 天</small></div>
      <div class="top-right">
        <span class="chip rep" title="对外信誉：影响外部接单概率、商路风险与可申请订单">信誉 {{ s.reputation }}</span>
        <span class="chip" :class="s.status">{{ s.status === 'running' ? '进行中' : s.status === 'win' ? '胜利' : '失败' }}</span>
        <button class="btn ghost small" @click="onExit">返回档案</button>
      </div>
    </header>

    <!-- 资源条 -->
    <section class="resbar">
      <div v-for="k in ['food','water','power','oxygen']" :key="k" class="res" :class="{ low: s.resources[k] < 20 && s.status==='running' }">
        <div class="res-name">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }}</div>
        <div class="res-val">{{ fmt(s.resources[k]) }}</div>
        <div class="res-track"><div class="res-fill" :class="k" :style="{ width: resPct(k)+'%' }"></div></div>
      </div>
      <button class="btn primary advance" :disabled="loading || s.status!=='running' || actionLocked" :title="pendingTitle" @click="advance">
        {{ crisis ? '等待危机抉择' : expPending ? '等待探索遭遇抉择' : loading ? '推进中…' : '推进一天' }}
      </button>
    </section>
    <div v-if="error" class="msg err global">{{ error }}</div>

    <!-- 主区 -->
    <div class="game-body">
      <nav class="tabs">
        <button :class="{ active: tab==='overview' }" @click="tab='overview'">总览</button>
        <button :class="{ active: tab==='residents' }" @click="tab='residents'">幸存者 ({{ alive.length }})</button>
        <button :class="{ active: tab==='expedition' }" @click="tab='expedition'">探索队<template v-if="s.expedition"> ({{ expMemberCount }})</template></button>
        <button :class="{ active: tab==='trade' }" @click="openTradeTab">商路救援<template v-if="inboundPendingOrders.length"> ({{ inboundPendingOrders.length }})</template></button>
        <button :class="{ active: tab==='build' }" @click="tab='build'">设施扩建</button>
        <button :class="{ active: tab==='log' }" @click="tab='log'">大事记</button>
      </nav>

      <!-- 总览 -->
      <div v-if="tab==='overview'">
        <div class="cards">
          <div class="card"><div class="k">幸存者</div><div class="v">{{ s.survivors }}</div><div class="hint">人口即火种</div></div>
          <div class="card"><div class="k">士气</div><div class="v">{{ s.residents.length ? fmt(alive.reduce((a,r)=>a+r.morale,0)/alive.length) : 0 }}</div><div class="hint">影响产出效率</div></div>
          <div class="card"><div class="k">设施</div><div class="v">{{ s.facilities.length }}</div><div class="hint">支撑循环</div></div>
          <div class="card"><div class="k">得分</div><div class="v">{{ s.score }}</div><div class="hint">生存评分</div></div>
        </div>
        <div class="fac-grid">
          <div v-for="f in s.facilities" :key="f.id" class="fac">
            <span class="fac-name">{{ f.name }}</span>
            <span class="chip">Lv.{{ f.level }}</span>
            <span class="dim">{{ {farm:'产食物',water:'产水源',power:'发电',oxygen:'产氧',med:'医疗',storage:'仓储'}[f.category] }}</span>
            <button v-if="s.status==='running'" class="btn tiny" :disabled="actionLocked" @click="upgrade(f.id)">升级</button>
          </div>
        </div>
      </div>

      <!-- 幸存者 -->
      <div v-if="tab==='residents'">
        <div v-for="r in s.residents" :key="r.id" class="person" :class="{ dead: !r.alive, away: r.away }">
          <div class="p-avatar">{{ r.name[0] }}</div>
          <div class="p-info">
            <div class="p-name">{{ r.name }} <span class="dim">{{ r.job_zh }}</span><span v-if="r.away" class="chip away-tag">{{ r.away_kind === 'escort' ? '押运中' : '探索中' }}</span></div>
            <div class="meter"><i>健康</i><span class="track"><span class="fill" :style="{width: r.health+'%', background:'#4caf50'}"></span></span><b>{{ fmt(r.health) }}</b></div>
            <div class="meter"><i>士气</i><span class="track"><span class="fill" :style="{width: r.morale+'%', background:'#ffb300'}"></span></span><b>{{ fmt(r.morale) }}</b></div>
          </div>
          <div class="p-actions" v-if="r.alive && s.status==='running'">
            <select :value="r.job" :disabled="actionLocked || r.away" @change="assignJob(r.id, $event.target.value)">
              <option value="engineer">工程师</option>
              <option value="farmer">农民</option>
              <option value="general">杂工</option>
            </select>
          </div>
        </div>
      </div>

      <!-- 探索队 -->
      <div v-if="tab==='expedition'">
        <!-- 无在外队伍：派遣 -->
        <div v-if="!s.expedition" class="exp-panel">
          <div class="exp-empty">
            <p>派遣幸存者携带物资外出探索，途中可能遭遇事件，返程时统一结算战利品与伤亡。</p>
            <p class="dim">离堡人员暂停地堡生产，不消耗地堡口粮；探索队消耗自带物资。</p>
            <button class="btn primary" :disabled="s.status!=='running' || actionLocked" @click="openExpeditionDialog">派遣探索队</button>
          </div>
        </div>
        <!-- 有在外队伍：状态 -->
        <div v-else class="exp-panel">
          <div class="exp-status">
            <div class="exp-row"><span class="k">队员</span><span class="v">{{ expMemberNames() }}</span></div>
            <div class="exp-row"><span class="k">行军</span><span class="v">第 {{ s.expedition.travel_days }} 天 / 上限 7 天</span></div>
            <div class="exp-row"><span class="k">自带物资</span><span class="v">食物 {{ Math.round(s.expedition.supplies.food||0) }} · 水 {{ Math.round(s.expedition.supplies.water||0) }}</span></div>
            <div class="exp-row"><span class="k">战利品（未结算）</span><span class="v loot">{{ expLootText() }}</span></div>
            <div class="exp-row" v-if="s.expedition.encounters_resolved"><span class="k">已处理遭遇</span><span class="v">{{ s.expedition.encounters_resolved }} 次</span></div>
          </div>
          <div class="exp-actions">
            <button class="btn primary" :disabled="s.status!=='running' || actionLocked" @click="returnExpedition">
              {{ crisis ? '请先处理危机' : expPending ? '请先处理遭遇' : '立即返程' }}
            </button>
            <span class="dim" v-if="!actionLocked">返程时统一结算战利品与伤亡</span>
          </div>
        </div>
      </div>

      <!-- 商路救援 -->
      <div v-if="tab==='trade'">
        <div class="trade-head">
          <div class="dim">与外部聚落围绕订单、物资和信誉协商：申请 → 审核 → 运输 → 交付（失败则回退）。信誉越高，外部越愿意接单、商路越安全。</div>
          <button class="btn small primary" :disabled="s.status!=='running' || actionLocked || tradeActiveOrders.length >= 3"
            @click="loadOffers(); openTradeDialog('')">
            申请贸易/救援
          </button>
        </div>

        <!-- 外部申请待审核 -->
        <div v-if="inboundPendingOrders.length" class="trade-section">
          <h3 class="trade-h">📨 外部队伍求援（待你审核）</h3>
          <div v-for="o in inboundPendingOrders" :key="o.id" class="order-card inbound">
            <div class="order-top">
              <span class="order-title">{{ o.title }}</span>
              <span class="chip" :class="o.kind">{{ o.kind === 'rescue' ? '救援' : '贸易' }}</span>
              <span class="dim">来自 {{ o.party }} · 第 {{ o.expire_day }} 天截止</span>
            </div>
            <div class="order-terms">
              <span v-for="(v,k) in o.payment" :key="'p'+k" class="term pay">支付 {{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} {{ v }}</span>
              <span v-for="(v,k) in o.reward" :key="'r'+k" class="term reward">回报 {{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} +{{ v }}</span>
              <span v-if="o.add_survivor" class="term reward">获救者加入地堡</span>
              <span v-if="o.morale_bonus" class="term">士气 {{ o.morale_bonus > 0 ? '+' : '' }}{{ o.morale_bonus }}</span>
            </div>
            <div class="order-actions">
              <button class="btn small primary" :disabled="s.status!=='running' || actionLocked" @click="reviewOrder(o, true)">批准（托管押金）</button>
              <button class="btn small danger" :disabled="s.status!=='running' || actionLocked" @click="reviewOrder(o, false)">拒绝</button>
            </div>
          </div>
        </div>

        <!-- 在途/待审订单 -->
        <div v-if="tradeActiveOrders.length" class="trade-section">
          <h3 class="trade-h">🚚 进行中的订单</h3>
          <div v-for="o in tradeActiveOrders" :key="o.id" class="order-card">
            <div class="order-top">
              <span class="order-title">{{ o.title }}</span>
              <span class="chip" :class="o.kind">{{ o.kind === 'rescue' ? '救援' : '贸易' }}</span>
              <span class="order-status" :class="o.status">{{ tradeStatusZh(o.status) }}</span>
              <span class="dim">{{ o.party }}</span>
            </div>
            <div class="order-terms">
              <span v-for="(v,k) in o.payment" :key="'p'+k" class="term pay">押金 {{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} {{ v }}</span>
              <span v-for="(v,k) in o.reward" :key="'r'+k" class="term reward">回报 {{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} +{{ v }}</span>
              <span class="term dim">押运：{{ orderEscortNames(o) }}</span>
            </div>
            <div v-if="o.status==='transporting'" class="order-progress">
              商队在途：第 {{ o.elapsed_days }} / {{ o.travel_days }} 天
            </div>
            <div v-else-if="o.review_by==='external'" class="order-progress dim">
              已发出申请，等待「{{ o.party }}」审核
            </div>
          </div>
        </div>

        <!-- 可申请订单 -->
        <div class="trade-section">
          <h3 class="trade-h">📜 商路告示（可主动申请）</h3>
          <div v-if="!offers.length" class="dim">加载中…</div>
          <div class="offer-grid">
            <div v-for="o in offers" :key="o.key" class="build-card">
              <span class="bc-name">{{ o.title }}</span>
              <span class="chip" :class="o.kind">{{ o.kind === 'rescue' ? '救援' : '贸易' }}</span>
              <span class="dim">{{ o.desc }}</span>
              <span class="cost" v-for="(v,k) in o.payment" :key="k">押金 {{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} {{ v }}</span>
              <span class="dim">回报：<template v-for="(v,k) in o.reward" :key="k">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }}+{{ v }} </template><template v-if="o.add_survivor">幸存者加入 </template></span>
              <span class="dim">在途 {{ o.travel_days }} 天 · 基础风险 {{ Math.round(o.risk*100) }}% · 信誉 +{{ o.rep_bonus }}<template v-if="o.morale_bonus"> · 士气 +{{ o.morale_bonus }}</template></span>
              <button class="btn small primary" :disabled="s.status!=='running' || actionLocked || tradeActiveOrders.length >= 3"
                @click="openTradeDialog(o.key)">申请</button>
            </div>
          </div>
        </div>

        <!-- 历史订单 -->
        <div v-if="tradeHistoryOrders.length" class="trade-section">
          <h3 class="trade-h">📦 订单记录</h3>
          <div v-for="o in tradeHistoryOrders" :key="o.id" class="order-card history" :class="o.status">
            <div class="order-top">
              <span class="order-title">{{ o.title }}</span>
              <span class="order-status" :class="o.status">{{ tradeStatusZh(o.status) }}</span>
              <span class="dim">{{ o.party }} · {{ o.result && (o.result.reason || '') }}</span>
              <button class="btn tiny ghost" @click="dismissOrder(o)">归档</button>
            </div>
            <div v-if="o.result && o.result.detail" class="order-result dim">{{ o.result.detail }}</div>
          </div>
        </div>
      </div>

      <!-- 扩建 -->
      <div v-if="tab==='build'">
        <div class="build-grid">
          <div v-for="b in buildings" :key="b.category" class="build-card">
            <span class="bc-name">{{ b.name }}</span>
            <span class="dim">等级加成 x1.6</span>
            <div class="cost" v-for="(v,k) in b.cost" :key="k">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} {{ v }}</div>
            <button class="btn small primary" :disabled="s.status!=='running' || actionLocked" @click="build(b.category)">建造</button>
          </div>
        </div>
      </div>

      <!-- 大事记 -->
      <div v-if="tab==='log'" class="logs">
        <div v-for="l in [...s.logs].reverse()" :key="l.id" class="log" :class="l.event_type">
          <span class="log-day">D{{ l.day }}</span>
          <div class="log-txt"><strong>{{ l.title }}</strong><p>{{ l.detail }}</p></div>
        </div>
      </div>
    </div>

    <!-- 结局弹层 -->
    <div v-if="s.status !== 'running'" class="overlay">
      <div class="ending" :class="s.status">
        <h2>{{ s.status === 'win' ? '曙光降临' : '地堡永寂' }}</h2>
        <p>{{ s.outcome.reason }}</p>
        <div class="end-stats">
          <div><span>存活天数</span><b>{{ s.outcome.day }}</b></div>
          <div><span>幸存者</span><b>{{ s.outcome.survivors }}</b></div>
          <div><span>得分</span><b>{{ s.score }}</b></div>
        </div>
        <button class="btn primary" @click="onExit">返回档案列表</button>
      </div>
    </div>

    <!-- 危机弹层 -->
    <div v-if="crisis" class="overlay">
      <div class="crisis">
        <h2>⚡ {{ crisis.title }}</h2>
        <p class="crisis-desc">{{ crisis.desc }}</p>
        <div v-if="crisis.needs_target" class="crisis-tgt">
          相关居民：{{ crisis.target_name }}<span class="dim">（仅标注「单人」的决策作用于本人，其余对全体生效）</span>
        </div>
        <div class="choices">
          <button v-for="c in crisis.choices" :key="c.key" class="choice" @click="resolve(c)">
            <strong>{{ c.label }}</strong>
            <span class="scope-tag" :class="{ solo: c.targeted }">{{ c.targeted ? '单人' : '全体' }}</span>
            <span class="hint">{{ c.hint }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 探索遭遇弹层 -->
    <div v-if="expPending" class="overlay">
      <div class="crisis expedition">
        <h2>🧭 {{ s.expedition.pending_encounter.title }}</h2>
        <p class="crisis-desc">{{ s.expedition.pending_encounter.desc }}</p>
        <div v-if="s.expedition.pending_encounter.needs_target" class="crisis-tgt">
          相关队员：{{ s.expedition.pending_encounter.target_name }}<span class="dim">（仅标注「单人」的决策作用于本人，其余对全体队员生效）</span>
        </div>
        <div class="choices">
          <button v-for="c in s.expedition.pending_encounter.choices" :key="c.key" class="choice" @click="resolveExpeditionEncounter(c)">
            <strong>{{ c.label }}</strong>
            <span class="scope-tag" :class="{ solo: c.targeted }">{{ c.targeted ? '单人' : '全体' }}</span>
            <span class="hint">{{ c.hint }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 派遣探索队弹层 -->
    <div v-if="showExpeditionDialog" class="overlay">
      <div class="crisis expedition">
        <h2>派遣探索队</h2>
        <p class="crisis-desc">选择在堡居民（最多 4 人）并分配自带物资。离堡人员暂停地堡生产，不消耗地堡口粮。</p>
        <div class="exp-member-pick">
          <div v-for="r in inBunkerAlive" :key="r.id" class="exp-member" :class="{ selected: expMembers.includes(r.id) }" @click="toggleMember(r.id)">
            <span class="p-avatar">{{ r.name[0] }}</span>
            <span>{{ r.name }}</span>
            <span class="dim">{{ r.job_zh }}</span>
          </div>
          <div v-if="!inBunkerAlive.length" class="dim">没有可派遣的在堡居民</div>
        </div>
        <div class="exp-supplies">
          <label>自带食物 <input type="number" min="0" v-model.number="expSupplies.food" /></label>
          <label>自带饮水 <input type="number" min="0" v-model.number="expSupplies.water" /></label>
          <span class="dim">每人每日消耗 1 食物 + 1 水</span>
        </div>
        <div class="choices">
          <button class="choice primary-choice" @click="sendExpedition" :disabled="loading">
            <strong>{{ loading ? '派遣中…' : '出发' }}</strong>
          </button>
          <button class="choice" @click="showExpeditionDialog=false"><strong>取消</strong></button>
        </div>
      </div>
    </div>

    <!-- 申请贸易/救援弹层 -->
    <div v-if="showTradeDialog" class="overlay">
      <div class="crisis expedition trade-dialog">
        <h2>申请贸易 / 救援</h2>
        <p class="crisis-desc">选择一笔商路订单并可选派押运员（最多 2 人）。申请需经外部聚落审核，通过后托管押金、商队启程；押运员离堡期间暂停地堡生产与消耗。</p>
        <div v-if="!tradeOfferKey" class="trade-offer-pick">
          <div v-for="o in offers" :key="o.key" class="trade-offer" @click="openTradeDialog(o.key)">
            <div class="to-top">
              <strong>{{ o.title }}</strong>
              <span class="chip" :class="o.kind">{{ o.kind === 'rescue' ? '救援' : '贸易' }}</span>
            </div>
            <div class="dim">{{ o.desc }}</div>
            <div class="dim">押金：<template v-for="(v,k) in o.payment" :key="k">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }} {{ v }} </template>｜回报：<template v-for="(v,k) in o.reward" :key="k">{{ {food:'食物',water:'水源',power:'电力',oxygen:'氧气'}[k] }}+{{ v }} </template><template v-if="o.add_survivor">+幸存者 </template>｜在途 {{ o.travel_days }} 天</div>
          </div>
          <div v-if="!offers.length" class="dim">当前信誉暂无可申请订单</div>
        </div>
        <template v-else>
          <div class="exp-member-pick">
            <div v-for="r in inBunkerAlive" :key="r.id" class="exp-member" :class="{ selected: tradeEscorts.includes(r.id) }" @click="toggleTradeEscort(r.id)">
              <span class="p-avatar">{{ r.name[0] }}</span>
              <span>{{ r.name }}</span>
              <span class="dim">{{ r.job_zh }}</span>
            </div>
            <div v-if="!inBunkerAlive.length" class="dim">没有可选的在堡居民（可无押运申请）</div>
          </div>
          <div class="choices">
            <button class="choice primary-choice" @click="submitTradeApply" :disabled="loading">
              <strong>{{ loading ? '提交中…' : '提交申请' }}</strong>
            </button>
            <button class="choice" @click="openTradeDialog('')"><strong>重选订单</strong></button>
            <button class="choice" @click="showTradeDialog=false"><strong>取消</strong></button>
          </div>
        </template>
      </div>
    </div>
  </div>`,
};