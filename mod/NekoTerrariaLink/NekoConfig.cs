using System.ComponentModel;
using Terraria.ModLoader;
using Terraria.ModLoader.Config;

namespace NekoTerrariaLink
{
    // 模组设置：在游戏内「模组设置」中可调，无需改代码。
    // 注意：mod_port 必须与 neko_terraria 插件 plugin.toml 里的 mod_port 保持一致。
    // 所有标签/提示文本由 Localization/en-US_Mods.NekoTerrariaLink.hjson 提供（tML 2026+ 本地化）。
    public class NekoConfig : ModConfig
    {
        public override ConfigScope Mode => ConfigScope.ClientSide;

        // 本模组监听的 TCP 端口（AI 猫娘插件通过它发送指令）
        [Range(1024, 65535)]
        public int ModPort { get; set; } = 9877;

        // 是否仅允许本机连接（loopback）。关掉可允许局域网内其它机器连接。
        public bool LocalOnly { get; set; } = true;

        // ===== 自动加入相关（替代损坏的 -skipselect） =====

        [DefaultValue(true)]
        public bool AutoSelectCharacter { get; set; } = true;

        // 自动选择/操作的目标角色名称
        [DefaultValue("Neko")]
        public string PlayerName { get; set; } = "Neko";
    }
}
