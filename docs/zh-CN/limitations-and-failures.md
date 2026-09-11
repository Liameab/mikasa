# 已知限制与失败案例档案

> 本文件的写作动机：**`mikasa doctor` 的报错信息直接引用本文**——体检
> 说"有问题"时，用户要能在这里读到"出了什么问题、为什么、怎么办"。
> 同时它也是全项目踩坑的诚实档案：每个"真实踩坑/真实教训"在代码里
> 都链向本文，不再散落在会话记忆里。
>
> 结构：①生态故障 ②清洗/解析启发式的已知限制 ③设计取舍的已知边界
> ④开发期真实 bug 案例（已修复，留档）⑤doctor 体检口径的演进。

## 一、生态故障：jieba 与 setuptools（2026）

**故障**：2026 年起新版 setuptools 移除 `pkg_resources`，而 jieba 0.42.1
内部依赖它——`import jieba` 在新装环境必崩（jieba#1043）。

**为什么"锁版本"不是答案**：官方临时方案是 `setuptools<82`，但把项目
命运押在一个随时可能失效的锁上不成立（用户装别的包就会升级 setuptools）。

**对策（三层防御，详见 ADR-0008）**：jieba 可用走 jieba；导入失败自动
降级纯 Python bigram（零依赖、确定性）；版本锁（`jieba==0.42.1` +
`setuptools<82`）只作性能优化。**何时报错**：doctor 检查
`get_tokenizer_name()`——注意它检查的是"实际生效的分词器"，不是"jieba
能否导入"（初版即栽在这，见 §五）。

**用户视角**：`mikasa doctor` 报"jieba 不可用，分词已降级为 bigram"=
不致命但质量降级，检索指标差异见 evaluation.md；想恢复 jieba 就
`pip install "setuptools<82"`。

## 二、清洗与解析启发式的已知限制

### PDF 页眉页脚剔除（loaders.py）

启发式规则：页码行按模式剔除；跨页**逐字重复**的行整体剔除（页眉页脚
每页相同，正文不会逐字重复）。已知限制：正文中真的存在逐字重复的行
（如代码块、引用文）会被误伤——概率低且误伤面可预期，没有引入
机器学习方案（语料规模撑不起）。样本 PDF 由 `tools/build_sample_binaries.py`
生成，规避了此问题。

### PyMuPDF 写 PDF 的两个坑（tools/build_sample_binaries.py）

1. `insert_text` 遇到超页宽的长行会**静默折行**，且续行与后插入行重叠
   丢失——折行必须由脚本自己按字符预算完成（中文 ≈ 半角两倍宽）；
2. 英文行约写到 x≈595pt（页宽边界，≈54 字符 @10pt）就被**静默截断**，
   续行根本不渲染——脚本用 43 字符/行的安全预算 + 左边距核算
   （473pt + 72pt < 595pt）。

教训两条：PyMuPDF 的越界行为是静默的（不抛错），凡是"写入型"工具
都要做**写入后验证**（读回检查行数与内容），脚本里样本 PDF 生成后
即读回校验。

## 三、设计取舍的已知边界

### MockLLM 的启发式判定（providers/llm.py）

MockLLM 判定"问题是否有资料支撑"用**最长公共连续子串 ≥ 4 字**，而
不是字符重叠率。真实教训：按重叠率判定时，问题里的通用字（一/学/做/
在…）与任何语料都能凑够阈值——"如何在一周内学会做菠萝包"会被误判
为有据可答。连续 ≥4 字的相同片段只能是主题专名/实义短语（"正则化"
"防止过拟合"），通用字拼不出 4 连。

仍是启发式（mock 只演示协议，不产生真实语义）：真实拒答语义由系统
提示词约束实现，mock 的误答样本（evaluation.md §5 实测 7/16）恰是
"协议层 ≠ 语义层"的实证。

### 多轮会话的语义连续性（pipeline/ask.py）

