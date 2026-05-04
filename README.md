# 🧬 Glorious Evolution

> MIA 风格的智能记忆与自改进框架 — AstrBot 插件

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| 📚 记忆存储 | 向量索引 + FTS5 全文搜索 + 自动分类 |
| 🧠 智能检索 | 余弦相似度 + 胜率加权 + 分类增强 |
| 📋 行动规划 | Plan → Judge → Replan 循环 |
| 🧬 自主进化 | 情景合并 + 洞察生成 + 规则蒸馏 + 低质量淘汰 |
| 🔒 敏感脱敏 | API Key / Token / 密码自动掩码 |
| 📊 胜率反馈 | 记忆使用后评分，影响未来检索排序 |

## 🛠 LLM Tools (10个)

| Tool | 用途 |
|------|------|
| `store_memory` | 存储新记忆 |
| `search_memory` | 语义检索记忆 |
| `update_win_rate` | 反馈记忆有效性 |
| `evict_memories` | 淘汰低质量记忆 |
| `get_evolution_stats` | 查看系统统计 |
| `trigger_evolution` | 手动触发进化周期 |
| `build_plan` | 构建行动计划 |
| `judge_replan` | 评估是否需要重规划 |
| `build_replan` | 构建修订计划 |
| `run_agent_loop` | 运行 Agent 循环 |

## 💬 命令

- `/ges` — 查看系统状态

## 📦 安装

在 AstrBot 插件市场搜索 `glorious_evolution` 或手动安装：

```bash
# 插件目录
cd /AstrBot/data/plugins/
git clone https://github.com/Kess66666/astrbot_plugin_glorious_evolution.git
```

## ⚙️ 配置

在 AstrBot 管理面板 → 插件配置 中设置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `embedding_provider_id` | `Qwen/Qwen3-Embedding-8B` | Embedding 供应商 ID |
| `evolution_interval_hours` | `6` | 进化周期间隔（小时） |

### 蒸馏配置 (distillation)

```json
{
  "distillation": {
    "distillation_window_start": 2,
    "distillation_window_end": 6,
    "distillation_batch_size": 20,
    "distillation_batch_interval_sec": 1
  }
}
```

## 🔧 环境依赖

- **AstrBot** >= 3.5
- **ChromaDB** — 向量持久化
- **numpy** — 向量计算
- **Embedding Provider** — 需在 `cmd_config.json` 中配置 `openai_embedding` 类型供应商

## 🏗 架构

```
┌─────────────────────────────────────────┐
│             GloriousEvolutionPlugin      │
├──────────┬──────────┬───────────────────┤
│  Memory  │ Reasoning│    Evolution      │
│  Layer   │  Layer   │     Layer         │
├──────────┼──────────┼───────────────────┤
│storage.py│reasoning │evolution_task.py  │
│memory_   │engine.py │                   │
│manager.py│agent_    │tool_sanitizer.py  │
│          │loop.py   │                   │
└──────────┴──────────┴───────────────────┘
```

**三层 MIA**：Memory (记忆存取) → Reasoning (规划推理) → Evolution (自主进化)

## 🐛 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| 向量检索不可用 | Embedding Provider 未配置 | `cmd_config.json` 添加 `openai_embedding` 类型供应商 |
| 分类器不工作 | LLM Provider 未就绪 | 检查 Provider 配置，插件会自动重试 |
| 进化周期超时 | LLM 响应慢或记忆过多 | 减少记忆量或增大 CYCLE_TIMEOUT |
| 数据目录 | — | `/AstrBot/data/glorious_evolution/` |

## 📜 更新日志

- **v1.0.20** — win_rate 初始值 0.5 + PENDING 保护
- **v1.0.15** — 修复 judge_replan f-string 语法错误
- **v1.0.14** — 蒸馏规则系统 + 记忆注入优化
- **v1.0.13** — 记忆感知 judge + 逐条贡献评分
- **v1.0.12** — Agent Loop 反馈闭环
- **v1.0.11** — ID 碰撞修复 + SQL 注入防护
- **v1.0.6** — 迁移到 SQLite 存储

## 📄 License

MIT
