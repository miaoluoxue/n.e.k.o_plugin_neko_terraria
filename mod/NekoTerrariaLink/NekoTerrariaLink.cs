using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Terraria;
using Terraria.IO;
using Terraria.ModLoader;
using Terraria.ModLoader.Core;
using Terraria.ID;
using Terraria.DataStructures;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;

namespace NekoTerrariaLink
{
        public class NekoTerrariaLink : Mod
    {

        // 事件监控实例（ModSystem 每帧调用）
        internal static NekoTerrariaLink Instance { get; private set; }

        // ModConfig 实例（游戏内模组设置）。tModLoader 会自动加载。
        public NekoConfig Config => ModContent.GetInstance<NekoConfig>();

        private TcpListener _listener;
        private Thread _listenThread;
        private readonly object _lock = new object();
        private readonly Queue<(string, NetworkStream)> _cmdQueue = new();
        private readonly object _cmdLock = new object();

        // 当前活跃的 Python 客户端流（事件推送目标）；ListenLoop accept/断开时更新
        private volatile NetworkStream _activeStream = null;

        // ── 事件监控状态（PostUpdate 每帧 edge 检测） ──
        private bool _prevAlive = false;        // 上帧玩家存活
        private bool _prevBossActive = false;   // 上帧有 Boss
        private string _prevBossName = "";      // 上帧 Boss 名
        private int _prevInvasionType = 0;      // 上帧入侵类型
        private int _prevHp = 0;                // 上帧血量
        private int _dmgAccum = 0;              // 战斗受伤累计
        private long _lastCombatPushMs = 0;     // 上次 combat_hit 推送时间
        private long _lastStatusPushMs = 0;     // 上次 player_status 推送时间
        private long _lastGameStatePushMs = 0;  // 上次 game_state 推送时间

        // 自动选角色 —— 替代 tML 2026.06+ 损坏的 -skipselect
        private bool _autoSelectDone = false;
        private int _autoSelectFrameWait = 0;

        // 加入服务器状态追踪 —— 协议状态机
        private bool _joinPending = false;
        private bool _joinStartRequested = false; // 主线程尚未执行 StartTcpClient
        private int _joinTimeout = 0;
        private string _joinHost = "";
        private int _joinPort = 0;
        private string _joinCharName = "";   // join_server 的目标角色名（Python 配置）
        private int _diagCmdCount = 0;
        private DateTime _joinStart; // 开始连接的时刻，用于判定超时

        // 统一的实体来源，用于 QuickSpawnItem / DropItem（1.4 要求 IEntitySource）
        private sealed class NekoEntitySource : IEntitySource
        {
            public string Context => "NekoTerrariaLink";
        }
        private static readonly IEntitySource Src = new NekoEntitySource();

