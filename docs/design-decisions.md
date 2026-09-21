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
| ADR-0016 | 阅读视图：去接缝在后端 + 原文件白名单 + 正文开口的边界 | Accepted |
| ADR-0017 | 合并「页面」视图 + 页码对齐 + Ollama 上下文窗口 | Accepted |
| ADR-0018 | 设置面板配置模型：用户配置覆盖层 + 免重启热生效 | Accepted |
| ADR-0019 | 在线找论文：双免费源 + 交错分页 + 有防线的下载器 | Accepted（第 8 点被 ADR-0020 部分取代） |
| ADR-0020 | 「找论文」独立成页：三源 + 能力声明 + 诚实的翻页契约 | Accepted |
| ADR-0021 | 笔记 = 带标记的普通文档：复用 source_ref、不加 schema 列、force 绕过内容去重 | Accepted |
| ADR-0022 | 应用内更新：客户端碰不到 URL + 与包同批的校验和 + 检查失败静默 | Accepted |
| ADR-0023 | 第四个来源 DOAJ：免密钥中文开放获取期刊 + 各来源的授权边界 + 不做 WAF 绕过 | Accepted |
| ADR-0024 | 更新下载可续传、任务可接续：「观察窗」+ 槽位自愈（修订 ADR-0022 第 3、7 点） | Accepted |
| ADR-0025 | 许可证改为 AGPL-3.0：发布物捆了 AGPL 的 PyMuPDF（不换依赖，换许可证） | Accepted |
| ADR-0026 | 自动题库 + 逐题校验：从你自己的语料出题，评测不再绑死一份语料 | Accepted |
| ADR-0027 | 拍照转笔记：视觉模型独立成段、识别只是草稿、原图锚在笔记 key | Accepted |
| ADR-0028 | 公式在本地排版：把 KaTeX 内置进发布物 | Accepted |
| ADR-0029 | 本地档改走 Ollama 原生接口：think / num_ctx 两个旋钮 + 作答呈现规范 | Accepted |
| ADR-0030 | 向量模型随包携带：固定 revision + 构建期取件 + 运行时铺设 | Accepted |
| ADR-0031 | 文生图：出图独立成段 + 字节落盘 + 只放行同源图片 | Accepted |

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

## ADR-0018 设置面板配置模型：用户配置覆盖层 + 免重启热生效

- 状态：Accepted ｜ M5 后完善（2026-09-15，用户提出）
- 关联：ADR-0001（三档 profile）、ADR-0002（密钥只经环境变量）、
  ADR-0014（嵌入维度纪律）、ADR-0017（Ollama 上下文窗口）

**背景**：首启引导写着"设置面板里能改"，但设置面板一直只有外观项——想让
应用换模型/换供应商只能手改 `.env`，而打包版连 `.env` 都在只读解包目录里。
用户要求做成 CC Switch 那种形态：选来源、贴密钥、测连通，保存即生效。

**决策**：

1. **持久化落在"用户可写的覆盖层"，不重写 profile**：面板写
   `user_data_root()/config.yaml`（源码模式 `data/config.yaml`；打包
   `%LOCALAPPDATA%\Mikasa\config.yaml`——打包后 `resource_root()` 只读）。
   它**只在基底是 profile 文件时**深合并；显式 `--config` 与
   `config/config.yaml` 保持"完整替换"语义（迁移演练与 E2E 工具都依赖它）。
   覆盖层里的 `profile:` 键一律忽略：档位决定 embedding/检索整套，允许面板
   改档会让新档的 embedding 与旧档的索引对不上（ADR-0014）。
2. **面板只写 `llm:` 段**：改嵌入模型会变向量维度、必须全量重索引，仍归 CLI
   决策；reranker/judge 也随档位。且只写面板自己拥有的字段（temperature/
   max_tokens/timeout 不写），避免把档位调优过的数字用旧快照固化。
3. **密钥写 `user_data_root()/.env`**（python-dotenv `set_key`，
   `quote_mode="always"`，缺文件时先建 UTF-8 头注释），覆盖层里永不出现密钥；
   读取仍按 ADR-0002 经 `api_key_env` 在请求时刻解析。`GET /api/settings/model`
   只回 `has_api_key`、永不回值；测试连接把密钥临时放进
   `MIKASA_SETTINGS_TEST_KEY` 环境变量并在 `finally` 摘除。清除密钥 = 删行 +
   从进程环境弹出，**且只动本次提交那一个变量**（api 档的
   `SILICONFLOW_API_KEY` 被 embedding/reranker/judge 共用，误清会静默打挂检索）。
4. **保存即热生效**：写文件 → 直接写进程环境（`load_dotenv` 不覆盖已存在变量）
   → 重新 `load_settings()` → **先**换 `services.settings` 再 `rebuild_ask()`
   （它用 `self.settings` 重建）→ 换 `app.state.settings`（`/api/health` 的取数口）。
   `_APPLY_LOCK` 串行化保存；在途问答持旧对象照常完成；新 AskService 索引缓存
   冷启动（低频显式动作，可接受）。
5. **`.env` 查找链改为全部加载**：旧实现"命中第一个存在的文件就停"。面板把
   密钥写进数据目录 `.env` 后，仓库根 `.env` 里的其它密钥会被静默屏蔽
   （api 档下 embedding/reranker/judge 全挂）。现在按链序全部加载，同名键
   先加载者胜——与旧语义（链序在前者优先）一致。
6. **测试连接不碰任何生效状态**：一次性客户端 `max_retries=0`
   （否则 20s 超时会拖成 3×20s）、`max_tokens=8`，恒回 200 +
   `{ok, latency_ms, error?}`——"连不上"是探测结果而非服务端错误，前端一个
   pill 直接渲染。

**被否方案**：把完整生效配置快照进覆盖层——会把 `${VAR}` 展开固化成字面量、
把将来的 profile 默认值调整永久压在旧快照下，且写错 `profile:` 一个键就触发
全量重索引；写随包 `resource_root()`——打包版只读，写不进去；面板切档位——
同上重索引风险。

**局限**：覆盖层对全部档位生效（含 offline；用户显式换模型即自担该档
"零调用"承诺失效）。配置来自 `--config`/`config.yaml` 时面板只读（端点回
400 并给出文件路径）。judge/reranker 暂不在面板内开放。

**代码**：`config/settings.py`（覆盖层加载合并、`user_config_path`、
`user_env_path`、`write_llm_overlay`、`write_api_key`、`clear_api_key`、
`.env` 全链加载）、`providers/ollama.py`（自 CLI 迁出）、`providers/llm.py`
（`max_retries`）、`web/routers/settings.py`（新）、`web/schemas.py`、
`web/static/js/model-settings.js`（新）、`static/index.html`、
`css/style.css`、`static/js/settings.js`、`static/js/onboard.js`；测试
`tests/unit/config/test_user_config.py`、`tests/unit/web/test_settings_api.py`、
`tests/conftest.py`（全局 `MIKASA_DATA_DIR` 隔离）；E2E
`tools/chrome_model_settings.py`。

## ADR-0019 在线找论文：双免费源 + 交错分页 + 有防线的下载器

- 状态：Accepted ｜ M7（2026-09-15/16，用户提出"在知识库里能搜到别的论文，像知网那样"）
  —— **第 8 点已被 ADR-0020 部分取代**（M8，2026-09-16）："面板落在知识库页"不再成立，
  该功能成了独立一页（`/papers`）并扩到三源。其余各点（来源/交错分页/降级/防线下载器/
  导入尾链/密钥纪律）仍然有效。
- 关联：ADR-0002（密钥只经环境变量）、ADR-0016（阅读视图与原文件白名单）、
  ADR-0018（本面板沿用的密钥纪律）、ADR-0020（后继）

**背景**：用户想在知识库里做知网式检索——搜到自己没上传的论文，导进来立刻能提问。
2026-09-11 的勘验结论是硬约束：知网/万方/维普的全文在付费墙后，无公开 API、有反爬，
硬爬是法律问题而非工程问题。**目标是"知网式体验"，不是"知网的数据"**——界面与文档
如实这么写。

**决策**：

1. **两个有公开 API 的免费源：arXiv（Atom XML）+ OpenAlex（works JSON）**。arXiv
   覆盖 CS/物理预印本、全部开放获取，但必须 https（实测 http 80 端口被墙）、官方
   限速约 1 请求/3 秒——模块级节流只对**成功**请求补睡。OpenAlex 覆盖带 DOI 的
   国内理工核心期刊（"水库坝"这种中文词直接命中几万条），但 2026-02 起必须免费
   API key（每日 10 万积分，列表查询一次 10 分），且摘要存倒排索引、重建后无标点
   无大小写。两者字段形状完全不同，归一成一份 `PaperResult`。
2. **交错分页取代按分数合并**：两源的相关性分数不可比，合并排序是编出来的。全局
   位置按奇偶分配——偶数给 arXiv、奇数给 OpenAlex——保证每页两源都出镜，中文结果
   （OpenAlex 路）不会永远沉在英文结果（arXiv 路）后面。`has_more` 取代 `total`：
   arXiv 的相关性计数会漂、OpenAlex 的 `meta.count` 是近似值，"这把取满了没有"
   才是诚实的翻页信号。
3. **逐源降级**：单源抛错只把中文消息塞进 `errors` 字典，响应照常 200 并带上另一源
   的结果；两源全灭才值得 502。前端把该字典渲染成结果上方的"部分来源暂时不可用"。
4. **导入端点不信客户端**：请求体只带 `{source, id}`——不带标题、不带 PDF 链接。
   服务端按 id 反查来源 API 取元数据、从自己的记录里推 PDF 地址；id 双重正则校验
   （schema 解析 + 拼接 URL 前），SSRF 防线在入口就闭合而不是在 socket 上。没有
   开放获取全文 = 409 + 落地页（DOI）跳转提示，而不是失败。
5. **有防线的下载器**（`papers/download.py`）：https 只放行解析到公网地址的主机；
   http 只放行回环（E2E 假源逃生门——回环打不到内网，SSRF 保证不削弱）；重定向
   逐跳复验、上限 3 跳；`Content-Type: application/pdf` + `%PDF-` 魔数嗅探；50 MB
   硬上限；每条失败路径都删半成品。已接受残差：校验与实际连接是两次独立 DNS，
   理论上的 DNS-rebinding 窗口仍在——闭合它要自建连接层，对本地个人应用不值。
