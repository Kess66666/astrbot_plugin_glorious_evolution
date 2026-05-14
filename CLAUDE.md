# 光荣进化系统 — CLAUDE.md

> 本文件面向 AI Agent（下次会话的自己），记录项目核心约定与红线。

## 项目身份

- **名称**: astrbot_plugin_glorious_evolution
- **版本**: v2.5.4
- **数据目录**: `/AstrBot/data/glorious_evolution/`（卸载安全）
- **插件目录**: `/AstrBot/data/plugins/astrbot_plugin_glorious_evolution/`

## 编码红线

- **所有源码必须无 BOM UTF-8 保存**。GBK 残留（如 `0xb9`）会导致 `SyntaxError: (unicode error) 'utf-8' codec can't decode byte 0xb9`
- 严禁从 Windows 剪贴板/GBK 编辑器直接粘贴代码到插件文件
- **SAFE FILE EDIT POLICY (v2.1.0 铁律)**：插件核心文件（main.py / evolution_task.py / memory_manager.py / reasoning_engine.py / storage.py）**永远禁止**使用 `dev_write_file` 全量覆写。必须使用 `astrbot_file_edit_tool` 精准替换。`dev_write_file` 仅限新建 scaffold 使用。违反此规则已导致一次 plugin 脑死亡事故（evolution_task.py 被截断，文件头全丢，IndentationError）。
- **编辑含转义字符的字符串时，禁止直接用文件编辑工具替换 `\n` 等转义符**（会变成物理换行符导致语法错误）。改用 `chr(10)` / `chr(9)` 等运行时构造，或整段重写
- **修复代码后务必清 `__pycache__/`**，否则 .pyc 缓存可能掩盖修复
- **修改后立即 `python3 -c "compile(...)"` 语法检查**，通过再 `dev_load_plugin`

## 常见反模式 + 排障

| 症状 | 根因 | 修复 |
|------|------|------|
| 多个插件同时报各种无关错误 | 排在最前面的插件有语法错误，导致加载链中断 | 修第一个插件的语法错误 + 清 cache |
| 插件日志无错误但 hook 不触发 | AstrBot LLM 管道未到达 `InternalAgentSubStage`（bot 无响应） | 先排查 bot 基础功能，再查 hook |
| 某 hook 触发但另一 hook 不触发（同 plugin） | `plugins_name` 在不同 event stage 不一致（请求阶段 vs 工具循环阶段） | 查 snapshot `event_diff` 字段，对比 `plugins_name_first` vs `plugins_name_last` |
| `_init_classifier()` 被 `asyncio.create_task` 包住 | 插件 initialize 返回太快，事件分发时 classifier 未就绪 | 改回 `await` 同步等待 |

## 架构速查

| 层 | 文件 | 职责 |
|----|------|------|
| 入口 | main.py | 插件注册 + on_llm_request 记忆注入 + _soft_feedback(0.7上限) 反馈闭环 + ChallengerModule 阴影挑战器 + `/ges` / `/ger` / `/gec` 命令 + DISABLE_AUTO_EVOLUTION 开关 |
| 存储 | storage.py | SQLite + FTS5 + Schema Migration |
| 管理 | memory_manager.py | CRUD + 向量检索 + 去重 + 胜率 + 三维统一评分(cos+wr+rec) |
| 推理 | reasoning_engine.py | 检索→规划→评估→重规划 |
| 进化 | evolution_task.py | 规则提炼 + 洞察 + 淘汰 |
| 循环 | agent_loop.py | 状态机驱动混合推理 |
| 安全 | tool_sanitizer.py | ToolCallHook 脱敏 |
| 模型 | models.py | 数据模型 + 状态机枚举 |
| 工具 | tools.py | LLM Tool 注册 |

## 关键约束

1. **记忆注入**: `on_llm_request` 单钩子，v2.1.0 起使用 `req.extra_user_content_parts.append(TextPart(...).mark_as_temp())`，不再 prepend 到 `req.system_prompt`。标记为 temp 的消息不持久化到 conversation history，不影响 provider 端 prompt cache 命中。
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
   - (3) 事件驱动进化 — 轮次阈值+空闲超时双触发替代纯定时6h ⚡ **2026-05-14 重新激活**: 深夜海外 API TLS 挂死导致蒸馏空转，轮数驱动天然规避——无对话不触发，发生时间天然在白天
   - (4) 蒸馏鲁棒性 ⚡ **2026-05-14**: API 不可用时空转无退避重试；失败后待蒸馏记忆无标记，下次不补蒸；产出为零时 silent fail（2026-05-14 白天蒸馏产出 5 insight/2 rules vs 夜晚疑似空转，差距明显）
   - (5) 认知对齐增强 ⚡ **2026-05-14**: 蒸馏停了 → insight 为零 → 评判池枯竭 → consolidated_rule 断流。~~存量记忆 96% pending（已判：实际 2%，v1.0.33 软反馈闭环已修复但静默）~~ → 改为实现 Stats 实时回流，防止"静默成功"导致认知漂移
