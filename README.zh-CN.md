# Mikasa

> 带引用溯源与自动化评测的个人文档问答工作台（LLM + RAG）
> *A personal document QA workspace — local-first RAG with citation tracing and automated evaluation.*
> *[English README](README.md)*

Mikasa是一个**从零手写**的 RAG 应用：检索（自实现 BM25 + 向量 + RRF 融合）、
中文结构化分块、引用溯源、三阶段自动化评测全部自研，不套 RAG 框架——
代码是透明的、评测口径是可复现的。

**项目状态**：M0 骨架 → M1 核心管道 → M2 自动化评测 → M3 Web 界面 →
M3.5 问答双模式 → M4 本地推理 profile → M4.5 会话管理升级（含首次真实
schema 迁移 v1→v2，见 ADR-0004 修订段）→ 语料文件夹 v3 → 表格形态呈现
→ 跨语言检索 → 英文引用附原文/译文对照 → 文档阅读视图 + 引用跳转
→ M5 前全量排查（修 23 项，含 4 项数据安全级）→ 安装版打包 → 设置面板模型接入。
当前 **738 tests 全绿**，ruff + mypy clean，覆盖率 ~93%。已发布 Windows 免安装包与安装向导
（v0.1.0 → v0.1.1），应用内自带「检查更新 → 一键下载安装」。

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
| [架构总览](docs/zh-CN/architecture.md) | 系统怎么组织、数据怎么流（含防幻觉三层防线） |
| [设计决策记录（ADR）](docs/zh-CN/design-decisions.md) | 每个"为什么"：选型、取舍、被推翻的决策 |
| [自动化评测](docs/zh-CN/evaluation.md) | 三阶段口径、黄金集防错配、裁判偏差修正、验收基线 |
| [已知限制与失败案例](docs/zh-CN/limitations-and-failures.md) | 生态故障、启发式边界、开发期真实 bug 档案 |
| [使用说明](docs/zh-CN/usage-guide.md) | 端到端工作流（查论文、追问技巧、被拒答了怎么办） |
| [已知问题与改进 backlog](docs/zh-CN/known-issues.md) | 未修复问题（裁决记录）与未排期候选的统一清单 |

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

# 4) 首次导入或切换 profile 后重建索引（会触发一次 bge-small-zh-v1.5 下载 ~100MB）
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
（一次检索 arXiv/OpenAlex/CORE 三源，带年份/语言/开放获取筛选与被引或
时间排序；点一条看完整摘要，导入开放获取 PDF 到语料库后即可提问，
已导入的会标「已在库中」。各来源声明自己的能力，做不到的选项置灰并说明
原因——知网式体验，不是知网的数据，付费墙全文拿不到）、**评测**页
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
| M5 | 文档定稿（英文 README + 通用示例语料）+ git 初始化 + GitHub 发布 + CI | 进行中 |
| M7 | 在线找论文：arXiv + OpenAlex 双源检索（交错分页 + 逐源降级）+ 有防线的 PDF 下载器 + 导入复用上传尾链（ADR-0019） | ✅ 578 tests |
| M8 | 找论文独立成页：三源（+CORE）轮转分页、来源能力声明与筛选/排序、详情面板与「已在库中」标记（documents.source_ref + v4 迁移，ADR-0020） | ✅ 679 tests |
| M6 ① | 知识库页写 Markdown 笔记：左写右预览、保存即入库可检索、原地编辑不产生重复（笔记 = 带 `note:` 标记的普通文档，ADR-0021） | ✅ 679 tests |
| v0.1.1 | 应用内更新：启动静默检查 → 弹窗 → 一键下载（带进度）→ 校验 sha256 → 自动安装；设置面板「关于」段可关可手查（ADR-0022） | ✅ 738 tests |

## 环境与工程纪律

- Python ≥ 3.11（推荐 3.13）；可选 NVIDIA 显卡（本地推理加速）；
- **源码注释为中文**（项目工作语言）；**文档双版**：`docs/` 英文（面向公开仓库）、
  `docs/zh-CN/` 中文原版；ruff + mypy 三绿基线
  `ruff format src tests && ruff check src tests && mypy src`；
- 测试与覆盖率：**738 单测**，覆盖率 ~93%（见 evaluation.md 回归门禁）；
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
