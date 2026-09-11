"""生成 sample-corpus 里的二进制样本：GBK 编码 TXT / 带样式与表格的 DOCX / 多页 PDF。

为什么把二进制样本交给脚本生成而不是手写（见 sample-corpus/README.md）：
  - 二进制无法 diff，脚本即"源代码"，可复现、可审查；
  - 每种格式都刻意踩一个 loader 特性：TXT 的 GBK 编码探测、
    DOCX 的 Heading 样式与表格、PDF 的多页页眉页脚（loader 剔除逻辑）。

用法：python tools/build_sample_binaries.py（输出到 sample-corpus/，幂等覆盖）
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "sample-corpus"


def build_txt_gbk() -> Path:
    """纯文本学习路线，GBK 编码（load_txt 的编码探测路径）。"""
    path = OUT / "机器学习学习路线.txt"
    content = """机器学习方向学习路线速览

第一阶段 基础补强（第 1-2 周）
线性代数：矩阵运算、特征值分解、矩阵求导与正规方程。
概率论：期望与方差、贝叶斯公式、极大似然估计、偏差方差分解。

第二阶段 核心算法（第 3-5 周）
监督学习：线性回归与梯度下降、逻辑回归与交叉熵、支持向量机与核方法。
无监督学习：K 均值聚类与轮廓系数。
集成学习：随机森林的 Bagging 思想、Boosting 的残差学习。

第三阶段 深度学习（第 6-8 周）
神经网络：反向传播的链式法则推导、常见激活函数与优化器。
结构模型：CNN 的权值共享与感受野、RNN 的梯度消失与 LSTM 门控。
注意力：Transformer 的自注意力与位置编码，弄清 QKV 投影的维度变化。

第四阶段 大模型与项目（第 9-12 周）
大模型：decoder-only 预训练、SFT 与 RLHF 对齐、上下文学习与思维链。
项目：检索增强生成（RAG）的召回-融合-生成流水线，重点掌握引用溯源、
拒答机制与评测指标的设计思路，能讲清每个设计取舍。

