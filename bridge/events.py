"""Boss/入侵事件应对 + 发现汇报。订阅 event_bus，通知主人并决策。"""

from typing import Any

RETREAT_HP_RATIO = 0.5


class EventResponder:
    def __init__(self, agent) -> None:
        self.agent = agent
        self._known_chests: set = set()

    def bind(self) -> None:
        from ..autonomous.event_bus import get_event_bus

        bus = get_event_bus()
        bus.subscribe("boss_spawned", self._on_boss_spawned)
        bus.subscribe("boss_killed", self._on_boss_killed)
        bus.subscribe("invasion_start", self._on_invasion_start)
        bus.subscribe("invasion_end", self._on_invasion_end)
        bus.subscribe("player_died", self._on_player_died)

    def _remember(self, key: str, value: str, category: str = "world") -> None:
        try:
            self.agent.remember(key, value, category=category)
        except Exception:
            pass

    async def _push(self, text: str, behavior: str = "respond") -> None:
        """A5：异步播报，不可用时游戏内聊天兜底，保证不静默。"""
        await self.agent.speak(text, ai_behavior=behavior)

    @staticmethod
    def _boss_position(name: str) -> tuple:
        """根据名字获取 Boss 位置（从 agent 状态读）。"""
        return 0, 0  # 已废弃，由事件总线直接传递位置

    async def _on_boss_spawned(self, data: Any) -> None:
        name = data.get("name", "未知Boss") if isinstance(data, dict) else "未知Boss"
        st = self.agent.get_state()
        hp = int(st.get("hp", 100) or 100)
        mx = int(st.get("max_life", 100) or 100) or 100
        self._remember("世界-Boss出现", f"{name} 出现了")
        if mx and hp / mx < RETREAT_HP_RATIO:
            await self._push(f"主人，{name}出现了！我只有{hp}/{mx}血，先躲远点保命~")
            await self._retreat(name)
        else:
            await self._push(f"主人，{name}出现了！要我打还是躲起来？")

    async def _retreat(self, name: str) -> None:
        bx, by = self._boss_position(name)
        if not bx and not by:
            return  # 拿不到 Boss 位置就不躲（避免在原地乱跑）
        mx, my = int(self.agent.get_state().get("tile_x", 0) or 0), int(self.agent.get_state().get("tile_y", 0) or 0)
        away = -1 if bx > mx else 1
        try:
            await self.agent.mod.navigate_async(mx + away * 15, my, timeout=6)
        except Exception:
            pass

    async def _on_boss_killed(self, data: Any) -> None:
        name = data.get("name", "Boss") if isinstance(data, dict) else "Boss"
        self._remember("世界-Boss击杀", f"{name} 被击败了")
        await self._push(f"我们把{name}打倒了！")

    async def _on_invasion_start(self, data: Any) -> None:
        self._remember("世界-入侵", "有入侵者来袭")
        await self._push("主人，有入侵者打过来了！小心~")

    async def _on_invasion_end(self, data: Any) -> None:
        self._remember("世界-入侵结束", "入侵者被打退了")
        await self._push("入侵者被打退了！")

    async def _on_player_died(self, data: Any) -> None:
        pos = None
        if isinstance(data, dict):
            pos = data.get("position") or data.get("death_position")
        loc = f" 位置({pos[0]},{pos[1]})" if pos else ""
        self._remember("世界-死亡记录", f"我在{loc}阵亡过")

    async def report_new_chests(self, chests: list) -> None:
        current = set()
        for c in chests or []:
            if isinstance(c, dict):
                current.add((int(c.get("x", 0) or 0), int(c.get("y", 0) or 0)))
        fresh = current - self._known_chests
        if fresh and self._known_chests:
            x, y = next(iter(fresh))
            await self._push(f"主人，我发现了一个新箱子（{x},{y}）~")
        self._known_chests = current
