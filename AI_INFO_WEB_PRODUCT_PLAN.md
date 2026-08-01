---
title: "AI 产品雷达（ai-info-web）产品方案"
tags: [ai-info-web, product-plan, mvp]
status: active
version: 1.1
created: 2026-08-01
updated: 2026-08-01
---

# AI 产品雷达（ai-info-web）产品方案

> **v1.1 修订（2026-08-01）**：纳入 Codex 技术评审 5 项——① 状态库（SQLite）持久化方案；② 发布原子性按托管方式定义；③ GitHub 无 token/失败语义定稿；④ 热度聚合粒度与缺失源重归一化；⑤ 摘要缓存与月度花费账本。另调整「今日新品」数量口径：按窗口实际数量展示，不凑数。

## 1. 产品概述

### 1.1 一句话定位
每日自动更新的 AI 产品情报站：从 GitHub 与 Product Hunt 采集最新/热门 AI 产品与项目，经清洗、去重、分类、热度计算与中文摘要后，以网站形式呈现给中文 AI 从业者。

### 1.2 目标用户
中文 AI 从业者（开发者、研究者、产品/运营、创业者）。

### 1.3 用户价值
- 省去用户每天跨 GitHub / Product Hunt 两个平台手动翻找的时间。
- 跨源合并去重：同一产品在双源同时出现只展示一条，保留双来源链接。
- 中文摘要降低阅读门槛：非英文母语用户快速判断产品是否值得深入了解。
- 每日固定更新 + 更新状态可见，数据可回溯（1/7 日热度增量）。

### 1.4 核心场景
用户每天早上打开站点 → 看「今日新品」发现过去 24–48 小时的新东西 → 切「热门榜」看近期增长快的项目 → 按分类筛选 → 点进详情页读中文摘要和来源链接。

### 1.5 差异化（相对竞品）
不做大而全目录（同类有 GH Trending、PH Daily、各类 AI 导航站），聚焦组合：**双源合并去重 + 中文摘要 + 每日精选**。

---

## 2. 产品目标与成功标准

### 2.1 产品目标（MVP）
每天稳定产出当日情报批次，用户可在站点完成「发现 → 筛选 → 了解」闭环。

### 2.2 MVP 成功标准（可量化，全部满足即验收通过）
1. GitHub 全链路每日可重复运行，无需人工干预。
2. 热门榜最多 50 条；今日新品展示符合 48 小时窗口的实际数量（不凑数）；每条有分类、热度值、中文摘要（摘要开关开启时）。
3. 同一产品在 GitHub 与 Product Hunt 同时出现时能正确合并（去重生效）。
4. 连续 7 次快照后，热门榜使用真实 7 日指标；不足 7 天窗口的产品按实际窗口计算并明确标注，不伪造数据。
5. 任一数据源或 LLM 失败时安全降级，不阻断当天发布，页面展示各源状态与数据更新时间。
6. 发布产物不含任何 secret；真实浏览器可完成筛选、打开详情与外链跳转。

---

## 3. MVP 范围与非范围

### 3.1 MVP 范围
| 模块 | 内容 |
|------|------|
| 采集 | GitHub Search API（AI 相关 topic/关键词查询集）；Product Hunt GraphQL v2（feature flag 控制，凭据缺失/许可未确认时 degraded） |
| 清洗 | 无关仓库过滤、空描述过滤、字段规范化 |
| 去重 | 同源幂等 upsert；跨源保守合并（URL/域名/名称归一化） |
| 分类 | 10 个固定分类，规则词表打标（V1 纯规则，不依赖 LLM 分类） |
| 热度 | 「今日新品」与「热门榜」两个 Tab 独立排序；记录 score 组成 |
| 摘要 | DeepSeek API 生成中文摘要，内容哈希缓存，预算上限控制 |
| 展示 | 列表页（Tab + 分类筛选）+ 详情页；深色视觉稿落地；更新状态 + 各源状态展示 |
| 运行 | 定时任务每日一次，成功才发布新批次，失败保留上一批次 |

