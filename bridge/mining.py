"""挖矿引擎：导航到矿点、循环挖掘、可被中断切换目标。"""

import asyncio
from typing import Any, Dict, Optional

from .mod_link import ModLink
from .item_npc_dict import item_id


class MiningEngine:
    def __init__(self, mod: ModLink, agent=None) -> None:
        self.mod = mod
        self.agent = agent
        self._cancel = asyncio.Event()

    def _stopped(self) -> bool:
        if self._cancel.is_set():
            return True
        ex = getattr(self.agent, "executor", None) if self.agent else None
        return bool(ex and ex.should_stop())

    def cancel(self) -> None:
        self._cancel.set()

    def reset(self) -> None:
        self._cancel.clear()

    async def mine_target(self, target_item: str, amount: int = 10,
                          state: Optional[Dict[str, Any]] = None) -> tuple:
        """挖掘目标矿物

        挖矿位置：玩家脚下方块（矿石在地底，向下挖最稳妥；mod 的
        break_tile 按 tile 坐标挖，不感知面向）。
        计数：以背包里该物品数量增量确认（不再凭空 +1，避免进度虚增）。

        Returns:
            (物品id, 挖到数量)
        """
        iid = item_id(target_item)
        mined = 0
        last_count = -1
        # 先拿背包基数（挖矿计数靠背包增量确认，基数拿不到就无法确认）
        if self.agent:
            try:
                self.agent._inv_full = await self.mod.get_inventory()
                last_count = self._count_item(
                    self.agent.get_inventory_sync(), iid)
            except Exception:
                last_count = -1
        no_gain = 0
        while mined < amount:
            if self._stopped():
                self._cancel.clear()
                return iid, mined
            if state is None and self.agent:
                try:
                    state = self.agent.get_state()
                except Exception:
                    pass
            # v0.8 找矿再挖（Lumi find_trees 做法）：先扫描附近矿（C# find_ore），
            # 导航到最近的目标矿再挖——不再盲目挖脚下（原逻辑挖 6 下土就放弃）
            try:
                ores = await self.mod.find_ore(
                    radius=30, tile_type=iid if iid < 250 else 0)
            except Exception:
                ores = []
            target = None
            if ores:
                target = ores[0]  # 最近（find_ore 已按 dist 排序）
            if target is None:
                # 附近没有目标矿：诚实返回（不假装挖到）
                return iid, mined
            # 导航到矿附近（fire-and-forget 流式导航，走到再挖）
            try:
                await self.mod.navigate_stream_fire(target["x"], target["y"])
                await asyncio.sleep(1.5)
            except Exception:
                pass
            # v0.8 原生物品挖掘（LumiBridge dig_tile：镐子动画/消耗/工具属性）
            try:
                ok = await self.mod.dig_tile(target["x"], target["y"])
            except Exception:
                ok = False
            if not ok:
                no_gain += 1
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(0.8)   # 等镐子动画/服务器结算
            # 挖完主动拉一次背包刷新缓存（C# 不再持续推背包）：
            # 挖矿计数靠真实背包增量确认，缓存不刷新会拿到旧值导致计数失效
            if self.agent:
                try:
                    self.agent._inv_full = await self.mod.get_inventory()
                except Exception:
                    pass
            if last_count >= 0:
                # 挖没挖到由背包变化说了算
                try:
                    new_count = self._count_item(
                        self.agent.get_inventory_sync(), iid)
                    if new_count > last_count:
                        mined += new_count - last_count
                        last_count = new_count
                        no_gain = 0
                    else:
                        no_gain += 1
                except Exception:
                    no_gain += 1
            else:
                # 背包基数不可用：不假计数（避免"没动就完成"）
                no_gain += 1
            # 连续无收获：目标矿位置已挖空/不可达，诚实返回
            if no_gain >= 6:
                return iid, mined
            await asyncio.sleep(0.5)
        return iid, mined

    @staticmethod
    def _count_item(inv: Dict[str, Any], iid: int) -> int:
        """统计背包中某物品 id 的总数（hotbar+equipped+inventory）。"""
        total = 0
        for slot in ("hotbar", "equipped", "inventory"):
            for it in (inv or {}).get(slot, []) or []:
                if it.get("id") == iid:
                    total += int(it.get("stack", 1) or 1)
        return total

