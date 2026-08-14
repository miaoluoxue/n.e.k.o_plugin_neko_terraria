"""状态快照周期推 + 游戏上下文注入 LLM + 主动 nudge + 事件发射器。

设计（参照 Lumi_Nox）：
- 游戏状态以 read 模式推给 LLM，不强制回复
- 紧急事件（低血/Boss）注入 InteractionEngine，统一管理响应
- GameEventEmitter 覆盖 19 种日常事件，自然语言叙事
"""

import asyncio
import time
from typing import Any, Dict, Optional

from ..autonomous.event_bus import get_event_bus
from ..autonomous.game_event_emitter import GameEventEmitter
from .context import (build_anchor_msg, build_user_context,
                      build_full_system_prompt, build_capability_block)


class TerrariaService:
    """游戏状态服务：状态采集 → 事件检测 → LLM 上下文注入。"""

    def __init__(self, plugin, push_message=None) -> None:
        self.plugin = plugin
        self.agent = plugin._agent
        self.cfg = plugin._config
        self.push = push_message
        self._running = False
        self._context_seq = 0
        self._last_hp = 100
        self._joined = False
        self._death_pushed = False
        self._respawn_pushed = False
        self.interaction = None
        self._llm_call = None
        self._prev_snap: Dict[str, Any] = {}
        self._event_emitter: GameEventEmitter = GameEventEmitter(self.agent)

    async def start(self) -> None:
        self._running = True
        if self.interaction:
            self._event_emitter.bind_interaction(self.interaction)
        asyncio.create_task(self._context_push_loop())
        bus = get_event_bus()
        bus.subscribe("player_died", self._on_player_died)
        bus.subscribe("player_respawned", self._on_player_respawned)

    async def stop(self) -> None:
        """停止所有后台任务并清理资源。"""
        self._running = False
        if self._event_emitter:
            self._event_emitter.reset()
        self.interaction = None
        self._prev_snap = {}
        self._joined = False
        self._death_pushed = False
        self._respawn_pushed = False
        self._context_seq = 0
        self._last_hp = 100

    def bind_emitter_to_interaction(self) -> None:
        if self.interaction:
            self._event_emitter.bind_interaction(self.interaction)

    def set_interaction(self, interaction) -> None:
        self.interaction = interaction

    def set_llm_call(self, fn) -> None:
        self._llm_call = fn

    @property
    def event_emitter(self) -> GameEventEmitter:
        return self._event_emitter

    async def _push_joined_game(self) -> None:
        char_name = self.cfg.get("character_name", "neko")
        prompt = build_full_system_prompt()
        anchor = build_anchor_msg(self.agent)
        body = build_user_context(self.agent)

        try:
            caps = await self.agent.capability.refresh()
        except Exception:
            caps = {}
        cap_block = build_capability_block(caps)

        text = f"{prompt}\n\n状态锚点：{anchor}\n\n当前状态：\n{body}\n\n{cap_block}"

        join_msg = (
            f"[系统] 我已经进入泰拉瑞亚世界啦！\n"
            f"我是{char_name}，身份：{self.cfg.get('role_desc', '猫娘助手')}\n"
        )
        await self._push(join_msg, "system")
        await self._push(text, "deep_context")
        # v3.0: 注入 ai_guidance（第一人称身份+能力+工具组合引导）——
        # 参照 vr_neko_cat：让宿主 LLM 知道怎么用 26 个工具，聊天下达命令更准
        try:
            from .context import build_ai_guidance
            await self._push(build_ai_guidance(), "deep_context")
        except Exception:
            pass

    async def _context_push_loop(self) -> None:
        interval = self.cfg.get("context_push_interval_seconds", 8.0)
        deep_interval = self.cfg.get("context_deep_push_interval_seconds", 30.0)
        last_deep = 0.0

        if not self._joined:
            self._joined = True
            await self._push(
                f"[系统] 我正在进入泰拉瑞亚世界，马上就好...",
                "system")
            await self._push_joined_game()

        while self._running:
            await asyncio.sleep(interval)

            try:
                state = self.agent.get_state()
            except Exception:
                continue

            # 统一由 emitter 检测所有事件（含 HP/Boss 紧急事件，不再独立检测）
            # tick 异常不得杀死上下文推送循环
            try:
                self._event_emitter.tick(state)
            except Exception:
                pass

            now = time.monotonic()
            if now - last_deep >= deep_interval:
                last_deep = now
                await self._push_deep_context()
                self._prev_snap = state
                continue

            delta = self._build_delta(state)
            if delta:
                anchor = build_anchor_msg(self.agent)
                if anchor:
                    msg = f"{anchor}\n最近变化：\n{delta}"
                else:
                    msg = f"最近变化：\n{delta}"
                await self._push(msg, "read")
            self._prev_snap = state

    async def _push_deep_context(self) -> None:
        try:
            caps = await self.agent.capability.refresh()
        except Exception:
            caps = {}
        cap_block = build_capability_block(caps)
        ctx = build_user_context(self.agent)
        anchor = build_anchor_msg(self.agent)
        char_name = self.cfg.get("character_name", "neko")

        header = f"[你是{char_name}，正在泰拉瑞亚世界中——采矿、战斗、探索。]\n"
        if anchor:
            header += f"当前状态：{anchor}\n\n"

        text = f"{header}{ctx}\n\n{cap_block}" if cap_block else f"{header}{ctx}"
        if text:
            await self._push(text, "read")

    def _build_delta(self, state: Dict) -> str:
        """对比上次快照，生成增量变化描述。无变化返回空串。"""
        prev = self._prev_snap
        if not prev:
            return ""
        lines = []

        hp = state.get("hp", 100)
        prev_hp = prev.get("hp", 100)
        max_hp = state.get("max_life", 100) or 100
        if hp != prev_hp:
            if hp < prev_hp:
                lines.append(f"血量 {prev_hp}→{hp}（掉了 {prev_hp - hp} 点，{hp}/{max_hp}）")
            elif hp > prev_hp:
                lines.append(f"血量 {prev_hp}→{hp}（回了 {hp - prev_hp} 点，{hp}/{max_hp}）")

        x = state.get("tile_x", 0)
        y = state.get("tile_y", 0)
        prev_x = prev.get("tile_x", 0)
        prev_y = prev.get("tile_y", 0)
        if x != prev_x or y != prev_y:
            lines.append(f"位置 ({prev_x},{prev_y}) → ({x},{y})")

        cur_time = str(state.get("time_of_day", "") or "").strip()
        prev_time = str(prev.get("time_of_day", "") or "").strip()
        if cur_time and cur_time != prev_time:
            time_labels = {"day": "白天", "night": "夜晚", "dusk": "黄昏", "dawn": "黎明"}
            lines.append(f"时间切换: {time_labels.get(prev_time, prev_time)} → {time_labels.get(cur_time, cur_time)}")

        cur_biome = str(state.get("biome", "") or "").strip()
        prev_biome = str(prev.get("biome", "") or "").strip()
        if cur_biome and cur_biome != prev_biome:
            lines.append(f"进入新区域: {cur_biome}")

        alive = state.get("alive", True) and not state.get("is_dead", False)
        prev_alive = prev.get("alive", True) and not prev.get("is_dead", False)
        if alive != prev_alive:
            lines.append("⚡ 复活了！重新开始探索")

        held = state.get("held_item", "")
        prev_held = prev.get("held_item", "")
        cur_name = held.get("name", held) if isinstance(held, dict) else str(held)
        prev_name = prev_held.get("name", prev_held) if isinstance(prev_held, dict) else str(prev_held)
        if cur_name and cur_name != prev_name and prev_name:
            lines.append(f"切换武器: {prev_name} → {cur_name}")

        return "\n".join(lines)

    async def _on_player_died(self, data: Any) -> None:
        if self._death_pushed:
            return
        self._death_pushed = True
        self._respawn_pushed = False
        self._prev_snap = {}
        self._event_emitter.reset()

        count = data.get("count", 0) if isinstance(data, dict) else 0
        pos = data.get("position") if isinstance(data, dict) else None
        msg = data.get("message", "") if isinstance(data, dict) else ""

        death_text = (
            f"死了！(第{count}次)\n"
            f"{'位置: ' + str(pos) if pos else ''}\n"
            f"{msg}\n"
            f"等复活后看看周围情况..."
        )
        await self._push(death_text, "respond")
        self.plugin.logger.info(f"[service] 已推送死亡事件 #{count}")

    async def _on_player_respawned(self, data: Any) -> None:
        if self._respawn_pushed:
            return
        self._respawn_pushed = True
        self._death_pushed = False
        self._prev_snap = {}
        self._event_emitter.reset()

        pos = data.get("position") if isinstance(data, dict) else None
        text = f"复活了！" + (f" 位置: {pos}" if pos else "")
        await self._push(text, "respond")
        self.plugin.logger.info("[service] 已推送复活事件")

    async def _push(self, content: str, behavior: str = "read") -> None:
        """推一条文本给宿主 LLM。

        宿主 push_message 是 keyword-only（SDK v2），且 ai_behavior 只接受
        respond/read/blind——legacy 的 system/deep_context 映射为 read。
        """
        if not self.push:
            return
        if behavior in ("system", "deep_context"):
            behavior = "read"
        try:
            await self.push(
                parts=[{"type": "text", "text": content}],
                ai_behavior=behavior)
        except Exception:
            pass  # 推送失败不阻塞状态循环