### 3.2 非范围（MVP 明确不做）
- 用户注册 / 收藏 / 评论
- 实时推送（Web/App 通知）
- 多语言（仅中文界面）
- 全文搜索（V1 仅分类筛选 + 排序）
- 移动端 App
- 历史趋势图表 / 数据导出
- 页面抓取作为数据源 fallback（合规与稳定性差，不做）

### 3.3 版本节奏
- **V1（本轮交付）**：GitHub 全链路可公开运行 + 静态站；PH provider 实现并 fixture 测试，默认 feature flag 关闭不公开；DeepSeek 摘要默认开启（可开关）。
- **V2（后续）**：PH 许可确认后开放 PH 数据、LLM 辅助分类、搜索、趋势图表。

---

## 4. 数据源与采集

### 4.1 GitHub（官方 REST Search API）
- 接口：`GET /search/repositories`（认证后搜索限额 30 次/分钟），仓库详情接口按需调用。
- 查询集：每日固定 8 条 query，覆盖 AI 主流领域：
  - `topic:artificial-intelligence`、`topic:llm`、`topic:ai-agent`、`topic:machine-learning`、`topic:rag`、`topic:multimodal`、`topic:ai-tools`、`topic:computer-vision`
- 每条 query 按 `stars` 排序取 top 100 候选（8 × 100 = 最多 800 候选，去重后远少于此）。
- 只对入选候选调用仓库详情；不拉取 commit 历史。
- 认证：只读、最小权限 token，环境变量 `GITHUB_TOKEN`；无 token 时全链路报错中止（GitHub 是 V1 关键路径）。
- 限流：每次响应记录 `x-ratelimit-remaining` / `x-ratelimit-reset`；`403`/`429` 按响应头退避重试；Search 与 core 限额分开处理。

### 4.2 Product Hunt（官方 GraphQL v2，feature flag）
- 接口：`https://api.producthunt.com/v2/api/graphql`，`Authorization: Bearer <token>`。
- 查询：`posts`（cursor 分页、`postedAfter` 过滤、`order: NEWEST/RANKING/VOTES`）。
- 字段：`name`、`tagline`、`description`、`createdAt`、`dailyRank`、`votesCount`、`commentsCount`、`website`、`url`、`topics`。
- 凭据：`PRODUCT_HUNT_CLIENT_ID` / `PRODUCT_HUNT_CLIENT_SECRET` 或 developer token。
- **合规**：官方条款默认禁止商业用途；当前个人工作台内部验证场景低风险。公开发布 PH 数据前必须向 `hello@producthunt.com` 邮件确认。
- 降级策略：凭据缺失 / 429 / 5xx → 标记 `degraded`，不影响 GitHub 链路与当天发布；页面展示 PH 源状态。

### 4.3 采集通用规则
- 每个 provider 独立实现，遵循统一 provider contract（`fetch_since` / `fetch_page` / `status`）。
- 每日快照：保存 `metric_snapshot`（当日 stars/forks/votes/rank 等），相邻快照差分计算真实增量。
- 幂等：按 `(source, external_id)` 唯一键 upsert，重复运行不产生重复记录。
- 密钥仅在后台定时任务环境变量中，绝不下发浏览器。

---

## 5. 数据清洗、去重与分类

### 5.1 清洗规则
1. 去空描述：GitHub 无描述且无 homepage 的仓库丢弃；PH 无 tagline 且无 description 的丢弃。
2. 相关性过滤（黑名单保守，宁放过不错杀）：
   - GitHub：描述/名称命中明显非 AI 关键词（如 `template`、`tutorial`、`book`、`awesome-list`、`dotfiles` 等）时降权或丢弃（具体词表 V1 提供初版，允许 Codex 调整并记录）。
   - PH：命中 `Freebie`、`Mockup` 等明显不相关内容丢弃（V2 完善）。
3. 字段规范化：名称 trim、URL 去尾部斜杠、域名小写去 `www.`、topic 全小写。
4. 收录门槛（「值得收」口径）：
   - GitHub：stars ≥ 50；或创建 ≤ 7 天且 stars ≥ 20 的新仓库（放宽以覆盖「最新」）。
   - PH：votesCount ≥ 30，或当日 dailyRank ≤ 50。