6. **导入复用上传尾链**：把上传端点的入库尾段抽成 `documents.ingest_web_file`，
   导入因此返回**逐字节同形**的 201/200/409——树刷新、toast、去重是同一条代码路径
   而不是副本。文件名 `标题[:80] (来源 id).pdf`：ingest 是"同名替换"语义，两篇同名
   论文会互相踩掉，id 后缀保它们互不相干；真·同内容仍走 sha256 去重（200，
   "已跳过重复导入"）。标题先截断再拼后缀——`sanitize_filename` 保头截尾。
7. **OpenAlex 密钥放在面板里**，沿用 ADR-0018 的三段语义（`null`=不动、`""`=清除、
   非空=写入）与纪律：写数据目录 `.env` + 同步进程环境（热生效），GET 只回布尔、
   永不回值。密钥行默认折叠——密钥是可选项（匿名有少量试用额度），不该跟搜索框
   抢注意力。
8. **面板落在知识库页**（`js/papers.js`）：导入的文档落在这里，左侧语料树能立刻
   给出反馈。设置面板只在问答页，所以密钥行由面板自带。结果行渲染标题/作者/年份/
   来源 + 可展开摘要；无开放获取的那条**提前禁用**导入按钮，让 409 退化成安全网
   而不是交互本身。
   *（M8 注：被 ADR-0020 反转——面板挤不下筛选器与详情面板，改成了 `/papers` 独立页；
   它让出的"导入后立刻看得见"反馈由 `documents.source_ref` 的"已在库中"标记补回。）*

**被否方案**：爬知网/万方/维普（付费墙 + 反爬 + 无 API = 法律问题，文档里也这么写）；
把 Semantic Scholar 当第三源（能用，但 429 限流，暂时不值第三个解析器）；
CORE/ChinaXiv 作中文 OA 补充（留在 backlog）；允许客户端直接给 PDF 链接（那正是本设计
要关掉的 SSRF 口子）；单开一个"论文"页（文档住在知识库，导入必须落在用户已经在的地方）；
两源按分数合并（见 2）；文件名用客户端传的标题（服务端会写下用户能伪造的名字）。

**局限**：CSSCI/社科中文覆盖 ≈ 0，知网独家全文拿不到——界面与文档如实说明并给 DOI
跳转。arXiv 的 3 秒节流让连续检索偏慢（一次检索可能是两次上游调用）。没有"跳到第 N 页"：
交错窗口按已收到条数前进，只能向后翻。OpenAlex 重建的摘要没有标点与大小写。导入即下载：
未导入的论文没有应用内预览，超 50 MB 的 PDF 一律拒收。

**代码**：`papers/sources.py`（`PaperResult` 与 `PaperSource` 协议）、`papers/arxiv.py`、
`papers/openalex.py`、`papers/download.py`、`papers/service.py`（交错 + 降级）、
`papers/errors.py`；`web/routers/papers.py`（新）、`web/schemas.py`、
`web/routers/documents.py`（`ingest_web_file` 抽取）、`web/static/js/papers.js`（新）、
`static/documents.html`、`static/js/documents.js`、`css/style.css`；测试
`tests/unit/papers/`（sources/service/download 三份）与 `tests/unit/web/test_papers_api.py`；
E2E `tools/chrome_papers.py`。

## ADR-0020 「找论文」独立成页：三源 + 能力声明 + 诚实的翻页契约

- 状态：Accepted ｜ M8（2026-09-16，用户用了一天 M7 后提出："这个搜索太简陋了，单独放一页"）
- 关联：ADR-0019（第 8 点被本篇取代）、ADR-0004（迁移纪律）、ADR-0016（阅读器只能看已入库文档）

**背景**：M7 把在线找论文做成了知识库页右栏的一个面板。用户用过后的判断是"太挤了"：
一个搜索框、一列结果、没有筛选。要求提成**独立一页**（与「问答/知识库/评测」并列），
并在四个方向上做丰富：每条结果信息更全、筛选与排序、更多来源、阅读与状态体验
（点一条看完整摘要，并且能看出哪些已经在库里）。

**决策**：

1. **第四页（`/papers`），不是更大的面板**：三栏布局（筛选 300px / 结果 / 详情 340-440px）
   需要的横向空间，知识库页给不出来——它右栏是上传区与语料区。导航在四个 HTML 里硬编码，
   加一项每处一行，外加 `_PAGES` 一条；**同一改动里删掉知识库页的面板**，不留两套会漂移的逻辑。
2. **第三个来源 CORE，靠实测选出来的**：CORE v3 works API 匿名可用（免密钥）、连打几次能撑住、
   `downloadUrl` 实测下到真 PDF（`%PDF-1.5`）。Semantic Scholar 暂缓（匿名池连试两次都 HTTP 429）；
   ChinaXiv 直接否掉——它的 `/api/search` 存在但契约猜不出（GET 与两种 POST 编码一律回
   "请求方法应为POST"，`/oai` 又拒绝匿名），要接只能 HTML 抓取，而那是不做的（ADR-0019 关于
   中文付费全文的判断依然成立）。
3. **来源声明自己的能力，界面禁用做不到的选项**：`SourceCaps(year, cited_sort, recent_sort,
   language, oa)` 是 `PaperSource` 协议的一部分，`GET /api/papers/sources` 把目录交给前端，
   前端据此置灰并说明原因。这条来自一次实测的**"假支持"**：CORE 的 `yearFrom`/`yearTo`
   **参数**返回 200 却被静默忽略（要 2020+，给回 2012/2018/2010 的论文）——它的年份过滤
   只能走查询语法（`... AND yearPublished>=2020`），现在的实现就是发这个。摆一个悄悄不生效的
   选项，比不提供更坏。
4. **`notes` 与 `errors` 是两回事**：errors = 失败（来源抛错），notes = 降级（来源答了但没法
   满足某个筛选：arXiv 没有被引数据，"按被引排序"就回落相关度）。两者都以各自的措辞出现在界面上。
   一个必须写清的细节：`oa: "always"` 是**已满足**而不是降级——天然全 OA 的来源本身就兑现了
   "只看开放获取"，所以它不产生 note；前端把它渲染成"已勾选且禁用 + 已自动满足"，与"不支持"
   的文案刻意分开。
5. **交错分页从两源推广到 N 源**：全局位置 `p` 归来源 `p % n`、是该源的第 `p // n` 条，
   窗口换算 `k ∈ [max(0, ceil((offset-i)/n)), floor((offset+limit-1-i)/n)]`。代回 `n=2`
   与旧的奇偶算术逐位相同（有单测钉住等价性）。合并**按全局位置装填、缺位留洞、不补位**：
   旧的 `zip + 尾巴补位` 会在某源提前耗尽时让第二页重复（首页拿到 `[a0, o0, o1, o2]`，
   客户端按收到的 4 条推进 offset，`o2` 在第二页又出现一次）。
6. **翻页契约按窗口推进，不按收到的条数**：页面上有空洞时，"按收到条数推进 offset"是错的；
   客户端按 `limit` 推进。两半都有回归锁——只改一半仍然会重复。
7. **OpenAlex 的窗口对齐得单独修**：它只有 `page`/`per-page`，页边界固定在 `per-page` 的
   整数倍上，问不出任意 offset。旧实现 `page = start // count + 1` 只在 `start` 是 `count`
   整数倍时侥幸正确（两源对半分 20 条时成立，N 源轮转一开始就不成立）。第一版修法
   （按 count 对齐、按 per_page 取页）依然错——两个倍数不是一回事。最终实现：**第 1 页带
   `per-page = start + count` 取回再切片** `[start : start+count]`：一次请求、不做边界算术，
   而 OpenAlex 按"每次查询 10 credits"计费、与页大小无关，多取不花钱。超过单页上限（200）
   的深窗口取不满 → `has_more` 自然为假，该源如实停翻而不是假装还有。
8. **"已在库中"要一列，不要标题后缀**：`documents` 加 `source_ref TEXT`
   （`"arxiv:2401.12345"` / `"core:72543"`），由导入链路写入、搜索端点一次 `IS NOT NULL`
   全表捞回。反查文件名后缀 `(arxiv 2401.12345)` 被否：用户会改名
   （`PATCH /api/documents/{id}` 只动 `title`）、改名会被重灌继承（永远不会自愈）、标题在 80 字
   处被截断后才拼后缀、唯一稳定载体（uploads 副本名）只能经已知脏字段 `file_path` 取到。
   加列完全照 v3 `folder_id` 的先例——**包括两处 reindex 保留清单**，遗漏它正是当年丢
   `folder_id` 的原因。
9. **sha256 跳过路径要回填来源**：用户手拖过这篇 PDF、之后又点导入同一篇，会命中内容哈希跳过；
   不回填的话那一行永远 `source_ref = NULL`，搜索页对一篇明明在库里的文档永远显示"未在库中"。
   跳过分支在（且仅在）原有行没有来源时补写。
10. **来源标识是公开元数据**：它经 `_public_document` 暴露，这是对脱敏边界的一次有意放宽——
    `file_path` 与 `file_sha256` 继续不外泄（本地文件系统事实），而 arXiv id / DOI 本来就
    印在每条检索结果上。
11. **公网 `http://` 全文重新放行**（实测后由用户拍板，2026-09-16）。ADR-0019 曾把 http 限死在
    回环——这条恰好挡住了本功能最主要的用例：三条查询抽样显示**中文开放获取链接里 5/7 与 3/6
    是明文 `http://`**（国内期刊与仓储普遍没上 https），而英文样本是 0/15，等于"中文论文搜得到、
    导不进来"。链接始终来自来源 API 的记录（不是用户输入——ADR-0019 在入口就把这条关死了）、
    下的是公开论文、不带任何凭据，所以"必须 https"在这里的传输安全论据很弱。**SSRF 防线一条没少**：
    只放行公网地址，外加 http 的回环例外（E2E 假源与本地服务），私网/保留/链路本地地址照旧一律拒。
    改动后实测：一条中文 `http://` 全文干净导入（138 块）；另一台主机无论怎么换浏览器式请求头
    都回 403（上游在拦，如实报 502，非我们可控）。残余风险是完整性——明文传输理论上可被中途
    替换成别的 PDF——对本地单用户工具，这个风险接受。

