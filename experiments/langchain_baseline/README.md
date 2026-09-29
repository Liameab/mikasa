# LangChain 对照组（不进 `src/`、不进主 CI）

**形态：对照，不底座。** 用 LangChain 搭出**同一套检索架构**，跑**同一份语料 + 同一份题库**，
和本仓自研的检索对表——产出的是一张"我两条路都走过，代价和收益是这样"的表，
而不是把自研检索换掉（换掉就等于把本仓唯一的差异化亲手拆了，见 `docs/design-decisions.md`
ADR-0009~0012 与 `docs/tech-roadmap-reply.md` 方向 5）。

**本目录不是产品的一部分**：它不进 `src/mikasa/`、不进主 `pyproject.toml` 的依赖、
不进主 CI（依赖树很重，见文末「依赖成本」）。主仓里留下的只有结论与指针
（`docs/evaluation.md` §8）。

---

## 1. 对照协议（先说清楚，否则数字没有意义）

| 变量 | 处理 |
| --- | --- |
| 语料 | **同一份快照**：由 `export_snapshot.py` 从 Mikasa 的数据目录导出（块 id、正文、词条、向量） |
| 题库 | `evals/golden_set.json` 的 47 道可答题（gold 是**块级** `chunk_id`），两边的分都按它算 |
| 嵌入 | **同源**：向量在导出时由 Mikasa 的生产配置（`bge-small-zh-v1.5`）算好，框架侧按文本取回同一批向量，**不重新嵌入** |
| 分词 | 同源：词条随快照下发（两边同一个分词器），框架侧按原文取回，不自己猜一套分词 |
| 检索口径 | 关重排、`bm25_top_k=20`、`dense_top_k=20`、融合窗口 10、RRF `k=60`（= 评测阶段 A 的口径） |
| 融合公式 | 两边都是 `Σ 1/(rank + 60)`、两路等权（LangChain 的 `EnsembleRetriever` 就是这个式子，`weighted_reciprocal_rank`） |
| 评分 | **同一份代码**：两边都喂进本仓 `src/mikasa/eval/metrics.py` 算 recall@k / MRR / nDCG@k，配对 bootstrap 也是同一份 |

**自研那一列是产品代码的输出**：快照里的 `mikasa_ranked` 由 `export_snapshot.py` 调用真实的
`Retriever.retrieve()` 得到（阶段 A 口径，关重排），不是在对照脚本里重写一遍的复刻品。

### 两边的真实差异（只留下无法消除的那些）

1. **BM25 实现不同**：框架侧是 `rank_bm25.BM25Okapi`（LangChain `BM25Retriever` 的底层），
   本仓是手写 `BM25Index`。两者 `k1=1.5 / b=0.75` 相同，但 **IDF 公式不同**：
   `rank_bm25` 用 `ln((N-df+0.5)/(df+0.5))`（负值再兜底成 `0.25×平均 IDF`），
   本仓用 `ln(1 + (N-df+0.5)/(df+0.5))`（恒正）。同一个词在两边的权重因此不是一个数。
2. **向量库实现不同**：框架侧 FAISS（`IndexFlatIP`，内积 + 我们自己先归一化 = 余弦），
   本仓是 numpy 精确矩阵乘。**实测两边逐题 top-20 完全一致**（47/47）——这条差异是零。
3. **框架侧没有的东西**：引用协议（`[n]` 编号、越界校验）、拒答规则、跨语言第二路、
   一跳扩展、分块器、评测——本仓这些层在框架里**都得自己再写一遍**，本次对照只比检索。

## 2. 怎么跑

