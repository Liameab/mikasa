# Mikasa

> 带引用溯源与自动化评测的个人文档问答工作台（LLM + RAG）
> *A personal document QA workspace — local-first RAG with citation tracing and automated evaluation.*
> *[English README](README.en.md)*

Mikasa是一个**从零手写**的 RAG 应用：检索（自实现 BM25 + 向量 + RRF 融合）、
中文结构化分块、引用溯源、三阶段自动化评测全部自研，不套 RAG 框架——
代码是透明的、评测口径是可复现的。

**项目状态**：M0 骨架 → M1 核心管道 → M2 自动化评测 → M3 Web 界面 →
M3.5 问答双模式 → M4 本地推理 profile → M4.5 会话管理升级（含首次真实
schema 迁移 v1→v2，见 ADR-0004 修订段）→ 语料文件夹 v3 → 表格形态呈现
→ 跨语言检索 → 英文引用附原文/译文对照 → 文档阅读视图 + 引用跳转
→ M5 前全量排查（修 23 项，含 4 项数据安全级）→ 安装版打包 → 设置面板模型接入 → 向量模型随包携带（首次入库不再联网，ADR-0030）
→ 文生图（问答页直接出图，ADR-0031）。
当前 **995 tests 全绿**，ruff + mypy clean，覆盖率 ~93%。已发布 Windows 免安装包与安装向导
（v0.1.0 → v0.1.10），应用内自带「检查更新 → 一键下载安装」——下载支持断点续传，
关掉弹窗/切页/刷新都不会打断，进度落在顶栏胶囊上（ADR-0024）。

## 亮点

- **引用溯源**：回答中每个事实性陈述带 `[n]` 标记，UI 可点击回溯到
  原文块（标题路径 + 页码）；模型无权自造编号，越界即违规计数；
- **防幻觉三防线**：编号硬校验 → 引用支持度判据 → 拒答纪律，三层在
  评测里各自留痕（LLM 无据时输出统一拒答句式）；
- **自动化评测三阶段**：A 检索层（recall/MRR/nDCG）→ B 生成协议层 →
  C 语义裁判（LLM-as-Judge，双轮位置交换防偏差）。真实验收基线：
  召回 recall@10=1.000、引用越界率 0、16 道不可答全部干净拒答；
- **跨语言检索**：中文提问自动译成英文作第二路查询，双路 RRF 合流；
  被引用的英文段落以「原文 + 中文翻译」对照呈现；
- **文档阅读视图**：点引用角标 → 侧栏打开原文，支持重建文本与 PDF
  原页渲染（引用区域高亮定位，与原文逐框对齐）；
- **三套运行形态**：云端 API / 本地推理 / 零密钥离线，`--profile` 一键
  切换，代码零硬编码；
- **零构建链 Web**：三页原生 HTML/JS + 手写 SSE 流式问答；
- **会话管理树**：任意层嵌套文件夹（菜单移动、防环、删空夹），
  首问自动提炼标题（LLM 一次 / 截断兜底，手动命名永不被覆盖）——
  侧栏即工作台（ADR-0015）。
- **界面内配置模型**：设置面板选来源（本机 Ollama / DeepSeek / SiliconFlow /
  任意 OpenAI 兼容服务）→ 贴密钥 → 测连通 → 保存即生效，不用重启也不用改
  `.env`；密钥只存本机数据目录、永不回显（ADR-0018）。

## 文档

| 文档 | 内容 |
| --- | --- |
| [架构总览](docs/architecture.md) | 系统怎么组织、数据怎么流（含防幻觉三层防线） |
| [设计决策记录（ADR）](docs/design-decisions.md) | 每个"为什么"：选型、取舍、被推翻的决策 |
| [自动化评测](docs/evaluation.md) | 三阶段口径、黄金集防错配、裁判偏差修正、验收基线 |
| [已知限制与失败案例](docs/limitations-and-failures.md) | 生态故障、启发式边界、开发期真实 bug 档案 |
| [使用说明](docs/usage-guide.md) | 端到端工作流（查论文、追问技巧、被拒答了怎么办） |
| [已知问题与改进 backlog](docs/known-issues.md) | 未修复问题（裁决记录）与未排期候选的统一清单 |

## 快速开始

