# 光荣进化系统 v1.0.8

> MIA 风格的智能记忆与自改进框架 — AstrBot 插件

## 架构

```
Memory    → storage.py + memory_manager.py  (SQLite + 向量索引 + 胜率)
Reasoning → reasoning_engine.py            (检索 → 规划 → 评估 → 重规划)
Evolution → evolution_task.py              (规则提炼 + 淘汰 + Insight)
Security  → tool_sanitizer.py             (ToolCallHook 脱敏)
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
| ToolCallHook 敏感信息脱敏 | ✅ |
| 全局数据目录 (卸载安全) | ✅ |
| 自动备份 | ✅ |

## LLM Tools

- `store_memory` — 存储记忆
- `search_memory` — 搜索记忆
- `update_win_rate` — 更新胜率
- `evict_memories` — 淘汰低质量记忆
- `get_evolution_stats` — 系统统计
- `build_plan` — 基于记忆生成行动计划
- `judge_replan` — 评估是否需要重规划
- `build_replan` — 生成补充计划
- `trigger_evolution` — 手动触发进化

## 命令

- `/ges` — 查看进化统计

## 安装

将本仓库放入 AstrBot `data/plugins/` 目录。

## 更新日志

### v1.0.8 — 数据持久化硬化
- 🔒 数据目录迁移至 `/AstrBot/data/glorious_evolution/`（与插件目录解耦，卸载不清空数据库）
- 💾 每次进化循环结束后自动备份到工作区
- 🔄 插件终止时执行最终备份
- 📦 旧路径数据自动迁移

### v1.0.7 — ToolCallHook 脱敏
- 🛡️ 拦截所有工具返回值中的敏感信息 (API Key, Token, 密码等)
- 📊 NumPy 批量矩阵运算优化向量检索 10-50x

### v1.0.6 — 双存储割裂修复
- 🧠 SQLite 替代 JSON 作为主存储
- 📈 FTS5 全文搜索
- 🔍 向量索引批量加载
