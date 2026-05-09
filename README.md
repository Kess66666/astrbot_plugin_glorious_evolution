# 光荣进化系统 v2.2.0-dev

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
| 去重 (余弦 ≥ 0.90) | ✅ |
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

### v2.2.0-dev — embedding_version + 语义统一 + IntentGate (2026-05-09)
- 🧬 **embedding_version v2_qc**: MemoryEntry + SQLite + 内存向量索引三端贯穿，版本漂移自动重嵌
- 🔗 **语义统一 Q+C 协议**: 每条记忆 question + newline + content 拼接后向量化，不再用 pipe 拼接
- 🎯 **DEDUP_THRESHOLD 0.85→0.90**: 收紧去重阈值，减少误合并
- 🚪 **IntentGate v2.2.0**: 无意义短句（≤8 字符无实体/技术词）跳过检索，省 token
- 📋 **版本日志锚点**: load_vectors() 启动时打印 Global Consciousness Online 带版本/策略/阈值
- 🤝 **交叉评审工作流**: ChatGPT + Gemini 双模型评审，分歧仲裁后执行
- 🤖 **三小弟流水线**: a1 写代码 → a2 审逻辑 → a3 规范把关，按改动规模分级调用

### v2.0.0 — 无偏检索 + 分层注入 + 分类器修复 (2026-05-08)
- 🎯 **无偏检索**: 检索阶段 cos=1.0, wr=0.0, rec=0.15 — 纯余弦海选 top-20，杜绝 win_rate 挤占
- 🪣 **分层注入**: 检索后按 wr 分 exploit/explore/cold 三桶，每桶取 top-N + shuffle
- 🏷️ **分类器修复**: insight/consolidated_rule 定义补齐 + 硬性规则，避免系统诊断误入 declarative
- 📊 **存量重分类**: 19 条 Insight 记忆从 declarative → insight，migration 后 insight avg_wr=59.1%
- 🔄 **T-006 软反馈 v2.0**: 分层注入替代平铺注入，exploit 直接提升 + explore 给机会

### v1.2.0 — 四刀流防御体系 (2026-05-08)
- 🛡️ 冷启动保护：进化入口双 gate（total_success<5 跳过，judged<20% 跳过淘汰）
- 💉 自动造血：所有检索路径出口自动 usage++，不依赖外部调用
- ⚠️ 0.7 软反馈上限（Gemini 建议）：自动加分天花板 0.7，防认知茧房
- 🔪 可追凶淘汰日志：每条 EVICT 结构化输出
- 🚫 DISABLE_AUTO_EVOLUTION 开关：关闭自动进化，仅手动触发

### v1.1.1 — 淘汰三线保护
- 🔒 数据充足门：已评判记忆 < 10 时跳过淘汰
- 🛡️ CORRECT 锁定：被判正确的记忆永不淘汰
- 🗑️ 两阶段淘汰：先清垃圾再走赢率底线
- 📐 参数收紧：EVICT_MIN_USAGE 3→5，EVICT_MAX_WIN_RATE 0.2→0.1

### v1.1.0 — 冷启动 + 进化引擎双重修复
- 🧊 key_fix: _add_vector 不再硬编码 win_rate=0.0
- 🧬 MemoryType 枚举补全：consolidated_rule / insight

### v1.0.33 — T-004 软反馈闭环
- 🔄 注入后异步标记成功，零 Token，pending→correct 转化率 100%

### v1.0.32 — Bayesian 胜率平滑
- 📐 (success+1)/(success+2) 替代 success/usage

### v1.0.31 — 三维统一评分
- 📐 0.60cos + 0.25wr + 0.15rec，merge-before-rerank

### v1.0.30 — Exploration Gate
- 🚪 无工具 agent 不注入未验证记忆

### v1.0.29 — 可见度梯度
- 🌈 二进制过滤 → 概率认知，低 wr 记忆 sampling 复现

### v1.0.28 — 能力感知路由
- 🧰 has_tools 检测 → 按能力注入不同规则

### v1.0.22 — Query Fallback 链
- 🔗 三级 fallback：req.prompt → event.message_str → req.contexts[user]

### v1.0.13–v1.0.21 — 存储硬化 + 健康检查 + 备份
- 🔒 全局数据目录 / 健康检查循环 / 自动备份轮转

### v1.0.12–v1.0.6 — 早期硬化
- Tool call 签名对齐 / 存储层硬化 / 记忆闭环 / 执行监控 / 数据持久化 / ToolCallHook 脱敏 / 双存储割裂修复