**被否方案**：留着面板把它做大（没有横向空间；语料树是知识库页的活）；为了凑数硬加第四个来源
（S2/ChinaXiv 是证据不足，不是口味问题）；允许客户端给任意 PDF 链接（ADR-0019 第 4 点——
SSRF 防线收在 `{source, id}` 上）；交错里补位（见第 5 点）；单开 `document_sources` 表
（一对一的事实多一套外键面）；把能力标记硬编码在前端（能力属于来源；硬编码正是"选项不生效"
的产生方式）。

**局限**：CORE 不支持按被引排序（`sort=citationCount` 回 HTTP 500）且连打会限流，自带 2 秒节流；
它的 `downloadUrl` 有相当比例是仓储的 `http://` 地址，会被下载器的安全策略拒收（只放行公网
https 或回环 http），这些记录导不进来；`yearPublished` 是脏的（实测见过 `710300`、`202022`），
年份按前四位解析并做范围校验；arXiv 完全没有被引数据，所以"按被引排序"只在所选来源能兑现时
才可用；OpenAlex 深翻页受 200 条单页上限约束；阅读器仍然无法预览未导入的论文（详情面板渲染
的是检索响应）。

**代码**：`papers/sources.py`（`SourceCaps`/`PaperFilters`/`PaperResult.cited_by`）、
`papers/core.py`（新）、`papers/arxiv.py` 与 `papers/openalex.py`（筛选翻译、能力声明、窗口修复）、
`papers/service.py`（N 源轮转、留洞合并、`attempted`/`notes`/`source_catalog`）、
`storage/db.py` + `models/document.py` + `storage/repo.py` + `ingest/service.py`
（`source_ref`、v4 迁移、reindex 保留清单）、`web/routers/papers.py`（含 `GET /api/papers/sources`
与 `in_library`）、`web/routers/documents.py`（`ingest_web_file(source_ref=...)`、跳过路径回填）、
`web/app.py`（`/papers`）；前端 `static/papers.html`、`static/js/{papers-page,papers-api,
papers-filters,papers-detail}.js`、`css/style.css` 与四处导航；测试
`tests/unit/papers/test_core.py`、`test_papers_service.py`、`test_sources.py`、
`tests/unit/storage/test_db_migration.py`、`tests/unit/ingest/test_service.py`、
`tests/unit/web/test_papers_api.py`；E2E `tools/chrome_papers.py`（三个假源）。

---

## ADR-0021 笔记 = 带标记的普通文档：复用 `source_ref`、不加 schema 列、force 绕过内容去重

- 状态：Accepted ｜ M6 第 ① 期（2026-09-16，用户选"开工新功能"后确认两条产品决策：笔记必须**可编辑**
  ——保存即自动重新入库；编辑器必须带 **Markdown 实时预览**）
- 关联：ADR-0020（本条目把它的 `source_ref` 语义拓宽）、ADR-0004（schema 迁移纪律——**刻意没走**的路）、
  ADR-0012（笔记原样复用的分块器）

**问题**：产品的既定形态是"笔记 + AI 问答双翼"，但进入知识库的唯一方式是从外面带一个文件进来。
想写一条笔记就得离开这个应用，而问问题时学到的东西无处沉淀。M6 第 ① 期是闭合这个环的最小一步：
在知识库页写 Markdown → 保存 → 立刻可被检索、可被引用。

**决策**：

1. **笔记是"带标记的普通文档"，不是新实体。** 标记 = `source_ref = "note:<key>"`。除此之外的一切
   ——文件夹树、拖拽、改名、删除、检索、引用、阅读视图——本来就在文档上工作，现在原样作用在笔记上。
   为什么不加列 / 不加表：`_KeptProps` 已经把它列入 reindex 与同名替换的保留清单，标记因此**一行代码
   不加**就能活过全量重建；`_public_document` 已经暴露该字段；而「找论文」页是**精确匹配** `arxiv:` /
   `core:`，`note:` 前缀不可能被误认成"这篇论文已在库中"。代价是这一列的含义从"来自哪个在线记录"
   拓宽为"文档来源标识"——四处在注释里断言过旧含义的地方在同一次改动里改掉了，否则那就是一条会慢慢
   变成谎话的注释。
2. **笔记是 `uploads/` 里一个真实的 `.md` 文件**，名字 `<净化标题> (note <key12>).md`。这是 ingest
   契约推出来的：uploads 副本的主名**就是**文档身份，所以名字由服务端生成一次、**永不更换**——给笔记
   改名只改显示标题，正是 `set_document_title` 对上传件早已承诺的语义。正文逐字节保存（二进制写 +
   LF 归一），编辑器往返无损。`<key>` 是 48 位随机数，使用前会同时查既有路径与既有标记：撞了不会报错，
   而是**顶掉别人的行**。
3. **笔记用 `force=True` 入库，这不是优化。** `_ingest_one` 的字节级内容去重对语料是对的（同一份字节
   的备份副本不该污染检索），对笔记是**静默错**：第二条同内容的笔记会返回成功却从未落库；把一条笔记
   编辑成与另一条同内容，响应还会指向**别人那行**。force 在两处去重判断上都放行，同时仍走同名替换路径
   → 编辑依旧是原地更新。两条笔记的块内容相同没问题——`chunks` 只对 `(document_id, seq)` 唯一。
4. **标题交给 ingest，而不是入库后再补。** `ingest_one(title=...)`，优先级：显式标题 > 继承标题 >
   loader 推导标题。两个彼此独立的理由：分块器把每块的索引文本构造成 `《标题》｜标题路径`，事后改标题
   会把旧名字留在 BM25 与向量**真正检索的那份表示**里——改完名就搜不到新名字；而同名替换会继承**旧行**
   的标题，没有显式覆盖的话，编辑时改标题根本不生效。
5. **编辑 = 同名替换，因此每次保存 `documents.id` 都会变。** 原地更新就是"删旧行 + 插新行"。所以响应
   一律**按 uploads 路径**查回来构造（绝不用请求里的 id），页面也用响应回传的新 id 重新选中——不这么做，
   刷新会判定"选中行已被删除"，把用户正在读的阅读区一起收掉。正文哈希与标题都没变的保存会短路成 200、
   一个字节都不动，于是"打开编辑器随手点保存"的代价是零。
6. **整段保存持有 ingest 锁**（`services.ingest.exclusive()`），与 DELETE 同一套纪律。没有它，
   "保存 vs 删除"会让被删掉的笔记以**没有标记的普通文档**复活：ingest 会照 web-tmp 文件重建副本并插一行。
7. **回填读的是 uploads 副本原文。** 编辑器必须拿用户真正写下的内容回填，故 `GET /api/notes/{id}` 返回
   文件字节解码后的正文。不能用 `/content`：那是拼接 chunk 的阅读正文，按构造已经丢掉了 `#` 标题行、
   代码围栏与引用标记——拿它回填，下次保存就会静默重写用户的笔记。
8. **端点拒绝触碰任何非笔记文档。** PUT 与 GET 都检查 `note:` 前缀，不符一律 404（不是 403：不存在性
   不构成信息），因此上传件与导入的论文永远不可能被笔记 API 覆盖。
9. **被否决的替代方案**：新增 `is_note` 列（v5 迁移 + 保留清单 + repo/API 全链改造，只为换一个"能从
   活过重建的字段推导出来"的布尔值）；独立的 `notes` 表（会一并失去文件夹树、检索、引用、阅读与删除
   这五条路，全都得重写重测）；以及用 `PATCH /api/documents` + `/content` 做编辑（见第 7 点）。

**后果**：**围栏代码块不进检索**——markdown loader 会整块跳过它（防代码里的 `#` 被读成标题），因此
"整篇只有代码"的笔记会被 400 拒绝，文案必须解释这一点；uploads 副本是笔记**唯一**的权威副本（没有
"用户手里的原件"可回退），所以任何 re-index 前都要备份 `data/`；解析后的文本仍会过
`strip_repeated_lines`（≥3 次重复行）与 Setext `---` 判定，故**索引表示**可能与文件不同（文件本身不动）；
两个标签页同时编辑同一条笔记是最后写入者胜（没做乐观锁——SQLite 的 `datetime('now')` 只有秒级精度，
当版本号没有意义）；每次保存都会重建 chunk，所以旧回答里的引用 chip 指向的是已删除的 chunk id——这与
重新上传一个文件本就有的后果相同。另一个**既有**风险（同名替换时 uploads 副本被外部程序占用 → 改名
那步在旧行已删之后才失败）同样适用于笔记，本期只记录不修：它位于语料库依赖的 ingest 回滚路径上。

**代码**：`web/routers/documents.py`（notes 段：`NOTE_REF_PREFIX`、`_note_stem`、`_note_name`、
`_note_payload`、`_get_note`、`_write_note_tmp`、`create_note` / `update_note` / `get_note`；
`ingest_web_file` 新增 `force` / `title` / `folder_id`）、`web/schemas.py`（`NoteIn`、`NoteUpdateIn`、
`NOTE_BODY_MAX`）、`ingest/service.py`（`ingest_one(title=...)` 与优先级规则）、`storage/db.py` +
`storage/repo.py`（`source_ref` 语义拓宽的注释层）；前端 `static/js/note-editor.js`（新）、
`static/js/tree.js`（`isNote`、`folderOptions`）、`static/js/kb-tree.js`（`onEditNote`、
`selectDocById`、meta 显示「笔记」）、`static/js/documents.js`、`static/documents.html`、
`css/style.css`、`DESIGN.md`（中文事实源）+ `DESIGN.en.md`（英文镜像）（`note-editor: 70` 层级）；测试
`tests/unit/web/test_notes_api.py`（新）、`tests/unit/ingest/test_service.py`；冒烟
`tools/smoke_tree.mjs`；E2E `tools/chrome_notes.py`（新）。

---

## ADR-0022 应用内更新：客户端碰不到 URL + 与包同批的校验和 + 检查失败静默

- 状态：Accepted ｜ v0.1.1（2026-09-19，用户报"桌面快捷方式一直是老版本"后拍板「一键下载并安装」）
- 关联：ADR-0019 / ADR-0020（下载防线与"回环逃生门"惯例，本条目沿用）、ADR-0018（设置面板——「关于」
  段的落点）、ADR-0002（密钥纪律——更新链路全程无凭据）

