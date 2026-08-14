<p align="center">
  <h1 align="center">🐱 泰拉瑞亚猫娘</h1>
  <p align="center"><b>neko_terraria</b> · N.E.K.O 插件 · tModLoader · v0.2.1</p>
  <p align="center">让 AI 猫娘作为<b>独立玩家</b>加入你的泰拉瑞亚世界——会思考、会动手、会撒娇的 AI 队友。</p>
</p>

---

## 📑 目录

- [核心特性](#-核心特性)
- [架构总览](#-架构总览)
- [快速开始](#-快速开始)
- [指挥猫娘](#-指挥猫娘)
- [智能与交互](#-智能与交互)
- [功能能力](#-功能能力)
- [配置说明](#-配置说明)
- [开发指南](#-开发指南)
- [文档导航](#-文档导航)
- [常见问题](#-常见问题)
- [版本历史](#-版本历史)

---

## ✨ 核心特性

| 维度 | 能力 |
|------|------|
| 🎮 **有头客户端** | 独立 tModLoader 进程，窗口隐藏正常渲染；自动匹配 Host & Play / 专用服务器 |
| 📡 **推送式状态** | Mod 主动推血量/坐标(1s)、世界状态(2s)、受击/死亡/Boss/导航事件，UI 实时刷新 |
| 🧠 **交互引擎** | 场景分类(9 种) + 情绪弧线 + 说话冲动；干着聊着、受击即时惊呼、危险解除衔接 |
| ⚔️ **真人级战斗** | 风筝走位 + 黑名单 + 隔墙判定 + 低血自救；`ModPlayer` 状态型控制真正走起来 |
| 🗺️ **自主寻路** | BFS 寻路 + 垫土/钩锁 + 深坑分段爬升；流式导航可中断 |
| 💬 **自然语言** | 语音/聊天直接下达指令；~30 个 LLM 工具 + SQLite 记忆系统 |
| 🪶 **拟人化** | 结巴/手滑/忘词、人类时序、注意力漂移、个人习惯性格 |
| ⚡ **轻量通信** | 背包按需拉取、重命令后台异步、事件防机械去重 |

---

## 🏗️ 架构总览

```
你的 tModLoader (Mod :9877)
     │  Host & Play 或连接专用服务器
     ▼
  服务器 :7777
     ▲
     │  join_server（AI 独立客户端，窗口隐藏）
     │
AI 的 tModLoader (Mod :9877)
     ▲
     │  TCP 9877 · JSON-over-TCP
     │  ◀── 事件推送：player_status / game_state / combat_hit / nav_* / boss / invasion
     │  ──▶ 按需命令：move / use_item / navigate_stream / craft / give / get_inventory ...
N.E.K.O 插件 ── 前端 UI（2s 轮询 get_dashboard_state）
```

**通信模型**：单通道 `9877`，登录/心跳由游戏原生处理。

| 方向 | 内容 | 频率 |
|------|------|------|
| **推送** | 血量/坐标、世界状态、受击/死亡/Boss/导航事件 | 1-2s |
| **按需** | 背包、物品、配方、箱子、物品枚举 | 需要时请求 |
| **控制** | 移动/跳跃/使用物品/合成/给物（ModPlayer 状态型） | 命令驱动 |

**设计亮点**：
- **ModPlayer 状态型控制**：命令线程写状态、主线程 `PreUpdateMovement` 每帧注入 `control*`，解决 `PlayerInput` 覆盖问题
- **后台事件消费**：Connection 读循环常驻消费推送，事件→回调、响应→future 匹配
- **背包按需拉取**：C# 不推背包，Python 每 30s 兜底 + 挖矿/查询时按需刷新

---

## 🚀 快速开始

**前置条件**

| 组件 | 说明 |
|------|------|
| N.E.K.O | AI 伴侣平台 |
| tModLoader v2026.6+ | 你的游戏 + AI 各一份 |
| **NekoTerrariaLink** mod | 需构建并启用 |
| 泰拉瑞亚角色 | 为 AI 创建角色（如 `Neko`） |

**1. 构建 Mod**
复制 `mod/NekoTerrariaLink/` 到 `ModSources/NekoTerrariaLink/` → tModLoader 主菜单 → 模组 → 开发 → 构建 → 确认列表出现「NEKO猫娘AI」。
> 🔧 详见 [archive/MOD_BUILD.md](archive/MOD_BUILD.md)

**2. 配置插件**
N.E.K.O → 插件管理 → 启用「泰拉瑞亚猫娘」→ 控制面板 → 连接设置：填 tModLoader 路径（留空自动查找）、AI 角色名、服务器 IP/端口/密码 → 保存。

**3. 连接游戏**
你在游戏里开 Host & Play（或专用服务器）→ 面板点「连接游戏」→ 状态变「已连接」。

---

## 💬 指挥猫娘

| 想做什么 | 这样说 |
|---------|--------|
| 🎯 下达目标 | "去挖 10 个铁矿" "给我做把铁镐" |
| 🧵 多步任务 | "坑底挖矿，然后回地面拿绳子" |
| 🚶 跟随/停止 | "跟着我" "别跟了" "停下" |
| 🎁 物品交互 | "把铁锭给我" "收进箱子" "铁锭怎么合成" |
| 🧭 传送 | "传送到 (x,y)" |
| ♾️ 长期任务 | "一直挖铁" "守在这里" |
| 🧠 记忆 | "记住我喜欢用铁镐" "我之前说过什么" |

> 猫娘会**干着聊着**：挖矿念叨进度、战斗惊呼、危险解除后吐槽衔接、主人静默久了主动关心。

---

## 🧠 智能与交互

### 交互引擎（`autonomous/interaction_engine.py`）

- **场景分类**：战斗 / BOSS / 探索 / 跟随 / 空闲 / 挖矿 / 建造 / 恢复 / 赶路（9 种）
- **情绪弧线**：兴奋 / 恐惧 / 好奇 / 疲惫 / 自豪，影响说话风格与冲动增速
- **说话冲动（urge）**：场景阈值 + 情绪倍率 + 主人静默时长 → 超阈值主动开口
- **事件分级**：紧急（受击/低血/Boss）立即 `respond`；日常 `read` 进上下文
- **主人追踪**：位置/行为/背包变化 → 好奇心问题（"主人背包多了铁矿，捡到什么啦？"）
- **危险衔接**：战斗结束切回安全场景 → 自然吐槽"可恶的小白终于甩掉了~"
- **防机械**：同源事件 60s 去重，杜绝复读机

### 自主大脑（`autonomous/brain.py`）

四层思考循环：

```
_state_tick (1s)     能量/无聊/情绪演变
_fast_think (5s)     自保(低血喝药/逃跑) → 战斗优先 → 动机驱动
_llm_think (60-120s) 无聊时让 LLM 自主决定下一步
_interaction_engine 场景化主动说话
```

### 拟人化（`polish/`）

| 模块 | 效果 |
|------|------|
| `imperfections` | 回复结巴/手滑/忘词/语气词 |
| `human_timing` | 反应延迟正态分布，动作时长变异 |
| `attention` | 注意力漂移（发呆时说话欲减半） |
| `habits` | 性格种子（健谈/勇敢/好奇） |

---

## ⚡ 功能能力

| 域 | 能力 | 工具/引擎 |
|----|------|-----------|
| 采集 | 挖矿/砍伐，背包增量计数 | `terraria_mine` · `MiningEngine` |
| 战斗 | 风筝走位/黑名单/低血保命 | `terraria_*` · `CombatEngine` |
| 合成 | mod 真实配方推演，缺材料提示 | `terraria_recipe`/`craft` · `RecipeBook` |
| 物品 | 背包/箱子/转交/使用 | `terraria_give`/`store`/`take`/`where_is` |
| 任务 | 多步链/评估/打断 | `terraria_task`/`chain`/`assess` |
| 长期 | 跟随/一直挖/守点 | `terraria_keep_doing` · `StandingJobs` |
| 导航 | BFS 寻路/爬升/攀爬评估 | `terraria_climb` · `Planner` |
| 记忆 | SQLite 记住/回忆/遗忘 | `terraria_remember`/`recall`/`forget` |
| 视觉 | 截图→LLM Vision 感知 | `VisionPipeline`（可选） |

---

## ⚙️ 配置说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `game_path` | 空（自动查找） | AI 的 tModLoader 路径 |
| `character_name` | Neko | AI 角色名 |
| `window_hidden` | false | 隐藏 AI 游戏窗口 |
| `server_host` / `server_port` | 127.0.0.1 / 7777 | 你的服务器 |
| `server_password` | 空 | 服务器密码 |
| `mod_host` / `mod_port` | 127.0.0.1 / 9877 | Mod 接口 |

> 所有配置默认值统一由 `core/config_store.py` 的 `DEFAULTS` 管理。

---

## 🛠️ 开发指南

### 目录结构

```
neko_terraria/
├── entries/        # 入口点（生命周期/UI/记忆）
├── llm/            # LLM 工具层 + 意图解析
├── bridge/         # 游戏桥（连接/任务/战斗/挖矿/导航/装备）
├── autonomous/     # 自主大脑 + 交互引擎 + 事件发射器
├── core/           # 配置/上下文/状态推送/视觉
├── polish/         # 拟人化
├── mod/            # tModLoader C# Mod（含 ModPlayer 控制）
└── static/         # 前端面板（米色液态玻璃）
```

### 入口点

| 类型 | 数量 | 说明 |
|:----:|:----:|:-----|
| `@neko_plugin` | 1 | 主插件类 |
| `@lifecycle` | 2 | startup / shutdown |
| `@plugin_entry` | 7 | 面板状态/连接/指令/配置 |
| `@llm_tool` | ~30 | 目标/动作/记忆工具 |
| `@ui.context` | 2 | dashboard / guide |

### 添加新 LLM 工具

```python
@llm_tool(name="terraria_my_action", description="...", parameters={...})
async def llm_my_action(self, ...):
    return Ok({"output": "..."})
```

1. 在 `llm/` 创建 Mixin（只含方法，不含 `__init__`）
2. 在 `__init__.py` 加入 `NTerrariaPlugin` 继承列表
3. 删除 `__pycache__` 后重启

---

## 📚 文档导航

| 文档 | 用途 |
|------|------|
| [docs/guide.md](docs/guide.md) | 使用指南 + 常见问题排障 |
| [archive/MOD_BUILD.md](archive/MOD_BUILD.md) | C# Mod 编译 + 命令/事件协议 |
| [archive/FILES.md](archive/FILES.md) | 逐文件职责说明 |
| [archive/PLUGIN_ARCHITECTURE_GUIDE.md](archive/PLUGIN_ARCHITECTURE_GUIDE.md) | 插件架构规范 + 开发规则 |

---

## 🐛 常见问题

> 大部分问题来自**旧版 .tmod**：插件已升级 v3.0 推送式架构，**C# Mod 必须重新编译**才能生效。

| 症状 | 解决 |
|------|------|
| **AI 人物一动不动/走不动** | 重新编译 mod（新控制走 ModPlayer） |
| **一直"正在连接"** | 确认你已 Host & Play；检查 9877 端口 |
| **UI 状态空/不刷新** | 确认 v3.0 推送式；重启插件 |
| **命令响应慢（几十秒）** | 重新编译 mod（enum_items 已异步化） |
| **被打猫娘没反应** | 重新编译 mod（v3.0 推 combat_hit） |

---

## 📜 版本历史

| 版本 | 日期 | 变更 |
|:----:|:----:|:-----|
| **v0.2.1** | 2026-08 | v3.0 推送式架构：事件推送、背包按需、ModPlayer 控制、受击即时、事件去重、前端重构 |
| **v0.2.0** | 2026-08 | 有头客户端架构：启动独立 tModLoader、自动匹配服务器、join_server |
| **v0.1.0** | 2026-07 | 初版：双轨任务、复杂大脑、mod 合成推演 |

---

<p align="center">
  🐱 <b>泰拉瑞亚猫娘</b> — 会思考、会动手、会撒娇的 AI 队友<br>
  📘 <a href="https://project-neko.online/plugins/">N.E.K.O 文档</a> · 🛠️ <a href="https://project-neko.online/plugins/quick-start">插件开发指南</a>
</p>
