# 设计决策记录（ADR）

> Mikasa 的"为什么"档案。配套代码：`src/mikasa/` 各模块头注释中的
> `ADR-XXXX` 引用即本文件条目；本文与 [`architecture.md`](architecture.md)（怎么组织的）
> 互补，是本文档的主体：**为什么这么做、不那样做**。
>
> 本文件于 M3 收工后（2026-09-09）补记完成；条目日期为所属里程碑，
> 精确决策日期当时未逐日记录，不假装有。

## 阅读约定

- 编号规则：ADR 号在**首个落地的代码文件**撰写时随手编定并写进注释，因此
  不是严格时间序，以代码注释引用为准；本文补写时未重排编号，避免破坏
  全仓库既有引用；
- **ADR-0005 编号空缺**：规划期草案跳过，从未落地任何决策，代码零引用，
  保留空洞（重排会让既有注释全部失联，不值得）；
- 状态：`Accepted`（现行）/ `Superseded`（被后续决策取代，保留记录价值）。
  被取代的决策不删除——技术评审里"决策如何被数据推翻"往往比决策本身更值钱。

| 编号 | 主题 | 状态 |
| --- | --- | --- |
| ADR-0001 | 三套 profile 配置（api / local / offline） | Accepted |
| ADR-0002 | 密钥只走环境变量，不进 YAML | Accepted |
| ADR-0003 | Provider Protocol + OpenAI 兼容协议统一三端 | Accepted |
| ADR-0004 | SQLite schema_version 最小迁移 | Accepted（M4.5 修订，见条目） |
| ADR-0005 | （规划期编号空缺，见阅读约定） | — |
| ADR-0006 | openai SDK 版本锁定与 TLS 逃生口 | Accepted |
| ADR-0007 | 文本清洗哲学：原文保真，不转标点 | Accepted（初版 Superseded） |
| ADR-0008 | 中文分词：双实现 + 自动降级 | Accepted |
| ADR-0009 | 自实现 BM25，不引 FTS5 / Elasticsearch | Accepted |
| ADR-0010 | numpy 精确向量检索，不引 FAISS / hnswlib | Accepted |
| ADR-0011 | RRF 融合，不做分数加权 | Accepted |
| ADR-0012 | 结构化分块器：标题边界 + 语义切分优先级 | Accepted |
| ADR-0013 | free 自由问答旁路：与 kb/评测解耦，mode 不落库 | Accepted |
| ADR-0014 | local profile 落地：免密钥占位 + 本地重排/裁判暂关 + 维度迁移纪律 | Accepted |
| ADR-0015 | 会话管理升级：文件夹树 + 标题三路径锁 + suggest 降级语义 | Accepted |

---

## ADR-0001 三套 profile 配置（api / local / offline）

- 状态：Accepted ｜ M0（2026-09）

**问题**：同一套代码要跑三种形态——云端 API（真实模型）、本地推理
（Ollama + CPU 嵌入）、零密钥离线（演示 / 测试 / CI）。若把端点、模型名
散落在代码里，切换形态要改代码，密钥管理也没有单一入口。

**决策**：`config/profiles/{api,local,offline}.yaml` 三套小文件 +
`config.example.yaml` 全字段注释手册；运行时 `--profile` 切换，profile 只写
覆盖字段，与默认配置 `_deep_merge` 递归合并。所有模型名、端点、批次参数
一律进 YAML，**代码零硬编码**（`deepseek-chat` 预告弃用时一行配置即可换）。
配置模型全部用 pydantic v2 `frozen=True`，构造即校验，类型即文档。

**后果**：三形态切换 = 一个 flag；代价是配置面变大，由 `config.example.yaml`
充当字段手册兜底。评测也因此天然支持"同题库、多口径"（见 evaluation.md
三阶段口径——A/B 阶段刻意用不同配置跑，皆出自本机制）。

**代码**：`src/mikasa/config/settings.py`；`config/profiles/*.yaml`。

## ADR-0002 密钥只走环境变量，不进 YAML

- 状态：Accepted ｜ M0（2026-09）

**问题**：YAML 配置里写字面密钥，等于把密钥和代码一起提交；三 profile
又要共享同一份密钥注入方式。

**决策**：配置字符串支持 `${ENV_VAR}` 与 `${ENV_VAR:-默认值}` 占位符展开
（`settings.py::expand_env_vars`，递归作用于 dict/list/str）。密钥字段只留
`api_key_env`（期望的环境变量名），实际值由 `.env`（python-dotenv 加载）
或系统环境变量提供。模型名可以默认值进 YAML，密钥永不。

**后果**：`.env` 与 `.env.example` 分离，仓库可安全公开；doctor 体检能
"只报缺哪个变量名"而不接触值。

**代码**：`src/mikasa/config/settings.py`；根目录 `.env.example`。

## ADR-0003 Provider Protocol + OpenAI 兼容协议统一三端

- 状态：Accepted ｜ M0-M1（2026-09）

**问题**：生成端有"云端 API、本地 Ollama、离线 mock"三类，嵌入端与重排端
同理；若为每端写专用调用代码，provider 会开成花，且评测/离线与真实链路
会分叉（"mock 能跑、真实跑挂"是最糟的分叉）。

**决策**：
1. 每类能力定义一个 `Protocol`（`LLMProvider` / `EmbeddingProvider` /
   `RerankerProvider`），外部代码只依赖协议，不感知实现；
2. **api 与 local 共用同一个实现**：DeepSeek、SiliconFlow、Ollama 全兼容
   OpenAI Chat Completions 协议（Ollama 提供 `/v1` 兼容端点），一套 HTTP
   客户端打穿，local 与 api 只差 base_url + model；
