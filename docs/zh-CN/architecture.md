# 架构总览

> Mikasa：带引用溯源与自动化评测的个人学习知识库问答系统（LLM + RAG）。
> 本文回答"**系统怎么组织、数据怎么流**"；"为什么这么做"见
> [`design-decisions.md`](design-decisions.md)（ADR），评测口径见
> [`evaluation.md`](evaluation.md)。全部源码注释与本文档均为中文。

## 一、定位：一次问答的数据流

```
                    ┌─────────── 入库链路（离线/Web 上传）───────────┐
  语料（md/txt/pdf/docx）→ loader → 清洗(text.py) → 结构分块(chunker)
                    → 分词落盘 + 向量化 → SQLite(documents/chunks/embeddings)
                    └────────────────────────────────────────────────┘
                                   ↓ meta.json 指纹快照（doctor/评测用）

                    ┌─────────── 问答链路（CLI / Web，双模式）─────────┐
  问题 ─┬→ kb 知识库：双路召回 ─→ RRF 融合 ─→ 精排(可选) ─→ top_n 块
        │       注入提示词(编号 1..N) ─→ LLM 生成 ─→ [n] 解析校验(L1)
        │       ─→ 答案 + 可点击引用溯源（无据拒答）
        └→ free 自由问答：跳过检索/注入 ─→ 直连 LLM 自由作答
                （无引用/拒答协议；需 api/local 模型；见 ADR-0013）
                    └────────────────────────────────────────────────┘

                    ┌─────────── 评测链路（CLI eval / Web 后台）──────┐
  golden_set(内置人工题 / 自动出题) ─→ 逐题校验 ─→ 三阶段：A 检索 / B 协议(mock) / C 语义裁判
                    ─→ eval_runs 落库 + 报告 md（CLI 与 Web 同一编排）
                    └────────────────────────────────────────────────┘
```

三个链路共享同一套 `Settings`（profile 切换三形态）与同一 SQLite，
这是"同一系统"而非三个脚本拼盘的关键。

## 二、分层与目录

| 层 | 目录 | 职责 |
| --- | --- | --- |
| 入口 | `cli/` `web/` `__main__.py` | typer+rich 命令；FastAPI + 原生前端 |
| 应用 | `pipeline/` | ask 编排：检索 → 注入 → 生成 → 校验 |
| 领域服务 | `ingest/` `index/` `eval/` `papers/` | 入库、索引、评测、在线论文检索（可插拔的算法区） |
| 提供方 | `providers/` | LLM / 嵌入 / 重排：Protocol + 实现（ADR-0003） |
| 存储 | `storage/` | SQLite 连接/仓库 + meta.json 快照 |
| 基础 | `config/` `models/` `utils/` `errors.py` | 配置、pydantic 模型、文本/日志 |

依赖方向严格向下（入口 → 应用 → 领域 → 存储/提供方），`providers` 与
`storage` 之间无依赖——评测与离线形态通过"换配置不换代码"实现
（ADR-0001/0003）。全系统刻意不引第三方 RAG 框架，检索与分块自实现
（ADR-0009~0012），框架层只有 FastAPI/typer/rich 这类胶水。

## 三、入库链路

源文件 → 清洗 → 结构化分块 → SQLite → 分词/向量化，一次 ingest 完成。
段落结构（Markdown 标题树、PDF 文本层、DOCX 段落）由各 loader 统一成
`Para` 中间表示后进入分块器——**分块器只认一种输入结构**，新格式只需
新 loader。

**幂等与增量规则**（`ingest/service.py`，面试可讲的设计点）：
- **内容未变**：任一文档的 `file_sha256` 已存在 → 跳过。重跑、重复导入
  零副作用（CLI 汇总里计 skipped）；
- **同名内容已变**：删旧文档行（外键级联清 chunks/embeddings）后重建，
  实现"改了笔记再导入 = 原地更新"的直觉语义；