```bash
# ① 导出快照（主 venv：需要本仓依赖 + 本地嵌入模型）
.venv/Scripts/python.exe experiments/langchain_baseline/export_snapshot.py \
    --data-dir build/eval-local/data --profile local --out experiments/langchain_baseline/out

# ② 建对照环境（一次；独立 venv，不碰主环境）
python -m venv experiments/langchain_baseline/.venv
experiments/langchain_baseline/.venv/Scripts/python.exe -m pip install -r experiments/langchain_baseline/requirements.txt

# ③ 跑对照（框架 venv；自研那一列直接读快照里算好的排名）
experiments/langchain_baseline/.venv/Scripts/python.exe \
    experiments/langchain_baseline/run_baseline.py --out experiments/langchain_baseline/out

# ④ 生成/引用层（框架 venv；本机 Ollama 在跑、有 qwen3:8b）
experiments/langchain_baseline/.venv/Scripts/python.exe \
    experiments/langchain_baseline/run_generation.py --out experiments/langchain_baseline/out
```

产物：`out/result.md`（人读的表）、`out/result.json`（逐题排名与差值，供复核）、
`out/generation.md` + `out/generation.json`（生成层）。`out/` 是派生数据，不进仓库。

> **④ 的两个环境坑（本机实测，2026-09-29）**：① **系统代理**——Python 的 httpx 会从注册表
> 读到 Windows 系统代理且**不认 ProxyOverride 的绕过列表**，本机 Ollama 流量会被塞进代理
> 并卡死（表现为进程 CPU 冻住、Ollama 日志零增长）。跑批前设
> `NO_PROXY=localhost,127.0.0.1,::1`。② **显存**——qwen3:8b 用 16384 上下文要约 7.1GB，
> 8GB 卡装不下，只要别的模型（如 `qwen2.5vl:7b`）在显存里就会被反复驱逐/重载；
> 跑批前先 `ollama ps` 看看，必要时 `keep_alive: 0` 卸掉。两条都会让整轮从 ~20 分钟
> 变成"看起来永远跑不完"。

## 3. 结果（2026-09-28，47 题、57 块语料、`bge-small-zh-v1.5`、jieba）

| 指标 | 自研（生产代码） | LangChain | 配对差值（LC − 自研） | 95% CI | 显著 |
| --- | --- | --- | --- | --- | --- |
| recall@5 | 0.982 | 0.982 | +0.000 | [+0.000, +0.000] | 否 |
| recall@8 | 0.982 | 0.982 | +0.000 | [+0.000, +0.000] | 否 |
| recall@10 | 0.982 | 0.982 | +0.000 | [+0.000, +0.000] | 否 |
| MRR | 0.924 | 0.911 | −0.012 | [−0.035, +0.000] | 否 |
| nDCG@10 | 0.933 | 0.924 | — | — | — |

- 检索耗时（中位数，**不含查询嵌入**）：自研 1.2ms ／ LangChain 0.4ms（取自本次产物
  `out/result.json` 的 p50 = 1.15 / 0.43ms；多次跑两边分别在 1.2~1.8ms / 0.4~0.8ms
  之间抖动，都在毫秒级）。
- 前 10 名集合完全一致的题：21/47；不同的 26 题逐条见 `out/result.json`。
- **逐路诊断**（脚本自动跑，写在 `out/result.md` 末尾）：稠密路 top-20 **47/47 逐题一致**；
  BM25 路 top-20 **0/47 一致**。

**怎么读这张表**：

1. **召回一样**（0.982，逐题零差异）：两边的融合都把题面素材捞回来了。差异出现在**排序**
   （MRR −0.012），区间跨 0 —— 这个题量下不能说框架更差，只能说"没有可测的差距"。
2. **差异全部来自 BM25 那一路**：向量路两边 top-20 逐题一致（47/47，同一批向量、同一个余弦），
   而 BM25 的 top-20 在 47 题上**全部**不同（IDF 公式不同，见 §1）——融合之后落到 top-10，
   26 题出现了不同的块。也就是说：**框架替你做的是管道，管道的零件性能并不天然更好**。