3. mock（`MockLLM`）是协议的完整实现：确定性输出、零密钥，跑通检索栈
   与引用协议的整条链路——质量由真实模型保证，协议正确性由 mock 在 CI
   每天保证；
4. `complete()` 返回 `Completion`（文本 + token 用量）——用量字段进协议
   而非旁路回调，因为评测报告与问答成本核算都依赖它。

**后果**：三端行为同构只差配置；评测阶段 B 用 mock 在 CI 离线回归引用协议，
阶段 C 才上真实模型。代价：mock 无语义（它只演示协议），因此有"offline
场次是协议层自检而非真实质量"的诚实限制（evaluation.md §5）。

**代码**：`src/mikasa/providers/{llm,embedding,reranker}.py`、`providers/__init__.py`（工厂）。

## ADR-0004 SQLite schema_version 最小迁移

- 状态：Accepted ｜ M1（2026-09）

**问题**：`mikasa.db` 随功能演进要加表加列；用户升级后旧库可能因缺列
静默坏掉或跑出错误结果。引入成熟迁移框架（alembic 等）对单人项目过重。

**决策**：`schema_version` 表记录当前版本；启动时版本不匹配**直接报错**
（"请删除旧库重跑 ingest"或走 CLI 迁移命令），绝不静默 "CREATE TABLE IF
NOT EXISTS" 假装兼容。版本常量当前为 1——一次成型，不假装有过多次迁移。

**后果**：升级路径是显式的、人会看见的；库结构一致性由硬检查兜底，
不存在"半迁移"状态。代价：早期迭代期版本升级要重建（ingest 幂等，
重建成本可控——见 architecture.md"入库链路"）。

**代码**：`src/mikasa/storage/db.py`；`schema_version` 校验逻辑。

---

**修订决策（M4.5，2026-09-09）**：会话管理升级（标题列/文件夹表）是
首次真实迁移——重建的代价第一次超过迁移本身（27 会话/88 消息是用户
实据），决策随之修订为**双轨**，原始精神不变：
- **低版本库 → 自动逐级迁移**（不再报错删库）：`_MIGRATIONS = {目标版本:
  迁移函数}` 沿链执行——每级内部幂等（ALTER 用 `PRAGMA table_info` 守卫、
  回填按 `title IS NULL` 条件续跑，半途崩溃后重跑可从已完成处续走）、
  每级独立提交一次 + `logger.info` 留痕；库内版本停留在"最后成功级"，
  不存在版本行先行的半迁移窗口；
- **高版本库 → 硬报错原样保留**：旧程序绝不读写新库（防止新列语义被
  旧代码误解，如 v2 的 title_manual 锁）；版本断层（缺迁移路径）同样
  硬报错并停在原地，数据不损坏；
- **新增约束**：schema 终态 SQL、历史版本快照（`_SCHEMA_V1_SQL`，
  immutable）、迁移函数三者同 PR 落地；`_SCHEMA_SQL` 终态**只在全新库**
  执行，历史库只走迁移函数（终态 SQL 全 IF NOT EXISTS，在旧库上是
  空操作、绝不给旧表补列——补列是 ALTER 的活）。
- **状态**：M4.5 条目仍 Accepted（初版"删库重来"段被本节取代）。

**代码**：`src/mikasa/storage/db.py`（init_db 三分支 + `_upgrade` +
`_migrate_v1_to_v2`）；迁移测试见 `tests/unit/storage/test_db_migration.py`。

## ADR-0006 openai SDK 版本锁定与 TLS 逃生口

- 状态：Accepted ｜ M1（2026-09）

**问题**：openai SDK 3.0 起更换底层传输层（httpx → 内置 httpx2），API 层
不变但行为与依赖面全变；个别环境（公司代理 / 老旧 TLS 栈）可能连不上。

**决策**：锁 `openai>=3.3,<4`（新传输层为主路径），pyproject 注释写明：
若遇 TLS 问题可退回 `openai<3` 的逃生口——这是**配置级**的退回，
代码只用 Chat Completions 稳定 API，与 SDK 大版本解耦。
另一条连带结论：3.x 内置的 httpx2 与经典 `httpx` **是两个共存包**，
starlette `TestClient` 需要经典 httpx 传输层，因此 dev 依赖显式声明
`httpx>=0.27,<1`——两个包名共存无冲突，别删。

**后果**：依赖策略 = "锁定已知良好的区间 + 每个区间留下退路 + 代码只用
跨版本稳定 API"。同类策略见 ADR-0008（jieba）——该项目统一不信任
"锁死单版本"能解决问题。

**代码**：`pyproject.toml` 版本锁定说明段（含注释）。

## ADR-0007 文本清洗哲学：原文保真，不转标点

- 状态：Accepted（初版 Superseded）｜ M1（2026-09）

**问题**：Windows 环境的中文文档自带三坑——GBK 等历史编码、PDF 页眉页脚
残留、粘连重复行（PDF 抽取的逐行文本）。入库前"洗不洗、洗多重"直接决定
下游展示质量。

**初版决策（已 Superseded）**：做**全角→半角归一**（`０-９`、`Ａ-Ｚ` 映射），
理由是想当然的"检索侧标点越少越好"。落地前即被评审推翻：

**修订决策（现行）**：清洗只做"不改变语义"的格式收敛——首尾与折叠空白、
控制字符过滤、空行清理；**不做任何全半角标点转换**。理由来自下游依赖
的实证：
1. 引用溯源展示与分块断句都依赖原文排版（`。"` 断开后句子边界即变）；
2. 检索侧分词器（jieba / bigram）天然忽略标点，转换对召回零收益；
3. 转换只服务"看起来整齐"，破坏的是展示真实性与可复现性。
另把编码探测收敛在此（GBK 等历史编码在此转 UTF-8），页眉页脚与重复行
问题在 loader 层解决（那是"行"的语义，不是"字符"的语义）。

