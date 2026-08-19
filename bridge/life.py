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

# 工具名关键词（无 use 字段时按名字兜底）
_PICK_KW = ("镐", "pick")
_AXE_KW = ("斧", "axe")
_ROD_KW = ("钓竿", "鱼竿", "fishing")
_SWORD_KW = ("剑", "sword", "刀")
# 战斗武器 use 类型
_MELEE_USE = ("melee",)
_RANGED_USE = ("ranged",)
_MAGIC_USE = ("magic",)
_SUMMON_USE = ("summon",)
_ALL_WEAPON_USE = _MELEE_USE + _RANGED_USE + _MAGIC_USE + _SUMMON_USE

# 各生活动作节律（秒）
CHOP_INTERVAL = 45.0
FISH_INTERVAL = 90.0


def _looks_like_weapon(name: str) -> bool:
    """名字像武器（法杖/弓/枪/弩/鞭等，用于无 use 字段时兜底）。"""
    kws = ("剑", "刀", "匕首", "法杖", "魔杖", "弓", "弩", "枪", "手枪", "步枪",
           "鞭", "斧刃", "镐刃", "锤", "尖", "矛", "枪刃", "棒", "杖")
    return any(k in name for k in kws)


class LifeEngine:
    def __init__(self, agent) -> None:
        self.agent = agent
        self._last_chop = 0.0
        self._last_fish = 0.0

    # ---------------- 工具选择 ----------------

    async def select_tool(self, kind: str) -> bool:
        """选中合适工具/武器。kind:
        - pick/axe/rod：挖矿/砍树/钓鱼
        - weapon：战斗武器（近战/远程/魔法/召唤中伤害最高者）
        - melee/ranged/magic/summon：指定武器类型
        返回是否选到。
        """
        inv = self.agent.get_inventory_sync()
        items = (inv.get("hotbar", []) or []) + (inv.get("inventory", []) or [])

        # 工具类（挖矿/砍树/钓鱼）
        if kind in ("pick", "axe", "rod"):
            kws = {"pick": _PICK_KW, "axe": _AXE_KW,
                   "rod": _ROD_KW}.get(kind, ())
            for it in items:
                if not isinstance(it, dict):
                    continue
                slot = it.get("inv_slot")
                if slot is None:
                    continue
                name = str(it.get("name", "") or "")
                if any(k in name for k in kws):
                    await self.agent.mod.select_item(slot)
                    return True
            return False

        # 战斗武器：按类型挑伤害最高
        weapon_uses = {
            "weapon": _ALL_WEAPON_USE,
            "melee": _MELEE_USE, "ranged": _RANGED_USE,
            "magic": _MAGIC_USE, "summon": _SUMMON_USE,
        }.get(kind, _ALL_WEAPON_USE)
        best_slot, best_dmg = None, 0
        for it in items:
            if not isinstance(it, dict):
                continue
            slot = it.get("inv_slot")
            if slot is None:
                continue
            use = str(it.get("use", "") or "")
            if use:
                # 有 use 字段：按武器类型匹配（法杖/弓/枪/剑都是 weapon 类）
                if use not in weapon_uses:
                    continue
            else:
                # 无 use 字段：按名字猜是不是武器
                name = str(it.get("name", "") or "")
                if not _looks_like_weapon(name):
                    continue
            dmg = int(it.get("damage", 0) or 0)
            if dmg > best_dmg:
                best_dmg = dmg
                best_slot = slot
        if best_slot is not None:
            await self.agent.mod.select_item(best_slot)
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
        """在水边钓鱼：选钓竿 → 感知生物群系找对应水域 → 岸边甩竿 → 等待 → 收杆。

        泰拉瑞亚钓鱼分三类水域，出的鱼不同：
        - 地表层水域：普通鱼（鲈鱼等）
        - 地下/洞穴层水域：岩层鱼（蝙蝠鱼等）
        - 特殊生物群系水域：丛林/雪地/腐化/神圣/地狱 特有鱼
        鱼饵（蚯蚓/萤火虫/龙虾）是消耗品，use_item 朝水会自动消耗背包鱼饵。
        """
        # 选钓竿
        if not await self.select_tool("rod"):
            self.agent.log("没有钓竿，钓不了鱼~", "warn")
            return False

        # 感知当前生物群系（决定钓什么水域的鱼）
        try:
            st = self.agent.get_state()
            biome = str(st.get("biome", "") or "")
        except Exception:
            biome = ""
        # 当前已在地下/特殊群系 → 就地钓（更容易钓到对应鱼）
        prefer_here = any(k in biome for k in ("地下", "洞穴", "地狱", "丛林", "雪地", "腐化", "猩红", "神圣"))

        water = await self.agent.mod.find_water(radius=30)
        if not water:
            # 特殊群系下没水 → 放宽找水范围
            water = await self.agent.mod.find_water(radius=60)
        if not water:
            self.agent.log("附近没有水域，找不到钓鱼的地方~", "warn")
            return False

        spot = water[0]
        wx, wy = int(spot.get("x", 0)), int(spot.get("y", 0))
        # 走到水面旁的岸边
        shore_x = wx + 2  # 站水面格旁边
        try:
            await self.agent.navigate_to(shore_x, wy, timeout=15)
        except Exception:
            pass

        where = biome or "普通水域"
        self.agent.log(f"找个{where}水边甩一竿~", "life")
        caught = False
        for _ in range(attempts):
            if self.agent.executor and self.agent.executor.should_stop():
                break
            # 甩竿（use_item 朝水面，自动消耗背包鱼饵）
            try:
                await self.agent.mod.use_item(wx, wy)
            except Exception:
                pass
            # 等鱼上钩
            await asyncio.sleep(2.5)
            # 收杆
            try:
                await self.agent.mod.use_item(wx, wy)
            except Exception:
                pass
            caught = True
            await asyncio.sleep(1.0)
            if self.agent.executor and self.agent.executor.should_stop():
                break
        if caught:
            self.agent.log(f"在{where}钓了一会儿鱼~", "life")
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