每周固定动作：整理疑问、复述一个算法的推导、用 Zettelkasten 笔记法沉淀。
"""
    path.write_text(content, encoding="gbk")
    return path


def build_docx() -> Path:
    """学习路线规划 DOCX：Heading 样式 + 三列表格。"""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    path = OUT / "AI 学习路线规划.docx"
    document = docx.Document()

    document.add_heading("我的 AI 方向学习与项目路线", level=1)
    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("以“能直接上手科研”为标准，把学习阶段拆成三个并行线程。")
    run.italic = True

    document.add_heading("线程一：算法与数学基础", level=2)
    document.add_paragraph(
        "主线是“线性代数 → 概率论 → 机器学习 → 深度学习”教材链。"
        "每晚固定两小时：一小时推公式（如反向传播的手推、SVM 对偶的推导），"
        "一小时刷经典题目。目标不是刷题量，而是能把每个算法“从头讲到尾”。"
    )
    document.add_heading("线程二：项目与工程能力", level=2)
    document.add_paragraph(
        "选择一个能每天真实使用的工具型项目持续打磨：我的选择是个人知识库问答系统，"
        "技术栈覆盖 RAG 全链路——中文分块、BM25 与向量混合检索、RRF 融合、"
        "引用溯源与拒答、自动化评测。项目要写到能扛住追问的程度：每个设计决策"
        "都能说出取舍，每张评测表都来自真实实验。"
    )
    document.add_heading("线程三：科研视野", level=2)
    document.add_paragraph(
        "每周精读一篇论文并写结构化笔记（动机-方法-实验-局限四段式），"
        "重点领域是检索增强与可信大模型方向，这既是技术视野的积累，"
        "也为独立研究打底。"
    )
    document.add_heading("周计划示例", level=2)
    table = document.add_table(rows=4, cols=4)
    table.style = "Table Grid"
    header = ["时间段", "主线任务", "产出物", "自检方式"]
    for i, text in enumerate(header):
        table.rows[0].cells[i].text = text
    rows = [
        ("第 1-3 周", "线性代数 + 概率论收尾", "公式推导笔记本", "复述贝叶斯与矩阵求导"),
        (
            "第 4-8 周",
            "机器学习经典算法 + 项目骨架",
            "算法手推题解 / 项目可问答",
            "每周一次模拟提问",
        ),
        ("第 9-12 周", "深度学习 + RAG 评测与实验", "消融实验表 / 论文笔记", "对照复盘清单"),
    ]
    for r, row in enumerate(rows, start=1):
        for c, text in enumerate(row):
            table.rows[r].cells[c].text = text
    document.save(path)
    return path


def build_pdf() -> Path:
    """多页论文研读笔记 PDF：每页重复页眉/页码（loader 剔除演示）+ 长行自动折行。

    折行必须由脚本自己完成：PyMuPDF 的 insert_text 遇到超页宽的长行会静默
    折行且续行与后插入行重叠丢失（真实踩坑，见 docs/limitations-and-failures.md），
    因此这里按字符预算（中文≈半角两倍宽）预折行，保证每行都能放进页宽。
    """
    import pymupdf

    path = OUT / "前沿论文研读笔记.pdf"
    doc = pymupdf.open()

    pages = [
        (
            "前沿论文研读清单（一）：检索与增强",
            [
                "Lewis et al., Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks, 2020",
                "提出 RAG：先用检索器召回文档片段，再让生成器基于片段作答，"
                "并证明检索能显著提升开放域问答的事实准确性。",
                "Robertson & Zaragoza, The Probabilistic Relevance Framework: BM25 and Beyond, 2009",
                "系统阐述 Okapi BM25 的概率检索框架，k1 与 b 参数分别控制词频饱和与长度归一化，"
                "是词法检索沿用至今的基线。",
            ],
        ),
        (
            "前沿论文研读清单（二）：注意力与大模型",
            [
                "Vaswani et al., Attention Is All You Need, 2017",
                "提出缩放点积注意力与多头机制，除以根号 dk 防止点积方差过大；"
                "decoder-only 自回归架构成为后续大模型的主流选择。",
                "Kaplan et al., Scaling Laws for Neural Language Models, 2020",
                "损失随参数量、数据量、算力呈幂律下降，指导了算力分配："
                "数据量提升往往比单纯放大模型更划算。",
                "Ouyang et al., Training language models to follow instructions with human feedback, 2022",
                "InstructGPT 论文，给出 SFT、奖励模型、PPO 的三段式对齐配方，"
                "是 RLHF 的经典范本，也是理解大模型对齐的第一篇必读。",
            ],
        ),
        (
            "前沿论文研读清单（三）：评测与可信",
            [
                "Es et al., RAGAS: Automated Evaluation of Retrieval Augmented Generation, 2023",
                "把 RAG 评测拆成忠实度、答案相关性与上下文相关性三个维度，"
                "用 LLM 作裁判自动化打分，主张指标分层报告而非只看端到端效果。",
                "阅读方法：每篇论文按动机-方法-实验-局限四段式写笔记，"
                "并追问一个问题：这篇的方法如果放在我的 RAG 项目里，消融实验会怎么设计？",
            ],
        ),
    ]
    # 内容行：固定 ≤43 字符/行（_MAX_LINE_CHARS），见 _wrap_by_width 说明
    for page_no, (title, lines) in enumerate(pages, start=1):
        page = doc.new_page()
        page.insert_text(
            (72, 56),
            "Mikasa语料库 · 论文研读笔记 · 页眉示例（每页重复，入库时自动剔除）",
            fontname="china-s",
            fontsize=7.5,
        )
        page.insert_text((72, 88), title, fontname="china-s", fontsize=14)
        y = 128
        for line in lines:
            for wrapped in _wrap_by_width(line):
                if y > 760:
                    break
                page.insert_text((72, y), wrapped, fontname="china-s", fontsize=10)
                y += 26
        page.insert_text(
            (72, 792), f"第 {page_no} 页 / 共 {len(pages)} 页", fontname="china-s", fontsize=7.5
        )
    doc.save(path)
    doc.close()
    return path


_MAX_LINE_CHARS = 43  # 安全上限见函数 docstring


def _wrap_by_width(text: str, max_chars: int = _MAX_LINE_CHARS) -> list[str]:
    """把文本折成 ≤max_chars 字符的行，超长 token（无空格 CJK 段/长链接）硬切。

    为什么定 43：PyMuPDF 内置 CJK 字体按接近全角的宽度排版拉丁字符，
    实测英文行约写到 x≈595pt 页宽边界（≈54 字符 @10pt）就被静默截断，
    续行根本不渲染（真实踩坑记录在 docs/limitations-and-failures.md）。
    43 × 约 11pt ≈ 473pt + 左边距 72 = 545pt < 595pt，任何字符组合都安全。
    """
    lines: list[str] = []
    current = ""
    for token in text.split(" "):
        if not token:
            continue
        while len(token) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(token[:max_chars])
            token = token[max_chars:]
        if current and len(current) + 1 + len(token) <= max_chars:
            current += " " + token
        elif current:
            lines.append(current)
            current = token
        else:
            current = token
    if current:
        lines.append(current)
    return lines


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for path in (build_txt_gbk(), build_docx(), build_pdf()):
        print(f"生成完成：{path.relative_to(REPO_ROOT)}（{path.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
