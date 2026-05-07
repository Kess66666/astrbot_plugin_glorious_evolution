# 光荣进化系统 — CLAUDE.md

> 本文件面向 AI Agent（下次会话的自己），记录项目核心约定与红线。

## 项目身份

- **名称**: astrbot_plugin_glorious_evolution
- **版本**: v1.0.31
- **数据目录**: `/AstrBot/data/glorious_evolution/`（卸载安全）
- **插件目录**: `/AstrBot/data/plugins/astrbot_plugin_glorious_evolution/`

## 编码红线

- **所有源码必须无 BOM UTF-8 保存**。GBK 残留（如 `0xb9`）会导致 `SyntaxError: (unicode error) 'utf-8' codec can't decode byte 0xb9`
- 严禁从 Windows 剪贴板/GBK 编辑器直接粘贴代码到插件文件
- 使用 `dev_write_file` 或 `astrbot_file_write_tool` 写入，确保 UTF-8 输出

## 架构速查

| 层 | 文件 | 职责 |
|----|------|------|
| 入口 | main.py | 插件注册 + on_llm_request 记忆注入 + `/ges` / `/ger` 命令 |
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
11. **v1.0.31 三维评分**: 检索层统一评分 `0.60·cos + 0.25·wr + 0.15·exp(-λ·days_retrieved)`, λ=ln(2)/30。`_vectors` 为四元组 `(vec, wr, retrieved_ts, feedback_ts)`，retrieved_ts 仅检索命中时 touch，feedback_ts 仅 update_win_rate 时 touch。FTS5 候选接入同一公式（位置代理 cosine），merge-before-rerank。FTS 保底配额 `FTS_CANDIDATE_BUDGET=5`。

## LLM Tools 清单

store_memory / search_memory / update_win_rate / evict_memories / get_evolution_stats / trigger_evolution / build_plan / judge_replan / build_replan / run_agent_loop
