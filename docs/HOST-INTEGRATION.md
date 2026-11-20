# 宿主接入与权限清单

`neko_terraria` 启动并控制一个带界面的 tModLoader 猫娘队友，通过 Mod
协议读取世界、执行任务并由宿主猫娘陪聊。当前清单只声明面板
`state:read`、`action:call`。

> 红线结论：不写 N.E.K.O 本体记忆库、对话数据库或角色记忆卡。本插件产生的
> 猫娘可见台词和隐藏上下文只通过官方 `push_message` 注入。

## 高风险权限使用矩阵（发布前必读）

本表按当前代码事实描述，**不把“需要用户确认”写成“默认关闭”**。

| 高风险能力 | 使用插件 | 为什么必须 | 当前默认状态 | 用户确认与限制 | 回滚 |
|---|---|---|---|---|---|
| 宿主记忆/对话写入 | 无 | AI 世界记忆可独立存储 | 未使用 | 只写插件 `data_path()` 内 SQLite，不写宿主记忆库 | 无需回滚 |
| 猫娘回复/隐藏上下文 | `neko_arcade`、`neko_fishpower`、`neko_pawpilot`、`neko_pbz`、`neko_terraria` | 回复必须由宿主当前人设生成 | 按功能需要调用 | 只走官方 `push_message`；不写角色记忆卡 | 停用插件或关闭对应推送 |
| 代理 DLL / UE4SS | 仅 `neko_pbz` | PBZ 游戏侧状态和字幕需要 UE4SS 加载自有 Mod | `neko_terraria` 未使用 | 泰拉使用 tModLoader Mod 协议，不安装代理 DLL | 不适用 |
| 键鼠输入注入 | 仅 `neko_pawpilot` | 用户明确同意后接力驾驶 | `neko_terraria` 未使用 | 泰拉通过游戏侧 Mod 执行 AI 动作，不注入键鼠 | 不适用 |
| 启动游戏进程 | 仅 `neko_terraria` | 拉起独立的 AI tModLoader 客户端 | **手动开启**：插件启动不拉起，只有用户点击“连接游戏”才调用 `subprocess.Popen` | 只管理自己创建的进程；不终止外部客户端 | 点击停止/断开，或停用插件；不结束其他玩家进程 |
| 屏幕截图 | `neko_pbz`、`neko_fishpower`、`neko_pawpilot`、`neko_terraria` | 游戏视觉侦察或 HUD OCR | **视觉不是默认关闭**：`vision_analysis_enabled` 默认 `true`，启动后运行视觉管线 | 截取主人游戏窗口，优先主人画面，失败回退 AI 视角；不上传第三方 | 在配置中关闭 `vision_analysis_enabled`，或停用插件 |
| 私有照片/文件读写 | `neko_arcade`、`neko_pawpilot` 保存照片；六个插件均有各自的数据目录 | AI 记忆、Mod、照片和日志必须持久化 | 插件运行时按功能写入 | 截图默认不落盘；记忆写插件私有 SQLite，Mod 只写 tModLoader Mod 目录，不扫描私人相册 | 删除插件数据、记忆库或本插件 Mod；不影响游戏存档 |

## 声明=实际

## 宿主能力

| 能力 | 实际用途 | 当前声明 |
|---|---|---|
| `@lifecycle` | 启动/停止 AI 客户端、Mod 桥、任务循环和 SQLite | 隐式 |
| `@plugin_entry` / `@ui.action` | 启停、任务、目标、记忆、配置和状态 | `action:call` |
| `@llm_tool` | 让宿主猫娘控制跟随、挖矿、战斗、合成和任务 | 隐式 |
| `push_message` | 任务进度、战斗、死亡/胜利和陪聊注入 | 未单列 |
| `data_path()` + SQLite | AI 世界记忆、目标、装备与长期事实 | 未单列 |
| 独立/宿主 LLM | 意图解析和自然语言回应 | 未单列 |

## 游戏侧接入

- 启动 tModLoader 进程并连接游戏侧 Mod 协议；
- 通过 Mod API 执行移动、战斗、挖矿、合成与背包操作；
- 不直接写游戏存档，不 Hook 游戏内存，不使用代理 DLL；
- 只管理由本插件启动的客户端进程；
- Mod 文件只进入 tModLoader 的 Mod 目录。