v1 的 chat 不做查询改写：续问只把历史压缩成摘要行注入提示词，检索仍
以当轮问题原文进行。局限：代词/省略问（"它有什么缺点？"）检索质量
无保证。刻意不做查询改写的原因：改写器引入第二条 LLM 调用（成本×2
且延迟×2），评测口径也复杂化——先用摘要机制跑通，查询改写是明确
记录的 v2 候选。

### 双语对照块的三个边界（pipeline/ask.py，2026-09-10 #8 实测）

1. **译文会"断在句中"——忠实于被切断的源块，不是 bug**。分块器按
   `chunking.size`（默认 400 字）定长切块，块本身就可能从句子中间截断
   （doc 71 实测：某块结尾是 `…during repeated loading. As`），译文照译
   便同样收在"由于/而对于"这类半句上。这与引用卡历来只显示
   `snippet = content[:200] + "…"` 是同一种行为。初版把原文截到 300 字
   展示，反而在 400 字的块上系统性砍掉四分之一、放大这种观感——现取
   500 上限（见 `_BILINGUAL_ORIGINAL_CAP`），正常配置下不再截断。
2. **译文偶有漏译**：实测 doc 71 出现 `随着 successive 荷载循环`
   （successive 未译）。提示词允许保留英文术语，属模型质量波动；
   解析层只保证分段结构，不校验翻译完整性。
3. **只对"被引用且英文主导"的块产出**：`is_english_dominant` 要求
   ASCII 字母 ≥20 且 CJK 占比 <20%，被切出的碎片块（如 <20 字母的残句）
   判非英文而跳过；中文块、拒答轮、mock 后端一律不产出。

### Web 单进程形态

`serve` 为单进程设计：内存态索引快照 + 评测单槽任务管理器（架构文档
§八）。已知边界：不能多 worker 水平扩展、评测期间会占住评测槽。这是
"单用户演示机"的刻意形态，不是缺陷掩盖。

### 会话标题的"一次提炼"边界（M4.5）

标题只在首轮问答后提炼一次，不随轮次漂移——多轮后主题走远了，标题
可能过时（fold_title 截断版更是只反映首问字面）。取舍见 ADR-0015 ①：
标题不该每轮变脸，手动改名是纠偏路径（菜单重命名 / AI 提炼标题可
随时补跑）。已知缺口：没有"主题漂移后提醒改名"的机制（known-issues
backlog，价值未实证不做）。

### 会话树的规模边界（M4.5）

- `GET /api/sessions` 沿袭 limit=50：会话超 50 时侧栏只显示最新 50 条，
  更早的会话在库里但不在树里（无搜索/分页入口，known-issues backlog）；
- 文件夹展开态不持久化：刷新页面回到全折叠（ADR-0015 8）；
- 删除语义：文件夹只删空夹（409 带计数），没有"连子树一起删"的
  快捷路径——防误删的刻意取舍（ADR-0015 4）。

### SQLite 版本依赖面（M4.5 迁移后）

schema 迁移用到的 `WITH RECURSIVE`（防环/后代查询）、`ALTER TABLE ADD
COLUMN ... REFERENCES`（无默认值 = NULL，合法）都是 Python 内置
sqlite3 长年支持的特性——本项目只依赖 CPython 自带的 SQLite，不经
Windows 系统版 sqlite3.dll，版本面 = Python 版本的 sqlite 下限
（3.13 自带 ≥3.40），迁移测试在纯内存/临时库覆盖，无跨版本差异需要
守护。首次真实迁移（v1→v2）的演练记录见 ADR-0004 修订段。

## 四、开发期真实 bug 案例（已修复，留档）

> 每条都是"当时为什么没测到"的复盘，不是事故炫耀。留档价值：技术评审讲
> 测试与调试故事的原材料。

