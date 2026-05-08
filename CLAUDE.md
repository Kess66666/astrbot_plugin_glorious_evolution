# 光荣进化系统 — CLAUDE.md

> 本文件面向 AI Agent（下次会话的自己），记录项目核心约定与红线。

## 项目身份

- **名称**: astrbot_plugin_glorious_evolution
- **版本**: v2.0.0
- **数据目录**: `/AstrBot/data/glorious_evolution/`（卸载安全）
- **插件目录**: `/AstrBot/data/plugins/astrbot_plugin_glorious_evolution/`

## 编码红线

- **所有源码必须无 BOM UTF-8 保存**。GBK 残留（如 `0xb9`）会导致 `SyntaxError: (unicode error) 'utf-8' codec can't decode byte 0xb9`
- 严禁从 Windows 剪贴板/GBK 编辑器直接粘贴代码到插件文件
- 使用 `dev_write_file` 或 `astrbot_file_write_tool` 写入，确保 UTF-8 输出
- **编辑含转义字符的字符串时，禁止直接用文件编辑工具替换 `
` 等转义符**（会变成物理换行符导致语法错误）。改用 `chr(10)` / `chr(9)` 等运行时构造，或整段重写
- **修复代码后务必清 `__pycache__/`**，否则 .pyc 缓存可能掩盖修复

## 常见反模式 + 排障

| 症状 | 根因 | 修复 |
|------|------|------|
| 多个插件同时报各种无关错误 | 排在最前面的插件有语法错误，导致加载链中断 | 修第一个插件的语法错误 + 清 cache |
| 插件日志无错误但 hook 不触发 | AstrBot LLM 管道未到达 `InternalAgentSubStage`（bot 无响应） | 先排查 bot 基础功能，再查 hook |
| `_init_classifier()` 被 `asyncio.create_task` 包住 | 插件 initialize 返回太快，事件分发时 classifier 未就绪 | 改回 `await` 同步等待 |

## 架构速查

| 层 | 文件 | 职责 |
|----|------|------|
| 入口 | main.py | 插件注册 + on_llm_request 记忆注入 + _soft_feedback(0.7上限) 反馈闭环 + `/ges` / `/ger` 命令 + DISABLE_AUTO_EVOLUTION 开关 |
| 存储 | storage.py | SQLite + FTS5 + Schema Migration |
| 管理 | memory_manager.py | CRUD + 向量检索 + 去重 + 胜率 + 三维统一评分(cos+wr+rec) |
| 推理 | reasoning_engine.py | 检索→规划→评估→重规划 |
| 进化 | evolution_task.py | 规则提炼 + 洞察 + 淘汰 |
| 循环 | agent_loop.py | 状态机驱动混合推理 |
| 安全 | tool_sanitizer.py | ToolCallHook 脱敏 |
| 模型 | models.py | 数据模型 + 状态机枚举 |
| 工具 | tools.py | LLM Tool 注册 |

## 关键约束

1. **记忆注入**: `on_llm_request` 单钩子，prepend 到 `req.system_prompt` 最前面
2. **能力感知路由**: `has_tools = getattr(req, "func_tool", None) is not None`
   - 有工具 → 注入 TRUST BOUNDARY / RELEVANCE BUDGET / CONFLICT RESOLUTION + TOOL GATE + TOOL RESTRAINT
   - 无工具 → 仅注入 3 条基础规则，跳过工具指令
3. **Query fallback**: `req.prompt` → `event.message_str` → `req.contexts[user]`
4. **FTS5 查询**: 必须用 `_sanitize_fts5_query()` 双引号包裹，防止点号等非法字符；查询词 ≤ 2 字符跳过检索
5. **插件重载**: 修改代码后由用户手动 `/reload`，不要靠 touch 反复触发文件监控
6. **Schema Migration**: `storage.py` 的 `_MIGRATIONS` 字典管理版本迁移，加字段只需追加一行，用 `PRAGMA user_version` 追踪
7. **待改清单**:
   - (1) ~~Unified Scoring v2~~ ✅ v1.0.31 — 三维统一评分 (0.60cos + 0.25wr + 0.15rec)，4元组 (vec,wr,retrieved_ts,feedback_ts)，FTS统一评分，merge-before-rerank
   - (2) importance 维度 — migration v2 + MemoryEntry 字段 + store_memory 参数
   - (3) 事件驱动进化 — 轮次阈值+空闲超时双触发替代纯定时6h