5. 展示上限：热门榜最多 50 条（热度 Top N）；今日新品按窗口实际数量展示，不足时不凑数；历史热门可补足热门榜。

### 5.2 去重规则（保守优先：宁可漏合，不可错合）
同源去重：`(source, external_id)` 唯一键，天然幂等。

跨源合并（GH + PH 同一产品），按优先级匹配，命中即合并：
1. 强匹配：GitHub repo URL 与 PH `url`/`website` 完全一致（如 PH 项目官网就是 GitHub 仓库页）。
2. 域名匹配：homepage 域名归一化后相同（`www`、协议、尾斜杠忽略）。
3. 弱匹配：名称归一化后完全相等（小写、去空格/连字符/常见后缀词如 `-ai`、`app`、`official`）→ **不自动合并，进入人工复核队列**。

合并规则：
- 合并后生成唯一 `product` 记录，保留双来源链接（`product_source` 映射表）。
- `is_primary` 标记主来源（信息更全者优先，默认 GitHub）。
- 弱匹配产品在站点「待复核」状态可见，由人工（或后续规则）决定。
- V1 不依赖 LLM 判断去重。

### 5.3 分类体系（10 类）
| ID | 分类 | 覆盖内容 |
|----|------|---------|
| agent | AI Agent | 智能体、AI 助手应用 |
| dev-tools | Developer Tools | 开发框架、CLI、代码工具 |
| infra-model | Infrastructure & Model | 模型、推理、向量库、训练平台 |
| content | Content & Media | 内容生成、视频、音频、写作 |
| design | Design & Creative | 设计、图像、创意工具 |
| productivity | Productivity | 效率办公、协作、笔记 |
| data-analytics | Data & Analytics | 数据分析、BI、监控 |
| research | Research & Science | 研究、论文工具、科学计算 |
| business | Business & Marketing | 商业、营销、客服、销售 |
| other | Other | 无法归类的兜底 |

分类规则（V1 纯规则）：
- 基于 topic 映射表（GitHub topics → 分类）优先。
- 其次基于名称/描述关键词映射表。
- 都不命中 → `other`。
- 规则词表以配置文件维护（`category_rules.json`），便于调整；V2 可引入 LLM 辅助。

---

## 6. 热度计算

两个 Tab 使用独立排序，避免混合公式的玄学：

### 6.1 今日新品 Tab
- 主排序：产品首次入库时间/发布时间降序（展示过去 24–48 小时新出现产品）。
- 次级排序：各自源热度指标（GH stars、PH votes）。

### 6.2 热门榜 Tab
```
score = 0.6 * github_score + 0.4 * ph_score   (PH 数据可用时)
score = github_score                          (PH 不可用时)
```
各源先归一化（min-max 到 0–100），避免绝对量级偏差：
```
github_score = 0.7 * norm(7日 stars 增量) + 0.3 * norm(7日 forks 增量)
ph_score     = 0.7 * norm(votesCount) + 0.3 * norm(1/dailyRank 反向)
```
新鲜度衰减（可选开关，V1 默认开启简单版）：
```
score_final = score * exp(-age_days / 30)
```
- 数据窗口：优先 7 日增量；快照不足 7 天的产品按实际窗口计算，**并在详情页标注数据窗口天数**，禁止伪造完整周期。
- **聚合粒度**：归一化分母 = **当日最终入选的 product 集合**（而非全部候选）。每个 product 取其所映射 source_item 的对应指标：GitHub 分量取主 GitHub source_item 的快照增量；PH 分量取 PH source_item 的 votes/rank；双源产品分别计算分量后按权重组合。
- **缺失源处理**：PH 关闭/降级时只计算 `github_score`，以单源 score 作为最终 score，并在 `score_breakdown` 标注「仅 GitHub 计分」；不强行补 PH 分量，也不改变归一化分母。
- `score_breakdown` 记录：原始值、归一化值、权重、窗口天数、计分来源列表，便于后续调权。
- 全部权重集中为配置项（`heat_config.json`），Codex 可读配置实现。

---

## 7. 中文摘要（DeepSeek）