| 案例 | 发现方式 | 根因 | 守护 |
| --- | --- | --- | --- |
| python-docx `Document` 当 context manager 用（`with docx.Document(...)`） | mypy | python-docx 对象不实现 `__enter__`/`__exit__`，代码从未被 M1 早期测试覆盖 | 修复 + loader 单测 |
| 分块重叠尾巴越界：块长 = 重叠尾 + 整段 > size 时超发 | 代码评审 | 重叠尾巴"续接整段"逻辑没算剩余容量 | 截短而非超发 + `test_overlap_respects_size` |
| 指纹只哈希内容、漏混 chunk id | 2026-09-09 真实验收第一轮 | `--reindex` 后 AUTOINCREMENT 使 id 平移，内容指纹不变而 gold_chunk_ids 全错位 → 63 题 recall 全 0 却指纹校验通过 | 指纹混入 id，任何重建显式失配（详录于 evaluation.md §2） |
| doctor 的 jieba 检查形同虚设 | 评审 | 检查"能否 import jieba"——降级路径下 import 必过，实际生效的却是 bigram，检查永远 OK | 改为检查 `get_tokenizer_name()` 的实际生效实现 |
| TestClient 报 500 看不清真实错误 | M3 Web 测试 | FastAPI 测试默认吞异常栈（raise_server_exceptions=True），HTTPException 之外的错误显示为壳 500 | 测试统一 `raise_server_exceptions=False`，`_error_body` 携带 status 字段 |
| git-bash curl 发 JSON body 损坏 | M3 Web 冒烟 | Windows git-bash 内联单引号 JSON 被转义吞字符 | 冒烟脚本一律 `--data-binary @file` |
| 引号嵌套 f-string 报语法错 | M3 Web | 引号内嵌引号 f-string 是 Python 3.12+ 语法，项目锁定 3.11 | 代码评审查语法下限 |
| 前端 esc() 转义反引号 | 渲染纯函数评审 | 反引号被转义会破坏 Markdown 行内 code 识别 | esc 不转义反引号（文本上下文无反引号 XSS 面，有注释论证） |
| 上传 415 文案成死代码 | M3 Web 测试 | sanitize 与后缀白名单职责混在一起，白名单分支永不触发 | 职责分离：sanitize 只管文件名安全，白名单独立返回 415 |
| el() 第二参显式传 null 抛 TypeError | 用户浏览器验收（health 胶囊空白、知识库页表格空） | `attrs = {}` 默认值只在省略时生效，`el("span", null, …)` 进 `Object.entries(null)` 抛错；又在 initTopbar 的 try 内被 catch 静默吞掉 → 控制台零报错、UI 静默空白 | `Object.entries(attrs ?? {})` 兜底 + 浏览器实测（无痕窗口过缓存验证） |
| reindex 中途失败会**静默删掉 uploads 源副本** | M4 实机验证（嵌入模型首次下载，连断两次） | 失败回滚把"源文件就是入库副本"（reindex 扫 uploads）也当半成品 unlink——uploads/01、02 两篇笔记被删，重跑 reindex 只扫到剩余 19 篇，**语料静默缩水且自洽**（doctor 全绿）；靠 build_golden 锚句零命中拒绝生成才暴露 | 原稿在 sample-corpus/notes 完好可重灌；金标"宁可报错不产出"再次当哨兵；真实盲点：uploads 无原稿的文档（Web 上传）遇此即真丢——清理前应区分"外部源"与"唯一副本" |
| LocalReranker 按 fastembed 早期 API 编写，0.8 已移除重排 API | M4 实装后 mypy 报错 | fastembed 未装时 `ignore_missing_imports` 吞掉整个 import，装真包（0.8.0）后 `TextCrossEncoder` attr 错误才暴露；docstring 还宣称"0.8 稳定 API"（文档-代码漂移） | docstring/ADR-0014 ② 改写实情；import 处显式 `type: ignore[attr-defined]` + 原因注释；启用本地重排前必须先做版本选型 |
| `python -m mikasa` 绕过 .env 装载（与 `mikasa` 命令不等价） | M4 F 组回切抽查（doctor api 假报密钥缺失） | `__main__.py` 直调 `app()`，而 `load_dotenv_file()`/`setup_logging()` 只在 `cli.main()` 里；docstring 还宣称"等价于 mikasa 命令"——本地/offline 无密钥感知不到，只有 api profile 暴露 | `__main__.py` 改调 `main()`（注释指向本文）；回归测试用 runpy 以 `__main__` 身份执行模块并断言走 main |
| qa-tree.js 用了 `$` 却漏 import（问答页整页静默死亡） | 2026-09-09 用户实测（health 胶囊永远"正在连接服务…"、问答无反应；知识库/评测两页正常） | 模块顶层 `const listBox = $("#session-list")` 在 import 列表（只含 apiFetch/el/esc/fmtTime/toast）之外 → ReferenceError 在**模块求值期**抛出 → qa.js 整条 import 链死亡；node --check 只查语法查不出未定义标识符；M4.5 只经后端 TestClient + node 冒烟（冒烟只覆盖 tree.js），问答页自改版后从未进过真实浏览器 | import 补 `$`；**此后前端改版一律用无头 Chrome（`--headless=new --enable-logging=stderr --dump-dom`）验收三页**：断言 health-pill 出实况、console 零 error——顺手发现全站静态资源 200 与语法通过都不等于模块链活着 |
| 行内改名输入框"一点就关"（点击想定位光标反而退出编辑，长文字尤甚） | 2026-09-09 用户实测反馈（正在做第二站细改） | 改名时 input 替换的是**行内名字 span**（仍是可点击行的后代）；行点击委托（qa-tree/kb-tree 各两处：文件夹 toggle、会话 onOpenSession、文档 selectDoc）只排除 `button`，未排除 `.tree-input` → 点 input 内部想移动光标，click 冒泡到行 → toggle/选中 → `renderTree()` 整树重绘 → 输入框随 innerHTML 清空而销毁。树里没有任何"编辑态"概念，这是"行内编辑 + 事件委托"组合的盲区 | 四处委托守卫统一 `closest("button, .tree-input")`；回归工具 **tools/chrome_rename_check.py**（无头 Chrome：进改名 → 点输入框内部 → 断言 input 仍 isConnected → Esc 还原零残留），问答/知识库两页全过。教训入库：**行内编辑元素若是行后代，行点击委托必须显式放行编辑元素** |
| 表格块"表尾碎片"污染检索（问答答"未明确标注参数名称"） | 2026-09-10 表格形态呈现收尾复问（问"胶结充填体这一行的各项参数"，LLM 答不出列名） | 链上有两环：① chunker 的 overlap 尾巴取自窗口尾部，表格块在窗口尾时把**表尾数据行复制成下一块开头** → 无表头的碎片块；② 检索问"某行的参数"时碎片反而压过完整表——碎片开头即命中行、短而相似度高，完整表（表头+全部 7 行 ~360 字）因掺杂大量无关行被语义稀释、融合分落出 top-10 → 注入的就是缺列名的碎片，LLM 只敢说"数值为 1.83、0.38…但名称未标注"（诚实但无用）。根因是**表格是"表头+行"结构整体，把它当普通文本参与 overlap/稀释** | ① overlap 尾巴**不跨表格段**（起点落在表段内 → 整段跳过，宁少重叠；`_overlap_tail`）；② 超长表格段（>size）按**行打包**切块而非标点/硬切（`_pack` 加 sep 参数，行间保留换行）；③ 修复后问原题：全库仅剩 1 块含该行 = 完整表块，LLM 自动按表格输出全部 7 列（无需提示"用表格"）。回归测试两条入 test_chunker.py（355 tests 全绿）。盲点复盘：形态标记虽把表格独立成段，但 chunker 只认"段落"不认"表格语义"，结构性内容参与文本级 overlap/标点切分时仍会被劈——**任何结构标记（表格/代码/引文）入库前都应考虑整段不可切** |
| 跨语言翻译路静默失效（中文问英文库 4 题全退步回"无翻译"态） | 2026-09-10 跨语言检索实测第一轮（Q1-Q4 latency 全无 translate 键、Q1 被库内中文论文抢走、Q2 从答对退到拒答） | **qwen3:8b 自带 thinking**：OpenAI 兼容路径下思考文本先耗尽 max_tokens（96）→ `content` 返回**空串**；`_translate_query` 的防御"空输出 → 回退单路"照常生效但**零日志**（debug 默认关、无 warning），服务日志干净得像没走过翻译分支——排查半天才靠直连 11434 复现：带 max_tokens=96 空、不带/放大正常。本地小模型的"思考先耗上限"与"空 content 静默"组合是短输出调用（翻译/标题）的隐形坑 | TRANSLATE_MAX_TOKENS 96→512（注释留档事故链）；教训：① 任何"短输出 + 本地 thinking 模型"的组合先实测再写死上限；② 防御性回退必须带 debug 可见痕迹（已存在，但默认日志级别看不见——排查时可临时开 debug） |

