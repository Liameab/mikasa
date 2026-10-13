# AGENTS.md —— Mikasa 项目规范（2026-10-13 自动生成）

> 本文件**自包含**：不读任何本机外部文件也能照它干活（本机另有一份《通用协作规范（跨项目）》，读不到不影响本文件效力）。
> 冲突时：**以最新发布为准**（最新 main + 最新代码 + 最新文档 + 在飞 PR 已获认可口径）、**不得伪造/不得假成功** 两条永远最高。
> 生成依据：`pyproject.toml`、`.github/workflows/ci.yml`、`README.md`、`docs/`、`.gitignore`（都是现读的，不是记忆）。

## 项目一句话 / 技术栈 / 入口
本地优先的**文档问答工作台（自研 RAG，不套 LangChain/LlamaIndex）**：BM25 + 向量 + RRF 融合 + 中文结构化分块 + 引用协议 + 三阶段自动化评测。
Python ≥3.11（CI 跑 3.13）+ FastAPI/uvicorn/pydantic + hatchling 打包；CLI 入口 `mikasa = mikasa.cli:main`；前端为内置静态页；发行物走 PyInstaller（`.[packaging]`）。

## 权威顺序（冲突时以左为准）
1. **代码与 CI**（`.github/workflows/ci.yml`、`pyproject.toml` 里的工具配置）——真实实现与真实规则；
2. `docs/design-decisions.md`（**ADR**：取舍与理由都在这）；
3. `docs/architecture.md`、`docs/evaluation.md`、`docs/known-issues.md`、`docs/limitations-and-failures.md`；
4. `README.md`、`docs/usage-guide.md`（对外说明）；
5. 内部：`docs/codex-handover.md`（**已 gitignore，不进仓库**）。

## 门禁命令（提交前必须全绿；命令取自 CI，不是记忆）
```bash
pip install -e ".[dev,local]"   # [local] 必须一起装：只装 dev 时 fastembed 缺席 → mypy 的 warn_unused_ignores 会报"多余 type: ignore"
ruff format --check .           # CI 查整仓（不是只查 src/tests）；本地修复用： ruff format .
ruff check .
python -m mypy src              # 只查 src（不含 tests）。必须 python -m，见下方"本机坑"
python -m pytest -q             # 同上。基线：fbe63c5 / 2026-10-13 / Windows 本机 venv → 1214 passed, 2 skipped
python -m mikasa doctor --profile offline   # 冒烟自检（本机全过，值得进本地门禁）
```
基线数字钉的是"**commit + 日期 + 平台**"，会漂；别照抄，以自己现跑为准。覆盖率要真数得另跑 `pytest --cov`（CI 才带 `--cov`，`-q` 复现不出，别把某个百分比当基线写死）。
发布/打包另装 `.[packaging]`（pyinstaller + pywebview）。

## 项目专属约定与坑（改之前先看"为什么"）
- **ruff `line-length = 100` 按显示宽度算**：一个汉字算两格——"我数了没超 100"是常见误判，报 E501 时别先怀疑配置。
- **`dev` 依赖每条都写上界**：CI 装的是 `-e ".[dev]"`（拿最新版），本地 venv 停在上次装的那天；不上界会出现"CI 红、本地永远复现不出"。升级上限时：改 `pyproject.toml` → 本地重跑门禁 → 再提交。
- **注释与界面文案全中文**；ruff `select = [E,F,I,UP,B,SIM]`，`tests/*` 允许 assert；`B008`（FastAPI `Depends` 默认参数）按需豁免。
- ruff 已排除 `sample-corpus`、`data`、`docs`、`.venv`；`.tmp-*/` 是本机调试残留，清理时只删 `.tmp-*`，**别动 `.venv`**。
- MCP：`.codex/config.toml` 里注册了 `mikasa mcp --profile local`（三个只读工具，ADR-0036）。
- **本机坑①：`.venv\Scripts\pytest.exe`、`mypy.exe` 坏着**——静默 exit 1、零输出（`ruff.exe` 没事）。所有门禁一律写 `python -m pytest` / `python -m mypy`，别去猜"零输出 exit 1"是啥意思。
- **本机坑②：`gh` token 失效**（`gh auth status` 报 keyring token invalid）→ 目前"PR 由用户本人发起"实际等于用户得先 `gh auth login` 才发得出去。
- **本机坑③：push 通道**——中文家目录会踩 MSYS ssh 的坑，已用全局 git config 绕过；偶发 `Internal Server Error` 拒 main 时**重试即可，别动 remote**。

## 内部文档（**已 gitignore，禁止提交**）
`docs/codex-handover.md`（交接：§1 已完成 / §2 还没做 / §3 别踩的坑 / §4 验证命令）、`docs/ideas-and-backlog.md`、`docs/interview-handbook.md`、`docs/advisor-readme.md`、`docs/tech-roadmap-*.md`、`docs/resume-*`（含照片）。
> 新增同类文件（简历/面试/路线草稿/调试记录）时，**同步补 `.gitignore`** 并核对是否已被覆盖。

## 协作规则（摘要；完整版见通用规范）
- 报告三段式：**要修项 → 后面要做的项 → 后面的规划**，逐条带证据（命令输出/文件:行号/实测结果）。
- 分支：一个主题一条；PR 合并当天删；只删自己的；名下远端保持在 1~2 条。
- PR 由用户本人发起；助手推分支 + 给标题/正文；文案归档到内部交接文档。
- 写操作真跑端到端（**在可丢弃的数据上**）并还原；UI 改动逐控件真点（**限本次改动触及的控件**）；修 bug 先 grep 全部调用方、修根因 + 补回归测试。
- 改 ADR / 依赖 / CI / 打包脚本这类"上游"前：先问用户，并说明影响。

## 当前状态与待办（指针）
先读 `docs/codex-handover.md` 的 §1（别重做）、§2（还没做，按优先级）、§3（别踩的坑）；对外版本与里程碑见 `README.md` 尾部与 `.github/release-notes/`。
