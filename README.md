# 光荣进化系统 v2.0.0

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
| 混合评分 (两阶段：检索纯余弦 + 注入分桶) | ✅ |
| 胜率管理 (简单比值/衰减) | ✅ |
| Plan / Judge / Replan LLM 推理 | ✅ |
| 状态机驱动 Agent Loop | ✅ |
| 记忆合并提炼 (Union-Find 聚簇) | ✅ |
| 胜率 Insight 生成 | ✅ |
| 记忆淘汰 | ✅ |
| ToolCallHook 敏感信息脱敏 | ✅ |
| on_llm_request 记忆自动注入 | ✅ |
| T-004 软反馈闭环 (注入→自动标记成功) | ✅ |
| 0.7 软反馈上限 (防认知茧房) | ✅ |
| 冷启动保护 (双 Gate) | ✅ |
| 自动造血 (检索→usage++) | ✅ |
| 可追凶淘汰日志 (EVICT 结构化) | ✅ |
| DISABLE_AUTO_EVOLUTION 开关 | ✅ |
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

### v2.0.0 — 无偏检索 + 分层注入 + 分类器修复 (2026-05-08)
- 🎯 **无偏检索**: 检索阶段 cos=1.0, wr=0.0, rec=0.15 — 纯余弦海选 top-20，杜绝 win_rate 挤占
- 🪣 **分层注入**: 检索后按 wr 分 exploit/explore/cold 三桶，每桶取 top-N + shuffle
- 🏷️ **分类器修复**: insight/consolidated_rule 定义补齐 + 硬性规则，避免系统诊断误入 declarative
- 📊 **存量重分类**: 19 条 Insight: 记忆从 declarative → insight，migration 后 insight avg_wr=59.1%
- 🔄 **T-006 软反馈 v2.0**: 分层注入替代平铺注入，exploit 直接提升 + explore 给机会

### v1.2.0 — 四刀流防御体系 (2026-05-08)
- 🛡️ 冷启动保护：进化入口双 gate（total_success<5 跳过，judged<20% 跳过淘汰）
- 💉 自动造血：所有检索路径出口自动 usage++，不依赖外部调用
- ⚠️ 0.7 软反馈上限（Gemini 建议）：自动加分天花板 0.7，防认知茧房
- 🔪 可追凶淘汰日志：每条 EVICT 结构化输出 (id/type/win_rate/reason)
- 🚫 DISABLE_AUTO_EVOLUTION 开关：关闭自动进化，仅手动触发

### v1.1.1 — 淘汰三线保护（飞书血案修复）
- 🔒 数据充足门：已评判记忆 < 10 时跳过淘汰，杜绝数据饥荒下的盲目清洗
- 🛡️ CORRECT 锁定：被判正确的记忆永不淘汰
- 🗑️ 两阶段淘汰：先清垃圾（INCORRECT+低用量），再走保守赢率底线
- 📐 参数收紧：EVICT_MIN_USAGE 3→5，EVICT_MAX_WIN_RATE 0.2→0.1

### v1.1.0 — 冷启动 + 进化引擎双重修复
- 🧊 冷启动搜索修复：`_add_vector` 不再硬编码 win_rate=0.0，新记忆初始 win_rate=0.5 正常参与排序
- 🧬 MemoryType 枚举补全：`consolidated_rule` / `insight` 纳入枚举，进化周期不再 silent fail
- 🧹 清理僵尸备份文件

### v1.0.33 — T-004 软反馈闭环
- 🔄 在 `_inject_relevant_memories` 出口添加 `_soft_feedback`，每次注入后异步标记记忆为成功
- ⚡ 零 Token 成本、零 LLM 参与，pending → correct 转化率 100%
- 🎯 解决 97% pending 的反馈缺失根因

### v1.0.32 — Bayesian 胜率平滑
- 📐 win_rate 公式从 `success_count / usage_count` 改为 `(success_count + 1) / (success_count + 2)`
- 🩹 消除"高频检索老记忆被分母碾死"问题（总体 30% → 59.1%）
- 🔧 移除 update_win_rate 中多余的 usage_count 递增（改由 increment_usage 单独管理）

### v1.0.31 — 三维统一评分
- 📐 检索评分公式：0.60·cos + 0.25·wr + 0.15·rec，合并重排序
- 🗂️ FTS 统一评分 + merge-before-rerank

### v1.0.30 — Exploration Gate
- 🚪 无工具 agent 模式下不注入未验证记忆，避免信号污染

### v1.0.29 — 可见度梯度
- 🌈 从「二进制过滤」升级到「概率认知」— 低 win_rate 记忆通过 sampling 获得复现机会

### v1.0.28 — 能力感知路由
- 🧰 `has_tools` 检测 → 有工具注入 TOOL GATE + TOOL RESTRAINT，无工具跳过

### v1.0.22 — Query Fallback 链
- 🔗 三级 fallback：req.prompt → event.message_str → req.contexts[user]
- 🐛 修复 Internal Agent 模式下 req.prompt="" 导致记忆注入从未触发的根因

### v1.0.13–v1.0.21 — 存储硬化 + 健康检查 + 备份
- 🔒 全局数据目录 `/AstrBot/data/glorious_evolution/`
- 🏥 健康检查循环（30min 间隔，缺失文件告警）
- 💾 自动备份 + 轮转清理
- 🛡️ terminate 钩子取消所有后台任务

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