**后果**：全仓库只有一处清洗；中文标点（""，。…）随原文保存。教训存档：
"清洗力度要由下游消费者（展示、断句、分词）逐层论证，不能凭整洁直觉"——
评审常问的过度清洗反例。

**代码**：`src/mikasa/utils/text.py`。

## ADR-0008 中文分词：双实现 + 自动降级

- 状态：Accepted ｜ M1（2026-09）

**问题**：项目要中文分词。jieba 是事实标准，但 2026 年起新版 setuptools
移除 `pkg_resources`，`import jieba` 在新环境必崩（jieba#1043；官方认可的
临时方案是锁 `setuptools<82`）。若直接依赖 jieba，"装得上就能跑"的假设
在用户机器上随时会碎；若只锁版本，则是把项目命运押在单一临时方案上。

**决策**：三层防御而非单点依赖——
1. `Tokenizer` 定义为**函数协议**（`Callable[[str], list[str]]`），调用方
   不感知实现；
2. 双实现：jieba（质量优先）与纯 Python 字符 bigram（零依赖、确定性）；
   jieba 导入失败（ImportError，真实故障；弃用 UserWarning 被静音不屏蔽）
   → 自动降级 bigram，探针结果缓存；
3. 版本上仍按官方方案锁 `jieba==0.42.1 + setuptools<82`，但作为**性能
   优化而非生存依赖**——方案失效最坏退回 bigram，不致命；
4. 分词质量影响被量化：bigram 与 jieba 对检索指标的差距在评测中实测
   并披露（evaluation.md）。

**后果**：doctor 能明确报告"jieba 不可用，已降级 bigram"（见
limitations-and-failures.md），而不是用户悄悄拿到降级质量还不知情。

**代码**：`src/mikasa/index/tokenizer.py`；`cli doctor` jieba 检查。

## ADR-0009 自实现 BM25，不引 FTS5 / Elasticsearch

- 状态：Accepted ｜ M1（2026-09）

**问题**：稀疏检索路选型。SQLite FTS5 现成、Elasticsearch 是工业标配，
但两者各有不适：FTS5 的默认中文分词基本不可用（需自挂 tokenizer，等于
半自实现）；ES 对个人知识库是大炮打蚊子（JVM + 运维）。

**决策**：手写 Okapi BM25（约 100 行）：
1. **教学与面试价值**：IDF、词频饱和、长度归一化（k1/b）是信息检索
   经典内容，公式可见可调，评测里可跑 ablations（这决定取舍——项目是
   展示型项目，算法透明是资产不是债务）；
2. **规模匹配**：个人库数千 chunk，全量线性打分实测 <10ms，倒排表的
   复杂度收益为零；
3. **性能工程**：入库时对全文**预分词落盘**（`chunks.tokens`，JSON），
   查询时只分词 query，构建 O(总词条数)、查询 O(文档数 × query 词条数)；
   文档按 chunk_id 升序固定顺序索引，返回 [行号, 分数]。
平滑 IDF 公式取 `ln(1 + (N-df+0.5)/(df+0.5))` 避免负分。

**后果**：换来一个完全可讲的 IR 模块与干净的消融实验面；代价是放弃
FTS5 的成熟边界（同义词等，个人库用不上）。

**代码**：`src/mikasa/index/bm25.py`。

## ADR-0010 numpy 精确向量检索，不引 FAISS / hnswlib

- 状态：Accepted ｜ M1（2026-09）

**问题**：稠密向量路需要检索引擎。FAISS / hnswlib 是标准答案，但本项目
约束"Windows 零编译安装"——二者在 Windows 上均无预编译 wheel
（faiss-cpu 仅 conda；hnswlib 仅源码），引它们 = 要求用户装编译器。

**决策**：numpy 精确（暴力）检索：行归一化矩阵 + 矩阵乘，dot 即余弦。
1. **规模论证**：个人库 <10 万 chunk，numpy 矩阵乘实测 <500ms——ANN 的
   加速收益在此规模下不成立；
2. **精确检索的额外红利**：无近似误差，评测里的 recall 是"真"recall，
   实验结论干净（近似索引会让消融结论混入索引误差）；
3. 内存账：n × dim × 4 字节，1 万 chunk × 512 维 ≈ 20MB，个人库无压力；
4. 接口定义为 `VectorStore` Protocol（runtime_checkable），保留日后切
   ANN 后端的扩展点。

**后果**：换掉了"库选型高级感"，换来零编译安装、精确可复现的评测面。
与 ADR-0009 同构：**在规模论证之后自实现，而非默认引库**。

**代码**：`src/mikasa/index/vector_store.py`。

## ADR-0011 RRF 融合，不做分数加权

- 状态：Accepted ｜ M1（2026-09）

**问题**：双路（BM25 + 稠密）结果如何合并？线性加权是最直觉方案，但
BM25 分与余弦分量纲、分布完全不同，权重需逐库调且脆；归一化又引入
自己的口径问题。

**决策**：RRF（Reciprocal Rank Fusion）：`score = Σ 1/(k + rank)`，
k=60（经典值）。
1. 只依赖排名，**无需任何跨路标定**；
2. 是 kaggle / 搜索竞赛中稳定有效的融合基线，效果有据可查；
3. 可手算，答辩可推演（对展示型项目是加分属性）。
已知性质照实披露：文档必须在两路都有排名才吃两路分数，单路独有但极靠前
的文档仍可能胜出——评测里能看到融合 > 单路的证据，而非只挑好话说。