## 截图与系统能力

- 使用 Pillow `ImageGrab` 截取主人游戏窗口，交给宿主视觉模型；
- 通过 Steam 注册表只读定位 tModLoader；
- 可启动/关闭本插件创建的 `subprocess.Popen`；
- 记忆 SQLite 位于插件数据目录，不写 N.E.K.O 本体记忆库、对话数据库或角色记忆卡。

## 红线

- 猫娘发言和隐藏状态只通过官方 `push_message`；
- 不直接写宿主记忆、对话或角色人格数据；
- 不把截图上传第三方，只交给用户配置的宿主视觉能力；
- 不修改游戏数值、存档或其他玩家客户端。

## 权限差距与回滚

云玩家权限模板应覆盖：消息注入、数据目录、SQLite、进程启动、游戏窗口截图、
Steam 注册表读取、游戏 Mod 协议和可选 HTTPS LLM。

回滚：

- 面板停止 AI 客户端；
- 停用插件会关闭由插件启动的进程和 SQLite；
- 删除 tModLoader Mod 目录中的本插件 Mod；
- 删除插件数据目录可清除 AI 记忆，不影响游戏存档。

## 完整权限清单

权限 ID 是本次审查标签，用于以后映射云玩家权限模板。

| 审计 ID | 权限 | 实际 API/资源 | 降级 |
|---|---|---|---|
| `HOST_LIFECYCLE` | 启停 | `@lifecycle startup/shutdown` | 关闭 AI 客户端和 SQLite |
| `HOST_ENTRY_UI` | 面板入口 | `@plugin_entry`、`@ui.action` | 面板关闭后已有进程可停止 |
| `HOST_LLM_TOOL` | 陪玩工具 | `@llm_tool` 目标/任务/跟随/挖矿等 | 只能用面板操作 |
| `HOST_PUSH_RESPOND` | 猫娘回复 | `push_message(ai_behavior="respond")` | 本地文本/静默 |
| `HOST_PUSH_READ` | 隐藏任务上下文 | `push_message(ai_behavior="read")` | 不注入上下文，任务仍可运行 |
| `HOST_DATA_PATH` | 插件数据 | `data_path()`、配置、日志、记忆 | 服务降级 |
| `HOST_PERSONA_READ` | 宿主角色人设 | `config/characters*.json` | 默认 AI 队友人设 |
| `REGISTRY_READ` | Steam/tModLoader 路径 | HKCU/HKLM Steam 注册表 | 要求手动填 `game_path` |
| `PROCESS_START` | 启动游戏客户端 | `subprocess.Popen(tModLoader)` | 不启动时只能连接已运行客户端 |
| `PROCESS_CONTROL` | 管理自己启动的进程 | 进程句柄与终止 | 不终止外部进程 |
| `FS_GAME_MOD` | 游戏 Mod 文件 | tModLoader Mod 目录、本插件 Mod | 缺 Mod 时无法连接世界 |
| `GAME_MOD_PROTOCOL` | AI 队友操作 | 游戏侧 Mod 协议/连接 | 连接失败时禁用自主行为 |
| `SCREEN_CAPTURE` | 主人游戏画面 | Pillow `ImageGrab` 游戏窗口 | 不截图时只使用 Mod 状态 |
| `HOST_VISION_MODEL` | 图像语义 | 截图交宿主视觉模型 | 视觉关闭时不影响任务状态机 |
| `SQLITE_PRIVATE` | AI 长期记忆 | 插件 `data_path()` 内 `neko_memory.db` | 损坏时可清空重建 |
| `NET_HTTPS_LLM` | 独立/辅助 LLM | `httpx`，用户配置 | 回退宿主或模板 |

不使用：宿主记忆库写入、宿主对话数据库写入、代理 DLL、游戏内存 Hook、键鼠注入。

红线审计：猫娘发言只通过 `push_message`；AI 记忆只写插件私有 SQLite。最高风险
权限是启动/控制游戏进程、写入 tModLoader Mod 目录、读取 Steam 注册表和截取游戏窗口。