- **嵌入失败**：整个文档回滚并标记 failed——保证向量矩阵与 chunk 表
  **恒一致**；IndexManager 对不一致直接报错（半成品会毒化全库，宁回滚
  不残留）；
- 向量只对新增/变更文档追加（INSERT OR REPLACE），同模型增量、不重嵌
  全库；`mikasa ingest --reindex` 提供全量重建路径（重建后 chunk id 平移，
  题库里引用了旧分块的题会被**逐题跳过**并在报告里写明——见 evaluation.md §2）。

Web 上传端点、在线找论文的导入端点、以及知识库页的笔记保存共用同一段
入库尾链（`documents.ingest_web_file`）：三方都交给它一个位于 web-tmp
独占子目录里的文件 + sha256，拿回**逐字节同形**的 201/200/409 响应
（ADR-0019）。笔记走这条链时多带三个参数：`force=True`（绕过内容去重，
否则第二条同内容笔记会被静默跳过）、`title=`（标题必须在分块前定下来，
否则索引词空间里留的是旧名字）、`folder_id`（"保存到"要落在同一把锁内）。
笔记不是新类型——它是 `source_ref = "note:<key>"` 的普通文档，因此检索、
引用、文件夹树、阅读视图全部零特殊分支（ADR-0021）。

## 四、分块策略

（决策取舍记于 design-decisions.md ADR-0012，此处为落地细节）

- **边界优先级**：标题变化 = 必切块；块内长文本按
  "段落 → 句末标点 → 逗号 → 硬切"逐级切分，避免中断词义；
- **重叠**：同一标题下的相邻块共享 ≤overlap 字符尾部（跨标题不共享），
  缓解"答案被拦腰截断"的召回漏失；重叠尾巴在容量不足时**截短而非超发**，
  长度不变量"块 ≤ size"有测试守护（`test_overlap_respects_size`）；
- **标题前置**：入库时把标题路径拼入块首（`# A > ## B\n正文`），提升
  稀疏/稠密两路召回——生成与展示不重复标题，故正文与标题在 `ChunkSpec`
  中分开携带（heading_path 单独落库为溯源元数据，page 同理）；
- **重叠的逆运算**：阅读视图要把 chunk 序列还原成连续全文，需裁掉块间
  重叠——`ingest/stitch.py`（决策与验证数据见 ADR-0016）。它与本节的
  重叠规则**同源**：跨标题不共享 → 跨标题必不裁；尾巴可能被截短 →
  除后缀规则外还有近尾规则。**改 overlap 规则时两处一起看**。
- 参数旋钮（size/overlap/title_prefix）全在配置，可消融（评测口径）。

## 五、检索链路

```
  query ─→ 分词 ─┬→ BM25 召回 top_k（预分词落盘，查询只分 query）
                 └→ 嵌入 query 向量 ─→ 稠密余弦召回 top_k
       ─→ RRF 融合(1/(k+rank)) ─→ 精排(重排器,可选) ─→ top_n 注入生成
```

- 稀疏与稠密两路互补：BM25 抓字面命中，向量抓语义近邻；融合只依赖
  排名（ADR-0011），分数不跨路标定；
- 稠密路用 numpy 精确余弦（ADR-0010），无近似误差；
- 性能（个人库数千 chunk 实测）：BM25 单查 <10ms，精确向量检索
  <500ms（10 万 chunk 内随规模线性），瓶颈从来不在检索——这也是
  自实现索引成立的规模前提；
- 重排器三档（api/local/none）可关：评测阶段 A 必须"关重排跑召回"
  （解耦口径，见 evaluation.md §1），产品形态才开精排。