**后果**：融合实现 20 行；评测的 RRF 效果由阶段 A 指标实证。

**代码**：`src/mikasa/index/hybrid.py`、`pipeline/retriever.py`。

## ADR-0012 结构化分块器：标题边界 + 语义切分优先级

- 状态：Accepted ｜ M1（2026-09）

**问题**：RAG 检索质量的第一道关口是分块。定长切块（如 400 字符硬切）
实现最简，但会拦腰截断句子与段落，破坏词义与引用粒度——检索质量与
溯源体验同时受损。

**决策**（详细策略见 architecture.md"分块策略"，此处记取舍）：
1. **结构优先**：标题变化 = 天然块边界（语料是带 Markdown 结构的原创
   笔记，标题是语义最强的分隔信号）；
2. **块内按"段落 → 句末标点 → 逗号 → 硬切"优先级切长文本**，避免中断
   词义——宁可块略短，不切词义；
3. **重叠窗口**：相邻块共享 ≤overlap 字符尾部，缓解"答案被拦腰截断"
   造成的召回漏失；重叠只在同标题下续接，不跨标题；
4. **标题前置**：入库阶段把标题路径拼入块首（检索用），正文与标题分开
   携带（展示与生成不重复标题）——可做消融实验；
5. **长度不变量（块 ≤ size）**作为硬约束：段落以 `\n\n` 连接，窗口预算
   计入分隔符；重叠尾巴按剩余容量**截短而非超发**——该处出过真实越界
   bug，测试守护（`test_overlap_respects_size`）。

**后果**：分块器成为可单测、可消融的核心组件；分块策略的每个旋钮
（size/overlap/title_prefix）都在配置里，进评测口径。

**代码**：`src/mikasa/ingest/chunker.py`、`ingest/service.py`。

## ADR-0013 free 自由问答旁路：与 kb/评测解耦，mode 不落库

- 状态：Accepted ｜ M3.5（2026-09-09，用户使用痛点驱动的功能版）

**问题**：系统只做严格 RAG——知识库外的问题一律以统一拒答句式拒绝
（这是 M2 验收特性，不能动）。但用户接入了真实 LLM API（DeepSeek），
"写题时由知识点引申出的库外知识"没有任何出口，学习体验被锁死。

**决策**：问答拆双模式，默认 kb 维持现状一字不改：
1. **旁路点在 AskService 服务层**（`mode="free"` 分支），而非 Generator：
   Generator 的 `build_answer` 依赖 hits 的引用解析（无 hits 无引用），
   给 Generator 加"无引用模式"会把 RAG 语义塞进不该懂它的组件。free
   直连 LLM、手工组装 `Answer(citations=[], refused=False, ...)`——旁路
   是刻意设计，三层防线（L1 硬校验/L2 支持度/L3 拒答纪律）只约束 kb；
2. **mode 不落库**：qa_messages 无 mode 列，规避 schema 迁移（ADR-0004
   的 schema_version=1 硬校验不动）。回放/渲染的判别信号用既有列：
   kb 的 `latency_ms` 恒含 `retrieve` 键，free 只有 `generate`——历史接口
   据键存在性决定 [n] chip 渲染（free 轮正文里的 `[1]` 是模型正文不是
   越界红标）；（M4.5 注：会话管理升级首破 schema_version=1、升到 v2
   加了 title/folder_id 列，mode 不落库的判别信号设计经受住首次迁移
   未被动摇——`qa_messages` 结构一字未改，见 ADR-0015）；
3. **守卫口径看 `settings.llm.backend` 而非 profile 字符串**：profile 与
   后端解耦（YAML 可覆盖），mock 无语义 → free 一律 ConfigError；
4. **free 与评测解耦**：`eval/` 硬编码 Retriever + Generator、不经
   AskService，协议指标天然不受自由模式污染；free 多轮历史注入最近
   ≤5 轮**原始消息**（不做 kb 的摘要压缩，保留指代连续性），FREE
   系统提示压制历史轮里的 [n]/拒答句式污染；kb 摘要对无引用轮
   （free 轮）改用诚实话术"已作答（未引用知识库）"，不再谎称"给出引用"。

**契约**：CLI `--mode`（typer Literal choices）→ service 层 ConfigError
（红字 exit 1）；Web 请求体 `mode` 由 pydantic Literal 校验（非法值 422）
→ AskService 守卫（free+mock → 400 错误壳）；SSE 流式里守卫在生成器
**首个 yield 前**抛出 → `_to_frames` 的 try 兜底产出**恰一帧 error、
零 meta/done**；前端 `/api/health.profile == "offline"` 置灰按钮
（渐进增强信号，最终权威是后端守卫）。

**后果**：入口三处（CLI/Web 非流式/Web 流式）各需透传 mode 与守卫测试；
界面"会话内随时切"的语义是每条提问携带当时的开关态，消息可回放分类。

**代码**：`pipeline/{ask,prompts}.py`、`cli/__init__.py`、`web/routers/qa.py`、`web/static/`。

## ADR-0014 local profile 落地：免密钥放行 / 本地重排与裁判暂关 / 维度迁移纪律

- 状态：Accepted ｜ M4（2026-09-09）

**问题**：M1 就把 local profile 的 provider 代码全部预留（OpenAICompatLLM
共用、LocalFastEmbed、工厂分派、`[local]` extra），但有三处硬卡点让
`--profile local` 无法真正跑起来：①Ollama 兼容端点不需要密钥，而
`_get_client` 对无密钥一律 ConfigError；②本地 reranker / judge 依赖
fastembed 交叉编码器与双厂商裁判，直接启用会把"全本地"拖回需要校准
的泥潭；③api 与 local 的 embedding 维度不同（bge-m3 1024 维 vs
bge-small-zh-v1.5 512 维），切 profile 不重建索引会让 dense 路静默
降级甚至崩溃。本条记录五个落定点：