- 模型：`deepseek-chat`（低成本）。
- 输入：产品名称 + 描述/tagline + 来源链接（V1 不喂 README 全文，控制成本与版权风险）。
- 输出：中文摘要 60–100 字，固定 prompt 模板（客观陈述，注明是第三方摘要）。
- 缓存：按输入内容 `content_hash` 唯一；仅首次入库或描述实质变化时调用 API；重复构建零成本。
- 成本控制：月度预算上限（默认 ¥10–20，配置文件可调），按 `summary_usage` 账本累计实际花费（estimated_cost），达到上限自动关闭摘要，降级为「描述直出」并标注「未生成摘要」。每次调用记录 tokens 与预估费用到 `summary_cache`（表结构见第 8 节）。
- feature flag：`ENABLE_SUMMARY`（V1 默认开）；LLM 超时/失败 → 跳过该条摘要，不阻塞整批发布。
- `DEEPSEEK_API_KEY` 仅后台环境变量，绝不出现在静态产物/前端。

---

## 8. 数据模型（SQLite）

### 8.1 表结构
```sql
-- 原始采集记录（幂等键: source + external_id）
CREATE TABLE source_item (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,          -- 'github' | 'producthunt'
  external_id   TEXT NOT NULL,
  raw_json      TEXT,                   -- 原始响应保留，可回溯
  name          TEXT NOT NULL,
  description   TEXT,
  url           TEXT,                   -- 原始链接
  homepage      TEXT,
  topics        TEXT,                   -- JSON 数组
  content_hash  TEXT,                   -- 描述内容哈希（摘要缓存键）
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  UNIQUE(source, external_id)
);

-- 每日指标快照（差分计算 1/7 日增量）
CREATE TABLE metric_snapshot (
  id            INTEGER PRIMARY KEY,
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  snapshot_date TEXT NOT NULL,          -- YYYY-MM-DD
  stars         INTEGER,
  forks         INTEGER,
  votes_count   INTEGER,
  comments_count INTEGER,
  daily_rank    INTEGER,
  UNIQUE(source_item_id, snapshot_date)
);

-- 合并后的产品
CREATE TABLE product (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  summary_zh    TEXT,                   -- DeepSeek 中文摘要
  summary_status TEXT DEFAULT 'pending',-- 'pending'|'ok'|'skipped'|'failed'
  category      TEXT DEFAULT 'other',
  heat_score    REAL,
  score_breakdown TEXT,                 -- JSON: 各分量 + 窗口天数
  first_seen_at TEXT,
  last_updated_at TEXT
);

-- 产品与来源的映射（合并保留双链接）
CREATE TABLE product_source (
  product_id    INTEGER NOT NULL REFERENCES product(id),
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  source        TEXT NOT NULL,
  is_primary    INTEGER DEFAULT 0,
  UNIQUE(product_id, source_item_id)
);

-- 每日运行日志（各源状态）
CREATE TABLE run_log (
  id         INTEGER PRIMARY KEY,
  run_date   TEXT NOT NULL,
  provider_status TEXT,                 -- JSON: {"github":"ok","producthunt":"degraded","summary":"ok"}
  items_seen INTEGER,
  items_new  INTEGER,
  errors     TEXT
);

-- 摘要缓存（输入统一按跨源合并后的 product 内容哈希）
CREATE TABLE summary_cache (
  content_hash   TEXT PRIMARY KEY,
  summary_zh     TEXT,
  status         TEXT DEFAULT 'ok',    -- 'ok' | 'failed'
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  estimated_cost REAL,
  created_at     TEXT NOT NULL
);

-- 摘要月度花费账本
CREATE TABLE summary_usage (
  month          TEXT PRIMARY KEY,     -- YYYY-MM
  estimated_cost REAL NOT NULL DEFAULT 0
);
```

### 8.2 状态库（SQLite）位置与持久化（已定稿：方案 A）
- **本地开发模式**：库文件位于项目外私有目录（如 `../ai-info-web-data/`，可配置），不进 git、不进 public。
- **生产模式（方案 A）**：GitHub-hosted runner 为临时环境；GitHub Actions 每轮开始前从**私有状态仓库**（专门承载 DB 的 GitHub private repo）下载/检出 DB 文件，运行成功后提交写回；**DB 绝不进入公开 Vercel/Pages 产物**。
- 对象存储（OSS/COS）与云服务器本机 cron 为后续迁移选项；存储层做抽象，切换不改业务代码。