8. **v1.0.29 设计定位**: 从「二进制过滤系统」升级为「概率认知系统」— 低 win_rate 记忆不再永久不可见，通过 exploration sampling (20%) 打通「错误记忆→验证→修正」闭环。副作用：toolless 模式下 exploration 记忆有信号污染风险（v1.0.30 已通过 exploration gate 修复）
9. **LivingMemory**: 已禁用，光荣进化是唯一记忆系统
10. **Emoji**: 源码中 emoji 必须使用真实 Unicode 字符，不能用 ASCII 占位符
11. **v1.0.33 load_vectors 在线补算**: `load_vectors()` 对 `embedding IS NULL` 的条目用 `_embed_func` 在线计算并持久化回 SQLite。解决老记忆 JSON→SQLite 迁移后 embedding 列为空导致 `_vectors` 为空的问题。
12. **v1.0.31 三维评分**: 检索层统一评分 `0.60·cos + 0.25·wr + 0.15·exp(-λ·days_retrieved)`, λ=ln(2)/30。`_vectors` 为四元组 `(vec, wr, retrieved_ts, feedback_ts)`，retrieved_ts 仅检索命中时 touch，feedback_ts 仅 update_win_rate 时 touch。FTS5 候选接入同一公式（位置代理 cosine），merge-before-rerank。FTS 保底配额 `FTS_CANDIDATE_BUDGET=5`。
13. **v1.0.33 T-004 软反馈闭环**: `_inject_relevant_memories` 出口调 `asyncio.create_task(self._soft_feedback(injected_ids))`。等 10 秒后 `update_win_rate(mid, True)`，零 Token 将 pending→correct。解决 97% pending 根因：feedback 闭环仅存在于 run_agent_loop（手动 Tool），普通对话路径从未触发。
14. **v1.0.32 Bayesian 胜率平滑 + v2.5.1 failure_count 集成**: 胜率公式 `(success_count + 1) / (success_count + failure_count + 2)`。消除"高频检索老记忆被分母碾死"问题。pending 天然 50%，无需硬编码。v2.5.1 migration v2 新增 `failure_count` 列（INTEGER DEFAULT 0），`update_win_rate()` 合并 success/failure 双通道更新。
15. **v1.1.0 冷启动搜索修复**: `_add_vector(entry_id, embedding)` 曾硬编码 `win_rate=0.0`，导致新记忆向量 win_rate 为 0，搜索排序全部被已有记忆碾压，冷启动后完全搜不到新记忆。根因：`_add_vector` 签名漏掉了 win_rate 参数。修复：(a) `MemoryEntry` win_rate 初值从 0.0 → 0.5；(b) `_add_vector` 改为 `_add_vector(entry_id, embedding, win_rate)`，传入 `entry.win_rate`。同时 `_vector_search` 的向量四元组改回用 `_vectors[idx][1]`（wr=1.0 权重偏置已移除）。
16. **v1.1.0 MemoryType 枚举补全**: `evolution_task.py` 的 `_consolidate()` 产生的 `consolidated_rule` 和 `_generate_insights()` 产生的 `insight` 在 `models.MemoryType` 枚举中缺失。`store_memory` 调用时触发 `ValueError`，导致进化周期 silent fail。修复：`models.py`/`tools.py`/`main.py` 三处补充 `CONSOLIDATED_RULE` 和 `INSIGHT`。
17. **v1.1.1 淘汰三线保护**: 飞书早上实测：进化周期在评判数据严重缺失（2/65）时盲目淘汰 16 条，吞掉头部优质记忆。修复：(a) 数据充足门 — judged<10 跳过淘汰；(b) CORRECT 锁定 — 任何被判正确的记忆永不淘汰；(c) 两阶段 — 先清 INCORRECT+低用量垃圾，再走赢率底线。EVICT_MIN_USAGE 3→5，EVICT_MAX_WIN_RATE 0.2→0.1。根因：v1.0.32 Bayesian 后 win_rate 最低 0.5，旧时代残党（pre-Bayesian 的 0/100=0.0）成了无差别屠杀的受害者。
18. **v1.2.0 四刀流防御体系** (2026-05-08):
    - 🛡️ **冷启动保护**: `run_evolution_cycle()` 入口双 gate — `total_success < 5` 跳过进化 + `judged_count < max(10, total*0.2)` 跳过淘汰。杜绝「还没学会打分就开删」。
    - 💉 **自动造血**: `retrieve_recent()` / `retrieve_top()` / `retrieve_all()` 出口自动 `_ensure_async(entry.increment_usage())`。检索即计数，不再依赖外部调用。
    - ⚠️ **0.7 软反馈上限** (Gemini 建议): `_soft_feedback()` 检查 `entry.win_rate >= 0.7` → 跳过。自动加分天花板 0.7，防止平庸记忆因频繁命中虚高霸榜。只有用户明确 `update_win_rate(mid, True)` 或 Task 成功闭环才能冲 0.9+。形成「平民(50%)→骨干(70%)→核心(90%+)」人才梯队。
    - 🔪 **可追凶淘汰日志**: `evict_low_quality()` 逐条输出结构化日志 `[EVICT] id=X type=Y cat=Z usage=N win_rate=W succ=S fail=F reason={USAGE|WIN_RATE}`。每条删除可追溯，死也要留全尸。
    - - ✅ **DISABLE_AUTO_EVOLUTION = False** (v2.5.1): 自动进化已恢复，冷启动 gate + 数据充足门保护充分。