**① 免密钥占位 key——放行点选在 provider 层，不是 settings 层**
Ollama `/v1` 不校验 `Authorization`（只要求非空），openai SDK 构造期却
强制 api_key 非空。local 档 `api_key_env: ""` → `api_key` 恒 None，于是
provider 层注入模块常量占位 `_LOCAL_API_KEY = "ollama"`。放行点刻意不
放 settings 层：若配置层把"无密钥"放行，api 档忘填密钥也会被静默放行
（最糟的失败模式）；放在 provider 层按 `config.backend` 分路，api 的
早失败语义（ConfigError："请在 .env 填入密钥"）一字不动。judge 未来走
同一 OpenAICompatLLM，自动继承放行。

**② 本地重排（reranker backend）保持 none**
LocalReranker 代码与 `[local]` 依赖都在，但 M4 不启用：RRF 融合召回已
在评测实证（recall@10=1.000），本地重排收益未做真实验收——启用一个
未经实证的环节只会让口径分叉（同题两档答案不可比）。**M4 实装追加事
实：fastembed 0.8.0 已移除全部重排 API**（顶层无 TextCrossEncoder /
TextReranker，连 rerank 子模块都不存在）——LocalReranker 是按早期
API 形态保留的起点，切 `local` 不是改一行配置就能开，启用前必须先做
版本/实现选型（回落 0.7.x 或换独立重排包，见 reranker.py 类 docstring
与 limitations-and-failures.md §四），取舍随实证数据翻转。

**③ 本地裁判（judge）保持 disabled**
评估阶段 C 的语义裁判靠"双厂商对冲"防自偏好（Qwen 判 DeepSeek、反之
亦然）。本地只有同一个 Ollama 模型，无从对冲；同模型自评判分未经质量
校准，混进验收基线会污染指标解释。local 评测因此只跑检索层与协议层
指标（recall/MRR/nDCG、越界率、拒答率），这些层不走 LLM，口径与
api/offline 同构。评价一个系统先评价它能评价的部分——诚实披露。

**④ embedding 维度迁移纪律（切 profile 必 reindex，三层防护）**
embeddings 表 `chunk_id` 为主键（每个 chunk 恰一条向量）→ 单库
**单模型语义**。api（bge-m3，1024 维）与 local（bge-small-zh-v1.5，
512 维）的向量在同一张表内不可共存、不可混查。纪律=切 embedding 模型
必须 `mikasa ingest --reindex`（ingest 幂等，重建成本可控），三层防护：
1. **doctor 索引三方一致性**（`_check_index_consistency`）：快照
   `meta.json` 模型/维度 × 库内向量行数（按模型分组、chunk 总数、
   维度集合）× 当前配置，任一不符即红行带 reindex 指引——"整库错模型"
   （快照是 bge-m3 而配置是 bge-small）与"部分迁移"（库内两种模型
   残留）都被捕获，后者靠 GROUP BY 行数而非取最新一条（`LIMIT 1` 会
   掩盖多数旧向量）；
2. **检索侧维度防御**：`ExactVectorStore.search` 对维度不符抛显式
   `StorageError`（提示 reindex），替代 numpy 裸 ValueError——cli 只捕
   StorageError/ConfigError，裸错会漏成 traceback；
3. **评测指纹防错配**（既有，corpus_digest 混入 chunk_id）：reindex 后
   黄金集指纹必然失配，强制重跑 `tools/build_golden.py`，不存在"用旧
   黄金集测新索引"的静默错配。
运行时 BM25 兜底降级 warning（manager）保留不动：单点事故的兜底语义
在，前置拦截靠 doctor。