### 8.3 发布批次
- 每次构建生成批次目录（`public/batches/YYYY-MM-DD/`），当前批次通过软链/固定路径 `public/latest` 原子切换；构建失败保留上一批。
- 静态数据文件：`public/data/products.json`（列表用，含摘要/分类/热度/来源）、`public/data/categories.json`、`public/data/status.json`（数据更新时间 + 各源状态）。

---

## 9. 展示层（前端）

### 9.1 信息架构
- **列表页**（首页）：
  - 顶部：站点名 + 数据更新于（日期 + 各源状态徽标 ok/degraded）。
  - Tab 切换：「今日新品」/「热门榜」。
  - 分类筛选条：10 类 + 全部。
  - 产品卡片：名称、中文摘要（或描述直出）、分类标签、热度值、来源徽标（GitHub/PH/双源）、入库时间。
- **详情页**（`/p/<id>`）：
  - 完整信息：名称、摘要、描述、分类、热度值与 score 组成（含数据窗口天数）、来源链接（GitHub 仓库 / PH 页面 / 官网）、数据更新时间。
  - 未生成摘要时显示「未生成摘要」标注。
- 底部：数据来源声明（GitHub 公开数据、Product Hunt API；PH 数据未公开时说明）。

### 9.2 技术要求
- 静态站：构建时生成页面，客户端仅处理当日有限数据集（筛选/排序），无需常驻 API。
- 视觉：以 `OUTBOX/AI_PRODUCT_RADAR_UI_DRAFT.html`（深色系视觉稿）为基础落地。
- 文案：中文界面；所有文案清晰说明数据来源与更新时间。

---

## 10. 技术架构与部署

### 10.1 架构（文字版）
```
GitHub Search API ─┐
                   ├→ provider 采集 → source_item + metric_snapshot 落库(SQLite)
Product Hunt API ──┘        │
                            ▼
                    清洗 → 去重合并(product) → 规则分类 → 热度计算 → 中文摘要(DeepSeek,缓存)
                            ▼
                    生成 public/data/*.json + 静态页面 → 原子切换批次
                            ▼
                    定时任务每日 00:30 UTC（北京时间 08:30）
```

### 10.2 技术栈
- 语言/运行时：Python 3.11+（数据管道）或 Node 20+（如前端工具链一致可统一 Node）——由 Codex 根据统一性选择，方案不锁死。
- 存储：SQLite（单文件，零运维）；量级上来后迁移 Postgres（V2）。
- 前端：静态站生成（Astro / Vite + 静态导出），以视觉稿为准。
- 调度：GitHub Actions `schedule`（cron）每日运行；本地亦可通过 `make run-daily` 手动触发。

### 10.3 部署（用户已确认：需要公网访问）
- **展示层**：静态站部署到 Vercel（免费、自带域名/CDN）；如后续使用阿里云/腾讯云服务器，也可部署在其上（Nginx 托管静态产物）。Vercel 仅托管静态产物，**不承担采集/定时任务**（serverless 无持久文件系统、执行时长受限）。
- **采集调度层（已定稿：方案 A）**：GitHub Actions `schedule` 定时运行（每日 00:30 UTC）+ **私有状态仓库**承载 SQLite（每轮开始前从私有仓库下载 DB，运行成功后提交写回；DB 绝不进公开产物）。对象存储（OSS/COS）与云服务器本机 cron 留作后续迁移选项（存储层做抽象，切换不改业务代码）。
- **发布原子性（按托管方式定义）**：本地模式先完整生成到隔离临时目录并通过校验后，一次性替换当前产物目录；Actions 模式在隔离目录完整生成并验证（含无 secret 检查）后，以**一次 deployment / 单个发布提交**切换；任一步失败不触发 deployment，托管平台自然保留上一版本。`public/latest` 软链仅适用于本地文件系统，不作为托管发布机制。
- SQLite 为构建私有状态，**不得**被静态托管直接暴露（仅导出 public/ 下的 JSON/页面）。

