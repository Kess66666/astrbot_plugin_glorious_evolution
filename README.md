# 光荣进化系统 v1.0.31

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
| 混合评分 (0.60×cos + 0.25×wr + 0.15×rec) | ✅ |
| 胜率管理 (简单比值/衰减) | ✅ |
| Plan / Judge / Replan LLM 推理 | ✅ |
| 状态机驱动 Agent Loop | ✅ |
| 记忆合并提炼 (Union-Find 聚簇) | ✅ |
| 胜率 Insight 生成 | ✅ |
| 记忆淘汰 | ✅ |
| ToolCallHook 敏感信息脱敏 | ✅ |
| on_llm_request 记忆自动注入 | ✅ (v1.0.22 修复，v1.0.23 Prompt 工程重构) |
| 自动分类器 (LLM 驱动) | ✅ |
| 全局数据目录 (卸载安全) | ✅ |
| 自动备份 + 轮转 | ✅ |
| 健康检查循环 | ✅ |
| Schema Migration (user_version) | ✅ |

## 记忆注入机制

v1.0.22 修复了 Internal Agent 模式下记忆注入从未生效的问题，v1.0.28 引入能力感知路由。

### 注入方式
- **钩子**: `on_llm_request`（单钩子，废弃了 v1.0.18 的两段式 on_agent_begin 方案）
- **位置**: prepend 到 `req.system_prompt` 最前面（记忆优先于系统指令）
- **格式**: `[MEMORY INJECTION — YOU MUST READ AND USE]` 头部 + USAGE INSTRUCTIONS 尾部约束

### 注入规则
- **基础规则**（始终注入）: TRUST BOUNDARY / RELEVANCE BUDGET / CONFLICT RESOLUTION
- **TOOL GATE**（条件注入）: 仅当 `req.func_tool is not None`（agent 有工具能力）时注入 TOOL GATE + TOOL RESTRAINT
- 能力感知检测通过 `getattr(req, "func_tool", None) is not None` 实现

### Query 提取 (三级 fallback)
1. `req.prompt` — 直接用户输入
2. `event.message_str` — AstrBot 事件原始消息
3. `req.contexts` 最后一条 user message — Internal Agent 模式兜底

### 可见度梯度分类 (v1.0.29)
记忆注入不再使用二进制过滤，改用概率认知模型，按 win_rate 分为四个梯度：strong(>70%始终注入)、normal(20-70%始终注入)、weak(5-20%概率50%)、exploration(0-5%概率20%，v1.0.30后需has_tools才注入)
- 蒸馏规则只取 win_rate ≥ 70% 的高胜率条目
- **Exploration Gate** (v1.0.30): 无工具能力的 Agent（如飞书）不注入 exploration 区记忆，防止信号污染

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

- `/ges` — 查看进化统计 (含三维评分参数 + recency 半衰期)
- `/ger` — 调试检索: 三维得分 (Sim/Win/Rec) + [VEC]/[FTS] 来源标记

## 安装

将本仓库放入 AstrBot `data/plugins/` 目录。

## 更新日志

### v1.0.31 — 三维统一评分 (Unified Scoring v2)
- 📊 检索层统一评分公式: `0.60·cos + 0.25·wr + 0.15·exp(-λ·days_retrieved)`，λ=ln(2)/30
- 🧬 向量索引改为四元组 `(vec, win_rate, retrieved_ts, feedback_ts)`，检索命中时 touch `retrieved_ts`
- 🔀 FTS5 候选接入同一评分公式，位置代理 cosine，`FTS_CANDIDATE_BUDGET=5`
- 🔀 merge-before-rerank: 向量+FTS5 先合并去重，再统一排序
- 🔍 `/ger` 调试命令: 显示每条结果的 Sim/Win/Rec 三维得分 + [VEC]/[FTS] 来源标记
- 📈 `/ges` 扩展: 显示 recency权重、半衰期、FTS 保底配额

### v1.0.30 — Exploration Gate
- 🛡️ 无工具 Agent（如飞书）不再注入 [EXPLORATION — UNVERIFIED] 区记忆
- 🔒 一行代码：`if exploration_lines and has_tools`
- 📐 保证 toolless 模式下的信号纯度，防止低胜率记忆引发幻觉

### v1.0.29 — 可见度梯度分类
- 🎚️ 从「二进制过滤」升级为「概率认知系统」
- 📊 四级梯度: strong(>70%始终) / normal(20-70%始终) / weak(5-20%概率50%) / exploration(0-5%概率20%)
- 🔄 低 win_rate 记忆不再永久不可见，通过概率采样打通「错误记忆→验证→修正」闭环
- 📈 注入结构从扁平改为分层: [RELATED] + [LOW CONFIDENCE] + [EXPLORATION]