```bash
# 1) 安装（开发模式；Windows 全程零编译）
pip install -e ".[dev]"

# 2) 环境体检（依赖/密钥/索引一致性；offline 模式零密钥）
mikasa doctor --profile offline

# 3) 导入语料（目录或单个文件，支持 md / txt / pdf / docx；重复导入自动跳过/替换）
mikasa ingest sample-corpus --profile offline

# 4) 提问（离线体验：--profile offline，内置 MockLLM）
mikasa ask --profile offline "什么是反向传播？" --show-sources
```

接入真实模型：把 `.env.example` 复制为 `.env` 填入密钥（DeepSeek /
SiliconFlow），`mikasa init` 会基于所选 profile 生成配置文件，之后
默认（api profile）即可使用真实模型。

**纯本地推理路径（local profile，零密钥零成本）**：

```bash
# 1) 安装本地依赖（fastembed CPU 嵌入；Ollama 本体需另行安装 https://ollama.com）
pip install -e ".[local]"

# 2) 拉取生成模型（约 4.9GB；可把模型目录重定向到 D 盘：
#    setx OLLAMA_MODELS "D:\ollama\models" 后重启 Ollama）
ollama pull qwen3:8b

# 3) 体检应全绿（fastembed 缺失/Ollama 未起/模型未拉取/索引错配都会红行指引）
mikasa doctor --profile local

# 4) 首次导入或切换 profile 后重建索引（会触发一次 bge-small-zh-v1.5 下载 ~100MB；
#    打包版随包携带该模型、首次入库不联网，见 ADR-0030）
mikasa ingest --reindex --profile local

# 5) 同一条命令换成 --profile local 即可
mikasa ask --profile local "L2 正则化如何防止过拟合？" --show-sources
```

## 三种运行形态

| profile | 生成 LLM | 嵌入 / 重排 | 适用 |
| --- | --- | --- | --- |
| `api` | DeepSeek（OpenAI 兼容 API） | SiliconFlow bge-m3 / bge-reranker | 真实问答与真实验收 |
| `local` | Ollama qwen3（兼容端点，**免密钥**） | fastembed bge-small-zh（512 维）/ 暂无本地重排 | 全离线本地推理（见 ADR-0014） |
| `offline` | 内置 MockLLM | 无（仅 BM25） | 零密钥演示 / 测试 / CI |

配置全部走 `config/profiles/*.yaml` 与 `config.example.yaml`（全字段注释手册）。

## Web 界面（M3 完成）

```bash
mikasa serve                 # 默认 api profile（真实模型）
# 警告：--host 0.0.0.0 会把服务暴露给整个局域网，且没有登录鉴权——
# 同网段任何人都能读走你导入的全部文档、删除文档、上传文件、
# 跑评测（消耗你的 API 额度）。只在你信任的网络里这么用。
mikasa serve --profile offline   # 零密钥离线演示
# 常用选项：--host 0.0.0.0 局域网访问 / --port 9000 / --reload 开发热重载
```

浏览器打开 http://127.0.0.1:8000/：**问答**页（SSE 流式 + 可点击引用溯源 +
多轮会话 + **知识库/自由问答双模式**：底部随时切换——知识库模式严格
RAG 带引用，自由问答模式直连接入的模型回答任何话题；offline 下自由
问答置灰）、**知识库**页（网页上传/删除文档，入库即被检索；**也能直接写 Markdown
笔记**——左写右预览，保存即入库可被引用，随时回来接着改）、**找论文**页
（一次检索 arXiv/OpenAlex/CORE/DOAJ 四源——其中三个免密钥，DOAJ 补上了
中文开放获取期刊；带日期区间/语言/开放获取筛选与被引或时间排序；**翻页**看得见页码与
总页数、可跳页；**点一条结果直接在浏览器打开论文原页**，「详情」按钮才展开完整
摘要与导入——导入开放获取 PDF 到语料库后即可提问，已导入的会标「已在库中」；**检索历史**一键重搜，详情里还有**相关论文 / 引用了它 / 参考文献**三个出口继续往下找。
各来源声明自己的能力，做不到的选项置灰并说明原因——知网式体验，不是知网
的数据，付费墙全文拿不到）、**评测**页
（后台跑黄金集评测、进度轮询、报告渲染）。接口文档（Swagger）在
http://127.0.0.1:8000/docs 。

## CLI 一览