**local 形态实况（M4）**：嵌入走 fastembed（onnxruntime，CPU 可跑）
的 bge-small-zh-v1.5（**512 维**，query 加 bge 检索指令前缀）；重排
backend=none——RRF 融合保序直通生成（实现保留，启用见 ADR-0014）；
生成走 Ollama（qwen3:8b）OpenAI 兼容 `/v1` 端点，**免密钥**（占位 key
注入，放行点论证见 ADR-0014 ①）。维度迁移纪律是 local 档的存亡线：
embeddings 表单库单模型（api bge-m3 1024 维 ≠ local 512 维），切
profile 必须 `ingest --reindex`，三层防护 = doctor 三方一致性 +
`ExactVectorStore.search` 维度防御 + 评测前的逐题校验（重建后旧分块
查不到 → 相关题被跳过；全对不上则拒绝跑，ADR-0014 ④ / ADR-0026）。

## 六、生成与引用协议（防幻觉的三层防线）

**提示词协议**（`pipeline/prompts.py`，唯一事实源——MockLLM 与真实 LLM
共用同一套标记，杜绝两处漂移）：
- 用**纯文本分区标记**（【资料片段】/【问题】/【历史对话摘要】）而非
  JSON 结构化输出：对 Ollama 等本地小模型更稳、流式友好，解析失败可
  观测（格式解析失败率是评测回归必看项）；
- 引用编号 `[n]` 与注入片段编号一一对应，**模型无权自造编号**——注入
  顺序即编号，越界即协议违规；
- 拒答统一句式常量 `REFUSAL_TEXT`：评测与 UI 都以字符串精确识别拒答，
  不靠模型自觉。

**防幻觉的三层防线**（校验逐层加码，对应"越早越便宜"原则）：

| 层 | 位置 | 校验 | 违规处置 |
| --- | --- | --- | --- |
| L1 硬校验 | 生成器 `pipeline/generator.py` | `[n]` 编号合法性（1..N 内、格式正确） | 越界/畸形标记丢弃并计数，进评测指标 |
| L2 语义支持度 | 评测层（judge 判据） | 引用是否真的支持所声称的陈述 | citation gold ratio 等指标披露 |
| L3 拒答纪律 | 评测层 | 拒答句出现时全文不得带任何 `[n]` | 单独指标（L3 违规，理论上不可达，出现即协议 bug） |

生成产物 `Answer`（文本 + 解析出的 Citation 列表 + 拒答标志）与
`Completion`（含 token 用量，供成本核算与评测）分离返回；引用溯源
闭环：chunk 的 heading_path/page 一路带到 Citation，Web 问答页据此
渲染可点击的来源卡片。

**free 自由问答是刻意的旁路**（ADR-0013）：同一 AskService 门面上
`mode="free"` 时跳过检索/注入/引用解析/拒答判定，直连 LLM——防线
L1~L3 只约束 kb 路径。旁路点在**服务层**而非 Generator（Generator 的
`build_answer` 依赖 hits 的引用解析，free 无 hits，不复用）；
评测（`eval/`）硬编码 Retriever + Generator、不经 AskService，故 free
天然与评测解耦——协议指标不受自由模式"污染"，两面口径各自干净。

## 七、存储布局

```
data/
├── mikasa.db          # SQLite：WAL 模式（读写不互斥）、外键级联
│    ├── documents / chunks / embeddings    # 正文、预分词 tokens、float32 向量
│    ├── qa_folders                        # 会话文件夹树（自引用 parent_id）
│    ├── qa_sessions / qa_messages          # 会话（title/title_manual/folder_id）与历史
│    └── eval_runs                          # 评测行（report_md 列 = 报告全文）
├── uploads/           # 文档入库副本（Web 上传的归一处；删除文档时同步删）
├── indexes/meta.json  # 派生快照：语料指纹/chunk 数——doctor 一致性体检与
│                      #   报告里"这套题是为哪份语料写的"的来源（可随时由 DB 重建，不是权威源）
└── eval-reports/      # {run_id}-{name}.md 评测报告（与 eval_runs 表同源）
```