**问题**：安装版用户桌面上的快捷方式指向安装目录里的副本，而那个副本**只在跑安装包时才会变**。
项目此前没有任何更新机制：改完代码我更新的是开发构建，用户那份永远是老版本（2026-09-11 起反复发生，
2026-09-19 用户点名要求解决）。用户拍板的形态：打开应用时若有新版 → 弹窗告知 → 一键下载 → 自动安装。

**决策**：

1. **检查端点代客户端说话，响应里不含任何 URL。** `GET /api/update/check` 由服务端去问 GitHub 的
   `/releases/latest`；下载地址永不出现在响应里，前端只会说"下载最新版"。客户端因此无从指定任何
   地址，SSRF 面被收窄到"服务端自己挑出来的那一条链"——下载入口只需一件事：主机白名单
   （`github.com` / `*.githubusercontent.com`，https）。
2. **回环 http 是逃生门，且只对它开。** 与 `papers/download.py` 同一取舍：非白名单主机必须是
   **http + 回环**（E2E 的本地假 GitHub 与假资产主机）。回环打不到内网，SSRF 保证不因它松动；
   顺带挡住"API 响应被换成任意公网主机"。
3. **下载完必须核对 sha256，校验和与包同批发布。** 安装包与同一 release 里的 `SHA256SUMS.txt`
   一起下，逐字节比对通过才允许启动安装器；**没有校验和文件就不自动更新**（宁可让用户手动下）。
   挡的是下载损坏 / 中途被替换；发布账号本身被攻破挡不住（校验和与包同源）——已接受的残差，记入
   limitations。任何失败都删掉半成品，绝不留一个"看起来能装"的文件。
   （**2026-09-19 修订，见 ADR-0024**：半成品改为只以 `.part` 后缀形态存在、永不参与启动；
   且**传输类**失败保留它（续传要用），只有校验和不符 / 体积超限 / 安全策略这三类才删。）
4. **启动安装器 = 双击语义**（`os.startfile`），三道闸：文件在、文件在数据目录 `updates/` 下、
   名字匹配 `Mikasa-Setup-*-win64.exe`。安装向导自己会先 `taskkill` 掉正在运行的 Mikasa，所以
   "启动"之后本进程会死——前端在这之后不再期待任何响应。E2E 用 `MIKASA_UPDATE_SKIP_LAUNCH=1`
   让这一步只记日志不启动（只**少做**一件事，是安全方向的开关）。
5. **检查失败一律静默**：离线用户不该被网络错误打扰，错误只进日志与状态接口；只有用户主动点
   「检查更新」才把失败说出来。检查结果 TTL 缓存 10 分钟（GitHub 匿名配额 60 次/小时/IP），
   「立即检查」走 `force=1` 绕开。
6. **「跳过此版本」存 localStorage，只在静默检查里生效**：手动检查会清掉该标记（主动问 = 现在就
   想知道）。设置面板新增「关于」段：当前版本号 + 启动检查开关 + 手动检查按钮。
7. **下载期间弹窗不可关闭**——否则用户看到的是"点了下载、窗口没了、什么都没发生"。下载中 Esc 与
   点遮罩都不响应；失败时给出文案，并把「打开发布页」留在手边（手动下载是永远的兜底）。
   （**2026-09-19 被 ADR-0024 取代**：这条的后半句在实现里变成了"切页就整个消失、回来也接不上"，
   正是用户抱怨的那种"断了"。现在弹窗可关，关掉 = 转后台，进度落到顶栏跨页胶囊上。）
8. **这段代码必须随 v0.1.1 一起发**：老版本里没有它，它不会自己知道有新版——这一次仍要手动装一次，
   之后才谈得上"打开就提示"。

**后果**：应用启动会向 `api.github.com` 发一次只读请求（不带凭据、不带任何本地数据），可以关——
本地优先的产品里这是一处**被明确披露的网络行为**，写进了使用说明；国内直连 github.com 的下载很慢
（实测小文件 18 秒），大安装包可能要几分钟，进度条与"失败回退浏览器下载页"就是为它准备的
（**2026-09-19 补充，见 ADR-0024**：现在断线会带 Range 接着下、并自动退避重试，"慢"仍在，
"断一次就白等几分钟"没有了）；自动安装
只覆盖 Windows（`os.startfile` 仅 Windows）；`updates/` 只保留最近一次下载的文件（旧残留在下一次
下载开始时清理）；发布流程多了一条纪律：**每次发布必须带上 `SHA256SUMS.txt`**，否则老用户点不动
自动更新。

**代码**：`src/mikasa/update/`（`release.py` 版本比较 / 发布解析 / 资产挑选，`install.py` 白名单下载
+ 校验 + 启动 + 单槽任务管理器，`checker.py` TTL 缓存，`errors.py`）、`web/routers/update.py`
（四个端点）、`web/services.py`（`updates` / `update_jobs`）、`web/app.py`（挂路由）；前端
`static/js/update.js`（新）、`static/js/qa.js`（启动接线）、`static/index.html`（设置面板「关于」段）、
`css/style.css`（`.upd-*`，z-index 95）、`DESIGN.md`（中文事实源）+ `DESIGN.en.md`（英文镜像）
（新层级与新组件）；
测试 `tests/unit/update/test_release.py`、`tests/unit/update/test_install.py`、
`tests/unit/web/test_update_api.py`；E2E `tools/chrome_update.py`（新：假 GitHub + 节流假下载 +
全程不启动任何可执行文件）。

---

## ADR-0023 第四个来源 DOAJ：免密钥的中文开放获取期刊 + 各来源的授权边界

- 状态：Accepted ｜ v0.1.3 周期（2026-09-19，用户要求"别人装上就能用，不用自己去注册"）
- 关联：ADR-0019 / ADR-0020（来源协议、能力声明、交错分页——本条目只是往注册表末尾加一个来源，
  协议零改动）、ADR-0022（同一件产品诉求的另一半：更新也要"打开就能用"）

**问题**：三个已有来源各有门槛——OpenAlex 自 2026 年起要求免费 key（匿名只有小额试用额度，
用光就 429）、CORE 匿名限流极紧、arXiv 只有英文预印本。对"别人装上就能用"来说，中文期刊这一块
是空的，而**让别人先去注册一个 key** 恰恰是用户明确否决的体验。

**决策**：

1. **接 DOAJ**：开放获取期刊的官方目录，**免密钥**、官方 REST API、元数据声明为 CC0。
   实测中文可用（"充填体"185 条，命中《工业水处理》等中文刊）。注册表追加在末尾——
   顺序即交错顺序，动前面的顺序会改变既有来源的全局位置映射（service.py 的注释纪律）。
2. **能力按实测声明，不按文档猜**：年份区间走查询语法（实测 185 → 115 条，有效）；
   按被引/按时间排序与语言过滤**实测超时**，一律声明为不支持——界面置灰并写明原因，
   宁可少给三个选项，也不假装能过滤。
3. **"开放获取"与"能一键导入"是两件事**：DOAJ 的 `link[]` 实测全是出版商落地页
   （抽样 25 条、0 条以 .pdf 结尾），所以 `pdf_url` 只在真出现 .pdf 时才给。界面因此有
   三种说法而不是两种：有直链 PDF（可导入）/ 开放获取但只有落地页（禁用导入 +
   "点「打开原页」去出版商页面"）/ 无开放获取全文。
4. **授权边界写进代码与文档**（用户直接问过）：能接的必须是"明确许可程序化访问"的来源——
   DOAJ（元数据 CC0）、OpenAlex（CC0）、arXiv（官方 API 条款 + 可识别 UA + 礼貌限速）、
   CORE（开放获取聚合，API 条款允许）。**不接**知网/万方/国家哲学社会科学文献中心一类：
   它们的题录是商业授权或内部接口，抓取即违约。
5. **明确不做"绕过反机器人机制"这件事**：候选里最诱人的是国家哲学社会科学文献中心
   （免费中文期刊、社科覆盖强），实测其检索接口是匿名可调的 JSON——但它挂在**网宿 WAF**
   后面：真浏览器能拿到结果、非浏览器客户端拿到空壳（同一条 URL、同一组参数，无头 Chrome
   打开有 265 篇命中，urllib 发就是 `total: 0`）。绕过去要解 WAF 挑战 cookie，那是
   **对抗站点自己的反机器人机制**，不是"技术难点"——不做。附带：它的前身 NSSD 已于
   2024-07-11 停服，资源并入现站。
6. **上游元数据是不可信输入，先剥标记再展示**：DOAJ 把论文 HTML 原样塞进摘要字段
   （实测界面直接显示 `1<sup>#</sup>`），于是有了 `utils/text.strip_markup`——只删标签、
   还原实体、折叠空白，**不做富文本渲染**（渲染等于把 XSS 面引进界面）。

**后果**：中文文献第一次有了"零配置"来源，代价是这一源的全文多数要跳去出版商页面（见第 3 点）；
来源总数到四个，页级 30 秒截止与并发取源保证"多一个源"不等于"多等一段"；`schemas.py` 里两张
硬编码的来源名单与注册表的漂移被一条守卫测试钉住（加 DOAJ 时实测踩中：前端勾上它就是一个 422）。

**代码**：`papers/doaj.py`（新）、`papers/service.py`（注册表末尾 + 并发取源 + 页级截止）、
`papers/http.py`（统一 UA + 网络级重试，只重试网络错误与 406）、`utils/text.py`（`strip_markup`）、
`web/schemas.py`（两张来源名单）、`web/routers/papers.py`（`_ID_VALIDATORS`）、前端
`papers-page.js` / `papers-detail.js`（标签 + 三种全文状态的文案）；测试
`tests/unit/papers/test_doaj.py`（新）、`tests/unit/papers/test_papers_http.py`（新）、
`tests/unit/web/test_papers_api.py`（来源名单守卫）；E2E `tools/chrome_papers.py`（第四个假源）。

---

## ADR-0024 更新下载可续传、任务可接续：「观察窗」与槽位自愈

- 状态：Accepted ｜ v0.1.4 周期（2026-09-19，用户报"下载巨慢、切到别的功能就断了、再点报
  『更新下载已经开始了』"后拍板）