| 命令 | 说明 |
| --- | --- |
| `init` | 初始化数据目录并生成配置文件 |
| `doctor` | 环境体检（版本/依赖/密钥/本地推理依赖/索引三方一致性），CI 冒烟用 |
| `ingest <文件或目录>` | 导入并建索引；`--reindex` 全量重建 |
| `list` / `index stats` | 已入库文档 / 索引状态 |
| `ask "<问题>"` | 单轮提问，无据则拒答（`--show-sources` 显示溯源；`--mode free` 直连模型自由问答） |
| `chat` | 多轮对话（`--mode kb` 历史摘要注入 / `--mode free` 原始消息直注） |
| `eval run/list` | 跑评测 / 历史评测回溯（`--profile offline` 零密钥可跑） |

## 评测入口

真实质量验收：`mikasa eval run --profile api`（DeepSeek 生成 + Qwen 裁判，
约 4-5 分钟）；结果落 `data/eval-reports/{run_id}-{name}.md`，读报告指南
见 evaluation.md。离线回归：`mikasa eval run --profile offline`。

## 里程碑

| | 内容 | 状态 |
| --- | --- | --- |
| M0 | 工程骨架（配置体系、目录、文档计划） | ✅ |
| M1 | 入库链路 + 双路检索 + 生成引用协议 + CLI | ✅ |
| M2 | 黄金集 + 三阶段自动化评测（真实验收 recall@10=1.000） | ✅ |
| M3 | Web 三页界面 + SSE 流式 + 评测后台任务 | ✅ 242 tests |
| M3.5 | 问答双模式：知识库（kb）/ 自由问答（free），Web 开关切换 | ✅ 259 tests |
| M4 | local profile：Ollama(qwen3:8b) + fastembed CPU(bge-small-zh-v1.5)（rerank/judge 暂关，见 ADR-0014）；实机验收：recall@5=0.975 / MRR=0.940 / 拒答 13/16，质量差异与取舍见 evaluation.md | ✅ 282 tests |
| M4.5 | 会话管理升级：多层嵌套文件夹树（菜单移动）+ 首问自动提炼标题（LLM ≤16 字 / mock 截断兜底）+ 会话与文件夹重命名/删除（删空夹 409、防环 409）；首次 schema 迁移 v1→v2（ADR-0004 修订 + ADR-0015） | ✅ 332 tests |
| M4.5+ | 语料文件夹 v3（文档树/拖拽/查找）→ 表格形态呈现 → 跨语言检索（中文问英文答）→ 英文引用附原文/译文 → 文档阅读视图 + 引用跳转（PDF 原页渲染 + 高亮定位） | ✅ 430 tests |
| M5 | 文档定稿（通用示例语料 + 双语文档树）+ git 初始化 + GitHub 发布 + CI | ✅（README 2026-09-20 起中文为主，英文见 [README.en.md](README.en.md)） |
| M7 | 在线找论文：arXiv + OpenAlex 双源检索（交错分页 + 逐源降级）+ 有防线的 PDF 下载器 + 导入复用上传尾链（ADR-0019） | ✅ 578 tests |
| M8 | 找论文独立成页：三源（+CORE）轮转分页、来源能力声明与筛选/排序、详情面板与「已在库中」标记（documents.source_ref + v4 迁移，ADR-0020） | ✅ 679 tests |
| M6 ① | 知识库页写 Markdown 笔记：左写右预览、保存即入库可检索、原地编辑不产生重复（笔记 = 带 `note:` 标记的普通文档，ADR-0021） | ✅ 679 tests |
| v0.1.1 | 应用内更新：启动静默检查 → 弹窗 → 一键下载（带进度）→ 校验 sha256 → 自动安装；设置面板「关于」段可关可手查（ADR-0022） | ✅ 738 tests |
| v0.1.4 | 更新下载可续传、任务可接续（ADR-0024）：断线带 `Range` 接着下、退避重试、槽位自愈（POST 幂等不再 409）、弹窗改成「观察窗」+ 顶栏跨页胶囊；同批含「找论文」引证关系与检索历史 | ✅ 815 tests |
| v0.1.5 | 一轮"只修问题不加功能"：上传重传不再复制出第二篇（写侧按名兜底修脏 `file_path`）、reindex 清库前逐行校验副本、笔记乐观锁、迁移跨进程锁、翻页深度（OpenAlex/DOAJ 到第 1 万条）、代码块进检索、许可证改 AGPL-3.0（ADR-0025） | ✅ 859 tests |
| v0.1.6 | 拍照/图片转笔记（视觉模型独立成段、识别即草稿、原图随笔记留存，ADR-0027）+ 评测"为我的资料自动生成题库"（ADR-0026）+ **公式排版**（内置 KaTeX，ADR-0028）+ 修好"本机模型一问就超时"（`localhost` 地址规范化）+ 更新下载在慢/断网下不放弃（换连接续下，ADR-0024 续作） | ✅ 918 tests |
| v0.1.7 | 本地档改走 **Ollama 原生接口**：「思考模式 / 上下文长度」两个旋钮（默认关思考、上下文 16384——修掉"不指定时只吃 2050 token、每次提问材料被砍六成"的静默截断，ADR-0029）+ 作答呈现规范（分节/表格/公式/示意图）+ 修好 **v0.1.6 双击打不开**（升级残留的 websockets 空壳；新增发版哨兵 `smoke_frozen`）+ 四路审查 17 条（跨站写 403、面板出网白名单、入库中断自愈、reindex 映射落盘…）+ 公式渲染漏检（跨行 `$$`、`$ x $` 空格） | ✅ 943 tests |