---

## 11. 安全与合规

### 11.1 密钥管理
- `GITHUB_TOKEN`、`PRODUCT_HUNT_CLIENT_ID/SECRET`、`DEEPSEEK_API_KEY` 仅存在于后台环境变量 / Actions Secrets。
- 静态产物、前端 bundle、仓库、日志中一律不含任何密钥；验收时检查发布产物。

### 11.2 合规
- GitHub：仅使用公开仓库元数据；不整文复制分发 README/代码（只做元数据 + 自生成摘要 + 来源链接）。
- Product Hunt：个人内部验证低风险；**公开发布 PH 数据前**必须邮件确认商业许可，并在站点展示来源归属。
- DeepSeek：摘要输出归用户；输入为公开项目描述，无合规问题。

### 11.3 成本
- DeepSeek 摘要：月度预算上限默认 ¥10–20，超限自动关闭并降级。
- GitHub API：免费配额内（每日 8 query + 少量详情调用，远低于限额）。
- 托管：静态托管免费层，V1 零托管成本。

---

## 12. 开发任务拆分（交付 Codex）

以下任务按依赖顺序编号。每项验收标准与总体验收（第 13 节）共同构成交付判定。

### T1 项目脚手架与数据模型
- **背景**：所有后续任务依赖统一目录结构与 schema。
- **目标**：建立可运行的项目骨架与数据库层。
- **范围**：目录结构（`src/`、`tests/`、`config/`、`public/`）、第 8 节表结构 DDL、SQLite 连接/迁移、配置加载（环境变量 + JSON 配置）、`make`/脚本入口。
- **非范围**：任何 provider 逻辑、前端页面。
- **输入**：本方案第 8 节。
- **输出**：可初始化空库并跑通 smoke test 的骨架代码。
- **业务规则**：幂等 upsert 帮助函数；时间统一 UTC。
- **边界情况**：数据库文件不存在自动创建；重复执行迁移幂等。
- **验收**：`make init` 建库成功；测试通过；schema 与第 8 节一致。
- **验证方法**：Codex 运行初始化 + 测试，报告输出。

### T2 GitHub provider
- **背景**：GitHub 是 V1 关键路径数据源。
- **目标**：每日可靠采集候选仓库并落快照。
- **范围**：Search API 查询集（第 4.1 节）、分页、详情调用、`source_item` upsert、`metric_snapshot` 写入、限流退避。
- **非范围**：清洗/去重/分类（后续任务）。
- **输入**：`GITHUB_TOKEN`、查询集配置。
- **输出**：`source_item` + `metric_snapshot` 数据；provider 状态。
- **业务规则**：按 `(source, external_id)` upsert；每日只追加当日快照。
- **边界情况**：403/429（退避）、搜索返回空、网络超时重试。
- **无 token / GitHub 失败语义**：GitHub 是关键源——首次运行无 token 或 GitHub 失败 → 整链状态 `failed`，**不发布**；已有成功批次时 → 保留上一批并记录失败原因。不允许以 degraded 继续发布（区别于 PH/LLM）。
- **验收**：连续 3 次运行无重复记录；快照日期正确；限流时安全退避；无 token 时状态为 failed 且不发布（有历史批次则保留上一批）。
- **验证方法**：Codex 用 token 实跑 + fixture 测试，报告运行结果与消耗的 API 调用数。

### T3 Product Hunt provider（feature flag）
- **背景**：双源去重是差异化核心；PH 凭据/许可未就绪不应阻塞。
- **目标**：实现可插拔 PH provider，默认不公开。
- **范围**：GraphQL `posts` 查询、cursor 分页、`source_item` upsert、快照；`ENABLE_PRODUCT_HUNT` flag；凭据缺失/失败时 `degraded` 状态。
- **非范围**：PH 数据公开展示（需许可确认）、页面抓取。
- **输入**：`PRODUCT_HUNT_*` 凭据（可选）。
- **输出**：PH provider 状态（ok/degraded）；数据入库（开启时）。
- **业务规则**：凭据缺失或 429/5xx → degraded 而非报错；低频批处理 + 指数退避。
- **边界情况**：无凭据、限流、GraphQL 错误、分页游标失效。
- **验收**：fixture 测试通过；无凭据时整链路由 run_log 显示 `producthunt: degraded` 且 GitHub 链路正常。
- **验证方法**：Codex fixture 测试 + 无凭据端到端跑一次。