        public override void Load()
        {
            Instance = this;
            // 诊断：确认程序集内类型（排查 ModSystem/ModPlayer 未注册）
            try
            {
                var names = typeof(NekoTerrariaLink).Assembly.GetTypes()
                    .Where(t => t.Namespace == "NekoTerrariaLink")
                    .Select(t => t.Name).ToList();
                Logger.Info($"程序集类型: {string.Join(", ", names)}");
            }
            catch { }
            // 读取模组设置中的监听端口（默认 9877），须与插件端 mod_port 一致
            int port = 9877;
            try
            {
                var cfg = ModContent.GetInstance<NekoConfig>();
                // 兜底：配置文件不存在时 C# int 默认值为 0，必须回退到硬编码默认端口
                if (cfg != null && cfg.ModPort > 0) port = cfg.ModPort;
            }
            catch { /* Config 可能尚未就绪，使用默认端口 */ }

            // 尝试启动监听器，如果端口被占用则尝试其他端口
            bool started = false;
            int finalPort = port;

            for (int tryPort = port; tryPort < port + 10 && !started; tryPort++)
            {
                try
                {
                    _listener = new TcpListener(IPAddress.Loopback, tryPort);
                    _listener.Start();
                    _listenThread = new Thread(ListenLoop) { IsBackground = true };
                    _listenThread.Start();
                    finalPort = tryPort;
                    started = true;

                    if (tryPort == port)
                    {
                        Logger.Info($"NekoTerrariaLink: 监听端口 {tryPort} 启动成功");
                    }
                    else
                    {
                        Logger.Warn($"NekoTerrariaLink: 默认端口 {port} 被占用，使用备用端口 {tryPort}");
                    }
                }
                catch (SocketException ex) when (ex.SocketErrorCode == SocketError.AddressAlreadyInUse)
                {
                    // 端口被占用，尝试下一个
                    if (tryPort == port + 9)
                    {
                        // 最后一次尝试也失败了
                        Logger.Error($"NekoTerrariaLink: 端口 {port}-{port+9} 全部被占用");
                        Logger.Error("解决方案:");
                        Logger.Error("  1. 使用命令: netstat -ano | findstr \"987\"");
                        Logger.Error("  2. 使用命令: taskkill /F /PID <进程ID>");
                        Logger.Error("  3. 或者重启计算机清理所有残留进程");
                    }
                    continue;
                }
                catch (Exception ex)
                {
                    Logger.Error($"NekoTerrariaLink: 端口 {tryPort} 启动失败 - {ex.Message}");
                    break;
                }
            }

            // ===== 接管角色自动选择（替代 tML 2026.06+ 损坏的 -skipselect） =====
            var drawMenuMethod = typeof(Main).GetMethod("DrawMenu",
                System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
            if (drawMenuMethod != null)
                MonoModHooks.Add(drawMenuMethod, Main_DrawMenu);
            Logger.Info($"NekoTerrariaLink: 自动选角色就绪（AutoSelectCharacter={Config.AutoSelectCharacter}）");

            // ===== 绕过窗口失焦：FNA 认为窗口永远激活 → 最小化/隐藏时 Update 也全速跑 =====
            try
            {
                var isActiveGetter = typeof(Game).GetProperty("IsActive")?.GetGetMethod();
                if (isActiveGetter != null)
                {
                    MonoModHooks.Add(isActiveGetter, new Func<Func<bool>, Game, bool>(
                        (orig, self) => true));
                    Logger.Info("NekoTerrariaLink: Game.IsActive 已钩住（窗口状态无关，全速运行）");
                }
            }
            catch (Exception ex)
            {
                Logger.Warn($"NekoTerrariaLink: 钩 IsActive 失败: {ex.Message}");
            }
        }

        public override void Unload()
        {
            try { _listener?.Stop(); } catch { }
            _activeStream = null;
            Instance = null;
        }

        // ══════════════════════════════════════════════════════════
        // 事件推送（每帧主线程检测，主动推给 Python 桥）
        // 事件格式：{"type":"event","event":"xxx","message":"..."}
        // 对应插件端 agent._handle_mod_event / connection._dispatch_event
        // ══════════════════════════════════════════════════════════

        internal void EventMonitorTick()
        {
            ProcessCommandQueue();   // 命令主线程执行（架构）
            // 无客户端连接时不检测（省帧开销）
            if (_activeStream == null) return;
            var p = Main.LocalPlayer;
            if (p == null || !p.active) return;

            bool alive = p.statLife > 0;

            // ── 死亡 / 复活 ──
            if (_prevAlive && !alive)
                PushEvent("player_died", $"玩家死亡（{p.name}）");
            else if (!_prevAlive && alive)
                PushEvent("player_respawned", $"玩家复活了");
            _prevAlive = alive;

            // ── 战斗受伤（节流：累计≥20 或 ≥3s 推一次） ──
            if (_prevHp > p.statLife && p.statLife > 0)
                _dmgAccum += _prevHp - p.statLife;
            _prevHp = p.statLife;
            if (_dmgAccum > 0)
            {
                long nowMs = Environment.TickCount64;
                if (_dmgAccum >= 20 || nowMs - _lastCombatPushMs > 3000)
                {
                    PushEvent("combat_hit", $"受到{_dmgAccum}点伤害");
                    _dmgAccum = 0;
                    _lastCombatPushMs = nowMs;
                }
            }

            // ── Boss 出现 / 击杀 ──
            string boss = FindActiveBoss();
            if (boss != null && !_prevBossActive)
                PushEvent("boss_spawned", $"Boss出现：{boss}", boss);
            else if (boss == null && _prevBossActive)
                PushEvent("boss_killed", $"击败了{_prevBossName}", _prevBossName);
            if (boss != null) _prevBossName = boss;
            _prevBossActive = boss != null;

            // ── 入侵开始 / 结束 ──
            if (Main.invasionType != 0 && _prevInvasionType == 0)
                PushEvent("invasion_start", "入侵开始了！");
            else if (Main.invasionType == 0 && _prevInvasionType != 0)
                PushEvent("invasion_end", "入侵结束了");
            _prevInvasionType = Main.invasionType;

            // ── 周期状态汇报（血量/位置，1s 一次）──
            // mod 主动推 player_status，Python 订阅更新缓存，
            // 不依赖 get_state 轮询的响应解析（避免解析问题导致 hp=0）
            if (Environment.TickCount64 - _lastStatusPushMs > 1000)
            {
                _lastStatusPushMs = Environment.TickCount64;
                try
                {
                    Send(_activeStream, new Dict {
                        ["type"] = "event", ["event"] = "player_status",
                        ["hp"] = p.statLife, ["max_hp"] = p.statLifeMax,
                        ["x"] = (int)(p.Center.X / 16),
                        ["y"] = (int)(p.Center.Y / 16),
                        ["alive"] = alive,
                    });
                }
                catch { }
            }

            // ── 轻量游戏状态推送（敌人/玩家/时间，2s 一次）──
            // 背包数据已移除：Python 侧按需发 get_inventory 拉取（挖矿/查询/给物品），
            // 不再每 2s 持续推送大体积背包 JSON，大幅降低 TCP 压力
            if (Environment.TickCount64 - _lastGameStatePushMs > 2000)
            {
                _lastGameStatePushMs = Environment.TickCount64;
                PushGameState();
            }
        }

        /// <summary>推送轻量游戏状态（玩家/附近敌人/附近玩家/时间）。背包不在此推送。</summary>
        private void PushGameState()
        {
            var s = _activeStream;
            var p = Main.LocalPlayer;
            if (s == null || p == null) return;
            try
            {
                // 附近敌人（50 格内）
                var npcs = new List<Dict>();
                for (int i = 0; i < Main.npc.Length; i++)
                {
                    var npc = Main.npc[i];
                    if (npc.active && npc.life > 0)
                    {
                        int dx = (int)(npc.Center.X / 16 - p.Center.X / 16);
                        int dy = (int)(npc.Center.Y / 16 - p.Center.Y / 16);
                        if (Math.Abs(dx) < 50 && Math.Abs(dy) < 50)
                            npcs.Add(new Dict { ["name"] = npc.TypeName, ["slot"] = i,
                                ["life"] = npc.life,
                                ["tileX"] = (int)(npc.Center.X / 16),
                                ["tileY"] = (int)(npc.Center.Y / 16),
                                ["damage"] = npc.damage });
                    }
                }
                // 附近玩家（300 格内，非自身，坐标有效）
                // 范围必须大于 Python 侧 follow_trigger_dist(60)：
                // 否则主人走出 80 格后 nearby_players 变空，跟随直接"跟丢"（猫娘永远追不上）
                // 已下线/未同步的槽位 active=true 但 Center 为 (0,0)，必须过滤，
                // 否则 Python 侧跟随/战斗会拿到地图原点的假坐标（距离 3000+ 格）
                var players = new List<Dict>();
                for (int i = 0; i < Main.player.Length; i++)
                {
                    var pl = Main.player[i];
                    if (pl == null || !pl.active || pl == p) continue;
                    int plTx = (int)(pl.Center.X / 16);
                    int plTy = (int)(pl.Center.Y / 16);
                    if (plTx == 0 && plTy == 0) continue;  // 残留槽位无真实位置
                    int dx = plTx - (int)(p.Center.X / 16);
                    int dy = plTy - (int)(p.Center.Y / 16);
                    if (Math.Abs(dx) < 300 && Math.Abs(dy) < 300)
                        players.Add(new Dict { ["name"] = pl.name,
                            ["tile_x"] = plTx, ["tile_y"] = plTy });
                }
                Send(s, new Dict {
                    ["type"] = "event", ["event"] = "game_state",
                    ["player"] = new Dict {
                        ["name"] = p.name, ["hp"] = p.statLife,
                        ["max_life"] = p.statLifeMax,
                        ["tile_x"] = (int)(p.Center.X / 16),
                        ["tile_y"] = (int)(p.Center.Y / 16),
                        ["selected_slot"] = p.selectedItem,
                        ["alive"] = p.statLife > 0,
                    },
                    // Python 侧用它过滤"自己"（避免把自己当成主人去追）
                    // 背包数据不再随 game_state 持续推送（2s 一次太浪费）。
                    // Python 侧在需要时（挖矿/查询/给物品）主动发 get_inventory 按需拉取。
                    ["nearby_npcs"] = npcs,
                    ["nearby_players"] = players,
                    ["time_of_day"] = Main.dayTime ? "白天" : "夜晚",
                });
            }
            catch { }
        }

        /// <summary>找场上活跃的 Boss（NPC.boss 属性；大怪兜底）。</summary>
        private static string FindActiveBoss()
        {
            for (int i = 0; i < Main.npc.Length; i++)
            {
                var npc = Main.npc[i];
                if (npc == null || !npc.active || npc.life <= 0) continue;
                if (npc.boss) return npc.TypeName;
                // 兜底：血量巨大的敌怪也当 Boss 报（避免漏报部分 mod Boss）
                if (npc.lifeMax >= 5000 && npc.damage > 30) return npc.TypeName;
            }
            return null;
        }

        /// <summary>向 Python 桥推送事件（仅主线程调用，Send 自带锁）。</summary>
        internal void PushEvent(string eventName, string message, string bossName = null)
        {
            var s = _activeStream;
            if (s == null) return;
            try
            {
                var d = new Dict {
                    ["type"] = "event",
                    ["event"] = eventName,
                    ["message"] = message,
                };
                if (!string.IsNullOrEmpty(bossName)) d["boss_name"] = bossName;
                Send(s, d);
            }
            catch (Exception ex)
            {
                Logger.Warn($"PushEvent({eventName}) 失败: {ex.Message}");
            }
        }

        private void Main_DrawMenu(Action<Main, Microsoft.Xna.Framework.GameTime> orig, Main self, Microsoft.Xna.Framework.GameTime gameTime)
        {
            orig(self, gameTime);
            AutoSelectTick();
            MonitorJoinTick();
        }

        private void AutoSelectTick()
        {
            if (_autoSelectDone || !Config.AutoSelectCharacter)
                return;

            // 等足够帧数让 UI 初始化
            if (_autoSelectFrameWait < 120) { _autoSelectFrameWait++; return; }

            // menuMode=0 主菜单 | menuMode=1 单人选角色 | menuMode=10 连服务器后选角色
            if (Main.menuMode != 0 && Main.menuMode != 1 && Main.menuMode != 10)
                return;

            _autoSelectDone = TryAutoSelectPlayer("DrawMenu");
        }

        /// <summary>
        /// 每帧监控 JoinServer 进度 —— 模仿 Terraria-Bot 的协议状态机。
        /// 不再 fire-and-forget，而是等待 Main.netMode→1 + player.active 来确认入服。
        /// </summary>
        private void MonitorJoinTick()
        {
            if (!_joinPending) return;

            // ★ 关键：在主线程上执行 Netplay.StartTcpClient()
            // Terraria 网络 API（StartTcpClient/ServerIP/ListenPort）不是线程安全的，
            // 在 TCP 后台线程调用会导致静默失败（无日志、无异常、无连接效果）。
            if (_joinStartRequested)
            {
                _joinStartRequested = false;
                Logger.Info($"JoinServer: 🔌 主线程发起连接 {_joinHost}:{_joinPort}... (netMode={Main.netMode}, menuMode={Main.menuMode}, thread={System.Threading.Thread.CurrentThread.ManagedThreadId})");

                // ★ 角色处理：由 AutoSelect 精确匹配（含自动重命名）完成，
                // 不再手工重置/加载 Main.player[0]——
                // v0.2.5（无 fresh Player 重置）能正常入服；
                // fresh Player 重置会引发 PlayerInfo 握手 NRE + 玩家不生成。
                // Main.player[0] 使用游戏自身创建的 Player（modPlayers 与
                // 当前 mod 一致），皮肤/外观数据由 AI 存档目录（Players/）加载。
                try
                {
                    Netplay.StartTcpClient();
                    Logger.Info($"JoinServer: ✅ StartTcpClient 调用完成，等待入服...");
                }
                catch (Exception ex)
                {
                    _joinPending = false;
                    Logger.Error($"JoinServer: ❌ StartTcpClient 异常 - {ex.Message}");
                    return;
                }
            }

            // 超时检测（30 秒 = 900 帧 @60fps）
            _joinTimeout--;
            if (_joinTimeout <= 0)
            {
                Logger.Warn($"JoinServer: ⏰ 超时！netMode={Main.netMode}, menuMode={Main.menuMode}");
                _joinPending = false;
                return;
            }

            // 状态机：等待 netMode 变成 1(client) 且玩家已激活
            bool inWorld = Main.LocalPlayer != null && Main.LocalPlayer.active;
            bool connected = Main.netMode == 1 || Main.netMode == 2;

            if (inWorld && connected)
            {
                var elapsed = (DateTime.Now - _joinStart).TotalSeconds;
                Logger.Info($"JoinServer: ✅ 成功入服！耗时 {elapsed:F1}s  (world={Main.worldName})");
                _joinPending = false;
                return;
            }

            // 每 5 秒打印一次进度（避免刷屏）
            if (_joinTimeout % 300 == 0)
            {
                string phase = inWorld ? "世界加载中..." : (Main.netMode == 1 ? "等待玩家生成..." : "等待服务器响应...");
                Logger.Info($"JoinServer: {phase} (netMode={Main.netMode}, menuMode={Main.menuMode}, 剩余{_joinTimeout/60}秒)");
            }
        }

        /// <summary>
        /// 尝试选中角色。优先按 preferredName 精确匹配；不存在时自动重命名第一个 .plr 为目标名。
        /// preferredName 为 null 时直接选第一个可用角色。
        /// </summary>
        private bool TryAutoSelectPlayer(string caller, string preferredName = null)
        {
            try
            {
                string playerDir = Main.PlayerPath;
                if (!Directory.Exists(playerDir))
                {
                    Logger.Warn($"AutoSelect({caller}): 角色目录不存在 {playerDir}");
                    return false;
                }

                var plrFiles = Directory.GetFiles(playerDir, "*.plr")
                    .Where(f => !f.EndsWith(".tplr"))
                    .OrderBy(f => f)
                    .ToList();

                if (plrFiles.Count == 0)
                {
                    Logger.Warn($"AutoSelect({caller}): 没有 .plr 文件");
                    return false;
                }

                // 1) 指定了目标名字 → 精确查找
                if (!string.IsNullOrEmpty(preferredName))
                {
                    string targetPath = Path.Combine(playerDir, preferredName + ".plr");
                    if (File.Exists(targetPath))
                    {
                        Logger.Info($"AutoSelect({caller}): 精确匹配 '{preferredName}'");
                        SetActivePlayer(preferredName);
                        return true;
                    }

                    // 已加载角色的 Name 匹配也算成功（文件名字可能已被改过）
                    if (Main.LocalPlayer?.active == true
                        && Main.LocalPlayer.name.Equals(preferredName, StringComparison.OrdinalIgnoreCase))
                    {
                        Logger.Info($"AutoSelect({caller}): LocalPlayer 已经是 '{Main.LocalPlayer.name}'，跳过");
                        return true;
                    }

                    // 名字不匹配 → 重命名第一个 .plr 为 preferredName
                    string oldPath = plrFiles[0];
                    string oldBase = Path.GetFileNameWithoutExtension(oldPath);
                    Logger.Info($"AutoSelect({caller}): '{preferredName}.plr' 不存在，"
                        + $"重命名 {oldBase}.plr → {preferredName}.plr");
                    RenamePlayerFile(oldPath, preferredName);
                    SetActivePlayer(preferredName);
                    return true;
                }

                // 2) 无指定 → 选第一个
                string firstName = Path.GetFileNameWithoutExtension(plrFiles[0]);
                Logger.Info($"AutoSelect({caller}): 选第一个 '{firstName}'");
                SetActivePlayer(firstName);
                return true;
            }
            catch (Exception ex)
            {
                Logger.Error($"AutoSelect({caller}): 异常 - {ex.Message}");
                return false;
            }
        }

        /// <summary>重命名 .plr 文件。</summary>
        private void RenamePlayerFile(string oldPath, string newBaseName)
        {
            string dir = Path.GetDirectoryName(oldPath);
            string newPath = Path.Combine(dir, newBaseName + ".plr");
            if (!File.Exists(newPath))
            {
                File.Move(oldPath, newPath);
                Logger.Info($"RenamePlayer: {Path.GetFileName(oldPath)} → {newBaseName}.plr");
            }
            else
            {
                Logger.Warn($"RenamePlayer: 目标文件 {newBaseName}.plr 已存在，跳过重命名");
            }
        }

        /// <summary>设置当前活动角色（替代 Terraria 1.4.5 移除的 Player.SelectPlayer）。</summary>
        private void SetActivePlayer(string name)
        {
            // tML 2026 官方方式：Player.GetFileData + SetAsActive（让游戏正确加载角色）
            try
            {
                string path = Path.Combine(Main.PlayerPath, name + ".plr");
                if (File.Exists(path))
                {
                    var fd = Player.GetFileData(path, false);
                    if (fd != null)
                    {
                        fd.SetAsActive();
                        Logger.Info($"SetActivePlayer: ✅ SetAsActive 选中 '{name}'");
                        return;
                    }
                }
            }
            catch (Exception ex)
            {
                Logger.Warn($"SetActivePlayer: SetAsActive 失败({ex.Message})，回退旧方式");
            }
            // 回退：从已加载列表匹配
            if (Main.PlayerList != null)
            {
                var pd = Main.PlayerList.FirstOrDefault(p => p != null && p.Name == name);
                if (pd != null)
                {
                    Main.ActivePlayerFileData = pd;
                    Logger.Info($"SetActivePlayer: 从 PlayerList 选中 '{name}'");
                    return;
                }
            }
            // 兜底：直接创建 PlayerFileData
            string path2 = Path.Combine(Main.PlayerPath, name + ".plr");
            if (File.Exists(path2))
            {
                var pd2 = new PlayerFileData(path2, false);
                Main.ActivePlayerFileData = pd2;
                Logger.Info($"SetActivePlayer: 从路径选中 '{name}'");
                return;
            }
            Logger.Warn($"SetActivePlayer: 无法选中 '{name}'，文件不存在");
        }

        private void ListenLoop()
        {
            while (true)
            {
                try
                {
                    Logger.Info("[TCP] 等待 Python 桥接客户端连接...");
                    using var client = _listener.AcceptTcpClient();
                    Logger.Info("[TCP] 客户端已连接");

                    // ── 握手：先发 welcome，让 Python 确认字节流是干净的 ──
                    var stream = client.GetStream();
                    _activeStream = stream;   // 事件推送目标（volatile）
                    SendRawUtf8(stream, "{\"welcome\":true}\n");
                    Logger.Info("[TCP] 已发送 welcome 握手");

                    var buf = new byte[4096];
                    var sb = new StringBuilder();
                    int n;
                    while ((n = stream.Read(buf, 0, buf.Length)) > 0)
                    {
                        // 诊断：打印收到的原始字节（前200字符），方便排查串行化问题
                        string rawChunk = Encoding.UTF8.GetString(buf, 0, Math.Min(n, 200));
                        Logger.Info($"[TCP] 收到 {n} 字节: 【{rawChunk.Replace("\n","\\n").Replace("\r","\\r")}】");
                        sb.Append(Encoding.UTF8.GetString(buf, 0, n));
                        string text = sb.ToString();
                        int idx;
                        while ((idx = text.IndexOf('\n')) >= 0)
                        {
                            string line = text.Substring(0, idx).Trim();
                            text = text.Substring(idx + 1);
                            if (line.Length > 0) HandleLine(line, stream);
                        }
                        sb.Clear(); sb.Append(text);
                    }
                    if (ReferenceEquals(_activeStream, stream))
                        _activeStream = null;
                    Logger.Info("[TCP] 客户端已断开，等待重连...");
                }
                catch (Exception ex)
                {
                    Logger.Warn($"[TCP] 连接异常: {ex.Message}");
                    Thread.Sleep(500);
                }
            }
        }

        private void SendAck(NetworkStream s, long reqId, bool ok) =>
            Send(s, new Dict { ["req_id"] = reqId, ["ok"] = ok });

        private void SendCraft(NetworkStream s, long reqId, int n) =>
            Send(s, new Dict { ["req_id"] = reqId, ["crafted"] = n });

        // #6: 把改世界/玩家/物品的命令投到主线程执行，避免 TCP 后台线程
        // 操作 Terraria 主线程数据（WorldGen/QuickSpawnItem/player.armor 等）导致崩溃。
        private void RunOnMain(NetworkStream s, long reqId, Func<bool> fn)
        {
            Main.QueueMainThreadAction(() =>
            {
                try
                {
                    SendAck(s, reqId, fn());
                }
                catch
                {
                    SendAck(s, reqId, false);
                }
            });
        }







        private void Send(NetworkStream s, Dict d)
        {
            // 同一 TCP 流被监听线程与 navigate_to 后台线程共用，必须加锁串行写，
            // 否则两线程同时 Write 会让回执字节交错导致 JSON 损坏
            lock (_lock)
            {
                try
                {
                    var bytes = Encoding.UTF8.GetBytes(d.ToJson() + "\n");
                    s.Write(bytes, 0, bytes.Length);
                    // 诊断：确认命令响应是否真的发出（定位响应超时问题）
                    double rid = d.GetNum("req_id");
                    if (rid > 0 && d.GetValue("type") != "event")
                        Logger.Info($"[Send] req_id={rid} len={bytes.Length}");
                }
                catch { }
            }
        }

        /// <summary>重命令（enum_items/get_recipes 等）后台执行，不阻塞 TCP 监听线程。
        ///
        /// 之前 SendItemRegistry / SendRecipes 在监听线程同步跑，遍历全物品/全配方
        /// 要几十秒，期间 get_state / navigate_to 全部排队，导致 AI 人物不动、UI 无状态。
        /// 现在改为后台 Task 生成响应，监听线程立刻返回继续处理下一条命令。</summary>
        private void RunInBackground(Action action, string name)
        {
            Task.Run(() =>
            {
                try
                {
                    action();
                }
                catch (Exception ex)
                {
                    try { Logger.Warn($"[NekoTerrariaLink] {name} 后台执行失败: {ex.Message}"); }
                    catch { }
                }
            });
        }

        /// <summary>直接发送原始 UTF-8 字节（不带 Dict 包装，用于握手等协议级消息）</summary>
        private static void SendRawUtf8(NetworkStream s, string text)
        {
            try
            {
                var bytes = Encoding.UTF8.GetBytes(text);
                s.Write(bytes, 0, bytes.Length);
            }
            catch { }
        }
    

        private void HandleLine(string line, NetworkStream stream)
        {
            // 回退：监听线程直接执行（老版本逻辑）——命令主线程化依赖
            // EventMonitorTick（UpdateUI），多文件版 UpdateUI 不触发时队列永不处理。
            // ProcessCommandQueue 保留：若主线程路径恢复，队列为空无害。
            try { ExecuteCommand(line, stream); }
            catch (Exception ex)
            {
                Logger.Warn($"[NekoTerrariaLink] 忽略非法指令: {line.Substring(0, Math.Min(line.Length, 200))}  错误: {ex.Message}");
            }
        }

        private void ProcessCommandQueue()
        {
            while (true)
            {
                (string line, NetworkStream stream) item;
                lock (_cmdLock)
                {
                    if (_cmdQueue.Count == 0) return;
                    item = _cmdQueue.Dequeue();
                }
                try { ExecuteCommand(item.line, item.stream); }
                catch (Exception ex)
                {
                    Logger.Warn($"[NekoTerrariaLink] 忽略非法指令: {item.line.Substring(0, Math.Min(item.line.Length, 200))}  错误: {ex.Message}");
                }
            }
        }

        private void ExecuteCommand(string line, NetworkStream stream)
        {
            long reqId = 0;
            try
            {
                var cmd = JsonParser.Parse(line);
                if (cmd == null) return;
                // 透传 req_id：Python 侧用它把回执与请求一一对应，避免串线
                reqId = (long)cmd.GetNum("req_id");
                var p0 = Main.LocalPlayer;
                if (++_diagCmdCount % 20 == 1)
                {
                    Logger.Info($"诊断: cmd={cmd.GetValue("cmd")} Main.LocalPlayer={(p0 != null ? $"'{p0.name}' active={p0.active}" : "null")} Main.player[0]={(Main.player[0] != null ? $"'{Main.player[0].name}' active={Main.player[0].active}" : "null")} Main.myPlayer={Main.myPlayer} Main.netMode={Main.netMode}");
                }
                string type = cmd == null ? "" : cmd.GetValue("cmd");

                // ===== 诊断：记录收到的命令 =====
                if (type != "ping")
                    Logger.Info($"[CMD] 收到 #{reqId} {type}  | 原始JSON: {line.Substring(0, Math.Min(line.Length, 120))}");

                switch (type)
                {
                    // #6: 只改 ModPlayer 字段（move/hook）后台线程安全，保留原样；
                    // 操作世界/玩家/物品/箱子的命令必须投主线程。
                    case "move": SendAck(stream, reqId, Move(cmd)); break;
                    case "place_tile": RunOnMain(stream, reqId, () => PlaceTile(cmd)); break;
                    case "break_tile": RunOnMain(stream, reqId, () => BreakTile(cmd)); break;
                    case "hook": SendAck(stream, reqId, Hook(cmd)); break;
                    case "use_item": RunOnMain(stream, reqId, () => UseItem(cmd)); break;
                    case "select_item": RunOnMain(stream, reqId, () => SelectItem(cmd)); break;
                    case "craft": CraftAsync(stream, reqId, cmd); break;
                    case "equip": RunOnMain(stream, reqId, () => Equip(cmd)); break;
                    case "give_item": RunOnMain(stream, reqId, () => GiveItem(cmd)); break;
                    case "drop_item": RunOnMain(stream, reqId, () => DropItem(cmd)); break;
                    case "use_item_slot": RunOnMain(stream, reqId, () => UseItemSlot(cmd)); break;
                    case "dig_tile": RunOnMain(stream, reqId, () => DigTile(cmd)); break;
                    case "navigate_to": NavigateTo(stream, cmd, reqId); break;
                    case "navigate_stream": NavigateStream(stream, cmd, reqId); break;
                    case "send_chat": Main.QueueMainThreadAction(() => SendAck(stream, reqId, SendChatMessage(cmd))); break;
                    case "get_inventory": SendInventory(stream, reqId); break;
                    case "enum_chests": Main.QueueMainThreadAction(() => SendChests(stream, reqId)); break;
                    case "store_item": RunOnMain(stream, reqId, () => StoreItem(cmd)); break;
                    case "take_chest": RunOnMain(stream, reqId, () => TakeFromChest(cmd)); break;
                    case "get_recipes": RunInBackground(() => SendRecipes(stream, reqId, cmd.GetValue("cat")), "get_recipes"); break;
                    case "get_state": SendState(stream, reqId, cmd.GetValue("player_name")); break;
                    case "enum_items": RunInBackground(() => SendItemRegistry(stream, reqId), "enum_items"); break;
                    case "get_capabilities": SendCapabilities(stream, reqId); break;
                    case "scan_ledges": Main.QueueMainThreadAction(() => SendLedges(stream, reqId, cmd)); break;
                    case "find_ore": Main.QueueMainThreadAction(() => SendOrePositions(stream, reqId, cmd)); break;
                    case "get_server_info": SendServerInfo(stream, reqId); break;
                    case "join_server": SendAck(stream, reqId, JoinServer(cmd)); break;
                    case "select_character": RunOnMain(stream, reqId, () => SelectCharacter(cmd)); break;
                    case "damage_npc": RunOnMain(stream, reqId, () => DamageNpc(cmd)); break;
                    case "warp": RunOnMain(stream, reqId, () => Warp(cmd)); break;
                    case "get_network_info": SendNetworkInfo(stream, reqId); break;
                    case "join_status": SendJoinStatus(stream, reqId); break;
                    case "screenshot": SendScreenshot(stream, reqId); break;
                    case "get_spawn": SendSpawn(stream, reqId); break;
                    case "use_mirror": RunOnMain(stream, reqId, () => UseMirror(cmd)); break;
                    case "place_chest": RunOnMain(stream, reqId, () => PlaceChest(cmd)); break;
                    case "quick_stack": RunOnMain(stream, reqId, () => QuickStack(cmd)); break;
                    case "find_trees": Main.QueueMainThreadAction(() => SendTreePositions(stream, reqId, cmd)); break;
                    case "chop_trees": RunOnMain(stream, reqId, () => ChopTrees(cmd)); break;
                    case "find_water": Main.QueueMainThreadAction(() => SendWaterPositions(stream, reqId, cmd)); break;
                    case "collect_items":
                        RunOnMainCollect(stream, reqId, cmd);
                        break;
                    default:
                        // 未知命令也回执，避免 Python 端 3s 超时空等
                        SendAck(stream, reqId, false);
                        break;
                }
            }
            catch (Exception ex)
            {
                Logger.Warn($"[NekoTerrariaLink] 忽略非法指令: {line.Substring(0, Math.Min(line.Length, 200))}  错误: {ex.Message}");
                // 异常也要回执：否则 Python 端 request_mod 超时 3s（命令"不执行"假象）
                try { SendAck(stream, reqId, false); } catch { }
            }
        }

        private bool Move(Dict cmd)
        {
            var dirs = cmd.GetArray("dirs") ?? new List<string> { cmd.GetValue("direction") };
            var ctrl = Main.LocalPlayer?.GetModPlayer<NekoControlPlayer>();
            if (ctrl == null) return false;
            ctrl.moveDir = 0;
            foreach (var d in dirs)
            {
                switch (d)
                {
                    case "left": ctrl.moveDir = -1; break;
                    case "right": ctrl.moveDir = 1; break;
                    case "jump": ctrl.jumpTicks = 8; break;
                    case "up": ctrl.moveDir = 0; ctrl.jumpTicks = 8; break;
                    case "down": case "stop": ctrl.moveDir = 0; break;
                }
            }
            return true;
        }

        /// <summary>同步箱子槽位到服务器（联机时本地直改 Main.chest 不会被服务器看到）。</summary>
        private static void SyncChest(int idx, int slot)
        {
            if (Main.netMode == NetmodeID.MultiplayerClient)
            {
                try
                {
                    NetMessage.SendData(MessageID.SyncChestItem, -1, -1,
                        null, idx, slot, 0f, 0f, 0, 0, 0);
                }
                catch { }
            }
        }

        /// <summary>
        /// 同步方块变化到服务器（mod 只在 AI 客户端，本地 WorldGen 不会让
        /// 服务器/其他玩家看到——必须广播 TileChange。action: 0=放置, 1=破坏）
        /// </summary>
        private static void SyncTile(int x, int y, int action)
        {
            if (Main.netMode == NetmodeID.MultiplayerClient)
            {
                try
                {
                    NetMessage.SendData(MessageID.TileManipulation, -1, -1,
                        null, action, x, y, 0f, 0, 0, 0);
                }
                catch { }
            }
        }

        /// <summary>玩家到目标格的曼哈顿距离（格）。</summary>
        private static int TileDist(int x, int y)
        {
            var p = Main.LocalPlayer;
            if (p == null) return int.MaxValue;
            int px = (int)(p.Center.X / 16f), py = (int)(p.Center.Y / 16f);
            return Math.Abs(px - x) + Math.Abs(py - y);
        }

        /// <summary>目标格是否在玩家可触及范围内（真走到附近才允许改世界，防远程假完成）。</summary>
        private static bool InReach(int x, int y, int range = 8)
        {
            return TileDist(x, y) <= range;
        }

        private bool BreakTile(Dict cmd)
        {
            int x = (int)cmd.GetNum("x"), y = (int)cmd.GetNum("y");
            if (!InReach(x, y, 8)) return false;   // 太远：人物没走过去就不许拆
            WorldGen.KillTile(x, y, false, false, true);
            SyncTile(x, y, 1);   // 破坏 → 服务器广播
            return true;
        }

        private bool PlaceTile(Dict cmd)
        {
            int x = (int)cmd.GetNum("x"), y = (int)cmd.GetNum("y");
            int tile = (int)cmd.GetNum("tile");
            if (!InReach(x, y, 8)) return false;   // 太远：人物没走过去就不许放
            bool ok = WorldGen.PlaceTile(x, y, tile, false, false, -1, 0);
            if (ok) SyncTile(x, y, 0);   // 放置 → 服务器广播
            return ok;
        }

        private bool Hook(Dict cmd)
        {
            var ctrl = Main.LocalPlayer?.GetModPlayer<NekoControlPlayer>();
            if (ctrl == null) return false;
            ctrl.hookTicks = 10;
            return true;
        }

        private bool UseItem(Dict cmd)
        {
            var ctrl = Main.LocalPlayer?.GetModPlayer<NekoControlPlayer>();
            if (ctrl == null) return false;
            // 始终使用当前选中物品（select_item 后 useSlot 可能残留旧的）
            ctrl.useSlot = Main.LocalPlayer.selectedItem;
            ctrl.useTicks = 15;   // 约 0.25s 持续挥动（战斗/砍树/钓鱼循环每轮刷新）
            // 支持目标坐标：让角色朝向/挥动方向对准目标（战斗/砍树/钓鱼用）
            if (cmd.Has("target_x"))
            {
                ctrl.digTargetX = (int)cmd.GetNum("target_x");
                ctrl.digTargetY = (int)cmd.GetNum("target_y");
            }
            else
            {
                // 不带目标时清掉旧目标，避免光标残留在上次战斗/砍树点
                ctrl.digTargetX = -1;
                ctrl.digTargetY = -1;
            }
            return true;
        }

        private bool SelectItem(Dict cmd)
        {
            int slot = (int)cmd.GetNum("slot");
            Main.LocalPlayer.selectedItem = slot;
            return true;
        }

        private bool JoinServer(Dict cmd)
        {
            Logger.Info($"[JoinServer] 进入方法，开始解析参数...");
            string host = cmd.GetValue("host");
            Logger.Info($"[JoinServer] host='{host}' (长度={host?.Length ?? 0})");
            int port = (int)(cmd.GetNum("port") > 0 ? cmd.GetNum("port") : 7777);
            Logger.Info($"[JoinServer] port={port}");
            string password = cmd.GetValue("password");
            // v3.0: 目标角色名（Python 配置 character_name）——精确匹配，
            // 不存在时自动重命名第一个 .plr（AutoSelect 内置逻辑）
            string characterName = cmd.GetValue("character_name");
            if (!string.IsNullOrEmpty(characterName))
                _joinCharName = characterName;

            if (string.IsNullOrEmpty(host))
            {
                Logger.Warn("[JoinServer] host 为空，返回 false");
                return false;
            }

            // 确保已选中角色（替代损坏的 -skipselect）：
            // 指定了 character_name → 精确匹配 + 自动重命名兜底
            if (Config.AutoSelectCharacter)
            {
                if (!string.IsNullOrEmpty(characterName))
                {
                    _autoSelectDone = TryAutoSelectPlayer("JoinServer", characterName)
                        || _autoSelectDone;
                    Logger.Info($"[JoinServer] 角色 '{characterName}' 检查完成 (done={_autoSelectDone})");
                }
                else
                {
                    _autoSelectDone = TryAutoSelectPlayer("JoinServer") || _autoSelectDone;
                    Logger.Info($"[JoinServer] 角色检查后 _autoSelectDone={_autoSelectDone}");
                }
            }

            try
            {
                Logger.Info($"[JoinServer] 检查 IPAddress.TryParse('{host}')");
                if (System.Net.IPAddress.TryParse(host, out var ip))
                {
                    Netplay.ServerIP = ip;
                    Logger.Info($"[JoinServer] ServerIP={ip}");
                }
                else
                {
                    Logger.Warn($"[JoinServer] IPAddress.TryParse 返回 false！");
                    return false;
                }
                Netplay.ListenPort = port;
                if (!string.IsNullOrEmpty(password))
                    Netplay.ServerPassword = password;

                // ★ 关键修复：不在这里调用 Netplay.StartTcpClient()
                // 因为 JoinServer 在 TCP 后台线程(线程8)上运行，
                // 而 Terraria 网络 API 必须在主线程执行。
                // 改为设置标志，由 MonitorJoinTick（主线程）发起连接。
                _joinPending = true;
                _joinHost = host;
                _joinPort = port;
                _joinStart = DateTime.Now;
                _joinTimeout = 900; // 30秒超时 (60fps × 30s)
                _joinStartRequested = true;
                Logger.Info($"[JoinServer] ✅ 参数已设置，等待主线程发起连接... (thread={System.Threading.Thread.CurrentThread.ManagedThreadId})");
                return true;
            }
            catch (Exception ex)
            {
                _joinPending = false;
                Logger.Error($"[JoinServer] ❌ 异常: {ex.GetType().Name} - {ex.Message}\n{ex.StackTrace}");
                return false;
            }
        }

        /// <summary>显式选择角色（select_character 命令），统一委托 TryAutoSelectPlayer。</summary>
        private bool SelectCharacter(Dict cmd)
        {
            string targetName = cmd.GetValue("name");
            long idx = (long)cmd.GetNum("index");

            // 1) 按名字 → TryAutoSelectPlayer 精确匹配 + 自动重命名兜底
            if (!string.IsNullOrEmpty(targetName))
            {
                _autoSelectDone = TryAutoSelectPlayer("SelectCharacter", targetName);
                return _autoSelectDone;
            }

            // 2) 按索引
            if (idx >= 0)
            {
                string playerDir = Main.PlayerPath;
                if (!Directory.Exists(playerDir)) return false;
                var plrFiles = Directory.GetFiles(playerDir, "*.plr")
                    .Where(f => !f.EndsWith(".tplr"))
                    .OrderBy(f => f).ToList();
                if (idx < plrFiles.Count)
                {
                    string name = Path.GetFileNameWithoutExtension(plrFiles[(int)idx]);
                    _autoSelectDone = TryAutoSelectPlayer("SelectCharacter", name);
                    return _autoSelectDone;
                }
            }

            // 3) 兜底：选第一个
            _autoSelectDone = TryAutoSelectPlayer("SelectCharacter");
            return _autoSelectDone;
        }

        private bool SendChatMessage(Dict cmd)
        {
            string text = cmd.GetValue("text");
            if (string.IsNullOrEmpty(text)) return false;

            // 本地显示消息
            Main.NewText(text, 255, 240, 20);

            // 如果在多人游戏中，发送到服务器
            if (Main.netMode == NetmodeID.MultiplayerClient)
            {
                // 使用兼容的聊天发送方法
                NetMessage.SendData(MessageID.ChatText, -1, -1,
                    Terraria.Localization.NetworkText.FromLiteral(text),
                    Main.myPlayer);
            }

            return true;
        }

        private bool DamageNpc(Dict cmd)
        {
            int slot = (int)cmd.GetNum("slot");
            int damage = (int)(cmd.GetNum("damage") > 0 ? cmd.GetNum("damage") : 1);
            if (slot < 0 || slot >= Main.npc.Length) return false;
            var npc = Main.npc[slot];
            if (npc == null || !npc.active) return false;
            if (Main.netMode == NetmodeID.MultiplayerClient)
            {
                // 联机：请求服务器结算伤害（客户端直接改 npc.life 会被服务器覆盖）
                try
                {
                    NetMessage.SendData(MessageID.DamageNPC, -1, -1, null,
                        npc.whoAmI, damage, 0f, 1f, 0, 0, 0);
                    return true;
                }
                catch { return false; }
            }
            // 单机：直接结算
            npc.life -= damage;
            if (npc.life <= 0) npc.checkDead();
            return true;
        }

        private bool Warp(Dict cmd)
        {
            int x = (int)cmd.GetNum("x");
            int y = (int)cmd.GetNum("y");
            var p = Main.LocalPlayer;
            if (p == null) return false;
            // 命令坐标是 tile 格，Teleport 需要像素
            p.Teleport(new Vector2(x * 16f, y * 16f), 1);
            return true;
        }

        /// <summary>查询世界出生点（基地定位用）。</summary>
        private void SendSpawn(NetworkStream s, long reqId)
        {
            int sx = Main.spawnTileX, sy = Main.spawnTileY;
            Send(s, new Dict { ["req_id"] = reqId, ["type"] = "spawn", ["x"] = sx, ["y"] = sy });
        }

        /// <summary>使用背包里的魔镜/冰雪镜回出生点（基地回家，合法物品）。</summary>
        private bool UseMirror(Dict cmd)
        {
            var p = Main.LocalPlayer;
            if (p == null) return false;
            // 在背包里找魔镜(50)/冰雪镜(3199)，没有就生成一个
            int mirrorId = -1;
            for (int i = 0; i < p.inventory.Length; i++)
            {
                int t = p.inventory[i].type;
                if (t == 50 || t == 3199) { mirrorId = i; break; }
            }
            if (mirrorId < 0)
            {
                var m = new Item();
                m.SetDefaults(50);
                p.QuickSpawnItem(Src, m, 1);
                // 再找一次
                for (int i = 0; i < p.inventory.Length; i++)
                {
                    if (p.inventory[i].type == 50) { mirrorId = i; break; }
                }
            }
            if (mirrorId < 0) return false;
            p.selectedItem = mirrorId;
            // 用魔镜：直接触发回城（Recursion 方式等效于右键使用魔镜）
            p.Teleport(new Vector2(Main.spawnTileX * 16f, Main.spawnTileY * 16f), 1);
            // 短暂无敌防出生点被怪秒
            p.AddBuff(BuffID.PotionSickness, 30);
            return true;
        }

        /// <summary>在 (x,y) 放置木箱（基地储物用）。</summary>
        private bool PlaceChest(Dict cmd)
        {
            int x = (int)cmd.GetNum("x");
            int y = (int)cmd.GetNum("y");
            int style = (int)(cmd.GetNum("style") > 0 ? cmd.GetNum("style") : 0);
            bool ok = WorldGen.PlaceTile(x, y, TileID.Containers, false, false, -1, style);
            if (ok) SyncTile(x, y, 0);
            return ok;
        }

        /// <summary>把背包物品快速堆叠进当前打开/最近的箱子。返回堆叠的物品种数。</summary>
        private bool QuickStack(Dict cmd)
        {
            var p = Main.LocalPlayer;
            if (p == null) return false;
            int cx = (int)(p.Center.X / 16f), cy = (int)(p.Bottom.Y / 16f);
            int chestIdx = Chest.FindChest(cx, cy);
            if (chestIdx < 0) return false;
            var chest = Main.chest[chestIdx];
            if (chest == null) return false;
            int stacked = 0;
            for (int i = 0; i < p.inventory.Length; i++)
            {
                var it = p.inventory[i];
                if (it == null || it.type <= 0 || it.stack <= 0) continue;
                if (it.favorited) continue;
                for (int k = 0; k < chest.item.Length; k++)
                {
                    var ci = chest.item[k];
                    if (ci == null || ci.type <= 0) continue;
                    if (ci.type == it.type && ci.stack < ci.maxStack)
                    {
                        int add = Math.Min(it.stack, ci.maxStack - ci.stack);
                        ci.stack += add;
                        it.stack -= add;
                        if (it.stack <= 0) it.SetDefaults(0);
                        stacked++;
                        SyncChest(chestIdx, k);
                        break;
                    }
                }
            }
            return stacked > 0;
        }

    

        private void NavigateTo(NetworkStream s, Dict cmd, long reqId)
        {
            int tx = (int)cmd.GetNum("x"), ty = (int)cmd.GetNum("y");
            int timeout = (int)(cmd.GetNum("timeout") > 0 ? cmd.GetNum("timeout") : 15);
            Task.Run(() => NavigateSync(s, tx, ty, timeout, reqId));
        }

        private void NavigateSync(NetworkStream s, int tx, int ty, int timeout, long reqId)
        {
            MonitorNav(s, tx, ty, timeout, reqId, streamEvents: false);
        }

        /// <summary>导航监控（带兜底）：后台线程监控 + 主线程 BFS。
        ///
        /// 之前的实现整个 MonitorNav 在 Task.Run 后台线程跑，直接访问
        /// Main.tile（BFS）/GetModPlayer 等主线程数据——抛异常后 Task 静默死亡，
        /// 无任何响应 → Python 侧全部超时 → 猫娘不动。现在：
        /// 1. BFS 路径规划移到主线程（QueueMainThreadAction）
        /// 2. 后台循环只监控（读 player.Center 可接受旧值，不会崩）
        /// 3. 全程 try/catch：任何异常都回错误响应（reason=nav_exception），不再静默
        /// </summary>
        private void MonitorNav(NetworkStream s, int tx, int ty, int timeout, long reqId, bool streamEvents)
        {
            try
            {
                MonitorNavInner(s, tx, ty, timeout, reqId, streamEvents);
            }
            catch (Exception ex)
            {
                try { Logger.Error($"[Nav] MonitorNav 异常: {ex.GetType().Name}: {ex.Message}\n{ex.StackTrace}"); }
                catch { }
                try { Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "nav_exception:" + ex.Message }); }
                catch { }
            }
        }

