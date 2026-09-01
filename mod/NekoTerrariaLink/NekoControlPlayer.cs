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
    /// <summary>每帧注入控制状态：命令线程写状态，主线程 PreUpdateMovement 应用 control。</summary>
    public class NekoControlPlayer : ModPlayer
    {
        public int moveDir;      // -1 左 / 0 停 / 1 右
        public int jumpTicks;    // 剩余跳跃帧
        public int useTicks;     // 剩余使用物品帧
        public int useSlot = -1; // 使用槽位
        public int hookTicks;    // 剩余钩锁帧
        internal List<NekoTerrariaLink.NavPoint> navPath;  // A* 导航路径（ModPlayer 逐帧推进）
        public int navIdx;
        public int navGen;   // 路径代际：fire-and-forget 导航防旧任务误清新路径
        public int digTargetX = -1, digTargetY = -1;   // 原生物品挖掘目标（挖掘目标修正）

        // ── 导航控制注入 ──
        // 联机模式下移动由服务器结算：客户端通过 NetMessage.SendData(13)（control 包）
        // 把 control 状态发给服务器。PreUpdateMovement（UpdateMovement 开头）注入的
        // control 可能赶不上 control 包的发送（原版网络段更早），导致服务器不知道移动。
        // SetControls 钩子在 control 读取区域（PlayerLoader.SetControls，Update 早期）、
        // 网络同步之前调用——在这里注入 control，包能带上，服务器才会结算移动。
        public override void SetControls()
        {
            var p = Player;
            // AI 角色只响应注入，不响应真实键盘：AI 窗口被点击/激活时，
            // 玩家的按键（WASD/空格）不得同时控制 AI 角色。
            p.controlLeft = p.controlRight = p.controlJump = p.controlDown = false;
            try
            {
                if (_diagFrame % 60 == 0)
                {
                    ModContent.GetInstance<NekoTerrariaLink>().Logger.Info(
                        $"[SC] called nav={navPath != null} idx={navIdx} moveDir={moveDir} " +
                        $"frozen={p.frozen} webbed={p.webbed} stoned={p.stoned} tongued={p.tongued} " +
                        $"mount={p.mount.Active} hook={p.grappling[0]} hasFocus={Main.hasFocus}");
                }
                // 移动改为 velocity 直驱（PreUpdateMovement）——SetControls 只负责键盘隔离
                if (_diagFrame % 60 == 0)
                {
                    ModContent.GetInstance<NekoTerrariaLink>().Logger.Info(
                        $"[SC] 注入后 cL={p.controlLeft} cR={p.controlRight} cJ={p.controlJump}");
                }
            }
            catch (Exception ex)
            {
                try { ModContent.GetInstance<NekoTerrariaLink>().Logger.Error($"[SC] 异常: {ex.Message}"); }
                catch { }
            }
        }

        /// <summary>PostUpdateRunSpeeds（失焦方案）：ResetControls/hasFocus 之后、
        /// HorizontalMovement 之前注入 control——原生物理（加速度/翅膀/坐骑），direction 自动，
        /// 焦点无关。导航路径 + move 命令都走这里。</summary>
        public override void PostUpdateRunSpeeds()
        {
            var p = Player;
            if (p.whoAmI != Main.myPlayer) return;
            if (navPath != null && navIdx < navPath.Count)
            {
                int px = (int)(p.Center.X / 16), py = (int)(p.Bottom.Y / 16);
                var g = navPath[navIdx];
                if (Math.Abs(px - g.X) <= 1 && Math.Abs(py - g.Y) <= 2)
                {
                    navIdx++;
                    if (navIdx >= navPath.Count)
                    {
                        navPath = null;
                        return;
                    }
                    g = navPath[navIdx];
                }
                int dx = g.X - px;
                int dy = g.Y - py;

                if (dx < 0) p.controlLeft = true;
                else if (dx > 0) p.controlRight = true;

                // 跳跃：路径点编码 Jump 高度 → 帧表精确按帧（mod 原生能力）
                if (dy < -2 && _jumpFrames <= 0 && g.Jump > 0)
                {
                    var tbl = JumpFrameTable();
                    int h = Math.Min(g.Jump + (NekoTerrariaLink.IsPlatform(g.X, g.Y + 1) ? 2 : 0), tbl.Length - 1);
                    _jumpFrames = tbl[h];
                }
                if (_jumpFrames > 0) { p.controlJump = true; _jumpFrames--; }

                if (g.Action == NekoTerrariaLink.NavAction.DropThroughPlatform
                    && NekoTerrariaLink.IsPlatform(px, py + 1) && dy > 0)
                    p.controlDown = true;
                return;
            }
            // move 命令：control 注入（原生物理）
            if (moveDir != 0)
            {
                if (moveDir < 0) p.controlLeft = true;
                else if (moveDir > 0) p.controlRight = true;
            }
            // 跳跃独立于移动方向：纯跳跃/“up” 命令也要生效
            if (jumpTicks > 0) { p.controlJump = true; jumpTicks--; }
        }

        public override void PreUpdateMovement()
        {
            var p = Player;
            if (navPath != null && navIdx < navPath.Count)
            {
                if (_diagFrame++ % 60 == 0)
                {
                    int px = (int)(p.Center.X / 16), py = (int)(p.Bottom.Y / 16);
                    var g = navPath[navIdx];
                    try { ModContent.GetInstance<NekoTerrariaLink>().Logger.Info($"[NavStep] 帧={_diagFrame} 我=({px},{py}) 目标=({g.X},{g.Y}) act={g.Action} navIdx={navIdx}/{navPath.Count} vel=({p.velocity.X:F1},{p.velocity.Y:F1}) frozen={p.frozen} webbed={p.webbed} mount={p.mount.Active}"); }
                    catch { }
                }
                NavStepVelocity(p);   // 后备 velocity 直驱（control 注入为主）
            }
            if (useTicks > 0)
            {
                if (useSlot >= 0) p.selectedItem = useSlot;
                p.controlUseItem = true;
                useTicks--;
            }
            if (hookTicks > 0) { p.controlHook = true; hookTicks--; }
            // 面向目标：近战挥动/挖掘方向对准目标（战斗时人物不动也朝怪方向砍）
            if (digTargetX >= 0 && digTargetY >= 0)
            {
                int tdx = digTargetX - (int)(p.Center.X / 16f);
                if (tdx != 0) p.direction = Math.Sign(tdx);
            }
        }
        private int _diagFrame = 0;

        /// <summary>后备 velocity 直驱：PostUpdateRunSpeeds 注入 control 为主（原生物理），
        /// 此处仅当 control 未生效（velocity 归零）时直驱兜底 + 发包。</summary>
        private void NavStepVelocity(Player p)
        {
            if (navPath == null || navIdx >= navPath.Count) return;
            int px = (int)(p.Center.X / 16), py = (int)(p.Bottom.Y / 16);
            var g = navPath[navIdx];
            if (Math.Abs(px - g.X) <= 1 && Math.Abs(py - g.Y) <= 2)
            {
                navIdx++;
                if (navIdx >= navPath.Count)
                {
                    navPath = null;
                    p.velocity.X = 0f;
                    p.controlJump = p.controlDown = false;
                    NetMessage.SendData(MessageID.PlayerControls, -1, -1, null, p.whoAmI);
                    return;
                }
                g = navPath[navIdx];
            }
            int dx = g.X - px;
            // 后备：control 已注入但 velocity 仍归零（钩子未生效）→ 直驱兜底
            if (dx != 0 && Math.Abs(p.velocity.X) < 0.1f)
            {
                p.velocity.X = Math.Sign(dx) * p.maxRunSpeed;
                p.direction = Math.Sign(dx);
            }
            NetMessage.SendData(MessageID.PlayerControls, -1, -1, null, p.whoAmI);
        }

        /// <summary>跳跃帧表：跳 h 格需要按住 jump 几帧（按帧表）。
        /// 模拟动力段（jumpSpeed×jumpHeight 帧）+ 惯性滑行 + 重力，返回"帧数→高度"映射。</summary>
        private int[] JumpFrameTable()
        {
            if (_jumpTable != null) return _jumpTable;
            var p = Player;
            float jumpSpeed = Player.jumpSpeed > 0f ? Player.jumpSpeed : 5.01f;
            int jumpHeight = Player.jumpHeight > 0 ? Player.jumpHeight : 15;
            float gravity = Player.defaultGravity > 0f ? Player.defaultGravity : 0.4f;
            int maxJump = Math.Max(1, (int)((jumpSpeed * jumpHeight + jumpSpeed * jumpSpeed / (2 * gravity)) / 16f));
            var table = new int[maxJump + 1];
            for (int hold = 1; hold <= jumpHeight + 30; hold++)
            {
                float y = 0f, vel = 0f;
                for (int f = 0; f < hold + 60; f++)
                {
                    if (f < hold && f < jumpHeight) vel = jumpSpeed; else vel -= gravity;
                    y += vel;
                    if (vel <= 0f) break;
                }
                int tiles = (int)(y / 16f);
                for (int h = 1; h <= Math.Min(tiles, maxJump); h++)
                    if (table[h] == 0) table[h] = hold;
                if (tiles >= maxJump) break;
            }
            for (int h = 1; h <= maxJump; h++)
                if (table[h] == 0) table[h] = jumpHeight;
            _jumpTable = table;
            return table;
        }
        private int _jumpFrames;
        private int[] _jumpTable;

        /// <summary>PreItemCheck（mod 原生能力）：修正挖掘/放置光标到目标 tile，
        /// 配合 controlUseItem 使用原生物品（镐子动画/消耗/工具属性）。</summary>
        public override bool PreItemCheck()
        {
            if (Player.whoAmI != Main.myPlayer) return true;
            if (digTargetX >= 0 && digTargetY >= 0)
            {
                Player.tileTargetX = digTargetX;
                Player.tileTargetY = digTargetY;
            }
            return true;
        }

        public override void OnHurt(Player.HurtInfo info)
        {
            if (Player.whoAmI != Main.myPlayer) return;
            string source = "未知";
            var ds = info.DamageSource;
            if (ds.SourceNPCIndex >= 0) source = Main.npc[ds.SourceNPCIndex].FullName;
            else if (ds.SourceProjectileLocalIndex >= 0) source = Main.projectile[ds.SourceProjectileLocalIndex].Name;
            else if (ds.SourcePlayerIndex >= 0) source = Main.player[ds.SourcePlayerIndex].name;
            else if (ds.SourceOtherIndex >= 0)
                source = ds.SourceOtherIndex switch { 0 => "摔落", 1 => "溺水", 2 => "岩浆", _ => "环境伤害" };
            var inst = NekoTerrariaLink.Instance;
            if (inst != null) inst.PushEvent("player_hurt", $"被{source}打了，掉了{info.Damage}点血，还剩{Player.statLife}点血");
        }

        public override void Kill(double damage, int hitDirection, bool pvp, PlayerDeathReason damageSource)
        {
            if (Player.whoAmI != Main.myPlayer) return;
            var inst = NekoTerrariaLink.Instance;
            if (inst != null) inst.PushEvent("player_died", damageSource.GetDeathText(Player.name).ToString());
        }
    }
}
