"""挖矿引擎：导航到矿点、循环挖掘、可被中断切换目标。"""

import asyncio
from typing import Any, Dict, Optional

from .item_npc_dict import item_id
from .mod_link import ModLink


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

    # ---------------- 含自动导航的挖掘（v0.8+） ----------------

    async def mine_target(self, target_item: str, amount: int = 10, state: Optional[Dict[str, Any]] = None) -> tuple:
        """挖掘目标矿物（含导航到矿）。

        Returns:
            (物品id, 挖到数量)  — 计数以背包真实增量确认；
            挖到 amount 个或目标矿不可达便返回，杜绝假完成。
        """
        if self.agent is None:
            return -1, 0
        iid = item_id(target_item)
        mined = 0
        no_gain = 0
        # 初始背包基数
        try:
            self.agent._inv_full = await self.mod.get_inventory()
            last_count = self._count_item(self.agent.get_inventory_sync(), iid)
        except Exception:
            last_count = -1
        while mined < amount:
            if self._stopped():
                self._cancel.clear()
                return iid, mined
            if state is None:
                try:
                    state = self.agent.get_state()
                except Exception:
                    pass
            mine = await self._find_target(state, iid, target_item)
            if mine is None:
                return iid, mined  # 附近没有目标矿，诚实返回
            tx, ty = int(mine.get("x", 0)), int(mine.get("y", 0))
            # 到矿点后挖 1 下（省导航，mine_ore_inplace 完成导航 + 挖 + 计数）
            got = await self.mine_ore_inplace(target_item, tx, ty)
            if got:
                mined += got
                last_count += got
                no_gain = 0
                if not state:
                    try:
                        state = self.agent.get_state()
                    except Exception:
                        pass
                try:
                    await self._notify_mining(target_item, got)
                except Exception:
                    pass
            else:
                no_gain += 1
                if no_gain >= 3:
                    break  # 连续挖不到：换下一个矿或直接返回
        return iid, mined

    async def _find_target(self, state: Optional[Dict[str, Any]],
                           iid: int, target_item: str = "") -> Optional[Dict[str, Any]]:
        """找最近可挖矿点（含距离过滤）。

        #14: tile_type 必须用 TileID（矿石方块类型），不是物品 ID——
        C# find_ore 拿 tile_type 与 Main.tile[].TileType 比较（铁矿 TileID=7 ≠ ItemID=11）。
        """
        from .item_npc_dict import tile_type_of
        tile = tile_type_of(target_item, iid)
        try:
            ores = await self.mod.find_ore(radius=30, tile_type=tile)
        except Exception:
            ores = []
        if not ores:
            return None
        st = state or {}
        mx = int(st.get("tile_x", 0) or 0)
        my = int(st.get("tile_y", 0) or 0)
        for o in ores:
            d = abs(int(o.get("x", 0)) - mx) + abs(int(o.get("y", 0)) - my)
            if d <= 30:
                return o
        return None

    async def mine_ore_inplace(self, ore: str, tx: int, ty: int) -> int:
        """原地挖一格矿：导航到位 → dig_tile → 背包计数确认。

        用于空闲挖矿（idle）等不重复 nav 的场景。返回挖到的数量（0 = 没挖到）。
        """
        iid = item_id(ore)
        before = -1
        try:
            self.agent._inv_full = await self.mod.get_inventory()
            before = self._count_item(self.agent.get_inventory_sync(), iid)
        except Exception:
            pass
        try:
            await self.mod.navigate_stream_fire(tx, ty)
            await asyncio.sleep(1.2)
        except Exception:
            pass
        try:
            ok = await self.mod.dig_tile(tx, ty)
        except Exception:
            ok = False
        if not ok:
            return 0
        await asyncio.sleep(0.8)
        if self.agent:
            try:
                self.agent._inv_full = await self.mod.get_inventory()
            except Exception:
                pass
        if before < 0:
            return 1  # 基数不可用：至少做了一次真实挖掘动作
        after = self._count_item(self.agent.get_inventory_sync(), iid)
        return max(0, after - before)

    async def _notify_mining(self, target_item: str, got: int) -> None:
        try:
            exe = getattr(self.agent, "executor", None)
            if exe:
                await exe.notify(
                    "step_done", kind="mine", desc=f"挖到 {got} 个 {target_item}", item=target_item, got=got
                )
        except Exception:
            pass

    @staticmethod
    def _count_item(inv: Dict[str, Any], iid: int) -> int:
        """统计背包中某物品 id 的总数（hotbar+equipped+inventory）。"""
        total = 0
        for slot in ("hotbar", "equipped", "inventory"):
            for it in (inv or {}).get(slot, []) or []:
                if it.get("id") == iid:
                    total += int(it.get("stack", 1) or 1)
        return total
