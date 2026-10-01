using System;
using Terraria;

namespace NekoTerrariaLink
{
    public partial class NekoControlPlayer
    {
        internal bool LiquidEscapeActive { get; private set; }
        private int _liquidEscapeFrames;
        private bool _liquidEscapeSuppressed;

        /// <summary>
        /// 在水中用原生跳跃控制向液面上浮。不要直接把人物瞬移或持续写死
        /// velocity：原版游泳、坐骑和多人同步仍应负责碰撞与速度结算。
        /// </summary>
        internal bool ApplyLiquidEscape()
        {
            var p = Player;
            if (_liquidEscapeSuppressed)
                return false;
            if (p == null || !p.wet || p.lavaWet || p.honeyWet)
            {
                LiquidEscapeActive = false;
                _liquidEscapeFrames = 0;
                return false;
            }

            LiquidEscapeActive = true;
            _liquidEscapeFrames = Math.Min(_liquidEscapeFrames + 1, 1800);
            // Terraria 的游泳/上浮入口是 controlJump；每帧重新注入，
            // 避免一次命令耗尽后在深水中再次下沉。
            p.controlJump = true;
            p.controlDown = false;
            p.controlUseItem = false;
            p.controlHook = false;
            if (p.velocity.Y > 1.5f)
                p.velocity.Y = -Math.Min(4.5f, p.velocity.Y * 0.65f);
            return true;
        }

        internal void ResetLiquidEscape(bool suppress)
        {
            LiquidEscapeActive = false;
            _liquidEscapeFrames = 0;
            _liquidEscapeSuppressed = suppress;
        }

        internal void ResumeLiquidEscape()
        {
            LiquidEscapeActive = false;
            _liquidEscapeFrames = 0;
            _liquidEscapeSuppressed = false;
        }
    }
}