**⑤ doctor 口径演进**
体检从"通用项"走向 backend 门控：local 档新增"Ollama 服务连通 /
模型已拉取 / fastembed 在位"三行，失败文案带可执行下一步（`ollama
pull` / `pip install -e ".[local]"` / Windows `OLLAMA_BASE_URL` 逃生口
指引）；探测函数模块级（可 monkeypatch，单测不真连服务）；**doctor
从不触发模型下载**（TextEmbedding 构造即拉 ~100MB，下载归首次
ingest/提问的运行时）。doctor 仍是硬门禁：任一项失败汇总红行后
exit 1，"告警"语义由失败文案承载，不由"不退出"承载。

**代码**：`src/mikasa/providers/llm.py`（_LOCAL_API_KEY）、
`cli/__init__.py`（探测函数 + doctor 门控）、`storage/repo.py`
（embedding_models_in_db）、`index/vector_store.py`（维度防御）。

## ADR-0015 会话管理升级：文件夹树 + 标题三路径锁 + suggest 降级语义

- 状态：Accepted ｜ M4.5（2026-09-09，用户交互痛点驱动的管理版）
- 关联：ADR-0004（首次真实迁移，见其修订段）、ADR-0013（mode 不落库
  经受住首次迁移未被动摇）

**问题**：侧栏会话列表只有"会话 #id"硬编码标题 + 一条流，会话一多
找不到、没法归类；用户点名要编译器左侧文件树的形态——多层嵌套
文件夹、会话归夹批量管理、标题自动提炼对话核心、可重命名/删除。

**决策**（逐条取舍）：

1. **标题策略 = 三路径锁（title_manual 列）**。自动提炼有三条写入路径，
   锁语义统一在 repo 层 WHERE 条件里（注释即契约）：
   - `auto_title_if_untitled`（首轮问答后 _record 钩子）：只写
     `title_manual=0 AND title IS NULL` 的会话——**只补第一轮**，不随
     轮次漂移（标题不该每轮变脸）；
   - `apply_suggested_title`（suggest 入口）：允许升级**已自动命名**的
     会话（截断兜底先落库、LLM 提炼后覆盖），但 `title_manual=0`
     守卫不变；
   - `set_session_title`（用户改名）：非空 → 落库并锁 `title_manual=1`
     ——**手动命名永不被自动覆盖**（LLM 提炼也只回建议不落库，
     applied=false 语义）；显式清除（null/空）→ 解锁回未命名，下轮
     问答自动重新补名（闭环：清除标题 ≠ 从此裸奔）。
   - `list_sessions` SELECT 有意不含 title_manual（防误用），锁状态
     只经 `get_session` 与 suggest 的 applied 暴露。
2. **标题素材与降级 = 截断兜底 + LLM 一次**。素材统一取库内首问 + 首答
   （`first_user_message` 为唯一事实源，CLI/Web/流式三入口共用 _record
   钩子 → CLI 零接线受益）。offline/mock 无 LLM 语义 → **截断兜底是
   合法回退不是错误**（首问 fold_title 20 字，毫秒级），suggest 报
   applied=true；api/local 走 LLM（system 提示 ≤16 汉字、无引号/编号/
   换行，temperature 走配置、max_tokens=24），**任何失败/空输出 →
   截断兜底**（logger 留痕，提炼失败绝不拖挂问答流）。CLI 永不 LLM。
3. **文件夹 = 多层嵌套树，但存储平铺、前端组树**。qa_folders 单表
   自引用 parent_id（NULL=根），列表 id 升序平铺，`buildTree` 在
   前端按 parent_id 组嵌套（外键保证父 id < 子 id，天然有序）。取舍：
   后端不递归序列化——树操作（移动/删除）都按 id 直达，会话归属
   文件夹只存 folder_id 一列，不需要闭包表。防环（新父 ∈ 自身∪后代）
   一条 `parent_id in folder_descendant_ids(...)`（WITH RECURSIVE）完成，
   前端移动菜单用同判据灰显目标（subtreeIds）。
4. **删除语义两分**：会话删 = 消息级联清（qa_messages 既有 ON DELETE
   CASCADE，容器内内容跟着删——会话与消息是组成关系）；文件夹删 =
   **只删空夹**（409 带子夹/会话计数文案，需先移出）——文件夹是容器，
   容器绝不级联连坐内容（qa_folders 外键 NO ACTION，应用层 409 万一
   被绕过由约束兜底，绝无静默级联删用户会话）。
5. **PATCH 覆盖语义 = model_fields_set 判定字段出现**：显式 null 是合法
   覆盖值（title null=清除解锁、folder_id null=移回根），字段缺省 =
   该属性不动——单字段改名不误伤另一属性（改名不重置文件夹归属等）。
6. **Web 菜单交互，不做拖拽**（用户拍板：菜单移动）：⋯ 菜单 + "移动
   到…"子菜单（路径表根级置顶、自身+后代灰显、当前位置灰显、← 返回
   主菜单）；行内改名/新建输入（Enter 提交、Esc 取消、点外取消）；
   删除一律 confirm() 二次确认。
7. **首轮自动提炼闭环在 Web 层**：发送时无活动会话 = 首问 → done 帧后
   POST /title/suggest（不 disable 输入、失败静默）→ 再刷侧栏。判别用
   "发送时 activeSession===null"而非 meta 帧——meta 每轮都发（续问也
   有），续问不该重跑提炼（材料是首问+首答，重跑只白烧 LLM 一次）。
8. **展开态不持久化**（刷新回全折叠）、标题不随轮次更新、会话搜索/
   归档/固定不做——见 known-issues.md 功能 backlog，价值未实证不预埋。

**后果**：首次真实 schema 迁移（v1→v2）连带 ADR-0004 修订（自动逐级
迁移 + 幂等重入）；27 会话/88 消息实库平滑升级并有演练记录（迁移
日志 1→2 一次、标题回填=各自首问截断、计数不变、二次启动零迁移）。
代价：CLI 聊天不显示标题（列表接口增强不影响），qa_sessions 增列后
任何"SELECT *"消费方自动多键（宽松超集，不破坏既有断言）。

**代码**：`storage/{db,repo}.py`、`utils/text.py`（fold_title）、
`pipeline/{ask,prompts}.py`、`web/{schemas.py,routers/sessions.py}`、
`web/static/js/{tree,qa-tree,qa}.js`；测试 `tests/unit/storage/
test_db_migration.py`、`test_repo_sessions.py`、`tests/unit/web/
test_sessions_api.py`；冒烟 `tools/smoke_tree.mjs`。

---

## ADR-0016 阅读视图：去接缝在后端 + 原文件白名单 + 正文开口的边界

- 状态：Accepted ｜ 论文阅读器 b 期（2026-09-10，用户点名"引用要看得到原文"）
- 关联：ADR-0012（结构化分块器的 overlap 规则——本 ADR 是它的**逆运算**）、
  ADR-0014（切片 profile 的单模型语义，与本文的"原文件解析"同属数据可错配区）

**问题**：`[n]` 角标此前只能跳到消息内的引用卡，看不到引文在原文里的位置；
导入的文档也点不开。要做"阅读视图 + 引用跳转"，先要回答两件事：
**渲染什么**、**引用怎么定位**。

**决策**（逐条取舍）：

1. **文本视图 = 按 seq 拼 chunks，去接缝放后端**（`ingest/stitch.py`）。
   库内**没有存解析后全文**——`Para`/`LoadedDocument` 只在 ingest 内存活
   一次（ingest/types.py），chunks 是唯一与 `citation.chunk_id` 原生对齐、
   且 23/23 篇都可用的数据。逆运算为什么必须与 chunker 同源：
   - 重叠只在相邻块 `heading_path` **相同**时产生（chunker.py:110-114 在
     标题变化处清空 tail）→ 守卫"跨标题必不裁"在真实数据上**零代价**
     （实测全库 941 对相邻块里"跨标题且存在重叠"= **0 例**）；
   - 重叠尾巴会被按剩余容量**截短**（chunker.py:94 的 `take = min(...)`），
     截短后它就不再是上块的后缀 → 除后缀规则外还需一条**近尾规则**
     （匹配起点须落在前块最后 `seam_window` 字内）。实测 PDF 类语料：
     后缀规则覆盖 68.8%，近尾规则再补 14.3%，合计 83.1%；
   - **验证不是"看起来合理"**：拼后字数与 loader 入库时统计的
     `documents.char_count` 逐篇吻合到 0.2%~2%——doc 71（866 块、281441 字）
     拼后 248767 字，而 loader 当初统计 248274 字，仅差 493 字（应即
     loader 加在表格块首的 `【表格】` 标记）。全库 309499 → 275034 字
     （裁掉 11.1%）。
   拼接**必然丢**md 的 `# 标题` 行、代码围栏、blockquote 标记与 PDF 页眉
   页脚（它们本就没进 chunk）——小标题靠 `heading_path` 补位，原文件视图
   是完整原文的出口，**不做假还原**。

