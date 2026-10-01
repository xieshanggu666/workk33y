# 末日地堡生存

一款基于 **FastAPI + SQLite** 后端与 **Vue 3（本地运行时，离线可用）** 前端的末日地堡模拟经营游戏。

## 游戏说明

地表已覆灭，你带领一批幸存者藏身地堡。你需要管理**有限的食物、水源、电力、氧气**，安置与分配**居民岗位**，扩建**基础设施**，并在每天的**危机轮盘**中做出抉择，坚持到第 120 天迎来曙光。

## 核心机制

- **资源守恒循环**：每日净变化 = 设施产出 − 人口消耗。产出受设施等级、相关岗位（农民/工程师）与平均士气加成影响。
- **四类资源**：食物 / 水源 / 电力 / 氧气，任一枯竭都会引发减员。
- **居民管理**：每位幸存者有健康与士气，健康低于阈值士气下滑；士气影响整体产出效率。可为居民指派岗位（工程师 / 农民 / 杂工）。
- **设施扩建**：穹顶菜园（产食物）、净水器（产水）、发电机（产电）、水培制氧（产氧）、医疗舱（回血）、仓储区。可建造与升级（等级加成 x1.6）。
- **危机轮盘**：每天约 45% 概率触发危机事件，均可多选决策（辐射风暴、内讧、氧气泄漏、疫病、盗匪、物资舱、暴雪），不同选择带来不同的资源 / 健康 / 士气后果。
- **结局结算**：存活至第 120 天**胜利**；人口归零或四资源全线枯竭则**失败**。胜局 / 败局均按幸存者 × 天数 × 士气系数结算评分。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.10+ / FastAPI / SQLAlchemy 2.0 / SQLite |
| 前端 | Vue 3 单页应用（vue.global.prod.js 本地运行时，hash-free 组件切换，无需构建工具） |
| 测试 | pytest（纯引擎逻辑） |

## 快速开始

环境要求：**Node.js**（任意近年版本，仅用于命令调度，前端无需构建）与 **Python 3.10+**。
前端静态资源由 FastAPI 后端同端口托管，因此一条命令即可启动前后端：

```bash
npm run setup   # 安装 Python 依赖并初始化数据库（仅需一次）
npm start       # 一键启动前后端
```

访问 http://127.0.0.1:8000 ，在档案大厅新建或继续一局游戏。

| 命令 | 作用 |
|---|---|
| `npm run setup` | 安装 Python 依赖 + 初始化数据库（幂等，含演示档案） |
| `npm start` | 生产模式启动（`PORT=8080 npm start` 可换端口，`HOST`/`PYTHON` 同理） |
| `npm run dev` | 开发模式启动（代码变更自动重载） |
| `npm run init-db` | 仅初始化/补齐数据库 |
| `npm test` | 运行 pytest 测试套件（`npm test -- -k xxx` 可透传参数） |

<details>
<summary>不使用 npm 的传统启动方式</summary>

```bash
pip install -r requirements.txt
python scripts/init_db.py
uvicorn app.main:app --reload
```

</details>

## 目录结构

```
bunker_survival/
├── app/
│   ├── main.py            # 应用入口，托管前端静态资源与 API
│   ├── core/              # 配置 / 数据库
│   ├── models/            # GameSession / Resident / Facility / EventLog
│   ├── schemas/           # Pydantic 校验模型
│   ├── services/engine.py # 游戏核心引擎（资源守恒 / 危机轮盘 / 决策树 / 结局）
│   └── api/router.py      # REST 接口
├── static/
│   ├── index.html         # Vue 单页应用入口
│   ├── css/style.css      # 样式
│   ├── js/                # api.js + LobbyView / GameView
│   └── vendor/            # Vue 3 本地运行时（离线可用）
├── scripts/
│   ├── manage.js        # npm 一键脚本调度（setup / start / dev / init-db / test）
│   └── init_db.py       # 数据库初始化与演示档案
├── package.json         # npm scripts 入口（零 Node 依赖）
├── tests/               # 引擎单元测试
└── data/                # SQLite 数据库
```

## 测试

```bash
npm test            # 等价于 pytest tests -q
```

覆盖资源守恒、建造/升级扣费、居民岗位、危机资源效果、结局判定（胜利/失败/拒绝推进）、
探索队派遣/遭遇/返程、并发乐观锁与幂等回放、旧存档迁移归一化，以及 HTTP 层端到端一致性。

## 状态流转与一致性约定

档案状态机只有四个阶段，所有后端守卫以前端禁用为辅助、以后端校验为准：

| 阶段 | 含义 | 允许的动作 |
|---|---|---|
| `daily` | 每日阶段 | 建造/升级/调岗/派遣探索队/推进一天 |
| `crisis` | 地堡危机待处理 | 仅结算当前危机 |
| `expedition` | 探索遭遇待处理 | 仅结算当前遭遇 |
| `ended` | 胜利/失败终局 | 只读，拒绝一切变更 |

- **每日推进**：`daily → 产出消耗/健康士气 → (行军+遭遇? | 危机? | 终局)`。抉择不可被“再点一天”跳过。
- **探索队**：离堡人员暂停地堡生产、不消耗地堡口粮、不参与地堡危机效果；行军消耗自带口粮，遭遇替代当日地堡危机，返程统一结算战利品/余粮/伤亡。终局日队伍仍在外时先安全返程再收敛到 `ended`，不留“僵尸队伍”。
- **并发**：`row_version` 乐观锁保证每日推进/危机/遭遇/返程只落一次；落败方凭一次性 `token` 与档案级幂等凭据（`last_resolution` / `last_expedition`）安全回放，过期或串档请求返回 409。
- **旧存档**：启动时自动补列（`pending_crisis` / `last_resolution` / `expedition` / `last_expedition` / `row_version`），并归一化损坏/悬空快照（已结束档案清除悬而未决状态、失踪成员/目标剔除、`survivors` 与居民表对齐），归一化本身幂等。