- 关联：ADR-0022（本条目**修订它的第 3、7 点**，其余防线原样保留）、ADR-0019 / ADR-0020
  （下载防线与"回环逃生门"惯例）

**问题**：用户那份 v0.1.3 想升新版，点「下载并安装」后撞上三件事——慢、切页就"断"、再也点不动。
查下来是三个不同的毛病：

1. **服务端其实没断**：下载跑在 `BackgroundTasks` 的同步任务里（anyio 工作线程），与请求、
   连接都无关。实测：客户端读完 202 立刻硬断连接、之后 12 秒零请求，下载照常跑完。
   "断"的是**界面**——四个功能页是整页跳转，一切页 JS（含轮询与弹窗）就没了，而回到问答页
   没有任何"还有任务在跑"的恢复逻辑（评测页有这一套，更新这块当时没写）。
2. **"再也下不动"是 409**：槽里还有 running 任务时 `POST /api/update/download` 回 409，前端把它
   渲染成「启动下载失败：更新下载已经开始了」。而槽只记状态、不记"工作线程还在不在"，线程一旦
   悄悄消失就永远停在 running——没有任何自愈路径。
3. **慢是真的慢，而且断一次就白等**：本机实测（经系统代理）单连接 ~300 KB/s，83.7MB 的安装包
   ≈ 4–5 分钟，中途抓到对端 reset；而下载失败即删半成品、没有 Range 续传、没有重试——
   **每次断线都从 0 重来**，这是最大的浪费。

**决策**：

1. **断点续传**：先落 `Mikasa-Setup-<版本>-win64.exe.part`，之后带 `Range: bytes=N-` 接着下
   （206 追加 / 200 整份重写 / 416 当"已下完"交给校验和判真伪）；传输类失败退避重试
   （3 次尝试、退避 2s/5s），**重试与跨会话都从 `.part` 接着下**。半成品**只能是后缀**——
   `Path.with_suffix` 会把 `.exe` 吃掉、留下"看着就像安装包"的名字，`launch_installer`
   因此另加一条显式闸门拒绝它：这条不变量不该交给 `SETUP_ASSET_RE` 的巧合。
2. **校验和那条线一个字节都不动**：仍与同批 `SHA256SUMS.txt` 逐字节比对，**先校验后改名**
   （`os.replace`），坏文件永远拿不到最终名字。`.part` 里可能是上一轮的半截、也可能远端已经
   重传过包——本模块无从判断，所以判真伪的**永远**是 sha256：不符就删掉整份重下一次
   （"毒前缀"闭环：不删的话用户每点一次都撞同一堵墙），再不符就如实报错。
   完整性判据改为**最终文件大小**（半成品 + 本次写入），不是"本次写了多少"——不改这条，
   续传会被自己的完整性检查判死。
3. **槽位自愈**：`UpdateManager` 除了状态，另记**工作线程存活**（`worker_started/finished`，
   由任务外壳保证配对）与**状态最后变化时刻**。线程没了且过了宽限期（10s，覆盖
   BackgroundTasks 在响应之后才起线程的空档）→ 新任务**接管**旧槽；`done` 且成品还在 →
   不接管（否则白下 80MB，前端该去点安装）。每个槽带 token，过期 token 的写入一律丢弃
   ——否则被接管的僵尸线程会把新任务的状态改回去，甚至往同一个 `.part` 里续写。
4. **POST 幂等**：下载端点不再回 409。已有活任务 → 202 + `adopted: true`（前端只跟随状态）；
   要不要重下由接管规则决定。前端也照这个来：点「下载并安装」先问状态，running/verifying/done
   就只跟随，不先发 POST。
5. **"观察窗"**：下载是服务端的后台任务，界面只是观察窗——弹窗**可以关**（关掉 = 转后台），
   顶栏留一枚跨页胶囊（四个功能页都看得见进度，点它回到弹窗）；切页、刷新、关弹窗都不打断下载，
   回到问答页自动接上（用户主动收起过就不再自动弹，胶囊一直在）。胶囊是**独立容器**，不能塞进
   `.health-pill`（那个被 `initTopbar` 每 20 秒用 `innerHTML=""` 重绘）。其余三页只挂胶囊、
   **不打 `/api/update/check`**：关掉"启动时检查"之后，任何页面都不该偷偷联网。
6. **弹窗不在场就不自动装**：下载完成时弹窗还开着（用户在看）→ 照旧自动启动安装器；否则只把
   胶囊换成「更新包已就绪 · 点此安装」。理由：安装器第一件事是 taskkill 掉 Mikasa
   （ADR-0022 第 4 点），用户不在场时把他的应用杀掉是冒犯。
7. **顺带修掉一句假话**：前端原来有个 30 分钟死线，到点写「下载超时」——可服务端还在下。
   删掉：终态一律以服务端为准，停滞由后端的 `stalled` 说，重试如实显示成
   「连接中断，正在重试（第 N 次）」。

**明确不做**：

- **多连接并行下载**：同一链路实测 4 连接并行 ≈442 KB/s vs 单连接 ≈300 KB/s，只有 1.4×，
  而失败分支要翻倍（有的 CDN 节点对 Range 直接回 `501 Unsupported client range`）。这条链路上
  续传 + 重试才是主要收益。
- **把下载从 BackgroundTasks 挪到独立线程**：实测客户端断开后它照样跑完，改它只会牺牲测试的
  确定性（TestClient 里后台任务是同步执行的）。
- **取消按钮**：卡死由"单块读超时 60s → 重试 → 仍失败就落 error"覆盖，槽位自愈保证再点得动。

**后果**：断线不再白等——半成品留在 `updates/<资产名>.part`，下次（含跨重启）接着下；
代价是失败时磁盘上留一个 ~90MB 的半成品，直到下一次下载开始才清理（已记入 limitations）。
自动更新仍只覆盖 Windows。**这次修复要下一个版本才生效**：用户手里那份是旧代码，这一次仍得
用浏览器手动装（浏览器自带续传）。

**代码**：`update/install.py`（`_part_path` / `_open(range_start)` / `_download_once` 的三种响应
分支 / 重试与毒前缀处理 / `JobTicket` 与 `UpdateManager` 的存活登记与接管）、
`web/routers/update.py`（`_run_job` 外壳 + 幂等 POST）；前端 `static/js/update.js`（tick 单循环
同时驱动胶囊与弹窗、可关弹窗、`initUpdateBadge`）、`documents.js` / `papers-page.js` / `eval.js`
（各一行接线）、`css/style.css`（`.upd-pill`）；测试 `tests/unit/update/test_install.py`、
`tests/unit/web/test_update_api.py`；E2E `tools/chrome_update.py`（假源加 Range 与"1MiB 处掐断
一次"，新增幂等 / 续传 / 跨页胶囊 / 收起弹窗 / 点了才装五段断言）。

---

## ADR-0025 许可证改为 AGPL-3.0：发布物捆了 AGPL 的 PyMuPDF

- 状态：Accepted ｜ 2026-09-20（用户问"MIT 与 AGPL 的 PyMuPDF 冲突怎么办"后拍板）
- 关联：ADR-0022 / ADR-0024（应用内更新与发布流程——**公开发布二进制**正是触发义务的那件事）

**问题**：项目一直挂 MIT，而 `pyproject.toml` 的核心依赖里有 **PyMuPDF**（"AGPL-3.0 或 Artifex
商业许可"二选一）。它负责 PDF 文本提取（`ingest/loaders.py`）与阅读器的原页渲染/文字层
（`web/routers/documents.py`）——是核心件，不是可选件。从 v0.1.1 起我们开始**公开分发打包好的
exe/zip**，那一刻 AGPL 第 5/6 条的义务就生效：分发含 AGPL 组件的二进制，整个发布物须按 AGPL
授权并向接收者提供对应源码。MIT 的声明对这部分无效，而且**没人会在本地开发时发现**——
它只在"分发"这个动作上成立。

**决策**：项目整体改为 **AGPL-3.0-or-later**。

1. `LICENSE` 换成 AGPL-3.0 全文（取自 gnu.org 的规范文本，逐字不改，LF 行尾）；
   `pyproject.toml` 的 `license = "AGPL-3.0-or-later"` 与 classifier 同步；README 双版写明
   许可证、§13 的网络服务义务与第三方声明的位置。
2. **不买商业许可，也不换掉 PyMuPDF**：阅读器"页面上选字要对上原文框"依赖它的词框坐标，
   PDF 表格几何重建与中文文本层质量也重度依赖它——换成 `pypdf`（文本）+ `pypdfium2`（渲染）
   是实打实的迁移工程，且质量很可能下降（那是按周计的活，收益却是负的）。
   **用许可证适配依赖，而不是为了许可证换掉依赖。**
3. 合规沿用既有机制、不新增流程：`tools/make_third_party_notices.py` 生成的
   `THIRD_PARTY_NOTICES.md` 早已把 PyMuPDF 单列（含"copyleft 组件要求提供对应源码"的提示），
   `tools/make_release.py` 与 `packaging/Mikasa.iss` 都已把 `LICENSE` 与那份声明放进发布物；
   §13 的"提供对应源码"由公开仓库天然满足。

**后果**：Mikasa 成为强著佐权项目——允许自由使用/研究/修改/再分发，但**改过的版本也必须 AGPL**，
且把它做成网络服务给别人用时要提供源码。少数法务严格的公司会因此不采用这份代码；对一个公开的
个人项目，这是可接受的代价，也比"挂着 MIT 却捆着 AGPL"诚实。**已发布的 v0.1.1–v0.1.4 里带的
仍是 MIT 声明**（改不回来），从下一个版本起仓库与发布物一致。

**代码/文档**：`LICENSE`（新文本）、`pyproject.toml`、`README.md`（中文事实源）/ `README.en.md`（英文镜像）、
`packaging/Mikasa.iss`（注释）、`THIRD_PARTY_NOTICES.md`（生成物，内容一直是对的）。

---

## ADR-0026 自动题库 + 逐题校验：评测不再绑死一份语料

- 状态：Accepted ｜ 2026-09-19（用户要"评我自己的资料"后落地）
- 关联：ADR-0003 / ADR-0018（可插拔 provider——出题复用同一个 LLM 客户端与设置面板接的模型）