8. **v1.0.29 设计定位**: 从「二进制过滤系统」升级为「概率认知系统」— 低 win_rate 记忆不再永久不可见，通过 exploration sampling (20%) 打通「错误记忆→验证→修正」闭环。副作用：toolless 模式下 exploration 记忆有信号污染风险（v1.0.30 已通过 exploration gate 修复）
9. **LivingMemory**: 已禁用，光荣进化是唯一记忆系统
10. **Emoji**: 源码中 emoji 必须使用真实 Unicode 字符，不能用 ASCII 占位符
11. **v1.0.33 load_vectors 在线补算**: `load_vectors()` 对 `embedding IS NULL` 的条目用 `_embed_func` 在线计算并持久化回 SQLite。解决老记忆 JSON→SQLite 迁移后 embedding 列为空导致 `_vectors` 为空的问题。
12. **v1.0.31 三维评分**: 检索层统一评分 `0.60·cos + 0.25·wr + 0.15·exp(-λ·days_retrieved)`, λ=ln(2)/30。`_vectors` 为四元组 `(vec, wr, retrieved_ts, feedback_ts)`，retrieved_ts 仅检索命中时 touch，feedback_ts 仅 update_win_rate 时 touch。FTS5 候选接入同一公式（位置代理 cosine），merge-before-rerank。FTS 保底配额 `FTS_CANDIDATE_BUDGET=5`。
13. **v1.0.33 T-004 软反馈闭环**: `_inject_relevant_memories` 出口调 `asyncio.create_task(self._soft_feedback(injected_ids))`。等 10 秒后 `update_win_rate(mid, True)`，零 Token 将 pending→correct。解决 97% pending 根因：feedback 闭环仅存在于 run_agent_loop（手动 Tool），普通对话路径从未触发。
14. **v1.0.32 Bayesian 胜率平滑**: `update_win_rate()` 不再递增 usage_count，胜率公式从 `success_count / usage_count` 改为 `(success_count + 1) / (success_count + 2)`。消除"高频检索老记忆被分母碾死"问题（如 succ=2, use=273 → 0.7% → 75%）。pending 天然 50%，无需硬编码。后续加入 failure_count 后分母改为 `(success+failure+2)`。
15. **v1.1.0 冷启动搜索修复**: `_add_vector(entry_id, embedding)` 曾硬编码 `win_rate=0.0`，导致新记忆向量 win_rate 为 0，搜索排序全部被已有记忆碾压，冷启动后完全搜不到新记忆。根因：`_add_vector` 签名漏掉了 win_rate 参数。修复：(a) `MemoryEntry` win_rate 初值从 0.0 → 0.5；(b) `_add_vector` 改为 `_add_vector(entry_id, embedding, win_rate)`，传入 `entry.win_rate`。同时 `_vector_search` 的向量四元组改回用 `_vectors[idx][1]`（wr=1.0 权重偏置已移除）。
16. **v1.1.0 MemoryType 枚举补全**: 
17. **v1.1.1 淘汰三线保护**: 飞书早上实测：进化周期在评判数据严重缺失（2/65）时盲目淘汰 16 条，吞掉头部优质记忆。修复：(a) 数据充足门 — judged<10 跳过淘汰；(b) CORRECT 锁定 — 任何被判正确的记忆永不淘汰；(c) 两阶段 — 先清 INCORRECT+低用量垃圾，再走赢率底线。EVICT_MIN_USAGE 3→5，EVICT_MAX_WIN_RATE 0.2→0.1。根因：v1.0.32 Bayesian 后 win_rate 最低 0.5，旧时代残党（pre-Bayesian 的 0/100=0.0）成了无差别屠杀的受害者。
18. **v1.2.0 四刀流防御体系** (2026-05-08):
    - 🛡️ **冷启动保护**: `run_evolution_cycle()` 入口双 gate — `total_success < 5` 跳过进化 + `judged_count < max(10, total*0.2)` 跳过淘汰。杜绝「还没学会打分就开删」。
    - 💉 **自动造血**: `retrieve_recent()` / `retrieve_top()` / `retrieve_all()` 出口自动 `_ensure_async(entry.increment_usage())`。检索即计数，不再依赖外部调用。
    - ⚠️ **0.7 软反馈上限** (Gemini 建议): `_soft_feedback()` 检查 `entry.win_rate >= 0.7` → 跳过。自动加分天花板 0.7，防止平庸记忆因频繁命中虚高霸榜。只有用户明确 `update_win_rate(mid, True)` 或 Task 成功闭环才能冲 0.9+。形成「平民(50%)→骨干(70%)→核心(90%+)」人才梯队。
    - 🔪 **可追凶淘汰日志**: `evict_low_quality()` 逐条输出结构化日志 `[EVICT] id=X type=Y cat=Z usage=N win_rate=W succ=S fail=F reason={USAGE|WIN_RATE}`。每条删除可追溯，死也要留全尸。
    - 🚫 **DISABLE_AUTO_EVOLUTION = True**: 关闭自动进化，仅手动 `trigger_evolution`。
19. **v2.0.0 无偏检索 + 分层注入** (2026-05-08): 检索阶段纯余弦评分 (cos=1.0, wr=0.0, rec=0.15)，消除 win_rate 偏见，公平海选 top-20。注入阶段分三桶 — exploit (wr高)、explore (wr中)、cold (wr低) — 每桶取 top-N + shuffle 保证曝光。T-006 `_soft_feedback` 升级为分层注入：exploit 直接提升，explore 给探索机会。70% 上限保留。
20. **分类器修复** (2026-05-08): `classify_memory()` prompt 补充 `insight`（系统诊断/胜率分布/病灶识别）和 `consolidated_rule`（固化规则/最佳实践）定义，加硬性规则禁止将系统分析归类为 declarative。存量 19 条 `Insight:` 记忆已通过 SQL `UPDATE` 从 declarative 迁入 insight。迁移后分布：insight avg_wr=59.1%，declarative avg_wr=30.8%。
19. **Soft Feedback 行为**: 注入记忆后 `asyncio.create_task(_soft_feedback(injected_ids))`，延迟 10s 后逐条检查 win_rate，<0.7 则 `update_win_rate(mid, True)` → Bayesian 平滑后涨一次分。`evolution_task.py` 的 `_consolidate()` 产生的 `consolidated_rule` 和 `_generate_insights()` 产生的 `insight` 在 `models.MemoryType` 枚举中缺失。`store_memory` 调用时触发 `ValueError: 'consolidated_rule' is not a valid MemoryType`，导致进化周期 silent fail。修复：`models.py`/`tools.py`/`main.py` 三处补充 `CONSOLIDATED_RULE` 和 `INSIGHT`。

## LLM Tools 清单

store_memory / search_memory / update_win_rate / evict_memories / get_evolution_stats / trigger_evolution / build_plan / judge_replan / build_replan / run_agent_loop