### v1.0.28 — 能力感知路由器
- 🧠 注入规则按 agent 能力分流：`has_tools = getattr(req, "func_tool", None) is not None`
- 🔧 有工具（Telegram）→ 注入 TOOL GATE + TOOL RESTRAINT，防过度工具调用
- 🪶 无工具（飞书）→ 跳过工具指令，避免向无能 agent 注入不可执行的规则
- 📋 3 条基础规则始终注入：TRUST BOUNDARY / RELEVANCE BUDGET / CONFLICT RESOLUTION

### v1.0.24 — Schema Migration 机制
- 🛡️ 新增 `_MIGRATIONS` + `_run_migrations()`，基于 `PRAGMA user_version` 追踪 schema 版本
- 📋 当前 schema 标记为 v1（baseline），未来 ALTER TABLE 只需追加一行
- 🔒 迁移幂等：同一连接/事务内完成，已执行版本不重复
- 🐛 修复 `/ges` 命令 emoji 显示为 `??` 的编码损坏
- 🛡️ 编码红线：所有源码必须无 BOM UTF-8，0xb9 GBK 残留会导致 SyntaxError

### 📋 待改清单

| # | 改进项 | 改动量 | 涉及文件 | 说明 |
|---|--------|--------|----------|------|
| - | ~~Unified Scoring v2~~ | ✅ | v1.0.31 | 三维评分 (0.60cos+0.25wr+0.15rec)，检索层统一排序 |
| 1 | importance 维度 | 小 | storage.py, models.py, main.py | `_MIGRATIONS` 加 v2 ALTER TABLE；`MemoryEntry` 加字段；`store_memory` 加参数 |
| 2 | 事件驱动进化 | 中 | main.py | `_evolution_loop` 从纯定时6h → 轮次阈值+空闲超时双触发，保留6h兜底 |

### v1.0.23 — Prompt 工程重构 + Emoji 修复
- 🧠 prepend 替代 append：记忆注入在 system_prompt 最前面
- 📋 MUST 指令头 `[MEMORY INJECTION — YOU MUST READ AND USE]`
- 📏 结构化英文标签 `[RELATED MEMORIES]` / `[DISTILLED RULES — HIGH CONFIDENCE]`
- 📜 USAGE INSTRUCTIONS 尾部 4 条约束（必须参考、个性化优先、约束即指令、不忽略）
- 🗑️ 移除废弃的 on_agent_begin 两段式钩子
- 🔍 恢复 win_rate < 0.2 过滤（v1.0.19 曾去掉）
- 🐛 修复 `/ges` 命令 emoji 显示为 `??` 的编码损坏
- 🛡️ 编码红线：所有源码必须无 BOM UTF-8，0xb9 GBK 残留会导致 SyntaxError

### v1.0.22 — 记忆注入修复
- 🐛 修复 Internal Agent 模式下 req.prompt 为空导致注入从未生效
- 🔗 三级 query fallback: req.prompt → event.message_str → req.contexts[user]
- 📊 source 标记加入日志便于诊断

### v1.0.21 — 健康检查 + 任务泄漏修复
- 🏥 health_check 移除自动创建空文件逻辑，改为告警
- 🔄 terminate() 遍历取消所有 6 个后台任务（之前只取消 3 个）
- 📋 每个 violation 单独打 logger.error

### v1.0.20 — (合入 v1.0.21)

### v1.0.19 — query 优化
- 🔍 on_llm_request 改用 event.message_str 做 query
- ⚠️ 去掉 win_rate < 0.2 过滤（v1.0.23 恢复）

### v1.0.18 — on_agent_begin 尝试
- 🧪 两段式方案：on_agent_begin 检索 + on_llm_request 注入
- ⚠️ 废弃：签名问题 + 复杂度高，v1.0.23 已移除

### v1.0.17 — 正则修复
- 🐛 tool_sanitizer.py rf-string 正则反斜杠解析歧义 → 变量拼接

### v1.0.16 — 蒸馏管线修复
- 🐛 set_distillation_config 从未被调用 → 在 __init__ 补回

### v1.0.15 — 语法修复
- 🐛 f-string 跨行语法错误

### v1.0.14 — 蒸馏规则表
- ✨ 新增 distilled_rules 表和 CRUD

### v1.0.13 — 记忆感知 Judge
- 🧠 LLM 逐条评价记忆贡献度，替代粗糙二值映射

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