**问题**：两件事卡在一起。

1. **人工题库只能评它写作时的那份语料**：`evals/questions.yaml` 的每道可答题都携带
   从示例语料逐字摘录的锚句——换一份语料做知识库，题目与标准答案就全部对不上，
   而"评我自己的资料"是用户最想要的评测形态。这条路只能靠自动生成：没人替用户
   自己的文档写过标准答案。
2. **全库指纹的守卫范围远大于风险范围**：原先评测前比对整库的 (chunk_id, 内容哈希)
   序列，往库里加任意一篇文档、改个标题，整份题库就作废报错；而真正的风险只涉及
   "题目引用的那几十个分块有没有变"。

**决策**：

1. **自动题库**（`eval/synth.py`）：从当前语料抽样分块，让模型读一段、出一道
   "只有这段能回答"的题，**那个分块即标准答案**（gold = chunk id + content_sha256）。
   采样三条纪律：**跨文档轮转**（否则题库堆在最长的那篇上，跑出来的其实是那一篇的
   检索质量）、**块长区间**（太短的块写不出有信息量的题，且被检索到几乎没有难度）、
   **固定 seed**（同一份语料可复现，便于横向比较两次改动）。
2. **两类题缺一不可**：可答题之外必须出**不可答题**（给几个分块，要一个"它们都
   答不上来"的问题）——拒答纪律是这套系统的核心承诺（见 limitations），没有不可答题
   就完全测不出来。任一类凑不出来即整体失败，不落盘。
3. **防错配改成逐题校验**（`GoldenItem.gold_hashes` 与 `gold_chunk_ids` 逐位对应）：
   评测前逐题核对"这块还在、内容没变"，变了的题**跳过**，报告首部写明跳了几道、
   正文逐条列出题号与原因。**跳过而不是放行**——静默用错位的标准答案算分会跑出
   "看着正常、其实毫无意义"的数字，这是评测工具最怕的事；**留痕而不是静默**——
   静默缩小分母会让分数看着变好。全部可答题被跳过才是硬错误（语料整体重灌）。
   `corpus_sha256` 降级为元信息（报告里展示"这套题是为哪份语料写的"）。
4. **诚实边界写进报告**：自动题是模型照着自己看过的原文出的，比人工题**简单**、
   分数天然偏高——**只能横向比**（同一份题库下比较两次检索改动），不能跟人工题的
   绝对分比。报告首部以 `source: synthesized` +「**自动生成**」显式标注。
5. **要真实模型**：出题是几十次 LLM 调用，离线 mock 档直接 400 说清楚（不让用户在
   "跑到一半全是废题"里摸索）；题库落数据目录 `eval/golden-auto.json`（用户数据，
   不进仓库）；单槽任务 + 1s 进度轮询——**与评测槽分开**，两条进度的含义不同
   （"已生成几道" vs "正在作答第几题"），合成一个状态机会把轮询端搞乱。

**明确不做**：

- **不自动重生成题库**：校验失配时提示重建（CLI 跑 `tools/build_golden.py`、
  Web 点「为我的资料生成题库」），不静默重跑一轮 LLM。
- **不做难度分层**：模型自评不可信，自动题硬编 medium。
- **不替代人工题库**：内置题仍是唯一覆盖 easy/medium/hard 与三类不可答的高难标尺
  ——它需要人挑边角、跨段推理，自动题做不到。

**后果**：任意语料（含用户自己的库）都能评测了；代价是自动题的绝对分不能跟人工题
比，且题库与语料的绑定变松——靠"逐题跳过 + 报告留痕"兜住该拦的错配，而不是靠
"整份拒绝"。已发布的旧题库（无 `gold_hashes`）退化成只校验分块 id 是否存在。

**代码**：`eval/synth.py`（采样 / 出题 / 不可答题 / 落盘）、`eval/golden.py`
（`gold_hashes` / `check_against_corpus` / `source` / `model`）、`eval/runner.py`
（逐题校验替代指纹比较，`skipped_items` 进 `EvalResult`）、`eval/report.py`
（跳过题小节 + 首部标注）、`index/manager.py`（`Corpus.content_hashes()`）、
`web/routers/eval.py`（`GET /api/eval/goldens`、`POST /api/eval/synthesize` + status、
`POST /api/eval/runs?golden=`）、`web/services.py`（`SynthJobManager` 单槽）、
`static/eval.html` + `static/js/eval.js`（题库选择 / 出题面板 / 进度轮询 / 收尾封顶）；
测试 `tests/unit/eval/test_synth.py`、`test_golden.py`、`test_runner.py`、
`tests/unit/web/test_eval_api.py`；E2E `tools/chrome_eval.py`（本机假 OpenAI 兼容
服务：自动出题 → 整场评测 → 报告，全流程）。

---

## ADR-0027 拍照转笔记：视觉模型独立成段、识别只是草稿、原图锚在笔记 key

- 状态：Accepted ｜ 2026-09-20（M6 ②，用户点名"拍照/图片转笔记文本"）
- 关联：ADR-0021（笔记 = 带标记的普通文档）、ADR-0018（设置面板模型接入）、
  ADR-0026（同一套"自动生成的内容必须自报家门"的诚实纪律）

**问题**：复习时拍下的板书、书页、手写提纲，现在只能当图片躺着——既不能提问
也不能检索，手打一遍又违背"随手记"的初衷。要把它接进笔记链路，四个问题得先答：

1. **视觉能力接在哪**：local 档（Ollama qwen3）没有视觉能力、offline 档是 Mock，
   而用户日常跑的正是 local 档；
2. **识别结果能不能直接入库**：不能——OCR 一定有错字（手写尤甚），直接落库
   等于把错误固化进知识库；
3. **原图留不留**：用户拍板**留**（要能事后对照），但图片不是文档，不能混进语料；
4. **存哪儿**：笔记是"同名替换"（每次保存换 `documents.id`），挂在 id 上的东西
   第一次编辑就成了孤儿。

**决策**：

1. **视觉独立成一段配置**（`VisionConfig`：`backend: none | api | local`），
   **默认 none**。不与 llm 合并：一个收文本、一个收图，混在一起"生成用 DeepSeek
   ＋识图用 SiliconFlow"这种真实组合无处落脚——而用户日常正是 local 生成 + 云端
   识图。`api.yaml` 默认接 SiliconFlow `Qwen2.5-VL-32B-Instruct`（与检索侧共用
   `SILICONFLOW_API_KEY`）；`local.yaml` / `offline.yaml` 明确 none（守住离线零调用）。
2. **Provider 独立成层**（`providers/vision.py`），**不动 llm 的 Protocol**：多模态
   content 数组过不了 `list[dict[str, str]]` 的静态标注，而放宽它会牵动 ask 链路与
   MockLLM 对数组内容崩溃的那处守卫。只从中抽出 `build_openai_client` 共用密钥解析
   （复制一份必然漂移成"识图能用、问答不能用"）。
3. **识别只是草稿，不是入库**：编辑器里「识别图片」→ 文本插到光标处 → 用户改 →
   保存才入库。图片**不进正文**：正文是索引的输入，图片标记进去就是检索噪声，
   还会让每次保存都重嵌一遍。
4. **原图锚在笔记 key**（`data_dir/note-media/<key>/<序号>-<sha8>.<ext>`）：
   `source_ref = "note:<key>"` 的 key 创建时定下、改名与编辑都不变（ADR-0021 已确认
   "副本文件名永不重生成"）。按内容去重；一篇可存多张；删文档连带清目录
   （**失败只记日志不阻断**——与 uploads 副本同款政策，且风险更低：note-media 不在
   uploads 里，reindex 不会把图片"复活"成文档）。
5. **面板新增「视觉模型」一组，但密钥只写不删**：`SILICONFLOW_API_KEY` 被
   embedding/reranker/judge 共用，在这里清空会打挂整条检索链，故障表现却是
   "搜索结果变差"——最难联想到密钥的那种静默降级。所以空串直接 422 并指路
   「模型」段。前端也在发送前就不带空密钥。面板是**纯新增**
   （`vision-settings.js` + `#v-*` DOM），不动既有模型段的回归面。
6. **识图端点**（`POST /api/notes/ocr`）：**魔数**验真（不看扩展名/Content-Type）、
   12MB 上限（**独立于 BodySizeLimit**——后者是 `create_app` 时烘焙的、不随热更新
   重算）、图片**不落盘**（内存 → base64 data URL）、未接入 → 400 可操作文案
   （指路面板或 `ollama pull qwen2.5vl:7b`）、上游失败 → 502（app 层处理器）。
7. **前端三条入口汇到一个函数**（按钮 / 拖拽 / 粘贴截图 → `addImage()`）；
   压缩复用从 settings.js 抽出的 `image-util.js`（两份拷贝迟早漂移成"一边压到
   1600、一边把手机原图整张发出去"）。识别失败**保留原图**（用户可能本就想存）。

**明确不做**：

- **不把原图当档案**：存的是压缩后 JPEG（长边 1600 ≈ 150dpi），"事后对照"够用；
- **不做孤儿目录清扫**：`ingest --reindex` 清空行会让 note-media 目录无人认领
  （记入 limitations，留二期）；
- **不做批量多图一次识别**：前端串行（识别十几秒一张，并发只会互相排队）；
- **不实测 Ollama VLM**：本期只给 `ollama pull qwen2.5vl:7b` 的指引。

**后果**：任意档位都能配识图——api 档开箱可用，local 档要么一键切云端、要么 pull
本机 VLM；图片与正文解耦，检索质量完全不受影响。代价：设置面板只在问答页
（知识库页没有入口，跨页配置是既成事实，用文案指路）；除删除笔记外没有自动清理
原图的路径（用户可在编辑器里逐张删）。