2. **原文件视图 = 浏览器自带阅读器内嵌**（`<iframe src="/api/documents/
   {id}/file#page=N">`），零新依赖（前端无 pdf.js，PDF 无 Range 则无法拖动
   进度条）。两个已核实的硬约束：
   - Starlette 的 `FileResponse` **默认 `content_disposition_type="attachment"`**
     （本机 1.6.0 实测）——不显式传 `"inline"`，Chrome 会直接下载而不内嵌
     渲染，整个 PDF 视图失效；
   - Range 由 `FileResponse` 自身支持（206/416/多段/`accept-ranges`，源码
     已核），28MB 的 PDF 可渐进加载与拖动。

3. **`/file` 的路径解析是白名单式的，因为 `file_path` 不可信**。实测 23 行
   里 **21 行指向已废弃的旧项目路径**（`D:\Code\MyProject1\data\uploads\…`）。
   两跳、每跳都做容器校验（`resolve().is_relative_to(uploads_dir)`）：
   ① 按名称（必须用 `PureWindowsPath`——脏数据是反斜杠形式，POSIX 的
   `Path` 会把整串当成单个文件名）；② 回退按 `stem == title` 并用
   `sha256_file` 与入库哈希验身。**不做模糊匹配**：发错文件比 404 更糟。
   媒体类型走显式白名单且**禁 `text/html` 与 `image/svg+xml`**——服务跑在
   127.0.0.1 同源下，这两类被浏览器当文档解析就是存储型 XSS 的唯一入口。

4. **正文开口是有意的脱敏例外，边界写死在代码里**。问答链路本就通过
   `Citation.snippet` 回显过正文片段，新增 `/content` 与 `/file` 是为了让
   引用可核查、文档可通读。边界：`file_path`/`file_sha256` **永不进响应体**
   （`_public_document` 不变，测试断言）；两个端点只按 doc_id 读库、不接收
   任何路径参数。将来要扩这个口子，先改这一条。

5. **面板形态 = JS 动态创建的右侧抽屉，两页共用**。三份 html 各自硬编码
   nav，新增页面要改 3 个文件并失去"树 ↔ 原文"的上下文连续性；而 §5.A 的
   原话本就是"**侧栏**可选文档打开"。DOM 由 `reader.js` 创建使两页零标记
   复制。两条已踩的交互坑写进注释：① 点外部关闭的白名单**必须包含开启源**
   （`.cite`/`.cite-card`/`.doc-item`），否则 chip 的处理先跑、冒泡到
   document 时被判"点外"，开了立刻关；② Esc 会同时关掉设置面板（其监听
   注册更早且无条件），**接受"Esc 收掉所有浮层"**而不做优先级拦截。
   知识库页的 `onOpen` 回调**刻意独立于 `onSelect`**：后者被 `notifyActive`
   复用、每次刷新重绘都会跑，并进去会导致"拖文档进文件夹就自动弹面板"。

5b. **一套渲染，两种呈现**（2026-09-10 用户实测反馈后补）。初版只有浮层：
   知识库页点文档行弹右侧抽屉，右栏本身仍是几张元数据字段——用户反馈
   "太丑了，只是几个文字在上面"。改为 `initReader(host)` 用 host 参数区分：
   不传 = 浮层（问答页点角标，不打断问答上下文）；传 = 内嵌进右栏
   （知识库页选中即读，正文占满、元数据压成底部一行状态栏，上传卡让位）。
   渲染、定位、双视图切换全部复用同一份代码，两页零标记复制。
   配套修的两个"改形态必然踩到"的点：① **拖文档进文件夹后状态栏的位置
   要自动跟进**——旧详情卡靠 `onSelect` 重渲染天然有，内嵌状态栏只在
   打开时构建，故补 `updateLocation()` 由 `onSelect` 推送；② `onOpen`
   回调补传 `location`（位置由树算，阅读器不该自己查文件夹树）。