19. **v2.0.0 无偏检索 + 分层注入** (2026-05-08): 检索阶段纯余弦评分 (cos=1.0, wr=0.0, rec=0.15)，消除 win_rate 偏见，公平海选 top-20。注入阶段分三桶 — exploit (wr高)、explore (wr中)、cold (wr低) — 每桶取 top-N + shuffle 保证曝光。T-006 `_soft_feedback` 升级为分层注入：exploit 直接提升，explore 给探索机会。70% 上限保留。
20. **分类器修复** (2026-05-08): `classify_memory()` prompt 补充 `insight`（系统诊断/胜率分布/病灶识别）和 `consolidated_rule`（固化规则/最佳实践）定义，加硬性规则禁止将系统分析归类为 declarative。存量 19 条 `Insight:` 记忆已通过 SQL `UPDATE` 从 declarative 迁入 insight。迁移后分布：insight avg_wr=59.1%，declarative avg_wr=30.8%。
21. **Soft Feedback 行为**: 注入记忆后 `asyncio.create_task(_soft_feedback(injected_ids))`，延迟 10s 后逐条检查 win_rate，<0.7 则 `update_win_rate(mid, True)` → Bayesian 平滑后涨一次分。
22. **v1.3 Rule Dedup (2026-05-10)**: `evolution_task.py` `_call_llm_and_store()` 新增存储前查重 — 对即将写入的 consolidated_rule 做向量检索，命中余弦相似度 > 0.82 的已有规则则合并源列表（`list(set(old_ids + new_ids))`），更新已有条目而非新建。杜绝同类规则膨胀（曾 11→7 条）。同时修正 `memory_type="declarative"` → `"consolidated_rule"`。
23. **v2.1.0 SAFE FILE EDIT POLICY (2026-05-10)**: `dev_write_file` 只写函数体导致 evolution_task.py 文件头全丢 → IndentationError → plugin 脑死亡。事后修复：文件从 git HEAD 恢复 + `astrbot_file_edit_tool` 精修。教训：核心文件永不用全量覆写工具，只做精准 diff 替换。修改后必须 syntax check → reload。
24. **v2.3.0 Fallback Exploration (2026-05-11)**: 查账发现 exploit 桶 90 条全活跃，explore 桶 6 条中 5 条 usage=0。根因：检索结果的马太效应——retrieve top-20 被 exploit 垄断，mid-wr 记忆永无曝光。修复：`_inject_relevant_memories` 增加 fallback — 当检索剩余结果中 mid_wr 为空时，直接从 DB 查全库 mid-wr 池，按 usage_count ASC 排序（新秀优先），强制注入 1 条 explore。日志标记 `fallback=True`。explore 配额从 2→1（精准扶贫）。
25. **v2.4.0 Walkman 随身听 + 安全截断 (2026-05-11)**:
    - 🎧 **Walkman**: `on_llm_tool_respond` hook 直接访问 `_ACTIVE_AGENT_RUNNERS`（AstrBot 私有 API）→ 绕过 `build_main_agent() return None` 时 hook 被跳过的问题。每次工具返回后向 `runner.run_context.messages[0].content`（system prompt）追加 1 条 mid-wr (0.2-0.7) explore 记忆，上限 3 轮。Per-session 互斥锁防竞态，round 计数防重复。解决 Tool-Loop Agent「只能看到 exploit 记忆」的问题。
    - ✂️ **安全截断**: `_safe_truncate()` 截断到最近句号/换行边界（不低于 max_len 的 50%），并用正则滤除 `ignore previous instructions` 等 prompt injection 模式。记忆内容先过安全截断再注入 system prompt。