- 版本管理：schema_version 分路（ADR-0004 修订）：低版本库沿
  `_MIGRATIONS` 自动逐级迁移（每级幂等 + 独立提交 + 日志留痕），
  高版本库硬报错、旧程序绝不读写新库；schema 终态、历史版本快照
  （immutable）、迁移函数三者同 PR；
- 会话树：qa_sessions 增 title/title_manual/folder_id 三列（v1→v2），
  title_manual 锁"自动提炼永不覆盖手动命名"（ADR-0015）；qa_folders
  平铺存储、前端组树，防环/删空语义见 ADR-0015；
- 一致性原则：DB 是权威源，meta.json 只是快照；一切"文档/块/向量"
  的删除走外键级联，无手写清理路径——半成品状态不存在。例外刻意
  存在：会话删走级联、**文件夹删只许空夹**（NO ACTION 外键兜底，
  容器绝不连坐内容）。

## 八、Web 服务与进程模型（M3）

`create_app(settings)` 工厂显式注入 settings（测试可传隔离 data_dir 的
offline settings）；uvicorn `--reload` 走 `serve_app_factory()` 导入字符串。
前端零构建链：原生 HTML + ES Modules，全部动态文本经 esc 后 innerHTML
（LLM 输出不可信），SSE 由 fetch + ReadableStream 手拆帧（不引
sse-starlette）。

**单进程约束**（serve 硬性设计）：内存态索引快照与评测
EvalJobManager（单槽状态机，threading.Lock）都活在进程内——
`serve` 不能多 worker（uvicorn --workers 不受支持，报错提示），
启动时对既有库做一致性校验并快照加载。换句话：**Web 形态是一台
单用户交互机，不是水平扩展服务**——规模边界在 README/FAQ 如实声明。

上传安全四道（`web/routers/documents.py`）：sanitize_filename（保留中文）
→ 后缀白名单 415 → `read(max+1)` 超限 413 → 临时文件 finally unlink；
DB 层 `file_path` 脱敏，杜绝路径探针。

**在线找论文**（M7/M8/ADR-0023）住在 `papers/`：四源检索服务——arXiv（Atom）与
OpenAlex/CORE/DOAJ（JSON）各写一个来源模块，**归一成同一份 `PaperResult`**；
并发取源 + 30 秒页级截止（挂住的源如实跳过）。DOAJ 是 2026-09-19 加入的第四个源：
免密钥的开放获取期刊目录，中文 OA 期刊的落脚点（决策与授权边界见 ADR-0023）。
其余不变：轮转交错的翻页与逐源降级、
一个有防线的 PDF 下载器（逐跳复验公网地址、PDF 嗅探、50 MB 上限）。
每个来源声明自己的能力（`SourceCaps`：年份过滤、被引排序、时间排序、语言过滤、
开放获取处理方式），做不到的条件以 `notes` 如实回报——降级要说出来，绝不静默
忽略（CORE 的年份**参数**会被上游收下后丢掉，所以那个来源改走查询语法）。
`web/routers/papers.py` 暴露五个端点——`POST /api/papers/search`、
`POST /api/papers/import`、`GET /api/papers/sources`、`GET/PUT /api/papers/settings`
（翻页不新增端点：第 N 页就是 `offset=(N-1)×50` 的同一个检索请求——服务层的窗口公式
把全局窗口摊到各来源上，所以"跳页"只是换一个 offset；总页数由前端按各源自报的命中数
求和算出，并按上游按页取数的 1 万条上限 × 来源数封顶，状态行里写明这个来路）
——导入路径按 id 反查来源 API 自行推 PDF 地址、不信客户端，因此真正进入 URL 的
用户输入只有一对正则校验过的 `{source, id}`。导入同时写入 `documents.source_ref`
（`"arxiv:2401.12345"`），搜索结果据此标"已在库中"；该列由 v4 迁移补上，并被
两处 reindex 保留清单带过全量重建。

