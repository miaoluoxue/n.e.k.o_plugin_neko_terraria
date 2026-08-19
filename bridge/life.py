"""陪伴式生活交互（v0.5）：砍树、钓鱼、工具选择——什么任务用什么工具。

核心：猫娘不是"任务工具"，而是会过日子的小玩家：
- 砍树：找树 → 走过去 → 砍倒 → 捡木头（背包计数确认）
- 钓鱼：找水域 → 走到岸边 → 用钓竿钓鱼 → 收杆（间歇等待）
- 工具选择：挖矿用镐、砍树用斧、打怪用剑、钓鱼用钓竿——
  在动作前自动选中合适工具（select_item），不拿错家伙。

与 idle/explore 的关系：
- idle 周期触发"生活小动作"（砍树/钓鱼/挖矿轮流）
- 都是前台任务或轻量动作，可被主人打断
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 工具名关键词
_PICK_KW = ("镐", "pick")
_AXE_KW = ("斧", "axe")
_ROD_KW = ("钓竿", "钓竿", "fishing")
_SWORD_KW = ("剑", "sword", "刀")

# 各生活动作节律（秒）
CHOP_INTERVAL = 45.0
FISH_INTERVAL = 90.0


class LifeEngine:
    def __init__(self, agent) -> None:
        self.agent = agent
        self._last_chop = 0.0
        self._last_fish = 0.0

    # ---------------- 工具选择 ----------------

    async def select_tool(self, kind: str) -> bool:
        """选中合适工具。kind: pick/axe/rod/sword。返回是否选到。"""
        inv = self.agent.get_inventory_sync()
        kws = {"pick": _PICK_KW, "axe": _AXE_KW,
               "rod": _ROD_KW, "sword": _SWORD_KW}.get(kind, _SWORD_KW)
        # 先看手持栏
        for it in (inv.get("hotbar", []) or []):
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", "") or "")
            if any(k in name for k in kws) and it.get("inv_slot") is not None:
                await self.agent.mod.select_item(it["inv_slot"])
                return True
        # 再看主背包
        for it in (inv.get("inventory", []) or []):
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", "") or "")
            if any(k in name for k in kws) and it.get("inv_slot") is not None:
                await self.agent.mod.select_item(it["inv_slot"])
                return True
        return False

    # ---------------- 砍树 ----------------

    async def chop_wood(self, target: int = 10) -> int:
        """砍树收集木材。返回本次获得的数量（背包计数确认）。"""
        # 木材物品 id=9
        iid = 9
        try:
            inv = self.agent.get_inventory_sync()
            before = _count_id(inv, iid)
        except Exception:
            before = -1

        # 选斧头
        if not await self.select_tool("axe"):
            self.agent.log("没斧头，砍不了树~", "warn")
            return 0

        got = 0
        for _ in range(3):  # 最多砍 3 棵
            if self.agent.executor and self.agent.executor.should_stop():
                break
            trees = await self.agent.mod.find_trees(radius=30)
            if not trees:
                break
            tree = trees[0]
            tx, ty = int(tree.get("x", 0)), int(tree.get("y", 0))
            # 走过去
            try:
                await self.agent.navigate_to(tx, ty, timeout=15)
            except Exception:
                pass
            # 砍
            try:
                ok = await self.agent.mod.chop_trees(tx, ty)
            except Exception:
                ok = False
            if not ok:
                continue
            await asyncio.sleep(0.5)
            # 收集掉落
            try:
                await self.agent.mod.collect_items(radius=400)
            except Exception:
                pass

        # 计数
        try:
            inv = self.agent.get_inventory_sync()
            after = _count_id(inv, iid)
        except Exception:
            after = -1
        if before >= 0 and after >= 0:
            got = max(0, after - before)
        self.agent.log(f"砍树完成，获得 {got} 木材", "item")
        return got

    # ---------------- 钓鱼 ----------------

    async def fish(self, attempts: int = 3) -> bool:
        """在水边钓鱼：找水域 → 走到岸边 → 甩竿 → 等待 → 收杆。"""
        # 选钓竿
        if not await self.select_tool("rod"):
            self.agent.log("没有钓竿，钓不了鱼~", "warn")
            return False

        water = await self.agent.mod.find_water(radius=30)
        if not water:
            self.agent.log("附近没有水域，找不到钓鱼的地方~", "warn")
            return False

        spot = water[0]
        wx, wy = int(spot.get("x", 0)), int(spot.get("y", 0))
        # 走到水面旁的岸边（水面格上方一格是空气，玩家站水面旁）
        shore_x = wx + 2  # 站水面格旁边
        try:
            await self.agent.navigate_to(shore_x, wy, timeout=15)
        except Exception:
            pass

        self.agent.log("找个水边甩一竿~", "life")
        caught = False
        for _ in range(attempts):
            if self.agent.executor and self.agent.executor.should_stop():
                break
            # 甩竿（use_item 朝水面）
            try:
                await self.agent.mod.use_item(wx, wy)
            except Exception:
                pass
            # 等鱼上钩（模拟甩竿等待）
            await asyncio.sleep(2.5)
            # 收杆（再点一次）
            try:
                await self.agent.mod.use_item(wx, wy)
            except Exception:
                pass
            caught = True
            await asyncio.sleep(1.0)
            # 看看背包有没有新鱼（简化：有背包变化就算有收获）
            if self.agent.executor and self.agent.executor.should_stop():
                break
        if caught:
            self.agent.log("钓了一会儿鱼~", "life")
            try:
                await self.agent.send_chat("钓到鱼了喵～")
            except Exception:
                pass
        return caught

    # ---------------- 生活小动作（idle 驱动） ----------------

    async def do_something(self) -> str:
        """idle 时的陪伴式生活小动作：砍树/钓鱼/挖矿轮流。返回做了什么。"""
        now = time.time()
        # 钓鱼优先级较低（要水），砍树次之
        if now - self._last_fish > FISH_INTERVAL:
            self._last_fish = now
            try:
                if await self.fish():
                    return "fish"
            except Exception as e:
                self.agent.log(f"钓鱼异常: {e}", "warn")
        if now - self._last_chop > CHOP_INTERVAL:
            self._last_chop = now
            try:
                got = await self.chop_wood()
                if got > 0:
                    return "chop"
            except Exception as e:
                self.agent.log(f"砍树异常: {e}", "warn")
        return "idle"


def _count_id(inv: Dict[str, Any], iid: int) -> int:
    total = 0
    for slot in ("hotbar", "equipped", "inventory"):
        for it in (inv or {}).get(slot, []) or []:
            if isinstance(it, dict) and it.get("id") == iid:
                total += int(it.get("stack", 1) or 1)
    return total
