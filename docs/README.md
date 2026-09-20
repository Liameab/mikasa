# 文档

Mikasa 的技术文档——写给想知道**为什么这样建**的人，而不只是"它做了什么"。
每篇都要求对取舍、边界与错误保持诚实。

> 本目录（`docs/*.md`）是**中文原版**；英文镜像在 [`en/`](en/)——两棵树同名文件一一对应，
> 由 `tests/unit/web/test_docs_trees.py` 盯着（缺一个或写坏一条相对链接就红）。
> 仓库首页 [README.md](../README.md) 同样是中文为主，英文版在
> [`README.en.md`](../README.en.md)。

| 文档 | 内容 |
| --- | --- |
| [架构总览](architecture.md) | 系统怎么组织、数据怎么流，以及防幻觉三层防线 |
| [设计决策记录（ADR）](design-decisions.md) | 每个"为什么"：选型、取舍、被后来的数据推翻的决策 |
| [自动化评测](evaluation.md) | 三阶段口径、黄金集防错配、裁判偏差修正、验收基线 |
| [已知限制与失败案例](limitations-and-failures.md) | 生态故障、启发式边界、开发期真实 bug 档案 |
| [使用说明](usage-guide.md) | 端到端工作流：查论文、追问技巧、被拒答了怎么办 |
| [已知问题与改进 backlog](known-issues.md) | 未修复问题（含裁决记录）与未排期候选 |
| [设计系统](../DESIGN.md) | Web UI 的 token（CSS 变量）、字阶、圆角语法、z-index 阶梯、状态与组件契约——有护栏测试盯着 |

`ideas-and-backlog.md`（想法总账）是**私人文档**，不进仓库、没有英文镜像。