3. **耗时不是检索的瓶颈**：两边都是毫秒级（差不到 1ms），而同一批问题的**查询嵌入要 40.8ms**。
   `rank_bm25` 的 numpy 实现确实比手写纯 Python BM25 快几倍，但那几毫秒在真实链路里
   听不见——这条也是"别为性能换实现"的实测依据。

**顺带复核掉的两条旧结论**（都写进 `docs/known-issues.md`）：

- **ADR-0010 说"FAISS 在 Windows 上没有 PyPI wheel，只有 conda"** —— 2026-09 实测
  `pip install faiss-cpu` 直接装上 `1.15.1`（Windows + CPython 3.13）。结论本身仍然成立
  （个人库规模下 numpy 精确检索够用、且零额外依赖），但**理由里的那条事实已经过期**。
- **`langchain-community` 已进入 sunset**（安装时官方 DeprecationWarning，指向
  `langchain-community#674`）：本次用到的 `BM25Retriever` / `FAISS` 都在里面，
  而 `langchain 1.4.2` 已把 `EnsembleRetriever` 挪进 `langchain-classic`。
  框架分层还在动——这正是"对照可以，底座要慎重"的现场证据。

## 4. 依赖成本（实测）

| 项 | 数值 |
| --- | --- |
| 框架侧环境 | 63 个包 / 253 MB（`.venv`，不含嵌入模型与语料） |
| 直接依赖 | `requirements.txt` 里 9 个（版本写死） |
| 本仓主环境 | 90 个包（含 fastembed / onnxruntime 这类本地嵌入栈） |

装的时候还会拿到两条过程性提示：`langchain-community` 的 sunset 警告（见上），
以及 `jieba` 那条与本仓 ADR-0008 同源的 `pkg_resources` 弃用（本仓已有降级路径，框架侧没有）。

## 5. 结果：生成/引用层（2026-09-29，63 题、qwen3:8b）

**口径**：两边吃**逐字相同**的 14 块上下文（生产注入窗口），框架侧不自己检索、自研侧也不检索，
差的只有生成这一层。框架侧 = `RetrievalQA.from_chain_type(chain_type="stuff")`（默认提示词，
资料无编号 + `StrOutputParser` 无校验）；自研侧 = 产品 `Generator`。

| 指标（63 题：可答 47 / 不可答 16） | LangChain 默认链 | 本仓协议层 |
| --- | --- | --- |
| 回答里出现 `[n]` 标记的比例（可答） | **0.0%** | 100% |
| 每份回答平均标记数 | 0.00 | 3.49 |
| 越界标记率 | 0.0%（空集） | 0.0% |
| 拒答率（不可答 16 题） | **0 / 16** | **10 / 16 = 62.5%** |
| 平均字数（可答） | 340 | 302 |
| 分节 / 列表项（每份均值） | 0.00 / 0.70 | 1.15 / 1.91 |
| 单题中位耗时 | 14.8s | 11.7s |
| 输入 / 输出 tokens | 151,882 / 11,955 | 212,181 / 9,940 |

**三句话读法**：

1. **框架那列的"0% 越界"是空集上的 0**——63 题里它一次都没吐过 `[n]`（逐题核对过），
   没有编号就无所谓越界。这一格不是"引用也规范"。
2. **拒答 0/16 是最硬的一条**：16 道库里没有答案的题，框架**全部**给了实质回答。
   默认链从不拒答——这就是幻觉的来源，也是"协议层必须自己写"的直接证据。
3. **代价是输入 token +39.7%**（编号/引用规则/拒答规则/示例都在提示词里）；中位耗时反而
   快 21%（拒答短），输出 token −16.9%。呈现形态（分节/列表）也是协议层带来的。

完整逐题数据：`out/generation.json`；`out/generation.md` 是同一张表。

## 6. 还没做的

- **api 档**（`bge-m3` + DeepSeek）同款对照：向量在云端算，成本与速率都由对方决定，暂缓。
- **检索层的耗时/内存曲线**：本机规模（57 块）下没有意义，语料上量后再看。