| v0.1.8 | **向量模型随包携带**：首次上传文档不再联网下载（91MB 的 bge-small-zh-v1.5 进安装包，构建期固定 revision + 运行时铺进缓存，另有「离线入库」发版哨兵与载荷断言，ADR-0030） | ✅ 951 tests |
| v0.1.9 | **文生图**：问答页一键出图、随会话留存；出图独立成段（默认关）、上游图片当场落盘（链接 1 小时过期）、前端只渲染同源图片（远程图片是追踪像素）、出网两侧都过 SSRF 闸门（ADR-0031）；同批修好「载荷里模型收了两份」（CI 走 Xet 时 blobs 落在**缓存根**成第二副本，zip 白大一倍——发版哨兵拦下、探针定位、ADR-0030 补丁） | ✅ 977 tests |

| v0.1.10 | 模型来源补齐：**Claude**（Anthropic 的 OpenAI 兼容入口）与 **OpenAI** 预设（共六个来源；文案里如实写明兼容层的边界与「订阅不能当 API」）；**本机模型在应用内拉取**（原生 /api/pull 流式进度、可取消、单槽状态机；装 Ollama 本身仍需自己装，不代下 1.5GB 安装器）；修掉面板「打开瞬间点预设被回填覆盖」的竞态（ADR-0032） | ✅ 995 tests |

## 环境与工程纪律

- Python ≥ 3.11（推荐 3.13）；可选 NVIDIA 显卡（本地推理加速）；
- **源码注释与文档一律中文为主**（项目工作语言）：README 与 `docs/*.md` 都是中文，
  英文镜像在 [README.en.md](README.en.md) 与 [`docs/en/`](docs/en/)——两棵树同名文件一一对应，
  有护栏测试盯着（`tests/unit/web/test_docs_trees.py`）；ruff + mypy 三绿基线
  `ruff format src tests && ruff check src tests && mypy src`；
- 测试与覆盖率：**995 单测**，覆盖率 ~93%（见 evaluation.md 回归门禁）；
- 零编译安装：Windows + CPython 3.13 全部依赖均有预编译 wheel
  （版本锁定理由见 pyproject.toml 注释与 ADR-0006/0008）。

## 项目结构

```
src/mikasa/
├── cli/          # typer+rich 命令
├── web/          # FastAPI + 原生 HTML/JS（问答/知识库/评测三页）
├── pipeline/     # 检索→注入→生成→校验编排（含引用协议）
├── ingest/       # 四格式 loader + 中文结构分块
├── index/        # 自实现 BM25 / numpy 精确向量 / RRF 融合 / 分词降级
├── eval/         # 黄金集评测：三阶段口径 + 裁判
├── providers/    # LLM/嵌入/重排：Protocol + 云端/mock 实现
├── storage/      # SQLite + meta.json 快照
└── config/       # pydantic 配置（三 profile 合并）
sample-corpus/    # 原创 AI 学习笔记语料（MD/PDF/DOCX/TXT）
evals/            # 黄金集源（questions.yaml → golden_set.json）
config/           # profiles/*.yaml + 全字段示例
```

## 许可证

**AGPL-3.0-or-later**——全文见 [LICENSE](LICENSE)。

这是刻意选的**强著佐权（copyleft）**许可证：应用打包时捆了
[PyMuPDF](https://pymupdf.readthedocs.io/)（AGPL-3.0 或商业许可二选一）用于 PDF 解析与页面渲染，
所以发布物没办法继续挂 MIT。你可以自由地使用、研究、修改与再分发；
如果你把它做成网络服务给别人用，§13 要求你向他们提供对应源码。
随包的第三方组件与它们的许可证见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（用 `python tools/make_third_party_notices.py` 重生成）。
