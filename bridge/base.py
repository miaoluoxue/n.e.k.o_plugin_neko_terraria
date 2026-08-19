"""基地系统（v0.5，基地与补给系统）。

猫娘在出生点附近建立基地：记录出生点 + 找/放箱子作为储物，背包满了回家存、
补给后再出发。让猫娘"会过日子"，像真人玩家一样有家可回。

- base 位置：世界出生点（get_spawn）或首次入服位置
- base 箱子：出生点附近最近的箱子；没有就放一个
- store_surplus：把背包里非必需物品存进基地箱（保留手持/装备/工具/药水）
- resupply：回家 → 存多余 → 补给基础物资
- go_home：用魔镜回出生点（合法物品），或导航回去
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 基地扫描半径（tile）
BASE_SCAN_RANGE = 60
# 背包满阈值：空位少于这个数就回家存
RESUPPLY_EMPTY_SLOTS = 5
# 补给间隔（秒）
RESUPPLY_COOLDOWN = 120.0

# 保留在身上的物品（不存进基地箱）
# 手持/装备由 inventory 结构区分；这里额外保留功能物品
_KEEP_NAMES = {
    "火把", "魔镜", "冰雪镜", "抓钩", "钩爪", "回忆药水",
    "铁镐", "铜镐", "银镐", "金镐", "钨镐", "钯金镐", "钴镐",
    "铁斧", "铜斧", "银斧", "金斧",
    "治疗药水", "治疗药水II", "治疗药水III", "魔力药水",
}


class BaseManager:
    def __init__(self, agent) -> None:
        self.agent = agent
        self.base_pos: Optional[Tuple[int, int]] = None
        self.base_chests: List[Dict[str, Any]] = []
        self._last_resupply = 0.0

    # ---------------- 初始化 ----------------

    async def init_base(self) -> bool:
        """初始化基地：确定出生点 + 扫描/放置基地箱。"""
        try:
            spawn = await self.agent.mod.get_spawn()
            if spawn:
                self.base_pos = spawn
            else:
                # 兜底：用当前玩家位置
                st = self.agent.get_state()
                self.base_pos = (int(st.get("tile_x", 0) or 0),
                                 int(st.get("tile_y", 0) or 0))
        except Exception:
            st = self.agent.get_state()
            self.base_pos = (int(st.get("tile_x", 0) or 0),
                             int(st.get("tile_y", 0) or 0))

        if not self.base_pos or self.base_pos == (0, 0):
            return False

        # 扫描出生点附近的箱子
        await self._refresh_base_chests()
        # 没有箱子就放一个（出生点脚边）
        if not self.base_chests:
            bx, by = self.base_pos
            try:
                ok = await self.agent.mod.place_chest(bx, by + 1)
                if ok:
                    await asyncio.sleep(0.3)
                    await self._refresh_base_chests()
            except Exception:
                pass

        self.agent.log(f"基地就绪: 位置{self.base_pos}, 箱子{len(self.base_chests)}个", "base")
        return True

    async def _refresh_base_chests(self) -> None:
        """扫描基地附近的箱子。"""
        if not self.base_pos:
            return
        bx, by = self.base_pos
        try:
            chests = await self.agent.mod.enum_chests()
        except Exception:
            chests = []
        self.base_chests = []
        for c in chests or []:
            if not isinstance(c, dict):
                continue
            cx = int(c.get("x", 0) or 0)
            cy = int(c.get("y", 0) or 0)
            if abs(cx - bx) <= BASE_SCAN_RANGE and abs(cy - by) <= BASE_SCAN_RANGE:
                self.base_chests.append(c)

    # ---------------- 回家 ----------------

    async def go_home(self) -> bool:
        """回基地：优先魔镜（合法瞬移），失败则导航。"""
        if not self.base_pos:
            return False
        try:
            if await self.agent.mod.use_mirror():
                await asyncio.sleep(1.0)
                return True
        except Exception:
            pass
        # 魔镜失败 → 导航回出生点
        bx, by = self.base_pos
        try:
            return await self.agent.navigate_to(bx, by, timeout=25)
        except Exception:
            return False

    # ---------------- 存储 ----------------

    async def store_surplus(self) -> int:
        """把背包里非必需物品存进基地箱。返回存入的物品种数。"""
        if not self.base_chests:
            await self._refresh_base_chests()
        if not self.base_chests:
            return 0
        chest = self.base_chests[0]

        inv = self.agent.get_inventory_sync()
        stored = 0
        # inventory = 主背包（hotbar 是手持栏，equipped 是装备）
        for it in (inv.get("inventory", []) or []):
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", "") or "")
            iid = int(it.get("id", -1) or -1)
            slot = it.get("inv_slot")
            if slot is None:
                continue
            if iid <= 0:
                continue
            # 保留功能物品
            if name in _KEEP_NAMES:
                continue
            # 有防御/伤害/工具属性的保留（防具武器）
            if int(it.get("defense", 0) or 0) > 0:
                continue
            if int(it.get("damage", 0) or 0) > 0:
                continue
            if int(it.get("pick", 0) or 0) > 0:
                continue
            if int(it.get("axe", 0) or 0) > 0:
                continue
            # 存进基地箱（全量）
            try:
                if await self.agent.mod.store_item(
                        chest["x"], chest["y"], slot, int(it.get("stack", 1) or 1)):
                    stored += 1
            except Exception:
                pass
        if stored:
            self.agent.log(f"存了 {stored} 种物品进基地箱", "base")
        return stored

    # ---------------- 补给循环 ----------------

    def inventory_nearly_full(self, threshold: int = RESUPPLY_EMPTY_SLOTS) -> bool:
        """背包空位是否少于阈值。"""
        inv = self.agent.get_inventory_sync()
        inv_count = len(inv.get("inventory", []) or [])
        hotbar_count = len(inv.get("hotbar", []) or [])
        # 主背包 40 格 + 手持 10 格
        total = 50
        empty = total - (inv_count + hotbar_count)
        return empty < threshold

    async def resupply(self) -> bool:
        """补给循环：背包满 → 回家 → 存多余 → 刷新。"""
        now = time.time()
        if now - self._last_resupply < RESUPPLY_COOLDOWN:
            return False
        if not self.inventory_nearly_full():
            return False
        self._last_resupply = now

        self.agent.log("背包快满了，回家存一下~", "base")
        if not await self.go_home():
            return False
        await asyncio.sleep(0.5)
        stored = await self.store_surplus()
        if stored:
            try:
                await self.agent.send_chat("背包满了，我回家存了点东西喵~")
            except Exception:
                pass
        return stored > 0

    # ---------------- 状态 ----------------

    def status(self) -> Dict[str, Any]:
        return {
            "base_pos": list(self.base_pos) if self.base_pos else None,
            "chests": len(self.base_chests),
            "inventory_full": self.inventory_nearly_full(),
        }
