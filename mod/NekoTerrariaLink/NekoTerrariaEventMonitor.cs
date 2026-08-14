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
    /// <summary>
    /// 每帧事件监控：ModSystem.UpdateUI(GameTime)（tML 1.4.5 源码 ModSystem.cs:132 确认）。
    /// 1.4.5 移除了 Mod.Update/ModSystem.Update(GameTime)，改用 UpdateUI 等细分钩子。
    /// </summary>
    public class NekoTerrariaEventMonitor : ModSystem
    {
        public override void UpdateUI(GameTime gameTime)
        {
            base.UpdateUI(gameTime);
            Main.hasFocus = true;
            Main.instance.InactiveSleepTime = TimeSpan.Zero;
            Main.autoPause = false;
            if (_focusDiag++ % 60 == 0)
            {
                try { ModContent.GetInstance<global::NekoTerrariaLink.NekoTerrariaLink>().Logger.Info($"[UI] hasFocus={Main.hasFocus} IsActive={Main.instance.IsActive} netMode={Main.netMode}"); }
                catch { }
            }
            global::NekoTerrariaLink.NekoTerrariaLink.Instance?.EventMonitorTick();
        }
        private int _focusDiag = 0;
}
}
