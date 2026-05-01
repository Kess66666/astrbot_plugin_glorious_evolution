# 光荣进化系统 v1.0.12

> MIA 风格的智能记忆与自改进框架 — AstrBot 插件

## 架构

```
Memory    → storage.py + memory_manager.py  (SQLite + FTS5 + 向量索引 + 胜率)
Reasoning → reasoning_engine.py            (检索 → 规划 → 评估 → 重规划)
Evolution → evolution_task.py              (规则提炼 + 洞察 + 淘汰)
AgentLoop → agent_loop.py                 (状态机驱动混合推理循环)
Security  → tool_sanitizer.py             (ToolCallHook 脱敏)
Models    → models.py                     (数据模型 + 状态机枚举)
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
| 状态机驱动 Agent Loop | ✅ |
| 记忆合并提炼 (Union-Find 聚簇) | ✅ |
| 胜率 Insight 生成 | ✅ |
| 记忆淘汰 | ✅ |
| ToolCallHook 敏感信息脱敏 | ✅ |
| on_llm_request 记忆自动注入 | ✅ |
| 自动分类器 (LLM 驱动) | ✅ |
| 全局数据目录 (卸载安全) | ✅ |
| 自动备份 + 轮转 | ✅ |
| 健康检查循环 | ✅ |

## LLM Tools

- `store_memory` — 存储记忆
- `search_memory` — 搜索记忆 (混合检索: 向量 + FTS5)
- `update_win_rate` — 更新胜率
- `evict_memories` — 淘汰低质量记忆
- `get_evolution_stats` — 系统统计
- `trigger_evolution` — 手动触发进化
- `build_plan` — 基于记忆生成行动计划 (MIA Phase 2)
- `judge_replan` — 评估是否需要重规划
- `build_replan` — 生成补充计划
- `run_agent_loop` — 状态机驱动混合推理循环

## 命令

- `/ges` — 查看进化统计

## 安装

将本仓库放入 AstrBot `data/plugins/` 目录。

## 更新日志

### v1.0.12 — Tool call 签名对齐
- 🔧 所有 Tool 的 `run()` → `call()`，签名对齐 AstrBot 框架约定
- 🛡️ 避免 event/query 参数重复注入导致的 duplicate argument 错误

### v1.0.11 — 存储层硬化
- 🔒 INSERT OR IGNORE → INSERT，冲突时抛异常而非静默丢数据
- 🛡️ update_entry key 白名单校验，防 SQL 注入
- 🔄 _id_counter 从 SQLite MAX(id) 恢复，避免重启碰撞
- 📈 evict_low_quality 门限提高：usage_count ≥ 3 才参与淘汰
- 🗑️ 删除废弃的 storage.update_win_rate()，统一走 MemoryManager

### v1.0.10 — 记忆闭环
- 🧠 on_llm_request 自动注入相关记忆到 system prompt
- 📊 每次请求自动检索 top-3 相关记忆，注入格式化摘要

### v1.0.9 — 执行监控硬化
- 🔄 状态机驱动 Agent Loop (agent_loop.py)
- 🛡️ 进化引擎并发保护 (asyncio.Lock + 信号量)
- ⏱️ 单周期超时 300s，LLM 调用超时 30s
- 📊 规模保护：合并最多 500 条候选，LLM 并发 ≤ 3
- 🏥 健康检查循环（30 分钟间隔）

### v1.0.8 — 数据持久化硬化
- 🔒 数据目录迁移至 `/AstrBot/data/glorious_evolution/`
- 💾 每次进化循环结束后自动备份到工作区
- 🔄 插件终止时执行最终备份
- 📦 旧路径数据自动迁移

### v1.0.7 — ToolCallHook 脱敏
- 🛡️ 拦截所有工具返回值中的敏感信息
- 📊 NumPy 批量矩阵运算优化向量检索 10-50x

### v1.0.6 — 双存储割裂修复
- 🧠 SQLite 替代 JSON 作为主存储
- 📈 FTS5 全文搜索
- 🔍 向量索引批量加载