**应用内更新**（v0.1.1，ADR-0022）在 `update/` 包，由 `web/routers/update.py` 暴露四个端点：
`GET /api/update/check`、`POST /api/update/download`、`GET /api/update/download/status`、
`POST /api/update/install`。服务端自己去问 GitHub 的 `/releases/latest` 并从响应里挑出安装包
资产——**客户端永远拿不到 URL**——然后在主机白名单（`github.com` / `*.githubusercontent.com`，
http+回环是 E2E 逃生门）后面下载，与同一 release 的 `SHA256SUMS.txt` 逐字节核对 sha256，最后用
双击语义（`os.startfile`）启动安装向导。检查失败保持静默（只进日志与状态端点），结果缓存 10
分钟；前端提供「跳过此版本」与设置面板里的启动检查开关。

ADR-0024 起下载与任务槽都是"可续、可接"的：半成品在 `updates/<资产名>.part`，后续请求带
`Range: bytes=N-`，传输类失败退避重试并从断点接着下；槽另记工作线程存活，线程死了能被接管
——所以 `POST /api/update/download` 是**幂等**的（202 + `adopted: true`），不再回 409。
前端用**一个轮询循环**同时驱动弹窗与顶栏胶囊：关弹窗、切页、刷新都不打断下载；只有弹窗还开着
（用户在场）时才自动启动安装器，否则胶囊停在「已就绪 · 点此安装」等人来按。

## 九、评测编排（CLI 与 Web 同一套）

`eval/service.py::run_and_persist` 是 CLI 与 Web 后台任务共用的
"跑完并落库"编排（M3 从 CLI 下沉）：golden 由调用方各自载入
（CLI 打横幅 / Web POST 时同步做逐题校验，全题对不上立刻 400，不在任务里
失败），落库顺序 = 先插占位行拿 run_id → 跑 → 渲染报告回填
（Web 轮询"有行但 report_md 为空"即"评测中/已失败"，语义天然对齐
前端）。失败语义：结构性失败（题库全对不上、空库）抛错由调用方呈现；
单题失败是评测正常组成，逐条留痕（record.error）不影响收尾。

三阶段口径、指标手写定义、裁判偏差修正见 evaluation.md——架构层面
只有一条：**评测与产品共用同一检索/生成代码路径**（换配置不换代码），
离线 mock 在 CI 保证协议正确性，真实质量靠 api/local 真跑阶段 C。

## 十、运行时形态

| 形态 | 命令 | 说明 |
| --- | --- | --- |
| CLI | `mikasa init/doctor/ingest/list/index/ask/chat/eval` | 全量能力，rich 输出 |
| Web | `mikasa serve [--profile/--host/--port/--reload]` | 三页（问答/文档/评测），SSE |
| 体检 | `mikasa doctor` | 依赖/密钥/**本地推理依赖（local 档门控 Ollama/fastembed）**/索引三方一致性，CI 冒烟用（口径见 limitations-and-failures.md） |
| 评测真跑 | `mikasa eval run --profile api/local` | 63 题 × 三阶段，api 档 4-5 分钟/轮 |

三形态共享：同一份配置（profile 切换）、同一份 SQLite、同一份评测
口径——offline 场次跑全流程零密钥（mock/none），正是 CI 与演示的
默认形态。

**配置查找顺序**：显式 `--config` → `config/config.yaml`（源码模式先看当前
目录再看仓库根）→ `config/profiles/<profile>.yaml`。在 profile 文件之上，还会
深合并一层用户可写覆盖层 `<数据目录>/config.yaml`（Web 设置面板写入，
ADR-0018）——基底由 `--config`/`config.yaml` 提供时它不生效，且其中的
`profile:` 键一律忽略。密钥不进以上任何文件：面板把密钥写进
`<数据目录>/.env`；查找链（`MIKASA_ENV_FILE` → 数据目录 → 资源根 → exe 同级）
上**所有存在的 .env 都会加载**，同名键先加载者胜。
