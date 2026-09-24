"""笔记互链：`[[标题]]` → `doc_links` 边（M6 ③ 的**确定性**写入方）。

**为什么是它而不是 LLM 抽取**（2026-09-25 用户拍板）：`doc_links` 表（schema v5）
当初只留了地基——写入函数、邻居查询、删除级联都在，但没有任何写入方，所以表
一直是空的。让模型读两篇文档判关系（ADR 里记的原计划）代价大、质量不确定，
而且用户此前已明确把它"留档待排期"。笔记互链补上的是**同一张表的另一半用法**：

  - 边是**用户自己写的**，精度 100%，没有"模型觉得这两篇相关"的噪声；
  - 写入发生在保存笔记的那一刻，**零额外调用**（不碰 LLM、不联网）；
  - 与"知识库 → 笔记库"的产品方向一致——互链本来就是笔记应用的核心动作。

LLM 抽取那一期落地时，写的是同一张表、同一个 `replace_doc_links` 入口，
`source` 字段区分来源（这里写 `wikilink`），互不冲突。

**语法**：`[[标题]]`，可选别名 `[[标题|显示文字]]`（别名只影响渲染，不参与解析）。
行内代码与围栏代码块里的 `[[...]]` 不算——那是代码字面量（同 KaTeX 那套闸门）。
"""

from __future__ import annotations

import re

from mikasa.config.settings import Settings
from mikasa.storage import repo
from mikasa.storage.db import open_db

# 行内代码 `x` 与围栏代码块 ```...``` 都不参与解析（代码里的 [[ ]] 是字面量）
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
# [[目标]] 或 [[目标|别名]]：标题里允许空格与中文，但不许换行、不许再套括号
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n|]+)(?:\|[^\[\]\n]*)?\]\]")


def extract_targets(text: str) -> list[str]:
    """正文里的互链目标（去重保序，已剥别名与首尾空白）。

    返回的是**标题原文**，解析成文档 id 是调用方的事（本函数是纯函数，
    可以在没有库的情况下测全部分支）。
    """
    without_code = _FENCED_CODE_RE.sub("", text or "")
    without_code = _INLINE_CODE_RE.sub("", without_code)
    seen: set[str] = set()
    targets: list[str] = []
    for raw in _WIKILINK_RE.findall(without_code):
        title = " ".join(raw.split())
        if title and title not in seen:
            seen.add(title)
            targets.append(title)
    return targets


def sync_doc_links(settings: Settings, doc_id: int, text: str) -> int:
    """把这篇笔记的出边同步成 `text` 里的互链，返回写进去的边数。

    三个口径与 `repo.replace_doc_links` 一致（整篇重跑 = 替换而非追加；
    只动出边；不动 `manual` 来源的边），所以这里**编辑一次笔记就等于把它的
    出边重算一遍**——删掉一个 `[[链接]]`，对应的边随之消失。

    标题解析：**同名文档全部落地**（个人库里重名多半是同一篇的两次导入，
    取其一会在用户改了标题之后静默指向另一篇）。解析不到的目标直接跳过——
    写了一半的笔记里 `[[还没写的笔记名]]` 是常态，那不是错误。
    """
    targets = extract_targets(text)
    with open_db(settings.db_path) as conn:
        if repo.get_document(conn, doc_id) is None:
            return 0  # 文档刚被删掉（并发）：没有可挂的边
        if not targets:
            # 没有互链时**也要跑一次替换**：否则删光链接后旧边会留下
            return repo.replace_doc_links(conn, doc_id, [])
        by_title: dict[str, list[int]] = {}
        for doc in repo.list_documents(conn):
            if doc.id is not None:
                by_title.setdefault(doc.title, []).append(doc.id)
        links = [
            {"dst_doc_id": dst_id, "relation": "mentions", "source": "wikilink", "confidence": 1.0}
            for title in targets
            for dst_id in by_title.get(title, [])
            if dst_id != doc_id
        ]
        added = repo.replace_doc_links(conn, doc_id, links)
        conn.commit()
    return added
