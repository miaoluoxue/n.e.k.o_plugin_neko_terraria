"""事件总线：模块间解耦通信，指令打断走这里。"""

import asyncio
from typing import Any, Callable, Dict, List

_event_bus: "EventBus | None" = None


class EventType:
    """游戏世界事件类型常量。"""
    PLAYER_DIED = "player_died"
    PLAYER_RESPAWNED = "player_respawned"
    COMBAT_HIT = "combat_hit"
    COMBAT_KILLED = "combat_killed"
    ENEMY_KILLED = "enemy_killed"
    BOSS_SPAWNED = "boss_spawned"
    BOSS_KILLED = "boss_killed"
    BOSS_NEARBY = "boss_nearby"
    COMBAT_SUMMARY = "combat_summary"
    ORE_FOUND = "ore_found"
    CHEST_FOUND = "chest_found"
    FOUND_CHEST = "found_chest"          # vision 层事件
    FOUND_RARE = "found_rare"            # vision 层事件
    ENEMY_SPOTTED = "enemy_spotted"      # vision 层事件
    TERRAIN_CHANGED = "terrain_changed"  # vision 层 + 通用
    RARE_ITEM = "rare_item"
    EQUIPMENT_UPGRADED = "equipment_upgraded"
    INVENTORY_FULL = "inventory_full"
    GOAL_SET = "goal_set"
    GOAL_COMPLETED = "goal_completed"
    GOAL_FAILED = "goal_failed"
    LOW_HP = "low_hp"
    HP_CRASH = "hp_crash"
    DROWNING = "drowning"
    IN_LAVA = "in_lava"
    FALLING = "falling"
    BIOME_CHANGED = "biome_changed"
    PLAYER_NEARBY = "player_nearby"
    TIME_CHANGED = "time_changed"
    WEATHER_CHANGED = "weather_changed"
    INVASION = "invasion"
    INVASION_START = "invasion_start"
    INVASION_END = "invasion_end"
    MULTIPLAYER_MODE_CHANGED = "multiplayer_mode_changed"
    COMMAND_INTERRUPT = "command_interrupt"


def get_event_bus() -> "EventBus":
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


class EventBus:
    def __init__(self) -> None:
        self._subs: Dict[str, List[Callable[[Any], Any]]] = {}

    def subscribe(self, event: str, cb: Callable[[Any], Any]) -> None:
        # 去重：面板断开→重连会多次 start()，各模块重复 bind/subscribe，
        # 同回调被 append 两次 → 事件双触发（player_died 推两次等）
        subs = self._subs.setdefault(event, [])
        if cb not in subs:
            subs.append(cb)

    def unsubscribe(self, event: str, cb: Callable[[Any], Any]) -> None:
        subs = self._subs.get(event, [])
        if cb in subs:
            subs.remove(cb)

    async def publish(self, event: str, data: Any) -> None:
        for cb in self._subs.get(event, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(data)
                else:
                    cb(data)
            except Exception:
                pass

    def fire(self, event: str, data: Any) -> None:
        for cb in self._subs.get(event, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.ensure_future(cb(data))
                else:
                    cb(data)
            except Exception:
                pass

    def fire_player_event(self, event: str, data: Dict[str, Any]) -> None:
        self.fire(event, data)

    def fire_combat_event(self, event: str, enemy: str,
                          damage: int = 0, **kwargs) -> None:
        self.fire(event, {"enemy_name": enemy, "damage": damage, **kwargs})

    def fire_goal_event(self, event: str, goal_type: str,
                        target: str, reason: str = "") -> None:
        self.fire(event, {"type": goal_type, "target": target, "reason": reason})

    def fire_explore_event(self, event: str, **kwargs) -> None:
        self.fire(event, kwargs)
