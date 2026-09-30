# RAGAS 对照组（不进 `src/`、不进主 CI）

**形态：对照，不底座。** 拿评测界的"事实标准" RAGAS 四维（faithfulness /
answer relevancy / context precision / context recall）跑**同一批题、同一份答案、
同一份上下文**，与本仓手写口径**对表**——产出的是"两把尺子量同一件事，量出来的
差在哪、为什么"的一页纸，而不是把本仓口径换成 RAGAS（那等于把复试里最值钱的
自研评测亲手拆掉，见 `docs/evaluation.md` §3 的口径纪律）。

**本目录不是产品的一部分**：不进 `src/mikasa/`、不进主 `pyproject.toml` 的依赖、
不进主 CI（依赖树见文末）。主仓只留结论与指针（`docs/evaluation.md` §11）。

---

## 1. 对照协议（先说清楚，否则数字没有意义）

| 变量 | 处理 |
| --- | --- |
| 题库 | `evals/golden_set.json` 的**可答题**里抽 20 道：按难度分层、种子 `20260930` 写死（`export_answers.py`），逐题清单见 `out/result.md` |
| 语料 | `build/eval-api/data`（api 档评测库，57 块，bge-m3 向量） |
| 答案与上下文 | **产品链路原样**：`export_answers.py` 与评测阶段 B 逐字同轨（含跨语言第二路、产品检索配置、产品生成）；RAGAS 侧**不重跑**，只读 `out/answers.jsonl` |
| reference | 黄金集 `notes` 字段（出题人写下的要点）——RAGAS 的 context precision/recall 需要它 |
| 裁判 LLM | **与主仓评测同一个**：SiliconFlow Qwen2.5-72B-Instruct，temperature 0 |
| 嵌入 | 同一家 bge-m3（answer relevancy 用它算"生成问题与原问题的相似度"） |
| 版本 | ragas **0.3.1** + langchain 0.3.x 家族（钉死在 `requirements.txt`；0.4.3 与 langchain-community 0.4.x 是坏组合，见 §5） |

**两边的真实差异（只留下无法消除的那些）**：

1. **faithfulness 的证据面不同**：RAGAS 用**检索到的全部上下文**验证断言；本仓的
   claim 级 L2 只认**该断言自己引用的块**（`[n]` → 第 n 条注入片段）——本仓更严。
   两把尺子量的都是"断言有没有依据"，但"依据面"一个是全库窗口、一个是引用闭包。
2. **context precision/recall 的粒度不同**：RAGAS 是**句/段级对参考答案**；本仓的
   citation gold 是**块级对 `gold_chunk_ids`**，recall@k 是"gold 块在不在前 k 名"。
   同一轴、不同刻度，数字不可直接比大小，只能看**方向是否一致**。
3. **answer relevancy 与本仓没有对应物**：它量"答案贴不贴题"（经嵌入相似度），
   不量对错；本仓的裁判 correctness（1-5）量"对不对"。表里并列只为对读者说明
   "四维里有三维能与本仓对上，一维没有"。

## 2. 怎么跑

```bash
# ① 导出答案与上下文（主 venv；需要 .env 里的 DeepSeek + SiliconFlow 密钥）
MIKASA_DATA_DIR=build/eval-api/data .venv/Scripts/python.exe \
    experiments/ragas_baseline/export_answers.py --out experiments/ragas_baseline/out

# ② 建对照环境（一次；独立 venv，不碰主环境）
python -m venv experiments/ragas_baseline/.venv
experiments/ragas_baseline/.venv/Scripts/python.exe -m pip install \
    -r experiments/ragas_baseline/requirements.txt

# ③ 跑 RAGAS 四维（对照 venv）
experiments/ragas_baseline/.venv/Scripts/python.exe \
    experiments/ragas_baseline/run_ragas.py --out experiments/ragas_baseline/out

# ④ 对表（主 venv；把两边的数字摆进 out/result.md）
.venv/Scripts/python.exe experiments/ragas_baseline/compare.py
```

产物：`out/answers.jsonl`（20 题的答案/上下文/本仓指标）、`out/ragas.json`
（RAGAS 四维逐题）、`out/result.md`（对照表）。`out/` 是派生数据，不进仓库。

## 3. 结果（2026-09-30，20 题）

| 口径 | 本仓 | RAGAS |
| --- | --- | --- |
| 断言级忠实性 | claim 支持率 0.982（161/164） | faithfulness **0.973** |
| 整段忠实性 | 裁判 1.000（20/20 全绿） | — |
| 引用/上下文质量 | citation gold 0.724、越界 0 | context precision **0.987** |
| 召回 | 阶段 A（gold 块进窗） | context recall **0.925** |
| 答案与问题 | 裁判 correctness 4.80/5 | answer relevancy **0.422** |

三条结论（完整分析见 `docs/evaluation.md` §11.3）：① 忠实性两把尺子对得上，差异
来自**证据面**（本仓只认引用闭包，RAGAS 用全部上下文）——q036 是"引用挂错块但内容
在别的命中里"的典型，只有引用闭包看得见；② **answer relevancy 0.422 是语言造成的**：
RAGAS 提示词与示例全英文 → 中文答案的反向问题被写成英文 → 量的是跨语言相似度
（q004 实测余弦 0.584），不能与英文论文的 0.8~0.9 比；③ context precision 与
citation gold 的落差是**粒度**落差（宽口径 vs 窄口径），不是谁错。

逐题表：`out/result.md`（聚合 + 20 行逐题）。

## 4. 依赖成本（实测）

| 项 | 数值 |
| --- | --- |
| 对照环境 | **99 个包 / 591 MB**（`.venv`，不含语料与模型） |
| 直接依赖 | `requirements.txt` 里 8 个（版本钉死） |
| 主环境（对照） | 93 个包 / 454 MB |
| 上游调用量 | 生成 + 裁判 + 嵌入共约 200 次（20 题 × 四维；answer relevancy 每题还生成 3 个反向问题） |

## 5. 踩坑记录

1. **pip 回溯地狱（装依赖本身成了最大的一次耗时）**：`pip install ragas langchain-openai`
   同时给两个包，解析器在 `langchain-core` 的版本区间上**回溯**——一个包一个包地
   试、每次都真下载，配合国内镜像 ~20 kB/s 的吞吐，半小时都装不完（日志里能看到
   它从 langchain-core 1.4.2 退到 1.2.29 再退）。**修法：先单独装 `ragas`**（让它自己
   选定 langchain-core），再按已装版本配 `langchain-openai`——把"联合求解"拆成两步
   顺序求解，回溯消失。
2. **"from versions: none" ≠ 包不存在**：首次安装时 `langchain-openai` 报"无可用版本"，
   而 `pip index versions langchain-openai` 同一时刻列得出 1.6.6——是**索引请求瞬时
   失败**被翻译成了"没有版本"。重试即好（`--retries` 帮不上这一种，它是解析期错误）。
3. **主仓一侧的真 bug（顺带被这次对照抓出来）**：claim 级核对的首版解析器只认
   `1: 是`，而 Qwen 把逐条判断写成有序列表（`1. 是`）→ 20 题里 16 题的断言整批记成
   "未判定"。见 ADR-0042 的踩坑留档。
