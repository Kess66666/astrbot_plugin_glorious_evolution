# 光荣进化系统 v0.4.0

> MIA 风格的智能记忆与自改进框架 — AstrBot 插件

## 架构

```
Memory    → storage.py + memory_manager.py  (SQLite + 向量索引 + 胜率)
Reasoning → reasoning_engine.py            (检索 → 规划 → 评估 → 重规划)
Evolution → evolution_task.py              (规则提炼 + 淘汰 + Insight) [Phase 3]
```

## 能力

| 能力 | 状态 |
|------|------|
| 记忆存储 + FTS5 全文搜索 | ✅ |
| 向量化 (AstrBot EmbeddingProvider) | ✅ |
| 去重 (余弦 ≥ 0.95) | ✅ |
| 正负分离检索 | ✅ |
| 混合评分 (0.7×cos + 0.3×胜率) | ✅ |
| 胜率管理 (简单比值/衰减) | ✅ |
| Plan / Judge / Replan LLM 推理 | ✅ |
| 记忆淘汰 | ✅ |

## LLM Tools

- `store_memory` — 存储记忆
- `search_memory` — 搜索记忆
- `update_win_rate` — 更新胜率
- `evict_memories` — 淘汰低质量记忆
- `get_evolution_stats` — 系统统计
- `build_plan` — 基于记忆生成行动计划
- `judge_replan` — 评估是否需要重规划
- `build_replan` — 生成补充计划

## 命令

- `/ges` — 查看进化统计
- `/store` — 手动存储记忆

## 安装

将本仓库放入 AstrBot `data/plugins/` 目录。