        private void MonitorNavInner(NetworkStream s, int tx, int ty, int timeout, long reqId, bool streamEvents)
        {
            var player = Main.LocalPlayer;
            if (player == null)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "no_player" });
                return;
            }
            var ctrl = player.GetModPlayer<NekoControlPlayer>();
            if (ctrl == null)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "no_control_player" });
                return;
            }
            // feet 语义：脚底行与寻路一致；Python 目标为 center 行 → feet 行 +1
            int sx = (int)(player.Center.X / 16), sy = (int)(player.Bottom.Y / 16);
            int tyFeet = ty + 1;

            // ── BFS 必须在主线程执行（Main.tile 是主线程数据） ──
            List<NavPoint> path = null;
            bool bfsDone = false;
            bool bfsError = false;
            string bfsErr = "";
            Main.QueueMainThreadAction(() =>
            {
                try
                {
                    path = FindPathAStar(sx, sy, tx, tyFeet);
                    if (path != null && path.Count > 0)
                    {
                        ctrl.navPath = path;
                        ctrl.navIdx = 0;
                        ctrl.jumpTicks = 0;
                        ctrl.navGen++;   // 路径代际：新导航接管（fire-and-forget 防旧任务误清）
                    }
                }
                catch (Exception ex)
                {
                    bfsError = true;
                    bfsErr = ex.Message;
                }
                bfsDone = true;
            });
            for (int i = 0; i < 300 && !bfsDone; i++) Thread.Sleep(10);  // 最多等 3s
            if (!bfsDone)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "bfs_timeout" });
                return;
            }
            if (bfsError)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "bfs_exception:" + bfsErr });
                return;
            }
            if (path == null)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "no_path" });
                return;
            }
            if (path.Count == 0)
            {
                // 已在目标点：BFS 返回空路径，直接算到达
                if (streamEvents) SendNavEvent(s, "nav_arrived", sx, sy);
                Send(s, new Dict { ["req_id"] = reqId, ["ok"] = true, ["x"] = sx, ["y"] = sy });
                return;
            }
            if (streamEvents) SendNavEvent(s, "nav_started", sx, sy);

            // ── 后台监控循环（只读 player.Center，可接受旧值） ──
            int myGen = ctrl.navGen;   // 本任务代际：被新导航接管时不清理路径
            int steps = 0, maxSteps = timeout * 10;
            int stuckCounter = 0, lastPx = 0, lastPy = 0;
            while (steps < maxSteps)
            {
                // #8: 被新导航接管（代际变化）→ 立即退出本线程，不再发 nav_* 事件。
                // 否则僵尸监控线程会在后续 20s 内继续推 nav_moving/stuck/arrived，
                // 导致 Python 侧 navigate_async 跨导航互相误判到达/超时，且 TCP 事件风暴。
                if (ctrl.navGen != myGen)
                {
                    return;
                }
                int px = (int)(player.Center.X / 16), py = (int)(player.Bottom.Y / 16);
                // 路径走完（ModPlayer 置空）且贴近目标 → 到达（须为本人路径，代际未变）
                if (ctrl.navGen == myGen && ctrl.navPath == null && Math.Abs(px - tx) <= 1 && Math.Abs(py - tyFeet) <= 2)
                {
                    if (streamEvents) SendNavEvent(s, "nav_arrived", px, py);
                    Send(s, new Dict { ["req_id"] = reqId, ["ok"] = true, ["x"] = px, ["y"] = py });
                    return;
                }
                if (streamEvents && steps % 10 == 0) SendNavEvent(s, "nav_moving", px, py);
                // 脚下无支撑 → 垫土/钩锁过坑（steps>0 跳过首轮，避免 lastPx=0 误判）
                if (steps > 0 && player.velocity.Y == 0 && Math.Abs(px - lastPx) + Math.Abs(py - lastPy) > 0)
                {
                    int dir = tx > px ? 1 : -1;
                    if (!IsStandable(px + dir, py + 1))
                    {
                        if (!TryBridge(px, py, dir)) TryHook(dir);
                    }
                }
                // 卡住：5s 基本未动（波动<=1格容忍撞墙抖动）→ stuck；中途垫土一次
                int moved = Math.Abs(px - lastPx) + Math.Abs(py - lastPy);
                if (moved <= 1)
                {
                    stuckCounter++;
                    if (stuckCounter > 50)
                    {
                        if (ctrl.navGen == myGen) ctrl.navPath = null;
                        if (streamEvents) SendNavEvent(s, "nav_stuck", px, py);
                        Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "stuck", ["x"] = px, ["y"] = py });
                        return;
                    }
                    if (stuckCounter == 25) TryStepUp(px, py);
                }
                else { stuckCounter = 0; lastPx = px; lastPy = py; }
                Thread.Sleep(100);
                steps++;
            }
            if (ctrl.navGen == myGen) ctrl.navPath = null;
            int fx = (int)(player.Center.X / 16), fy = (int)(player.Center.Y / 16);
            if (streamEvents) SendNavEvent(s, "nav_timeout", fx, fy);
            Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "timeout", ["x"] = fx, ["y"] = fy });
        }

        // ══════════════════════════════════════════════════════════
        // 流式导航：BFS 寻路 + 逐点执行 + 状态流回传
        // 协议：navigate_stream 命令 → 推 {"type":"event","event":"nav_moving|nav_arrived|nav_stuck|nav_timeout",x,y}
        //       最终带 req_id 响应（ok）。Python 侧订阅 nav_* 事件流，可中断。
        // ══════════════════════════════════════════════════════════

        private void NavigateStream(NetworkStream s, Dict cmd, long reqId)
        {
            int tx = (int)cmd.GetNum("x"), ty = (int)cmd.GetNum("y");
            int timeout = (int)(cmd.GetNum("timeout") > 0 ? cmd.GetNum("timeout") : 20);
            Task.Run(() => NavigateStreamSync(s, tx, ty, timeout, reqId));
        }

        private void NavigateStreamSync(NetworkStream s, int tx, int ty, int timeout, long reqId)
        {
            MonitorNav(s, tx, ty, timeout, reqId, streamEvents: true);
        }

        /// <summary>推导航状态事件（走现有事件通道，Python 侧 bus.fire 分发）。</summary>
        private void SendNavEvent(NetworkStream s, string evt, int x, int y)
        {
            try
            {
                Send(s, new Dict {
                    ["type"] = "event", ["event"] = evt, ["x"] = x, ["y"] = y,
                });
            }
            catch { }
        }

        /// <summary>路径点动作类型（路径点动作类型）。</summary>
        internal enum NavAction { Move, DropThroughPlatform }

        /// <summary>路径点（feet 语义）：X=脚底列，Y=脚底行（支撑在 Y+1）。
        /// Jump = 到达此点需跳的高度（格），执行器按帧表精确按住跳跃，治"跳过头"。</summary>
        internal class NavPoint
        {
            public int X, Y;
            public NavAction Action;
            public int Jump;
            public NavPoint(int x, int y, NavAction a, int jump = 0) { X = x; Y = y; Action = a; Jump = jump; }
            public override bool Equals(object o) => o is NavPoint p && p.X == X && p.Y == Y;
            public override int GetHashCode() => X * 65536 + Y;
        }

        /// <summary>移动能力（动态跳高，移植 Bridge）：一次空中段最大上升/水平跨越。</summary>
        private struct MovementCaps
        {
            public int MaxRise;
            public int MaxGap;
        }

        private static MovementCaps GetMovementCaps(Player p)
        {
            int jumpTiles = MaxJumpHeight(p);
            int wingTiles = p.wingTimeMax > 0 ? Math.Clamp(p.wingTimeMax / 8, 6, 24) : 0;
            int maxRise = Math.Clamp(jumpTiles + wingTiles, 6, 30);
            int maxGap = wingTiles > 0 ? 8 : 5;
            return new MovementCaps { MaxRise = maxRise, MaxGap = maxGap };
        }

        /// <summary>实际最大跳高（格）：动力段（jumpSpeed×jumpHeight 帧）+ 惯性滑行模拟（移植 Bridge）。</summary>
        private static int MaxJumpHeight(Player p)
        {
            float jumpSpeed = Player.jumpSpeed;      // 配饰可修改
            int jumpHeight = Player.jumpHeight;
            float gravity = Player.defaultGravity;   // 0.4
            if (gravity <= 0f) return 6;
            float px = jumpSpeed * jumpHeight;       // 动力段
            float vel = jumpSpeed;
            while (vel > 0f) { vel -= gravity; if (vel > 0f) px += vel; }  // 滑行段
            return Math.Max(1, (int)(px / 16f));
        }

        /// <summary>该格可站立（feet 语义：脚下有实心或平台支撑）。</summary>
        internal static bool IsStandable(int x, int y)
        {
            return IsSolid(x, y + 1) || IsPlatform(x, y + 1);
        }

        /// <summary>2x3 体宽检查：站立行 + 上方两行在该列无实心。</summary>
        private static bool BodyFits(int x, int y)
        {
            return !IsSolid(x, y) && !IsSolid(x, y - 1) && !IsSolid(x, y - 2);
        }

        /// <summary>垂直走廊：三列均无实心（平台不阻挡上升）。</summary>
        private static bool RowPassable(int x, int y)
        {
            return !IsSolid(x, y) && !IsSolid(x - 1, y) && !IsSolid(x + 1, y);
        }

        /// <summary>从 (x,y) 上下寻找可站立行（吸附）。</summary>
        private static bool TrySnapToStand(ref int x, ref int y, int range)
        {
            for (int dy = 0; dy <= range; dy++)
            {
                if (IsStandable(x, y - dy)) { y -= dy; return true; }
                if (dy > 0 && IsStandable(x, y + dy)) { y += dy; return true; }
            }
            return false;
        }

        /// <summary>A* 寻路（feet 语义，移植）：
        /// 边 = Walk(含一步台阶)/悬崖下落/平台下落/跳跃(带垂直走廊检查 + 跳跃高度编码)。
        /// 目标宽松判定 |dx|&lt;=1, |dy|&lt;=2。</summary>
        private static List<NavPoint> FindPathAStar(int x0, int y0, int x1, int y1,
                                                    int maxNodes = 5000)
        {
            if (x0 == x1 && y0 == y1) return new List<NavPoint>();
            int sx = x0, sy = y0, gx = x1, gy = y1;
            if (!TrySnapToStand(ref sx, ref sy, 30) || !TrySnapToStand(ref gx, ref gy, 25))
                return null;
            if (sx == gx && sy == gy) return new List<NavPoint>();

            long key0 = ((long)sx << 16) | (uint)(sy & 0xFFFF);
            long goalKey = ((long)gx << 16) | (uint)(gy & 0xFFFF);
            var records = new Dictionary<long, (float g, long parent, NavAction kind, int jump, bool closed)>();
            var open = new PriorityQueue<long, float>();
            records[key0] = (0f, -1, NavAction.Move, 0, false);
            open.Enqueue(key0, H(sx, sy, gx, gy));

            var caps = GetMovementCapsStatic();
            var neighbors = new List<(int, int, float, NavAction, int)>(32);
            int expansions = 0;
            long endKey = -1;

            while (open.Count > 0 && expansions < maxNodes)
            {
                long curKey = open.Dequeue();
                var cur = records[curKey];
                if (cur.closed) continue;
                cur.closed = true;
                records[curKey] = cur;
                expansions++;

                int cx = (int)(curKey >> 16), cy = (int)(curKey & 0xFFFF);
                if (Math.Abs(cx - gx) <= 1 && Math.Abs(cy - gy) <= 2)
                { endKey = curKey; break; }

                neighbors.Clear();
                GenerateNeighbors(cx, cy, caps, false, 10, neighbors);
                foreach (var (nx, ny, cost, kind, jump) in neighbors)
                {
                    long nk = ((long)nx << 16) | (uint)(ny & 0xFFFF);
                    float ng = cur.g + cost;
                    if (records.TryGetValue(nk, out var ex) && (ex.closed || ex.g <= ng))
                        continue;
                    records[nk] = (ng, curKey, kind, jump, false);
                    open.Enqueue(nk, ng + H(nx, ny, gx, gy));
                }
            }
            if (endKey == -1) return null;

            var path = new List<NavPoint>();
            long k = endKey;
            while (k != -1)
            {
                var r = records[k];
                path.Add(new NavPoint((int)(k >> 16), (int)(k & 0xFFFF), r.kind, r.jump));
                k = r.parent;
            }
            path.Reverse();
            return path;
        }

        private static MovementCaps GetMovementCapsStatic()
        {
            return GetMovementCaps(Main.LocalPlayer);
        }

        private static float H(int x, int y, int tx, int ty) =>
            Math.Abs(tx - x) + Math.Abs(ty - y) * 2f;

        /// <summary>生成邻居（feet 语义，移植）：Walk/Fall/Drop/Jump。
        /// 元组第 5 项 = 跳跃高度（格），执行器按帧表精确跳跃。</summary>
        private static void GenerateNeighbors(int x, int y, MovementCaps caps,
            bool allowPlatformDrop, int maxDropTiles, List<(int, int, float, NavAction, int)> result)
        {
            // Walk（含一步台阶上/下）
            for (int dir = -1; dir <= 1; dir += 2)
            {
                int nx = x + dir;
                foreach (int ny in WalkCandidateRows(y))
                {
                    if (!IsStandable(nx, ny)) continue;
                    if (ny < y && !BodyFits(x, ny)) continue;
                    result.Add((nx, ny, 1f + 0.4f * Math.Abs(ny - y), NavAction.Move, 0));
                    break;
                }
            }
            // 悬崖下落（侧向空中落地）
            for (int dir = -1; dir <= 1; dir += 2)
            {
                int nx = x + dir;
                if (!BodyFits(nx, y) || IsStandable(nx, y)) continue;
                for (int ny = y + 1; ny <= y + 45; ny++)
                {
                    if (!BodyFits(nx, ny)) break;
                    if (IsStandable(nx, ny))
                    {
                        result.Add((nx, ny, 1.2f + 0.25f * (ny - y), NavAction.Move, 0));
                        break;
                    }
                }
            }
            // 平台下落（许可 + 距离限制）
            if (allowPlatformDrop && IsPlatform(x, y + 1))
            {
                int limit = Math.Min(45, Math.Max(2, maxDropTiles));
                for (int ny = y + 1; ny <= y + limit; ny++)
                {
                    if (IsSolid(x, ny)) break;
                    if (ny > y + 1 && IsStandable(x, ny))
                    {
                        result.Add((x, ny, 1f + 0.2f * (ny - y), NavAction.DropThroughPlatform, 0));
                        break;
                    }
                }
            }
            // 跳跃（垂直走廊清晰 + 水平偏移，Jump 高度编码）
            bool corridorClear = true;
            for (int rise = 1; rise <= caps.MaxRise && corridorClear; rise++)
            {
                int ny = y - rise;
                corridorClear = RowPassable(x, ny - 2);
                if (!corridorClear) break;
                for (int dx = -2; dx <= 2; dx++)
                {
                    int nx = x + dx;
                    if (nx == x && rise == 1) continue;
                    if (!IsStandable(nx, ny)) continue;
                    if (dx != 0 && !BodyFits(x + Math.Sign(dx), ny)) continue;
                    result.Add((nx, ny, 2f + 0.8f * rise + 0.4f * Math.Abs(dx), NavAction.Move, rise));
                }
            }
        }

        private static IEnumerable<int> WalkCandidateRows(int y)
        {
            yield return y;
            yield return y - 1;
            yield return y + 1;
        }

        internal static bool IsSolid(int x, int y)
        {
            if (x < 0 || y < 0 || x >= Main.maxTilesX || y >= Main.maxTilesY) return false;
            var t = Main.tile[x, y];
            if (t == null || !t.HasTile) return false;
            int type = t.TileType;
            // 门（10/11/388/389）、平台（19）、笼子（51/52/382/385/387）可穿过（三分类）
            if (type == 10 || type == 11 || type == 19 || type == 51 || type == 52
                || type == 382 || type == 385 || type == 387 || type == 388 || type == 389) return false;
            return true;
        }

        /// <summary>OneWay 平台（可站立、可按↓穿过）——按生存循环惯例 的 Tile 三分类。</summary>
        internal static bool IsPlatform(int x, int y)
        {
            if (x < 0 || y < 0 || x >= Main.maxTilesX || y >= Main.maxTilesY) return false;
            var t = Main.tile[x, y];
            if (t == null || !t.HasTile) return false;
            return Main.tileSolid[t.TileType] && Main.tileSolidTop[t.TileType];
        }

        private bool TryBridge(int px, int py, int dir)
        {
            int bx = px + dir, by = py + 1;
            if (IsStandable(bx, by)) return false;
            bool placed = false;
            Main.QueueMainThreadAction(() =>
            {
                placed = WorldGen.PlaceTile(bx, by, 0, false, false, -1, 0);
                if (placed) SyncTile(bx, by, 0);   // 联机广播铺路
            });
            for (int i = 0; i < 10 && !placed; i++) Thread.Sleep(20);
            return placed;
        }

        private bool TryStepUp(int px, int py)
        {
            bool placed = false;
            int ty = py + 1;   // 实际垫土的目标 y
            Main.QueueMainThreadAction(() =>
            {
                if (!IsStandable(px, py + 1))
                {
                    placed = WorldGen.PlaceTile(px, py + 1, 0, false, false, -1, 0);
                    ty = py + 1;
                }
                else
                {
                    placed = WorldGen.PlaceTile(px, py - 1, 0, false, false, -1, 0);
                    ty = py - 1;
                }
                if (placed) SyncTile(px, ty, 0);   // 联机广播垫土
            });
            for (int i = 0; i < 10 && !placed; i++) Thread.Sleep(20);
            return placed;
        }

        private bool TryHook(int dir)
        {
            var player = Main.LocalPlayer;
            if (player == null || !HasHook(player)) return false;
            player.controlHook = true;
            Thread.Sleep(400);
            player.controlHook = false;
            return true;
        }

        private static bool HasHook(Player player)
        {
            // 1.4：检测背包是否持有钩爪。无直接 API 时，用 ProjectileID 钩爪列表判断：
            // 钩爪弹幕的 aiStyle 为 7（钩爪），遍历背包检查持有物是否发射该风格弹幕。
            for (int i = 0; i < player.inventory.Length; i++)
            {
                var it = player.inventory[i];
                if (it.shoot > 0)
                {
                    try
                    {
                        var proj = ContentSamples.ProjectilesByType[it.shoot];
                        if (proj != null && proj.aiStyle == 7) return true;
                    }
                    catch { }
                }
            }
            return false;
        }

    

        private void CraftAsync(NetworkStream s, long reqId, Dict cmd)
        {
            // 真实合成：查配方 → 检查材料 → 扣材料 → 生成物品。
            // 之前是 QuickSpawnItem 凭空生成（作弊），不检查/扣除材料。
            // Recipe/背包主线程数据（命令在监听线程执行）
            int id = (int)cmd.GetNum("item_id");
            int amount = (int)(cmd.GetNum("amount") > 0 ? cmd.GetNum("amount") : 1);
            Main.QueueMainThreadAction(() =>
            {
                int crafted = 0;
                try
                {
                    var player = Main.LocalPlayer;
                    if (player == null) { Send(s, new Dict { ["req_id"] = reqId, ["crafted"] = 0 }); return; }
                    // 查配方
                    Recipe recipe = null;
                    for (int i = 0; i < Recipe.numRecipes; i++)
                    {
                        var r = Main.recipe[i];
                        if (r != null && r.createItem != null && r.createItem.type == id)
                        { recipe = r; break; }
                    }
                    if (recipe == null) { Send(s, new Dict { ["req_id"] = reqId, ["crafted"] = 0 }); return; }
                    // 连续合成，每次检查材料够才扣
                    for (int n = 0; n < amount; n++)
                    {
                        bool enough = true;
                        foreach (var ing in recipe.requiredItem)
                        {
                            if (ing == null || ing.type <= 0) continue;
                            if (player.CountItem(ing.type) < ing.stack) { enough = false; break; }
                        }
                        if (!enough) break;
                        // 扣材料：手动遍历背包（不依赖 ConsumeItem 重载，签名在各 tML 版本不同）
                        foreach (var ing in recipe.requiredItem)
                        {
                            if (ing == null || ing.type <= 0) continue;
                            int remaining = ing.stack;
                            for (int slot = 0; slot < player.inventory.Length && remaining > 0; slot++)
                            {
                                var it = player.inventory[slot];
                                if (it == null || it.type != ing.type || it.stack <= 0) continue;
                                int take = Math.Min(remaining, it.stack);
                                it.stack -= take;
                                remaining -= take;
                                if (it.stack <= 0) it.SetDefaults(0);
                            }
                        }
                        var created = new Item();
                        created.SetDefaults(recipe.createItem.type);
                        created.stack = recipe.createItem.stack;
                        player.QuickSpawnItem(Src, created, created.stack);
                        crafted++;
                    }
                }
                catch { }
                Send(s, new Dict { ["req_id"] = reqId, ["crafted"] = crafted });
            });
        }

        private bool Equip(Dict cmd)
        {
            int inv = (int)cmd.GetNum("inv"), equip = (int)cmd.GetNum("equip");
            var player = Main.LocalPlayer;
            if (inv >= 0 && inv < player.inventory.Length)
            {
                var item = player.inventory[inv];
                player.armor[equip] = item.Clone();
                return true;
            }
            return false;
        }

        private bool GiveItem(Dict cmd)
        {
            int id = (int)cmd.GetNum("item_id"), stack = (int)cmd.GetNum("stack");
            var player = Main.LocalPlayer;
            var item = new Item();
            item.SetDefaults(id);
            item.stack = stack;
            player.QuickSpawnItem(Src, item, stack);
            return true;
        }

        private void SendInventory(NetworkStream s, long reqId)
        {
            // 背包主线程数据（命令在监听线程执行）
            Main.QueueMainThreadAction(() => SendInventoryMain(s, reqId));
        }

        private void SendInventoryMain(NetworkStream s, long reqId)
        {
            var player = Main.LocalPlayer ?? Main.player[0];
            var hotbar = new List<Dict>();
            var inv = new List<Dict>();
            if (player == null || !player.active)
            {
                Send(s, new Dict {
                    ["req_id"] = reqId, ["type"] = "inventory",
                    ["hotbar"] = hotbar, ["equipped"] = new List<Dict>(), ["inventory"] = inv,
                    ["selected_slot"] = 0, ["error"] = "player_not_ready",
                });
                Logger.Info($"SendInventory: Main.LocalPlayer 尚未就绪 (null/inactive)，返回空库存");
                return;
            }
            for (int i = 0; i < player.inventory.Length; i++)
            {
                var it = player.inventory[i];
                if (it.type == 0) continue;
                var entry = new Dict {
                    ["id"] = it.type, ["stack"] = it.stack, ["inv_slot"] = i,
                    ["name"] = it.Name, ["defense"] = it.defense,
                    // 工具/武器属性：use(melee/ranged/magic/tool…) + 伤害/镐力/斧力
                    ["use"] = ItemUse(it),
                    ["damage"] = it.damage,
                    ["pick"] = it.pick,
                    ["axe"] = it.axe,
                };
                if (i >= 0 && i < 10) hotbar.Add(entry);
                else if (i >= 10 && i < 50) inv.Add(entry);
            }
            var equipped = new List<Dict>();
            for (int a = 0; a < player.armor.Length; a++)
            {
                var it = player.armor[a];
                if (it.type == 0) continue;
                equipped.Add(new Dict {
                    ["id"] = it.type, ["stack"] = it.stack, ["armor_slot"] = a,
                    ["name"] = it.Name, ["defense"] = it.defense,
                });
            }
            Send(s, new Dict {
                ["req_id"] = reqId, ["type"] = "inventory",
                ["hotbar"] = hotbar, ["equipped"] = equipped, ["inventory"] = inv,
                ["selected_slot"] = player.selectedItem,
            });
        }

        private bool DropItem(Dict cmd)
        {
            int slot = (int)cmd.GetNum("slot");
            int stack = (int)(cmd.GetNum("stack") > 0 ? cmd.GetNum("stack") : 1);
            var player = Main.LocalPlayer;
            if (slot < 0 || slot >= player.inventory.Length) return false;
            var item = player.inventory[slot];
            if (item.type == 0) return false;
            var drop = item.Clone();
            drop.stack = Math.Min(stack, item.stack);
            player.inventory[slot].stack -= drop.stack;
            if (player.inventory[slot].stack <= 0)
                player.inventory[slot].SetDefaults(0);
            Item d = drop;
            player.TryDroppingSingleItem(Src, d);
            return true;
        }

        /// <summary>收集半径内地面掉落物（把物品移到玩家身上，联机广播）。
        /// 返回拾取数量；radius 为像素半径（与 Python 端语义一致）。</summary>
        private void RunOnMainCollect(NetworkStream s, long reqId, Dict cmd)
        {
            Main.QueueMainThreadAction(() =>
            {
                try
                {
                    int n = CollectItems(cmd);
                    Send(s, new Dict { ["req_id"] = reqId, ["ok"] = true, ["collected"] = n });
                }
                catch
                {
                    Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["collected"] = 0 });
                }
            });
        }

        private int CollectItems(Dict cmd)
        {
            int radius = (int)(cmd.GetNum("radius") > 0 ? cmd.GetNum("radius") : 600);
            var player = Main.LocalPlayer;
            if (player == null) return 0;
            float px = player.Center.X, py = player.Center.Y;
            float rr = radius;   // 像素半径（与轮子项目一致）
            int collected = 0;
            for (int i = 0; i < Main.item.Length; i++)
            {
                var it = Main.item[i];
                if (it == null || !it.active || it.type <= 0) continue;
                float dx = it.Center.X - px, dy = it.Center.Y - py;
                if (dx * dx + dy * dy > rr * rr) continue;
                // 让物品进背包（玩家靠近自动拾取）
                try
                {
                    if (player.CanAcceptItemIntoInventory(it))
                    {
                        player.GetItem(player.whoAmI, it, GetItemSettings.InventoryEntityToPlayerInventorySettings);
                        collected++;
                    }
                }
                catch { }
                it.active = false;
                if (Main.netMode == NetmodeID.MultiplayerClient)
                    NetMessage.SendData(MessageID.SyncItem, -1, -1, null, i, 0f, 0f, 0f, 0, 0, 0);
            }
            Logger.Info($"[CollectItems] radius={radius} collected={collected}");
            return collected;
        }

        /// <summary>原生物品挖掘（mod 原生能力）：自动选镐子 → PreItemCheck 修正光标 →
        /// controlUseItem 持续挖（工具动画/消耗/属性）。不可挖 tile 返回 false。</summary>
        private bool DigTile(Dict cmd)
        {
            int x = (int)cmd.GetNum("x"), y = (int)cmd.GetNum("y");
            var p = Main.LocalPlayer;
            if (p == null) return false;
            var ctrl = p.GetModPlayer<NekoControlPlayer>();
            if (ctrl == null) return false;
            if (!TrySetDigTarget(x, y)) return false;
            if (!InReach(x, y, 8)) return false;   // 太远：人物没走过去就不许挖
            // 找背包里镐力最强的镐子
            int pickSlot = -1, pickPower = 0;
            for (int i = 0; i < p.inventory.Length; i++)
            {
                var it = p.inventory[i];
                if (it == null || it.type <= 0) continue;
                if (it.pick > pickPower) { pickSlot = i; pickPower = it.pick; }
            }
            if (pickSlot < 0) return false;
            ctrl.useSlot = pickSlot;
            ctrl.digTargetX = x;
            ctrl.digTargetY = y;
            ctrl.useTicks = 40;   // 持续挖掘约 0.7s
            return true;
        }

        /// <summary>tile 可挖性检查（Bridge TrySetDigTarget 简化）：树/箱子/祭坛/仙人掌等
        /// 锚点类 tile 不可挖（挖了也不掉落/卡动画）。</summary>
        private static bool TrySetDigTarget(int x, int y)
        {
            if (x < 0 || y < 0 || x >= Main.maxTilesX || y >= Main.maxTilesY) return false;
            var t = Main.tile[x, y];
            if (t == null || !t.HasTile) return false;
            int type = t.TileType;
            if (type == 5 || type == 21 || type == 26 || type == 80) return false;
            return true;
        }

        private bool UseItemSlot(Dict cmd)
        {
            int slot = (int)cmd.GetNum("slot");
            var player = Main.LocalPlayer;
            if (player == null || slot < 0 || slot >= player.inventory.Length) return false;
            if (player.inventory[slot].type == 0) return false;
            var ctrl = player.GetModPlayer<NekoControlPlayer>();
            if (ctrl == null) return false;
            ctrl.useSlot = slot;
            ctrl.useTicks = 30;   // 持续使用约 0.5s
            return true;
        }

        private void SendChests(NetworkStream s, long reqId)
        {
            // 监听线程可能拿到 null/未就绪的 LocalPlayer（入服瞬间），必须兜底
            var player = Main.LocalPlayer;
            if (player == null || !player.active)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["type"] = "chests", ["chests"] = new List<Dict>() });
                return;
            }
            var list = new List<Dict>();
            int px = (int)(player.Center.X / 16), py = (int)(player.Center.Y / 16);
            for (int i = 0; i < Main.chest.Length; i++)
            {
                var c = Main.chest[i];
                if (c == null || c.x <= 0 || c.y <= 0) continue;
                var contents = new List<Dict>();
                for (int k = 0; k < c.item.Length; k++)
                {
                    var it = c.item[k];
                    if (it == null || it.type == 0) continue;
                    contents.Add(new Dict { ["id"] = it.type, ["name"] = it.Name, ["stack"] = it.stack });
                }
                int dist = Math.Abs(c.x - px) + Math.Abs(c.y - py);
                list.Add(new Dict { ["index"] = i, ["x"] = c.x, ["y"] = c.y, ["dist"] = dist, ["items"] = contents });
            }
            Send(s, new Dict { ["req_id"] = reqId, ["type"] = "chests", ["chests"] = list });
        }

        private int OpenChestNear(int x, int y)
        {
            var player = Main.LocalPlayer;
            int px = (int)(player.Center.X / 16), py = (int)(player.Center.Y / 16);
            if (Math.Abs(px - x) > 4 || Math.Abs(py - y) > 4) return -1;
            int idx = Chest.FindChest(x, y);
            if (idx < 0) return -1;
            player.chest = idx;
            return idx;
        }

        private bool StoreItem(Dict cmd)
        {
            // 直接操作箱子物品槽（最稳，不依赖 Chest 的 put API 重载差异）
            int x = (int)cmd.GetNum("x"), y = (int)cmd.GetNum("y");
            int slot = (int)cmd.GetNum("slot");
            int stack = (int)(cmd.GetNum("stack") > 0 ? cmd.GetNum("stack") : 1);
            var player = Main.LocalPlayer;
            int idx = OpenChestNear(x, y);
            if (idx < 0) return false;
            try
            {
                var src = player.inventory[slot];
                if (src.type == 0) return false;
                var chest = Main.chest[idx];
                for (int k = 0; k < chest.item.Length; k++)
                {
                    var ci = chest.item[k];
                    if (ci.type == 0)
                    {
                        var nw = src.Clone();
                        nw.stack = Math.Min(stack, src.stack);
                        chest.item[k] = nw;
                        SyncChest(idx, k);
                        return true;
                    }
                    if (ci.type == src.type && ci.stack < ci.maxStack)
                    {
                        int add = Math.Min(stack, Math.Min(src.stack, ci.maxStack - ci.stack));
                        ci.stack += add;
                        src.stack -= add;
                        if (src.stack <= 0) src.SetDefaults(0);
                        SyncChest(idx, k);
                        return true;
                    }
                }
                return false;
            }
            finally { player.chest = -1; }
        }

        private bool TakeFromChest(Dict cmd)
        {
            int x = (int)cmd.GetNum("x"), y = (int)cmd.GetNum("y");
            int id = (int)cmd.GetNum("item_id");
            int stack = (int)(cmd.GetNum("stack") > 0 ? cmd.GetNum("stack") : 1);
            var player = Main.LocalPlayer;
            int idx = OpenChestNear(x, y);
            if (idx < 0) return false;
            try
            {
                var chest = Main.chest[idx];
                for (int k = 0; k < chest.item.Length; k++)
                {
                    var it = chest.item[k];
                    if (it.type != id || it.stack <= 0) continue;
                    int take = Math.Min(stack, it.stack);
                    var got = it.Clone(); got.stack = take;
                    player.QuickSpawnItem(Src, got, take);
                    it.stack -= take;
                    if (it.stack <= 0) it.SetDefaults(0);
                    SyncChest(idx, k);
                    return true;
                }
                return false;
            }
            finally { player.chest = -1; }
        }

        private List<string> NearbyStations(Player player)
        {
            var names = new List<string>();
            if (player == null) return names;
            int px = (int)(player.Center.X / 16), py = (int)(player.Center.Y / 16);
            const int R = 6;
            var seen = new HashSet<int>();
            for (int x = px - R; x <= px + R; x++)
            {
                for (int y = py - R; y <= py + R; y++)
                {
                    if (x < 0 || y < 0 || x >= Main.maxTilesX || y >= Main.maxTilesY) continue;
                    var t = Main.tile[x, y];
                    if (t == null || !t.HasTile) continue;
                    int type = t.TileType;
                    if (seen.Contains(type)) continue;
                    seen.Add(type);
                    if (Main.tileTable[type] || type == TileID.WorkBenches || type == TileID.Furnaces
                        || type == TileID.Anvils || type == TileID.MythrilAnvil || type == TileID.AdamantiteForge
                        || type == TileID.Hellforge || type == TileID.Bottles || type == TileID.CookingPots
                        || type == TileID.Loom || type == TileID.Sawmill || type == TileID.TinkerersWorkbench
                        || type == TileID.AlchemyTable)
                    {
                        names.Add(TileName(type));
                    }
                }
            }
            return names;
        }

        private void SendRecipes(NetworkStream s, long reqId, string cat)
        {
            // Main.recipe/玩家背包只能主线程读——后台线程访问会卡死主线程
            Main.QueueMainThreadAction(() =>
            {
                try
                {
                    bool onlyAvailable = cat == "available";
                    var list = new List<Dict>();
                    for (int i = 0; i < Recipe.numRecipes; i++)
                    {
                        Recipe r = Main.recipe[i];
                        if (r == null || r.createItem == null || r.createItem.type <= 0) continue;
                        if (onlyAvailable && !RecipeAvailable(r)) continue;

                        var mats = new List<Dict>();
                        foreach (var ing in r.requiredItem)
                        {
                            if (ing == null || ing.type <= 0 || ing.stack <= 0) continue;
                            mats.Add(new Dict { ["id"] = ing.type, ["name"] = ing.Name, ["stack"] = ing.stack });
                        }
                        if (mats.Count == 0) continue;

                        var stations = new List<Dict>();
                        foreach (var tile in r.requiredTile)
                        {
                            if (tile <= 0) continue;
                            stations.Add(new Dict { ["tile"] = tile, ["name"] = TileName(tile) });
                        }

                        var createItem = r.createItem;
                        string modName = createItem.ModItem == null ? "Terraria" : createItem.ModItem.Mod.Name;

                        list.Add(new Dict {
                            ["item_id"] = createItem.type, ["name"] = createItem.Name,
                            ["amount"] = createItem.stack, ["mod"] = modName,
                            ["available"] = RecipeAvailable(r),
                            ["materials"] = mats, ["stations"] = stations,
                            // v0.5: 物品属性（升级引擎比较用）——武器伤害/镐力/斧力/防御
                            ["damage"] = createItem.damage,
                            ["pick"] = createItem.pick,
                            ["axe"] = createItem.axe,
                            ["defense"] = createItem.defense,
                        });
                    }
                    Send(s, new Dict { ["req_id"] = reqId, ["type"] = "recipes", ["recipes"] = list });
                }
                catch (Exception ex)
                {
                    Logger.Error($"[Recipes] 生成配方列表异常: {ex.GetType().Name}: {ex.Message}");
                    Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "recipe_exception" });
                }
            });
        }

        private static bool RecipeAvailable(Recipe r)
        {
            var player = Main.LocalPlayer;
            if (player == null) return false;
            foreach (var ing in r.requiredItem)
            {
                if (ing == null || ing.type <= 0 || ing.stack <= 0) continue;
                if (player.CountItem(ing.type) < ing.stack) return false;
            }
            return true;
        }

        private string TileName(int tile)
        {
            try
            {
                var mt = TileLoader.GetTile(tile);
                if (mt != null) return mt.Name;
            }
            catch { }
            switch (tile)
            {
                case TileID.WorkBenches: return "Work Bench";
                case TileID.Furnaces: return "Furnace";
                case TileID.Anvils: return "Iron Anvil";
                case TileID.MythrilAnvil: return "Mythril Anvil";
                case TileID.AdamantiteForge: return "Adamantite Forge";
                case TileID.Hellforge: return "Hellforge";
                case TileID.Bottles: return "Bottle";
                case TileID.Tables: return "Table";
                case TileID.CookingPots: return "Cooking Pot";
                case TileID.Loom: return "Loom";
                case TileID.Sawmill: return "Sawmill";
                case TileID.TinkerersWorkbench: return "Tinkerer's Workshop";
                case TileID.AlchemyTable: return "Alchemy Table";
                default: return "Tile" + tile;
            }
        }

        private Player FindTrackedPlayer(string playerName)
        {
            if (!string.IsNullOrWhiteSpace(playerName))
            {
                // 统计在线玩家
                int onlineCount = 0;
                string onlineNames = "";

                // 遍历所有玩家槽位，寻找匹配的在线玩家
                for (int i = 0; i < Main.maxPlayers; i++)
                {
                    if (i >= Main.player.Length) break; // 防止数组越界
                    var candidate = Main.player[i];
                    if (candidate != null && candidate.active)
                    {
                        onlineCount++;
                        if (onlineNames.Length > 0) onlineNames += ", ";
                        onlineNames += candidate.name;

                        if (string.Equals(candidate.name, playerName,
                            StringComparison.OrdinalIgnoreCase))
                        {
                            ModContent.GetInstance<NekoTerrariaLink>().Logger.Debug(
                                $"FindTrackedPlayer: 找到玩家 '{playerName}' 在槽位 {i}");
                            return candidate;
                        }
                    }
                }
                // 未找到玩家
                ModContent.GetInstance<NekoTerrariaLink>().Logger.Debug(
                    $"FindTrackedPlayer: 未找到玩家 '{playerName}'，当前在线 {onlineCount} 人: {onlineNames}");
            }
            return Main.LocalPlayer ?? Main.player[0];
        }

        private void SendItemRegistry(NetworkStream s, long reqId)
        {
            // Item.SetDefaults 只能主线程调用——后台线程生成会卡死主线程
            Main.QueueMainThreadAction(() =>
            {
                try
                {
                    var byMod = new Dictionary<string, List<Dict>>();
                    for (int i = 0; i < ItemLoader.ItemCount; i++)
                    {
                        var modItem = ItemLoader.GetItem(i);
                        if (modItem == null || modItem.Name == null || modItem.Name.Length == 0) continue;
                        string modName = modItem.Mod == null ? "Terraria" : modItem.Mod.Name;
                        var itemInst = new Item();
                        itemInst.SetDefaults(i);
                        if (!byMod.ContainsKey(modName))
                            byMod[modName] = new List<Dict>();
                        byMod[modName].Add(new Dict {
                            ["id"] = i, ["name"] = modItem.Name, ["use"] = ItemUse(itemInst), ["tags"] = ItemTags(itemInst),
                        });
                    }
                    var mods = new List<Dict>();
                    foreach (var kv in byMod)
                        mods.Add(new Dict { ["mod"] = kv.Key, ["count"] = kv.Value.Count, ["items"] = kv.Value });
                    Send(s, new Dict { ["req_id"] = reqId, ["type"] = "item_registry", ["mods"] = mods });
                }
                catch (Exception ex)
                {
                    Logger.Error($"[Registry] 生成物品表异常: {ex.GetType().Name}: {ex.Message}");
                    Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["reason"] = "registry_exception" });
                }
            });
        }

        private string ItemUse(Item item)
        {
            if (item.healLife > 0) return "heal";
            if (item.healMana > 0) return "mana";
            if (item.buffType > 0 && item.consumable) return "buff";
            if (item.CountsAsClass(DamageClass.Summon)) return "summon";
            if (item.damage > 0 && !item.accessory && item.ammo == 0)
                return item.mana > 0 ? "magic" : (item.CountsAsClass(DamageClass.Ranged) ? "ranged" : "melee");
            if (item.defense > 0 || item.headSlot > 0 || item.bodySlot > 0 || item.legSlot > 0) return "armor";
            if (item.accessory) return "accessory";
            if (item.consumable) return "potion";
            if (item.pick > 0 || item.axe > 0 || item.hammer > 0) return "tool";
            if (item.createTile > 0 || item.createWall > 0) return "placeable";
            if (item.material) return "material";
            return "misc";
        }

        private List<string> ItemTags(Item item)
        {
            var tags = new List<string>();
            if (item.healLife > 0) tags.Add("heal");
            if (item.healMana > 0) tags.Add("mana");
            if (item.buffType > 0) tags.Add("buff");
            if (item.CountsAsClass(DamageClass.Summon)) tags.Add("summon");
            if (item.pick > 0) tags.Add("pickaxe");
            if (item.axe > 0) tags.Add("axe");
            if (item.hammer > 0) tags.Add("hammer");
            if (item.defense > 0 || item.headSlot > 0 || item.bodySlot > 0 || item.legSlot > 0) tags.Add("armor");
            if (item.accessory) tags.Add("accessory");
            if (item.material) tags.Add("material");
            if (tags.Count == 0) tags.Add("misc");
            return tags;
        }

    

        private void SendCapabilities(NetworkStream s, long reqId)
        {
            var player = Main.LocalPlayer;
            bool hasHook = player != null && HasHook(player);
            int dirtCount = 0, hasPick = 0, pickPower = 0, rope = 0, hasAxe = 0, hasRod = 0;
            if (player != null)
            {
                for (int i = 0; i < player.inventory.Length; i++)
                {
                    var it = player.inventory[i];
                    if (it.type == 0) continue;
                    if (it.type == 0 || it.type == 1) dirtCount += it.stack;
                    if (it.pick > 0) { hasPick = 1; pickPower = Math.Max(pickPower, it.pick); }
                    if (it.axe > 0) hasAxe = 1;
                    if (it.fishingPole > 0 || it.Name.Contains("钓竿") || it.Name.Contains("鱼竿")) hasRod = 1;
                    if (it.createTile == 65 || it.createTile == 415) rope += it.stack;
                }
            }
            Send(s, new Dict {
                ["req_id"] = reqId, ["type"] = "capabilities",
                ["has_hook"] = hasHook, ["hook_range"] = hasHook ? 22 : 0,
                ["dirt_count"] = dirtCount, ["has_pickaxe"] = hasPick == 1,
                ["pickaxe_power"] = pickPower, ["rope_count"] = rope,
                ["has_axe"] = hasAxe == 1, ["has_rod"] = hasRod == 1,
                ["nearby_stations"] = NearbyStations(player),
            });
        }

        /// <summary>矿石 tile 类型表（Main.tileOre 在 1.4.5 不存在，用显式列表； find_trees 同款扫描）。</summary>
        private static readonly int[] OreTileTypes = {
            TileID.Copper, TileID.Tin, TileID.Iron, TileID.Lead, TileID.Silver, TileID.Tungsten,
            TileID.Gold, TileID.Platinum, TileID.Meteorite, TileID.Demonite, TileID.Crimtane,
            TileID.Obsidian, TileID.Hellstone, TileID.Cobalt, TileID.Palladium, TileID.Mythril,
            TileID.Orichalcum, TileID.Adamantite, TileID.Titanium, TileID.Chlorophyte,
        };

        /// <summary>扫描附近矿石（参照  find_trees）：返回最近 10 个矿坐标，
        /// tile_type&gt;0 时只返回该类型（铁矿石 tile 类型 = 铁矿物品 id，Python 直接匹配）。</summary>
        private void SendOrePositions(NetworkStream s, long reqId, Dict cmd)
        {
            var p = Main.LocalPlayer;
            if (p == null)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["type"] = "ore_positions", ["ores"] = new List<Dict>() });
                return;
            }
            int cx = (int)(p.Center.X / 16f), cy = (int)(p.Bottom.Y / 16f);
            int radius = (int)(cmd.GetNum("radius") > 0 ? cmd.GetNum("radius") : 30);
            int wantType = (int)cmd.GetNum("tile_type");
            var ores = new List<Dict>();
            for (int y = cy - 20; y <= cy + 40; y++)
            {
                if (y < 0 || y >= Main.maxTilesY) continue;
                for (int x = cx - radius; x <= cx + radius; x++)
                {
                    if (x < 0 || x >= Main.maxTilesX) continue;
                    var t = Main.tile[x, y];
                    if (t == null || !t.HasTile) continue;
                    int type = t.TileType;
                    if (Array.IndexOf(OreTileTypes, type) < 0) continue;
                    if (wantType > 0 && type != wantType) continue;
                    ores.Add(new Dict { ["x"] = x, ["y"] = y, ["type"] = type,
                        ["dist"] = Math.Abs(x - cx) + Math.Abs(y - cy) });
                }
            }
            ores.Sort((a, b) => (int)a.GetNum("dist").CompareTo((int)b.GetNum("dist")));
            if (ores.Count > 10) ores = ores.GetRange(0, 10);
            Send(s, new Dict { ["req_id"] = reqId, ["type"] = "ore_positions", ["ores"] = ores });
        }

        /// <summary>扫描附近树木（ find_trees 同款）：返回最近树的树根坐标。</summary>
        private void SendTreePositions(NetworkStream s, long reqId, Dict cmd)
        {
            var p = Main.LocalPlayer;
            if (p == null)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["type"] = "tree_positions", ["trees"] = new List<Dict>() });
                return;
            }
            int cx = (int)(p.Center.X / 16f), cy = (int)(p.Center.Y / 16f);
            int radius = (int)(cmd.GetNum("radius") > 0 ? cmd.GetNum("radius") : 30);
            var trees = new List<Dict>();
            var seenX = new HashSet<int>();
            for (int x = cx - radius; x <= cx + radius; x++)
            {
                if (x < 0 || x >= Main.maxTilesX || seenX.Contains(x)) continue;
                int baseY = -1;
                for (int y = cy - 35; y <= cy + 10; y++)
                {
                    if (y < 0 || y >= Main.maxTilesY) continue;
                    var t = Main.tile[x, y];
                    if (t != null && t.HasTile && (t.TileType == TileID.Trees
                        || t.TileType == TileID.PalmTree
                        || t.TileType == TileID.Cactus))
                        baseY = y;
                }
                if (baseY >= 0)
                {
                    trees.Add(new Dict { ["x"] = x, ["y"] = baseY,
                        ["dist"] = Math.Abs(x - cx) });
                    seenX.Add(x);
                }
            }
            trees.Sort((a, b) => (int)a.GetNum("dist").CompareTo((int)b.GetNum("dist")));
            if (trees.Count > 8) trees = trees.GetRange(0, 8);
            Send(s, new Dict { ["req_id"] = reqId, ["type"] = "tree_positions", ["trees"] = trees });
        }

        /// <summary>砍掉指定的树（整列 kill，含树干/树叶）。返回是否砍到。树类型：树/棕榈/仙人掌。</summary>
        private bool ChopTrees(Dict cmd)
        {
            var p = Main.LocalPlayer;
            if (p == null) return false;
            int tx = (int)cmd.GetNum("x"), ty = (int)cmd.GetNum("y");
            if (!InReach(tx, ty, 10)) return false;   // 太远：人物没走过去不许砍（防远程假完成）
            int count = 0;
            for (int dy = -25; dy <= 2; dy++)
            {
                int y = ty + dy;
                if (y < 0 || y >= Main.maxTilesY) continue;
                var t = Main.tile[tx, y];
                if (t != null && t.HasTile && (t.TileType == TileID.Trees
                    || t.TileType == TileID.PalmTree
                    || t.TileType == TileID.Cactus
                    || t.TileType == TileID.PalmTree  // 棕榈
                    || t.TileType == TileID.Cactus))
                {
                    WorldGen.KillTile(tx, y, false, false, true);
                    SyncTile(tx, y, 1);
                    count++;
                }
            }
            return count > 0;
        }

        /// <summary>扫描附近的水域（钓鱼用）：返回水面格坐标（有液体水 + 上方空气）。</summary>
        private void SendWaterPositions(NetworkStream s, long reqId, Dict cmd)
        {
            var p = Main.LocalPlayer;
            if (p == null)
            {
                Send(s, new Dict { ["req_id"] = reqId, ["type"] = "water_positions", ["water"] = new List<Dict>() });
                return;
            }
            int cx = (int)(p.Center.X / 16f), cy = (int)(p.Center.Y / 16f);
            int radius = (int)(cmd.GetNum("radius") > 0 ? cmd.GetNum("radius") : 30);
            var water = new List<Dict>();
            var seen = new HashSet<(int, int)>();
            for (int y = cy - 20; y <= cy + 20; y++)
            {
                for (int x = cx - radius; x <= cx + radius; x++)
                {
                    if (x < 0 || y < 0 || x >= Main.maxTilesX || y >= Main.maxTilesY) continue;
                    var t = Main.tile[x, y];
                    if (t == null) continue;
                    // 有水且上方是空气（水面）
                    if (t.LiquidAmount > 50 && t.LiquidType == LiquidID.Water)
                    {
                        var above = Main.tile[x, y - 1];
                        if (above != null && !above.HasTile)
                        {
                            // 粗略去重（相邻水面格）
                            var key = (x / 4, y / 4);
                            if (seen.Add(key))
                            {
                                water.Add(new Dict { ["x"] = x, ["y"] = y,
                                    ["dist"] = Math.Abs(x - cx) + Math.Abs(y - cy) });
                            }
                        }
                    }
                }
            }
            water.Sort((a, b) => (int)a.GetNum("dist").CompareTo((int)b.GetNum("dist")));
            if (water.Count > 10) water = water.GetRange(0, 10);
            Send(s, new Dict { ["req_id"] = reqId, ["type"] = "water_positions", ["water"] = water });
        }

        private void SendLedges(NetworkStream s, long reqId, Dict cmd)
        {
            int x0 = (int)cmd.GetNum("x0"), y0 = (int)cmd.GetNum("y0");
            int x1 = (int)cmd.GetNum("x1"), y1 = (int)cmd.GetNum("y1");
            int loY = Math.Min(y0, y1), hiY = Math.Max(y0, y1);
            int loX = Math.Max(0, Math.Min(x0, x1) - 40);
            int hiX = Math.Min(Main.maxTilesX - 1, Math.Max(x0, x1) + 40);
            var found = new List<Dict>();
            var foundY = new List<int>();
            for (int y = hiY - 1; y >= loY && found.Count < 24; y--)
            {
                int bestX = -1, bestDist = int.MaxValue;
                for (int x = loX; x <= hiX; x++)
                {
                    if (!IsStandable(x, y)) continue;
                    if (IsSolid(x, y - 1) || IsSolid(x, y - 2)) continue;
                    int d = Math.Abs(x - x0);
                    if (d < bestDist) { bestDist = d; bestX = x; }
                }
                if (bestX < 0) continue;
                bool tooClose = false;
                foreach (int py in foundY)
                    if (Math.Abs(py - (y - 1)) < 6) { tooClose = true; break; }
                if (tooClose) continue;
                found.Add(new Dict { ["x"] = bestX, ["y"] = y - 1 });
                foundY.Add(y - 1);
            }
            Send(s, new Dict { ["req_id"] = reqId, ["type"] = "ledges", ["points"] = found });
        }

        private void SendServerInfo(NetworkStream s, long reqId)
        {
            // 构建 tModLoader 版本字符串 (Packet 1 格式: tModLoader.v{全版本}!{短版本})
            // BuildInfo.tMLVersion 在此 tModLoader 版本中返回 System.Version 对象，需 ToString()
            string tmlVer = "";
            try { tmlVer = BuildInfo.tMLVersion?.ToString() ?? ""; } catch { }
            if (string.IsNullOrEmpty(tmlVer)) tmlVer = "2026.6.3.0";
            string shortVer = tmlVer;
            int lastDot = tmlVer.LastIndexOf('.');
            if (lastDot > 0) shortVer = tmlVer.Substring(0, lastDot);
            string versionStr = $"tModLoader.v{tmlVer}!{shortVer}";

            // 世界难度 (0=Classic, 1=Expert, 2=Master, 3=Journey)
            int gameMode = Main.GameMode;

            // 世界大小
            string worldSize = "Small";
            if (Main.maxTilesX >= 8400) worldSize = "Large";
            else if (Main.maxTilesX >= 6300) worldSize = "Medium";

            // 邪恶类型
            string evilType = WorldGen.crimson ? "Crimson" : "Corruption";

            // 世界名称
            string worldName = Main.worldName ?? "";

            Send(s, new Dict {
                ["req_id"] = reqId, ["type"] = "server_info",
                ["tmod_version_str"] = versionStr,
                ["tmod_version"] = tmlVer,
                ["terraria_version"] = Main.versionNumber ?? "",
                ["game_mode"] = gameMode,
                ["world_size"] = worldSize,
                ["evil_type"] = evilType,
                ["world_name"] = worldName,
            });
        }

        private void SendNetworkInfo(NetworkStream s, long reqId)
        {
            // 当前玩家的网络状态：单机/客户端/服务器
            int netMode = Main.netMode;  // 0=单机, 1=客户端, 2=服务器(Host & Play 或专用服务器)
            string modeLabel = netMode switch
            {
                0 => "single",
                1 => "client",
                2 => "server",
                _ => "unknown"
            };

            Send(s, new Dict {
                ["req_id"] = reqId, ["type"] = "network_info",
                ["net_mode"] = netMode,
                ["mode_label"] = modeLabel,
                ["is_hosting"] = netMode == 2,
                ["world_name"] = Main.worldName ?? "",
                ["server_ip"] = Netplay.ServerIP?.ToString() ?? "",
                ["server_port"] = Netplay.ListenPort,
            });
        }

        private void SendJoinStatus(NetworkStream s, long reqId)
        {
            // 有头客户端 join 进度查询：用 netMode 判断连接状态
            bool inWorld = Main.LocalPlayer != null && Main.LocalPlayer.active;
            bool connected = Main.netMode == 1 || Main.netMode == 2;
            bool joined = inWorld && connected;

            string phase = "idle";
            if (_joinPending) phase = "connecting";
            else if (joined) phase = "in_world";
            else if (Main.menuMode == 10 || Main.menuMode == 15) phase = "loading";

            Send(s, new Dict {
                ["req_id"] = reqId, ["type"] = "join_status",
                ["phase"] = phase,
                ["net_mode"] = Main.netMode,
                ["in_world"] = inWorld,
                ["connected"] = connected,
                ["joined"] = joined,
                ["pending"] = _joinPending,
                ["timeout"] = _joinTimeout,
                ["menu_mode"] = Main.menuMode,
                ["target_host"] = _joinHost,
                ["target_port"] = _joinPort,
                ["elapsed_ms"] = _joinPending ? (int)(DateTime.Now - _joinStart).TotalMilliseconds : 0,
            });
        }

        private void SendState(NetworkStream s, long reqId, string playerName)
        {
            // Main.player/Main.npc 主线程数据（命令在监听线程执行）
            Main.QueueMainThreadAction(() => SendStateMain(s, reqId, playerName));
        }

        private void SendStateMain(NetworkStream s, long reqId, string playerName)
        {
            var p = FindTrackedPlayer(playerName);
            if (p == null)
            {
                ModContent.GetInstance<NekoTerrariaLink>().Logger.Warn(
                    $"SendState: 玩家 '{playerName}' 不在线，无法返回状态");
                Send(s, new Dict {
                    ["req_id"] = reqId, ["type"] = "state", ["found"] = false,
                });
                return;
            }
            // 诊断：主线程实际读到的玩家状态（排查 hp=0 / 未入服）
            Logger.Info($"SendState: playerName='{playerName}' 玩家 '{p.name}' active={p.active} "
                + $"hp={p.statLife}/{p.statLifeMax} netMode={Main.netMode} "
                + $"pos=({p.position.X:F0},{p.position.Y:F0}) myPlayer={Main.myPlayer}");
            var npcs = new List<Dict>();
            for (int i = 0; i < Main.npc.Length; i++)
            {
                var npc = Main.npc[i];
                if (npc.active && npc.life > 0)
                {
                    int dx = (int)(npc.Center.X / 16 - p.Center.X / 16);
                    int dy = (int)(npc.Center.Y / 16 - p.Center.Y / 16);
                    if (Math.Abs(dx) < 50 && Math.Abs(dy) < 50)
                        npcs.Add(new Dict {
                            ["name"] = npc.TypeName, ["slot"] = i, ["life"] = npc.life,
                            ["tileX"] = (int)(npc.Center.X / 16), ["tileY"] = (int)(npc.Center.Y / 16),
                            ["damage"] = npc.damage,
                        });
                }
            }
            var players = new List<Dict>();
            for (int i = 0; i < Main.player.Length; i++)
            {
                var pl = Main.player[i];
                if (pl == null || !pl.active || pl == p) continue;
                int plTx = (int)(pl.Center.X / 16);
                int plTy = (int)(pl.Center.Y / 16);
                if (plTx == 0 && plTy == 0) continue;  // 残留槽位无真实位置
                int dx = plTx - (int)(p.Center.X / 16);
                int dy = plTy - (int)(p.Center.Y / 16);
                if (Math.Abs(dx) < 300 && Math.Abs(dy) < 300)
                    players.Add(new Dict {
                        ["name"] = pl.name, ["tileX"] = plTx, ["tileY"] = plTy,
                        ["hp"] = pl.statLife, ["max_life"] = pl.statLifeMax,
                        ["velocityX"] = pl.velocity.X, ["velocityY"] = pl.velocity.Y,
                    });
            }
            // 活动 buff 名称（身体感：猫娘知道自己中了什么状态）
            var buffs = new List<string>();
            for (int i = 0; i < p.buffType.Length; i++)
            {
                if (p.buffType[i] > 0 && p.buffTime[i] > 0)
                {
                    try { buffs.Add(BuffID.Search.GetName(p.buffType[i])); }
                    catch { }
                }
            }
            // 亮度采样（3x3，情感素材：暗→害怕、亮→安心）
            int ptx = (int)(p.Center.X / 16f), pty = (int)(p.Center.Y / 16f);
            float brightness = 0f; int samples = 0;
            for (int bx = ptx - 1; bx <= ptx + 1; bx++)
                for (int by = pty - 1; by <= pty + 1; by++)
                    if (bx >= 0 && by >= 0 && bx < Main.maxTilesX && by < Main.maxTilesY)
                    {
                        var col = Lighting.GetColor(bx, by);
                        brightness += (col.R + col.G + col.B) / 765f;
                        samples++;
                    }
            if (samples > 0) brightness /= samples;

            Send(s, new Dict {
                ["req_id"] = reqId, ["type"] = "state",
                ["player"] = new Dict {
                    ["name"] = p.name, ["hp"] = p.statLife, ["maxLife"] = p.statLifeMax,
                    ["mana"] = p.statMana, ["maxMana"] = p.statManaMax,
                    ["x"] = p.position.X, ["y"] = p.position.Y,
                    ["tileX"] = (int)(p.Center.X / 16), ["tileY"] = (int)(p.Center.Y / 16),
                    ["velocityX"] = p.velocity.X, ["velocityY"] = p.velocity.Y,
                    ["grounded"] = p.velocity.Y == 0, ["selectedItem"] = p.selectedItem,
                    ["biome"] = BiomeName(p), ["buffs"] = buffs,
                    ["movement_state"] = MovementState(p),
                    ["brightness"] = Math.Round(brightness, 2),
                },
                ["nearbyNpcs"] = npcs, ["nearbyPlayers"] = players,
                ["time"] = new Dict { ["dayTime"] = Main.dayTime, ["time"] = Main.time },
            });
        }

        /// <summary>当前生物群系（第一人称身体感：猫娘知道自己在哪）。</summary>
        private static string BiomeName(Player p)
        {
            if (p.ZoneDungeon) return "地牢";
            if (p.ZoneUnderworldHeight) return "地狱";
            if (p.ZoneSkyHeight) return "天空";
            if (p.ZoneJungle) return "丛林";
            if (p.ZoneSnow) return "雪地";
            if (p.ZoneCorrupt) return "腐化之地";
            if (p.ZoneCrimson) return "猩红之地";
            if (p.ZoneHallow) return "神圣之地";
            if (p.ZoneDesert || p.ZoneUndergroundDesert) return "沙漠";
            if (p.ZoneGlowshroom) return "发光蘑菇地";
            if (p.ZoneBeach) return "海滩";
            if (p.ZoneMeteor) return "陨石坑";
            if (p.ZoneRockLayerHeight) return "洞穴层";
            if (p.ZoneDirtLayerHeight) return "地下";
            return "地表";
        }

        /// <summary>运动状态（身体感：我在跑/跳/游/落）。</summary>
        private static string MovementState(Player p)
        {
            if (p.mount.Active) return "mounted";
            if (p.wet) return "swimming";
            if (p.velocity.Y < -0.5f) return "jumping";
            if (p.velocity.Y > 0.5f) return "falling";
            if (p.velocity.X != 0f) return "running";
            return "grounded";
        }

        /// <summary>截图一帧返回 base64 PNG（Python 侧视觉管线消费）。
        ///
        /// 命令队列已保证主线程执行（GraphicsDevice 渲染线程数据）；
        /// 截图 = 读出当前 render target 像素 → PNG 编码 → base64 回执。
        /// 无 render target（未进入游戏画面）时回 ok=false，Python 侧静默降级。
        /// </summary>
        private void SendScreenshot(NetworkStream s, long reqId)
        {
            // GraphicsDevice 只能主线程访问（命令在监听线程执行）
            Main.QueueMainThreadAction(() =>
            {
                try
                {
                    var gd = Main.instance.GraphicsDevice;
                    var targets = gd.GetRenderTargets();
                    if (targets.Length == 0 ||
                        !(targets[0].RenderTarget is RenderTarget2D rt) || rt.IsDisposed)
                    {
                        Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["error"] = "no_render_target" });
                        return;
                    }
                    int w = rt.Width, h = rt.Height;
                    if (w <= 0 || h <= 0)
                    {
                        Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["error"] = "bad_size" });
                        return;
                    }
                    var pixels = new Color[w * h];
                    rt.GetData(pixels);
                    string b64;
                    using (var ms = new MemoryStream())
                    {
                        using (var img = new Texture2D(gd, w, h))
                        {
                            img.SetData(pixels);
                            img.SaveAsPng(ms, w, h);
                        }
                        b64 = Convert.ToBase64String(ms.ToArray());
                    }
                    Send(s, new Dict { ["req_id"] = reqId, ["ok"] = true,
                                        ["image"] = b64, ["mime"] = "image/png",
                                        ["w"] = w, ["h"] = h });
                }
                catch (Exception ex)
                {
                    Send(s, new Dict { ["req_id"] = reqId, ["ok"] = false, ["error"] = ex.Message });
                }
            });
        }

    
    }
}