### T4 清洗、去重与分类
- **背景**：决定内容质量的核心环节。
- **目标**：实现第 5 节全部规则。
- **范围**：清洗过滤、收录门槛、跨源合并（强/域名/弱匹配 + 人工复核队列）、分类规则词表。
- **非范围**：LLM 分类、自动处理弱匹配合并。
- **输入**：`source_item` 数据。
- **输出**：`product` + `product_source`；弱匹配复核队列文件。
- **业务规则**：按第 5 节；去重保守（弱匹配不自动合并）。
- **边界情况**：同源重复数据、空字段、URL 变体（www/协议/尾斜杠）、名称大小写差异。
- **验收**：同一官网域名的 GH/PH 项目合并且保留双链接；重复运行不新增记录；分类准确率抽检 ≥ 80%（人工抽 20 条）。
- **验证方法**：Codex 用 fixture + 真实数据跑清洗去重，提供抽检样例与结果。

### T5 热度计算
- **背景**：排序是站点核心体验。
- **目标**：实现第 6 节双 Tab 排序与 score 分解。
- **范围**：7 日增量计算（不足窗口标注）、归一化（分母 = 当日最终入选 product 集合）、双源合并后 product 的指标聚合口径、PH 缺失时单源计分（标注「仅 GitHub 计分」）、组合公式、新鲜度衰减、`score_breakdown` 输出（原始值/归一化值/权重/窗口天数/来源列表）。
- **非范围**：调权（权重为配置，后续可调）。
- **输入**：`metric_snapshot` + `product`。
- **输出**：`product.heat_score` + `score_breakdown`。
- **业务规则**：窗口不足 7 天按实际窗口并标注；PH 不可用时单源计分。
- **边界情况**：新入库无历史快照、快照缺失、全部为 0 增量（归一化除零保护）。
- **验收**：单元测试覆盖归一化/除零/窗口标注；榜单排序合理（人工抽查）。
- **验证方法**：Codex 单测 + 真实数据跑榜，附 Top10 样例。

### T6 DeepSeek 中文摘要
- **背景**：中文摘要为差异化卖点。
- **目标**：实现摘要生成、缓存与成本控制。
- **范围**：`deepseek-chat` 调用、prompt 模板、content_hash 缓存（落 `summary_cache` 表）、月度花费账本（`summary_usage` 表）、预算上限、`ENABLE_SUMMARY` flag、失败降级。
- **非范围**：README 全文输入、其他模型。
- **输入**：`DEEPSEEK_API_KEY`、产品名称/描述。
- **输出**：`product.summary_zh` + `summary_status`。
- **业务规则**：摘要 60–100 字；同 content_hash 不重复调用；预算超限关闭摘要并降级「描述直出」。
- **边界情况**：LLM 超时/5xx（跳过该条）、空描述、预算耗尽、key 无效。
- **验收**：缓存生效（同内容第二次不调用 API）；预算上限生效；失败不阻塞发布。
- **验证方法**：Codex 用真实 key 小批量（≤10 条）验证 + mock 测试，报告消耗费用估算。

