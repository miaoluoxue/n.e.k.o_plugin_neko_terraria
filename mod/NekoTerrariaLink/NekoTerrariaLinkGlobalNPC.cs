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
    /// <summary>击杀事件（情感素材）：猫娘附近的怪被击杀时推送 npc_killed。</summary>
    public class NekoTerrariaLinkGlobalNPC : GlobalNPC
    {
        public override void OnKill(NPC npc)
        {
            var player = Main.LocalPlayer;
            if (player == null || !player.active) return;
            if (npc.Distance(player.Center) > 1200f) return;
            var inst = NekoTerrariaLink.Instance;
            if (inst != null) inst.PushEvent("npc_killed",
                $"干掉了{npc.FullName}", npc.boss ? npc.FullName : null);
        }
    }
}