26. **v2.5.0 Shadow Challenger Protocol (2026-05-13)**: 双阈值触发（熵<0.15 或连续 10 轮无挑战），从 Top-3 exploit 中随机选 victim，用 cold > explore 挑战者替换。差分验证——不伪造失败，让真实结果决定老兵是冗余还是关键。`/gec` 查看挑战日志。ENABLE_CHALLENGER = True（代码已就绪，WebUI 重载后激活）。
27. **v2.5.1 基础设施硬化 (2026-05-14)**:
    - 🔗 **SQLite 连接复用**: `Storage._get_conn()` 懒连接 + `SELECT 1` 健康检查 + 自动重连 + `wal_autocheckpoint=1000` 防 WAL 膨胀。17 处 `sqlite3.connect/close` → 1 个长连接，消除每次操作的连接开销。
    - 📊 **向量矩阵缓存**: `MemoryManager._get_cached_matrix()` 懒重建 + dirty flag。`_add_vector`/`update_win_rate`/`evict` 设脏标记，避免每次 `_vector_search` 重建 60MB 矩阵。
    - 🛡️ **`/getoggle` 权限检查**: `cmd_toggle_sanitize` 增加配置驱动 `admin_whitelist`（fallback `["7223158438"]`），未授权拦截 + 审计日志记录操作人和平台。
    - 🏥 **健康检查清扫加固**: `_health_check_loop` 末尾追加 `_walkman_cleanup_stale()`，每 30min 自动清理异常断开的 walkman 残留状态。
28. **v2.5.4 事件驱动进化 + 蒸馏鲁棒性 (2026-05-14)**:
    - 🔄 **事件驱动进化**: `_evolution_loop` 重写。双触发：50轮阈值 / 2h空闲超时（30s轮询）。`on_llm_request` 累加 `_evo_round_counter`。`_evo_lock` 防并发。根因：深夜 API TLS 挂死后纯定时器持续空转——无对话不触发，天然规避。
    - 🔬 **跨类型蒸馏**: `distill_rules()` 候选池从 declarative+consolidated_rule → 四类全包 (declarative + consolidated_rule + procedural + insight)。消除「分类即命运」——procedural/insight 的高 win_rate 记忆也能进蒸馏。
    - 🧠 **Prompt 抽象约束**: 蒸馏 Prompt 追加「当批次中同时存在方法论级记忆和操作级记忆时，以方法论为主体框架，操作级作为应用示例融入，严禁输出可被高层方法论统一的多条并列规则」。LLM 被强制优先选高层抽象。
    - 🧹 **存储前语义去重**: 新蒸馏规则存入前 cosine sim >= 0.85 检查已有 DR-* 规则，命中则合并 source_ids 和 avg_win_rate 而非新建。不同批次产出语义相近的规则自动合并，DR-* 数量不增反降时说明系统在「压缩」而非「复制」。
    - 🎯 **Provider 优先级**: deepseek 官方 > opencode-go > get_using_provider() > providers[0]。DeepSeek 更稳定，避免 opencode-go TLS 挂死。
    - ⏰ **蒸馏窗口放宽**: 2-6h → 2-8h。加 DEBUG 日志覆盖（入口/候选池/provider 类型），方便排查 silent fail。
    - 🚫 **DISABLE_AUTO_EVOLUTION = False**: 保护充分（冷启动 gate + 事件驱动天然规避深夜空转），恢复自动进化。
29. **v2.5.4 Forgejo 推送备忘 (2026-05-14)**:
    - 🔗 **Remote 路径修正**: `forgein` → `http://192.168.100.199:3000/kess66666/astrbot_plugin_glorious_evolution.git`（旧路径 `/forgein/` 已失效）
    - ⚠️ **认证方式**: HTTP Basic auth 不能直接嵌 URL (`http://user:pass@host` 不认)，必须走 git credential helper。GITEA_TOKEN 未注入容器需每次手动传入。详见 MEM-20260514-239。

## LLM Tools 清单

store_memory / search_memory / update_win_rate / evict_memories / get_evolution_stats / trigger_evolution / build_plan / judge_replan / build_replan / run_agent_loop