## 五、doctor 体检口径的演进

doctor 的价值不在"报一堆 OK"，而在**每个检查都指向可执行的下一步**。
口径随项目演进记录如下：

- 检查项语义：依赖检查查**生效行为**而非导入能力（jieba 案例）；
  密钥检查只报缺哪个环境变量名、不接触值；索引一致性在 M4 升格为
  **三方一致性**（meta.json 快照 × 库内向量行数/维度 × 当前配置——
  DB 权威、快照派生，失配一律提示 `mikasa ingest --reindex`，
  见 ADR-0014 ④）；
- 已拦截上限：Python ≥3.14 未验证即报错（下限由 pyproject 保证）；
- 本地推理门控（M4）：`llm.backend == "local"` 才查 Ollama 服务连通 /
  模型已拉取（带 `ollama pull` 指引与 Windows `OLLAMA_BASE_URL` 逃生口
  文案），`embedding.backend == "local"` 才查 fastembed 可导入——offline
  / api 零感知；探测函数是模块级实现（单测整体替换，doctor 从不真连
  外部服务）；doctor **不触发嵌入模型下载**（TextEmbedding 构造即拉
  ~100MB，下载归首次 ingest/提问的运行时）；
- 分级与失败出口：doctor 是**硬门禁**——任一项失败都会汇总红行 +
  分类排查建议 + 退出码 1（本文档早期写"可选依赖失败仅告警不退出"，
  与代码不符，已修正：可选依赖缺失也是失败，文案负责给出"装什么、
  去哪装"的告警语义）；密钥合格线三档分化——mock 无需 / local 免密钥
  （Ollama /v1 不校验 Authorization，provider 层注入占位 key，见
  ADR-0014 ①）/ api 严格——一张表同时覆盖。