**被否方案**：① 新增 `/documents/{id}` 阅读页——要改三份 nav + `_PAGES`，
且丢掉侧栏上下文；② 入库时另存解析全文（schema v4 + 全库重灌）——成本
最高，且拼 chunk 的还原精度已被 `char_count` 对账证明够用；③ 前端
pdf.js——引新依赖且与"无构建链"取向冲突，浏览器自带阅读器已满足。

**代码**：`ingest/stitch.py`（新）、`web/routers/documents.py`（三端点 +
`_resolve_upload_file`）、`web/static/js/{reader,reader-view}.js`（新）、
`common.js`（chip/引用卡加 `data-chunk-id`）、`kb-tree.js`（`onOpen`）、
`documents.js`/`qa.js`（接线）；测试 `tests/unit/ingest/test_stitch.py`、
`tests/unit/web/test_documents_api.py`；冒烟 `tools/smoke_reader.mjs`、
E2E `tools/chrome_reader.py`。

---

## ADR-0017 合并「页面」视图 + 页码对齐 + Ollama 上下文窗口

- 状态：Accepted ｜ 论文阅读器 b 期续（2026-09-11，用户实测反馈驱动）
- 关联：ADR-0016（阅读视图的地基）、ADR-0012（分块器）

**问题**：文本视图"字很乱、表格乱、图片没有"；用户要求把"原文件"与"高亮标注"
**合并成一个视图**。此前计划的解法是"入库时恢复表格列结构 + 提取图片进库"。

**决策**：

1. **合并视图 = 页面渲染图 + 坐标高亮**，而不是改解析器。把 PDF 每一页按需
   渲染成 PNG（`GET /api/documents/{id}/page/{n}.png`，PyMuPDF 单页毫秒级、
   不落盘、确定性结果给私有缓存），再把引用块按坐标画成覆盖层高亮
   （`GET /api/documents/{id}/locate/{chunk_id}` 返回**归一化到 0~1**、按行
   分组的矩形）。表格/图片/公式**自动正确**——它们本来就在页面渲染里。
   于是"提取表格列 + 图片资产目录 + schema 变更 + 全库重灌"整条链都不需要
   （schema 影响面归零）。代价：页面图不能选中文字/不能 Ctrl+F——头部留
   「↗ 原文件」链接（Chrome 内置阅读器）兜底。
2. **定位用词元序列匹配，不用 `page.search_for`**。chunk 文本入库时被清洗过，
   与 PDF 原文非逐字一致——`search_for` 实测只有 **55%** 命中。把两边都做
   NFKC 归一 + 去标点后按词元比对，实测 **98%**（`ingest/pagelocate.py` 纯
   函数）。定位不到返回空 rects，前端只显示页面不高亮——优雅降级。
3. **页码对齐：同一类 bug 有两处，都要修**。`enumerate(cleaned, start=1)`
   按列表位次编号，"一页一元素"是硬不变量。`_drop_page_furniture` 与
   `_cut_reference_tail` 却各自用 `if kept:` / `[t for t in rest if t.strip()]`
   把空页滤掉——空白页一多，后续页码系统性前移（doc 71 实测 +3~+5 位，
   两处叠加把 301 页截到 189 且前部错位）。修法：两处都**始终追加、保留空串
   占位**（下游 `if not page_text.strip(): continue` 会照常跳过）。回归测试
   各自锁定。
4. **Ollama 运行时上下文窗口只有 2048，模型本身是 40960**（2026-09-11 实抓：
   6000+ token 的提示词被截到 2050，且从**开头**丢——系统提示词的引用/拒答
   规则可能从未进过模型；暗号测试修前空回复、修后 `紫罗兰七号` 全对）。
   Mikasa.bat 加 `OLLAMA_CONTEXT_LENGTH=16384`（qwen3:8b 的舒适余量）。
   这也是"检索给的内容太少"的一大半根因——注入 10 块 ≈3500 字符早就超窗。
   配套把 local `fusion_top_k` 10→14、api `top_n` 5→8（改 top_n 会平移评测
   基线，跨基线比较要留意）。
5. **引用卡加显式「↗ 打开原文件」按钮**：`[n]` 角标是可点的隐藏交互，新用户
   发现不了——每张引用卡带一个明示按钮（新标签开原件），不依赖先发现角标。
6. **字号调节只在阅读区**：`--reader-font` 变量挂在阅读器根节点（浮层或内嵌
   皆可）而非 `documentElement`，`mikasa.ui.readerFont` 本地持久化；控件
   A-/A+ 放阅读器头部（`documents.html` 没有设置面板，放设置面板知识库页
   够不着）。

**被否方案**：入库恢复表格列（改 `_reflow_by_geometry` 的 join 加 ` | `，
列隙数据本在 join 前可得）+ 图片提取进库（`data_dir/assets` 目录 +
`【图:...】` 标记或 `document_assets` 表）——合并视图让两者都不再必要；
代价远大于收益（三处 join 改动 + 资产生命周期三处清理 + reindex 孤儿清扫
+ 3 个既有回归断言重写）。

**代码**：`ingest/pagelocate.py`（新）、`ingest/loaders.py`（两处页码修复）、
`web/routers/documents.py`（/page/{n}.png、/locate/{chunk_id}）、
`web/static/js/reader.js`（页面视图 + 字号 + 显式按钮）、`common.js`/`qa.js`
（引用卡按钮）、`Mikasa.bat`（OLLAMA_CONTEXT_LENGTH）、
`config/profiles/{local,api}.yaml`（fusion_top_k/top_n）；测试
`tests/unit/ingest/test_pagelocate.py`、`test_loaders.py`（两处页码回归）、
`tests/unit/web/test_documents_api.py`；E2E `tools/chrome_reader.py`（PDF
页面视图断言）。