**代码**：`config/settings.py`（`VisionConfig` / `note_media_dir` /
`write_section_overlay` 的读-改-写）、`providers/vision.py`（新，含魔数嗅探与
`NoVision` 的可操作报错）、`providers/llm.py`（抽出 `build_openai_client`）、
`web/routers/documents.py`（`/api/notes/ocr` + media 四端点 + 删除连带）、
`web/routers/settings.py`（`/api/settings/vision` 三端点）、`web/schemas.py`、
`web/services.py`（`services.vision` + `rebuild_vision`）、前端
`static/js/{image-util,vision-settings,note-editor,reader,settings}.js` +
`index.html` + `style.css`；测试 `tests/unit/providers/test_vision.py`、
`tests/unit/web/test_note_ocr_api.py`、`test_note_media_api.py`、
`test_vision_settings_api.py`、`tests/unit/config/test_user_config.py`；
E2E `tools/chrome_note_ocr.py`（假多模态服务 + 真浏览器全流程）、
`tools/chrome_model_settings.py`（扩视觉段，含"配视觉不伤模型段"的端到端红线）。

## ADR-0028 公式在本地排版：把 KaTeX 内置进发布物

**背景**：用户在自由问答里问了道高等数学题，答案内容正确、结构也清楚，但里面
`$$…$$` 的块级公式全是**源码原样显示**——用户原话"为什么这个文字的排版是这样的，
特别的乱"。查下来不是回归：本项目**从来没有渲染过公式**。`renderAnswer` 是一套
手写的轻量 Markdown 渲染器（围栏代码、行内代码、粗体、标题、列表、表格、`---`、
引用角标），`$…$` / `\frac{}` 一律当普通文字输出；全仓 grep
`KaTeX|MathJax|LaTeX` 是 0 命中。

**决定**：把 KaTeX 0.18.7（`katex.min.js`、`katex.min.css`、20 个 `.woff2` 字体、
`LICENSE`）内置到 `web/static/vendor/katex/`，并在 `renderAnswer` 里以**前置一遍**
的方式排版公式：从**原始文本**抽出 LaTeX → 换成占位符 → 走既有的
（esc → 受控替换）管线 → 最后把 KaTeX 的 HTML 还原回去。

**为什么是前置字符串处理、而不是渲染后再扫 DOM**：KaTeX 的 `renderToString` 是纯
字符串函数，这样"无构建链、不依赖 DOM"这条性质原封不动（Node 冒烟里也能跑真
KaTeX）。而且必须在 `esc()` **之前**抽：放到之后，交给 KaTeX 的会是 `x &lt; y`，
渲染出来是错的。

**两道闸**：先摘掉围栏代码与行内代码（`echo $HOME` 不是公式）；成对的 `$` 也不够
——内容里有中文且不含 LaTeX 命令、或整体是纯数字的，按散文/金额处理
（`价格$5到$10之间`、`$1000$`）。这条判据的**第一版太严**，在用户那条回答里
静默漏掉了 8 处真公式（`(-1, 1]`、`n!`、`2n+1`、`o(x)`），已改成"先排除、其余
默认放行"，并用冒烟断言把这个漏法钉死。

**考虑过并否决**：让模型别写 LaTeX（恰好在最需要公式的学科里变得难读）；MathJax
（更重，且它按 DOM 遍历的设计跟这个"返回字符串"的渲染器不合）；CDN 加载（这个应用
必须离线可用，桌面窗口没有可靠的联网前提——本项目已经在 GitHub 连通性上栽过一天）。

**代价与收益**：公式密集的回答会带上约 150 KB 的 KaTeX HTML（用户那份泰勒展开：
15 个块级 + 42 个行内 → 渲染后 148 KB，原文 6 KB），发布包增大 545 KB；换来的是
公式可读、可选、可复制，且断网也能用。KaTeX 缺席时（脚本没加载 / Node 冒烟）原样
显示 LaTeX 源码，**绝不吞内容**。

**代码**：`web/static/vendor/katex/**`（内置的 MIT 资源，已由
`tools/make_third_party_notices.py` 记进 `THIRD_PARTY_NOTICES.md`）、
`web/static/js/common.js`（"公式渲染"段）、四个页面（vendor 样式排在 `style.css`
**之前**，好让项目里的字号覆盖生效）、`css/style.css`（`.katex` / `.katex-display`，
长公式横向滚动而不是撑破气泡）；测试 `tools/smoke_render.mjs`（假 KaTeX：接线、
代码遮蔽、货币误判）、`tools/smoke_math.mjs`（真 KaTeX，24 条断言）、
`tests/unit/web/test_frontend_static.py`（文件在 + 四页都引 + 样式顺序）。

## ADR-0029 本地档改走 Ollama 原生接口：think / num_ctx 两个旋钮 + 作答呈现规范

**背景**：2026-09-20 用户的两条反馈——"本机答得比 DeepSeek 少太多"与"排版不太行"。
查下来不是同一个问题，但根子都在本地通道的能力上：

1. **思考模式吃掉全部输出预算**。qwen3:8b 自带 thinking，而 Ollama 的 OpenAI 兼容面
   （`/v1/chat/completions`）**不认** `think` 参数——于是模型每次都先想半天：实测同一个
   问题，**14.2 秒、617 字推理、答案 0 字**（400 token 的输出预算被推理吃光）；把思考关掉
   是 **1.4 秒直接给答案**。用户在界面上看到的就是"转很久，然后什么都没有"。
2. **上下文被静默砍半再砍半**。Ollama 运行时的默认上下文对本机是 4096，而单次请求真正
   能装下的提示词还要少——**实测同一段提示词，不传 `num_ctx` 只处理 2050 token**。
   本档的提示词是"系统提示词 + 14 个检索块 + 历史摘要"，轻松上万 token，等于每次提问都被
   砍掉大半（且通常从开头丢，引用与拒答规则可能从未进过模型）。这也是"给的内容太少"的
   一大半根因。
3. **作答形态全靠模型自觉**。DeepSeek 会自己分节、列表、上表格；8B 只会平铺直叙。这不是
   能力差距的必然——大模型是自己"选了"这些形态，小模型需要把要求写明才会照做。

**决定**：

- local 档的生成端从 OpenAI 兼容面**切到 Ollama 原生 `/api/chat`**（新类
  `OllamaNativeLLM`，`providers/ollama.py`）。api 档不受影响，仍走 OpenAI 兼容。
- 新增两个 **local 专属**旋钮（api 档忽略、面板里只在"本机"来源下出现）：
  - `llm.think`：三态（`None` 不传参、`true` / `false`），**local.yaml 默认 `false`**；
  - `llm.num_ctx`：`None` 或 token 数，**local.yaml 默认 `16384`**。
  "没配置"与"关掉"是两件事：`None` 时不写进请求体，跟随 Ollama 默认。
- 两条系统提示词（kb 与 free）追加同一份**输出呈现规范**（`OUTPUT_FORMAT_CONTRACT`）：
  结论先行、`## ` 分节、该用表格就用表格、公式走 LaTeX、结构用代码块画文本示意图、
  求全不求短。与规则 6 不冲突——"资料原文是表格就原样呈现"仍是硬约束，规范只补
  "该用形态时别写成排比句"。

**为什么必须换通道**：`think` 与 `options.num_ctx` **只有原生接口认**，兼容面会**静默忽略**
（不报错、不生效）。也就是说，只要还走 `/v1/chat/completions`，这两个旋钮就永远是摆设，
第 1、2 条症状无解。

**为什么用 stdlib urllib 而不是现成的 openai SDK**：原生接口的返回形状 SDK 不认——
流式是 **NDJSON** 逐行、思考内容在 `message.thinking`（与 `message.content` 并列）。
本地服务无密钥、无代理，urllib 足够，也顺带避开 httpx 那套地址选择的老坑
（`normalize_base_url` 那条，见 limitations §七）。

**实测数据**（2026-09-20，Ollama 0.34.2 + qwen3:8b，4060 8G）：

| 提示词 | num_ctx | 实际处理的 token |
| --- | --- | --- |
| 5296 token | 不传（运行默认 4096） | **2050** |
| 5296 token | 4096 | 2050 |
| 5296 token | 8192 | 5296（完整） |
| 5296 / 8992 token | **16384** | 5296 / **8992（完整）** |
| 8992 token | 32768 | 8992（完整） |

16384 这个值是显存与效果的分界：qwen3:8b 的 KV cache 在 16384 下约占 2.4GB，再往上
（32768 约 4.7GB）就要和 5GB 的模型权重抢显存。`Mikasa.bat` 给源码版设的
`OLLAMA_CONTEXT_LENGTH=16384` 是同一个值——但那只救得了从 bat 启动的 Ollama，
打包版（双击 exe、Ollama 由托盘自启）拿不到，所以**这行必须写在配置里**。

**代价与边界**：

- **只有 Ollama 走这条路**。LM Studio / vLLM 等本机服务要用 api 档 + 自定义地址
  （它们没有 `think` / `num_ctx` 这两个概念）。
- **思考模式默认关**是取舍：难题、推导类问题上开思考确实更稳，但代价是 10 倍耗时且
  可能把预算烧光。默认关、需要时去设置面板打开。
- **面板不放行内网地址**（SSRF 闸门，见 limitations §九）：局域网里的 Ollama
  要直接改配置文件。
- 呈现规范是提示词层面的约束，**不保证小模型每次照做**；它是把"该有的形态"从
  "模型自己想起来"变成"写在要求里"，实测同一问题 8B 的表格/分节使用率明显上升。

**代码**：`providers/ollama.py`（`OllamaNativeLLM`）、`providers/__init__.py`（工厂分流）、
`config/settings.py`（`LLMConfig.think` / `num_ctx`）、`config/profiles/local.yaml`（默认值）、
`pipeline/prompts.py`（`OUTPUT_FORMAT_CONTRACT`）、`web/routers/settings.py` +
`web/schemas.py`（面板字段与探测走同一条通道）、`web/static/{index.html,js/model-settings.js}`
（「本机选项」两个下拉）；**真机复现** `tools/smoke_ollama_native.py`（2026-09-21 实测：同一问题 think 关 **1.6s / 32 字**、开 **28.3s / 51 字**）；测试 `tests/unit/providers/test_ollama_native.py`、
`tests/unit/providers/test_factory.py`、`tests/unit/web/test_settings_api.py`。
E2E 的假服务同步学会两种协议（`tools/fake_llm.py`）。

## ADR-0030 向量模型随包携带：固定 revision + 构建期取件 + 运行时铺设