### T7 定时编排、静态站与部署
- **背景**：让每天的数据稳定变成可访问的站点。
- **目标**：全链路自动化 + 静态站生成 + 部署就绪。
- **范围**：GitHub Actions `schedule` 工作流（每日 00:30 UTC）、状态库持久化（Actions 模式每轮从私有状态仓库恢复/写回）、发布原子性（隔离目录生成 → 验证 → 单次 deployment 切换，失败不部署）、`public/data/*.json` 生成、列表/详情页生成、部署（Pages/Vercel，待用户确认）、页面更新状态与各源状态展示。
- **非范围**：常驻 API、用户体系。
- **输入**：T1–T6 产物。
- **输出**：可访问的静态站 + 定时流水线 + 部署文档。
- **业务规则**：成功构建才发布；页面展示「数据更新于」+ 每源状态；SQLite 不暴露。
- **边界情况**：构建中途失败（保留上一批）、Actions 超时、部署目标未配置（本地可跑）。
- **验收**：端到端跑通一次（采集→展示）；连续 2 次运行快照不丢失（Actions 模式验证 DB 恢复/写回）；构建中途失败时上一版本保持可访问；真实浏览器可筛选、打开详情、外链跳转；发布产物 grep 无 secret；断网/无 key 场景按 T2 语义（failed 不发布 / 保留上一批）处理。
- **验证方法**：Codex 本地端到端 + 真实浏览器检查，返回文字版结论（检查页面、视口、问题、修改、验证结果、截图本地路径）。

---

## 13. 总体验收清单（评审用）

| # | 验收项 | 判定方式 |
|---|--------|---------|
| 1 | GitHub 全链路每日可重复运行；无 GH token/GitHub 失败：首次 failed 不发布，已有批次保留上一批 | 连续 3 次运行记录 + 场景测试 |
| 2 | 热门榜最多 50 条，今日新品按窗口实际数量，含分类/热度/摘要 | 数据文件抽检 |
| 3 | 跨源去重生效（双源项目合并） | fixture + 真实数据验证 |
| 4 | 7 日热度窗口真实、不足窗口有标注 | 数据文件检查 |
| 5 | 四种降级场景（无 GH token / PH token 缺失 / PH 429 / LLM 超时）安全完成或降级 | Codex 场景测试报告 |
| 6 | 重复执行不新增重复记录 | 幂等测试 |
| 7 | 发布产物无 secret | 产物 grep 检查 |
| 8 | 浏览器可用性（筛选/详情/外链） | Codex 文字版视觉报告 |
| 9 | 页面展示数据更新时间与各源状态 | 页面检查 |
| 10 | Actions 模式状态库跨运行持久化（快照不丢失，7 日热度可用） | 连续运行记录检查 |
| 11 | 发布原子性：构建失败保留上一版本，成功才切换 | 故障注入测试 |

---

## 14. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| PH 商业许可未确认 | 公开部署合规风险 | 默认不公开 PH 数据；上线前邮件确认 |
| GitHub 搜索限流 | 采集失败 | 只读 token + 响应头退避 + 控制 query 数 |
| DeepSeek 成本失控 | 费用超支 | content_hash 缓存 + 月度预算上限 + 自动降级 |
| 去重误合并 | 展示错误产品信息 | 保守规则，弱匹配人工复核 |
| 数据源政策/接口变化 | 链路中断 | provider 独立可替换 + 降级不阻塞 |
| 模型不支持图片（协作约束） | 视觉验收受阻 | 视觉检查由 Codex 文字版报告，主频道纯文本 |

---

## 15. 待用户确认事项

1. ✅ **部署目标**：已确认需要公网访问；展示层计划部署到 Vercel（免费）或阿里云/腾讯云。
2. ✅ **状态库承载方式**：已确认方案 A —— GitHub Actions + 私有状态仓库承载 SQLite（零成本）。对象存储/云服务器 cron 留作后续迁移。
3. **每日收录量**：热门榜上限 50 条，新品按实际窗口数量（默认按此执行，可后续调整）
3. **每日运行时间**：默认 00:30 UTC（北京 08:30），可调整。
4. **摘要预算上限**：默认 ¥10–20/月，可调整。
5. **PH 数据公开节奏**：默认 V1 不公开 PH 数据，确认后按此执行。

---

## 附：协作约定（主频道纯文本）

- 本频道只允许纯文本（需求/方案/字段/任务/评审结论/文件路径）。
- 禁止图片、截图、image_url、base64 等一切多模态内容进入频道上下文（评审模型不支持图片，会导致请求 400）。
- 视觉检查由 Codex 在本地完成，仅返回文字版结论（页面、视口、问题、修改、验证、截图本地路径）。
- 每任务最多两轮评审；费用/正式部署/API Key/重大范围调整先经用户确认。