**盲点自陈**：doctor 只体检"环境与静态一致性"，不体检"检索质量"——
那由评测（`mikasa eval run --profile offline`）回答，CI 里两者串行执行，
别把 doctor 当质量门禁用。

---

## 六、开发期真实 bug 案例（续，2026-09-11）

### 页码系统性偏移：同一类 bug 的两处（ingest/loaders.py）

`load_pdf` 用 `enumerate(cleaned, start=1)` 给页编号，"一页一元素"是硬
不变量。但 `_drop_page_furniture` 与 `_cut_reference_tail` 各写了一次
`if kept:` / `[t for t in rest if t.strip()]`，把剔除后为空的页从列表里
滤掉——空白页（doc 71 的第 3/5/7 页）一多，后续页码系统性前移，实测
+3~+5 位，两处叠加把 301 页截到 189 且前部错位。**教训：同一个"列表位次
即身份"的不变量要在所有消费点核对**——第一次只修了 `_drop_page_furniture`，
偏移依旧，直到拿"cut 后 189 页"反推才找到第二处。

### Ollama 运行时上下文窗口只有 2048（模型本身 40960）

qwen3:8b 声明 40960，但 Ollama 运行时默认 `num_ctx=2048`——6000+ token
的提示词被静默截到 2050，且从**开头**丢：系统提示词（引用协议、拒答规则）
可能从未进过模型。暗号测试：长占位文本中间夹问开头暗号，修前空回复、
设 `OLLAMA_CONTEXT_LENGTH=16384` 后全对。这也是"检索给的内容太少"的一大半
根因——注入 10 块 ≈3500 字符早就超窗。Mikasa.bat 已固化该环境变量。