**背景**：2026-09-15 用户实测"文档入不进去"——日志栈底是
`fastembed → huggingface_hub.model_info` 的 `httpx.ConnectTimeout`。根因不是 Ollama、
也不是解析：**首次入库要下 ~91MB 的 bge-small-zh-v1.5**（那只是向量模型，提问才用
Ollama），而国内直连 huggingface.co 常超时；更糟的是这个下载发生在**上传文档的请求
里**——整条入库链路以一句看不懂的网络异常失败，用户以为软件坏了。

当时的两层缓解（失败自动换 hf-mirror 镜像重试 + 缓存从系统 Temp 挪进数据目录）只是
降低概率，**没有消除"第一次必须联网成功"这个前提**。用户 2026-09-15 提出「不装任何
外置东西、下载即用」，本次落地的第一块就是把这个模型塞进包里。

**决定**：

1. **模型随发布物分发**（安装版与免安装 zip 同源）：载荷 `_internal/models/embed/`，
   布局是 fastembed 认的 HuggingFace 缓存形状（`models--Qdrant--bge-small-zh-v1.5/`）。
2. **revision 固定**（`46fbe35f…`，写死在取件工具里）：所有用户的向量必须出自同一份
   权重。跟 main 走的代价是上游一更新，新用户的查询向量就与老用户的索引不同源——
   不报错、只是检索悄悄变差（ADR-0014 的单模型语义）。换模型 = 改这个常量 + 重建索引
   的有意决策。
3. **构建期取件**：`tools/fetch_embed_model.py` 把快照取到 `build/embed-model/`
   （顺序：目标已有 → 什么都不做；本机已有同 revision 缓存 → 直接复制，离线也能构建；
   否则联网，直连失败自动换镜像）。`Mikasa.spec` **缺件直接失败**——少带模型的包
   "看着能跑"，代价全留给用户的第一次上传（`MIKASA_ALLOW_NO_EMBED_MODEL=1` 是给
   验证性构建留的逃生口）。模型**不进 git**（91MB 不该进版本历史），由流水线取。
4. **运行时铺设**：构造后端前，把随包模型**按文件补齐**进数据目录的 fastembed 缓存
   ——大小一致就跳过，用户早先下好的缓存原样保留（不重下、不覆盖）；用户显式设了
   `FASTEMBED_CACHE_PATH` 则完全不碰。
5. **有完整模型就只认本地**：铺好后构造 `TextEmbedding(..., local_files_only=True)`，
   不联网解析远端（快、离线可用），也不会把远端更新过的权重混进同一个索引。
6. **判"缓存里有模型"看证据链**：`refs/main` → `snapshots/<rev>/*.onnx`。
   只查目录存在会把半截下载当成品——正是 limitations §八 websockets 空壳那一类
   （**带一份坏的最糟**）。
7. **两道哨兵**：`tools/smoke_frozen.py` 在**断网环境变量**（`HF_HUB_OFFLINE=1` +
   端点指向不可达地址）下真启动打包产物并上传一份文档，必须 201——这是"下载即用"
   的唯一硬证据，模型没带上会以网络错误红掉，而不是悄悄下 91MB 把冒烟骗绿；
   发布流水线另加"载荷里有模型"的断言。

**两个写这处时踩到的坑**（都写进了代码注释与回归测试）：

- **缓存目录按 HF 仓库名命名，不是模型名**：配置写 `BAAI/bge-small-zh-v1.5`，
  而 fastembed 实际拉的仓库是 `Qdrant/bge-small-zh-v1.5`。按模型名拼目录会**永远
  查不到缓存** → 静默退回联网（不报错）。查注册表（`list_supported_models`）才对。
- **`allow_patterns` 是过滤器、不是必需清单**：`preprocessor_config.json` 这个仓库
  根本没有；把它当"必需"，本机明明有完整模型也会被判成"不完整"、白下一次 91MB。

**代价与边界**：

- 安装包 84MB → 约 170MB、zip 110MB → 约 200MB。但用户此前也要单独下这 91MB 模型
  （且常失败），总量反而更少、且一次装完。
- 随包的只有**向量**模型；回答用的本地大模型（Ollama + qwen3:8b，约 5.2GB）仍要用户
  自己装——那是"下载即用"的第二期，另一份方案评估（Ollama 便携版 vs llama.cpp）。
- 源码版（`pip install -e ".[local]"`）行为不变：第一次仍联网下载（只是开发机多半
  已有缓存，取件工具会直接复制它）。
- 用户想换向量模型仍可改配置（`embedding.model`）+ 重建索引；随包只是"默认那一份"。

**代码**：`tools/fetch_embed_model.py`（新，构建期取件）、`packaging/Mikasa.spec`
（收载荷 + 缺件失败）、`src/mikasa/providers/embedding.py`（`_seed_bundled_model` /
`_cached_snapshot` / `_hf_repo_of`）、`.github/workflows/release.yml`（取件 + 断言）、
`tools/smoke_frozen.py`（离线入库哨兵）、`tools/make_release.py`（安装说明文案）；
测试 `tests/unit/providers/test_embed_model.py`（8 条）。

## ADR-0031 文生图：出图独立成段 + 字节落盘 + 只放行同源图片

**背景**：用户拍了"三个全做"——识图（M6 ②）、公式排版、**文生图**，这是第三件。
需求原话是"不要只能输出文字，希望软件自己能出图"。

**决定**：

1. **出图独立成一个配置段**（`image: none|api`，默认 `none`），与 vision 分开：
   一个收图、一个出图，模型名与请求参数都不是一回事（SiliconFlow 的出图端点要
   `image_size`，OpenAI 那套要 `size`），混在一段里"识图用 A、出图用 B"无处落脚。
   offline 档永远 `none`（该档承诺零密钥、零外部调用）。
2. **取回的是字节，不是 URL**。上游给的图片链接**只有 1 小时有效期**
   （SiliconFlow 文档明写），把 URL 直接写进回答就是"一小时后变裂图"。
   落盘到 `data_dir/generated/`（与 uploads 分开——uploads 是 ingest 的输入，
   图片进去会被 reindex 当文档扫），文件名 = `日期-内容哈希.扩展名`（同图重生成
   不堆副本），回答里引用的是本机地址、永久有效。
3. **两张形状都认**：SiliconFlow 是 `{"images":[{"url":…}]}`，OpenAI/DALL·E 是
   `{"data":[{"url"|"b64_json":…}]}`。只认一种，换家服务商就是"调用成功但没图片"。
4. **出网两条路都过闸门**（复用论文下载器那套 `reject_reason`）：发请求的目标
   （用户填的 base_url）与**下载图片的目标**（上游返回的 URL，算半可信输入）。
   后者不能省——上游被攻破或返回内网地址时，那是一个现成的内网探测原语。
5. **未配密钥槽位 = 本机免密钥服务**（注入占位钥匙，同 `llm._LOCAL_API_KEY`）；
   **配了槽位但没填 = 如实报错**。"我知道这个服务不要密钥"与"忘了填"是两件事。
6. **接口**：`POST /api/images/generate`（提示词 → 图）与
   `GET /api/images/generated/{name}`（取图）。取图端点的文件名是**服务端拼的**
   （正则白名单 → 目录拼接 → is_file），路径穿越在这层没有入口；inline + nosniff +
   私有缓存（与 note-media 同款纪律）。
7. **没会话就建一个**：生成成功后把图片作为一条助手消息落进会话（`![提示词](同源地址)`
   + 一行 `（模型 · 尺寸）` 说明）。不落库的话刷新即失——用户要的是"这条对话里
   有这张图"。失败路径一个空会话都不留（同 ask 的 `_drop_session_if_empty` 纪律）。
8. **前端只放行同源图片**（`renderAnswer` 新增独占一行的 `![alt](url)` 分支，
   URL 必须是单个 `/` 开头的同源路径）。远程图片会让"用户看了这条回答"变成一次
   对外请求（图片即追踪像素）；不放行的写法降级成原文一行，不静默吞掉。
   加载失败由消息级委托（捕获阶段，`error` 不冒泡）挂 `.broken`，CSS 显示一行说明。
9. **「测试连接」不生成图片**：出图按张计费或耗免费额度，拿它当探针不合适。
   只读 `/models` 列表并核对模型名在不在其中（几百字节）。
10. **对话框的尺寸跟随配置**（输入框只给占位符 + 预设下拉，留空即用配置值）。
    写死一个默认值会静默覆盖用户在面板里选好的尺寸——各模型推荐值不同
    （Qwen-Image 1328x1328 / Kolors 1024x1024），猜错就是白等几十秒。
    （这条是 E2E 当场抓出来的：假服务收到的 image_size 是对话框默认值而不是配置值。）
11. **视频不做**（按秒计费 + 要转码），写进 limitations。

**实测**（2026-09-21，SiliconFlow + Qwen/Qwen-Image，1328x1328）：
一次出图 **21.0 秒**、1199KB PNG，响应走 `images[].url` 分支 → 当场下载落盘。
客户端头的四张预设之外，SiliconFlow 上还有 `Kwai-Kolors/Kolors`、
`Tongyi-MAI/Z-Image(-Turbo)`、`baidu/ERNIE-Image-Turbo` 等出图模型（实测 /models 列表 95 个模型里有 7 个出图模型）。

**代价与边界**：

- **出图必须出网**（云端模型），本机档默认关闭；提示词会发到所选服务商，
  面板与对话框里都写明了。
- 生成图**不进知识库**（它不在 uploads，也不参与检索）——要沉淀为可检索内容，
  走"笔记 + 原图"那条路（M6 ②）。
- 图片尺寸/质量取决于所选模型；本项目不做本地出图（要另引 SD/ComfyUI 一类运行时）。

**代码**：`config/settings.py`（`ImageConfig` + `generated_dir`）、
`providers/image.py`（`OpenAICompatImage` / `NoImage`）、`providers/__init__.py`、
`web/services.py`（`rebuild_image`）、`web/routers/images.py`、
`web/routers/settings.py`（`/api/settings/image` 三端点）、`web/schemas.py`、
`web/static/js/image-gen.js`（对话框）+ `image-settings.js`（面板段）+
`common.js`（`renderAnswer` 的图片块）+ `qa.js`（入口与追加）+ `style.css`；
测试 `tests/unit/providers/test_image_provider.py`、`tests/unit/web/test_images_api.py`、
`tools/smoke_render.mjs`（13 条断言）、E2E `tools/chrome_image.py`（自带假出图服务）。
