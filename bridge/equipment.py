"""装备管理：自动穿戴、使用、转交（丢地上给玩家）。"""

from typing import Any, Dict, List, Optional

from .mod_link import ModLink


class EquipmentManager:
    def __init__(self, mod: ModLink) -> None:
        self.mod = mod

    async def _all_items(self) -> List[Dict[str, Any]]:
        # 三大类展平为单列表，便于统一查找
        inv = await self.mod.get_inventory()
        return (inv.get("hotbar", []) + inv.get("equipped", [])
                + inv.get("inventory", []))

    async def auto_equip(self) -> bool:
        """自动穿上背包里防御更高的防具。

        mod 契约（C# SendInventory）：hotbar/inventory 条目带 inv_slot + defense；
        equipped 条目带 armor_slot + defense。装备命令 equip_item(inv_slot, armor_slot)。
        防具槽位限定 0-2（头/胸/腿），不碰饰品槽。
        """
        inv = await self._all_items()
        worn_def: Dict[int, int] = {}      # 已穿防具槽 → 防御
        for item in inv:
            slot = item.get("armor_slot")
            if slot is not None and 0 <= int(slot) < 3:
                worn_def[int(slot)] = max(
                    worn_def.get(int(slot), 0), int(item.get("defense", 0) or 0))
        # 背包候选：有 inv_slot、尚未穿着（equipped 条目没有 inv_slot）
        candidates = [i for i in inv
                      if i.get("inv_slot") is not None
                      and i.get("armor_slot") is None
                      and int(i.get("defense", 0) or 0) > 0]
        candidates.sort(key=lambda i: -int(i.get("defense", 0) or 0))
        ok = True
        for item in candidates:
            d = int(item.get("defense", 0) or 0)
            # 优先替换防御更低的已穿槽位
            target = None
            for slot, worn_d in sorted(worn_def.items(), key=lambda kv: kv[1]):
                if d > worn_d:
                    target = slot
                    break
            # 否则穿到空防具槽
            if target is None:
                used = set(worn_def)
                for s in range(3):
                    if s not in used:
                        target = s
                        break
            if target is None:
                continue
            if await self.mod.equip_item(item["inv_slot"], target):
                worn_def[target] = d
            else:
                ok = False
        return ok

    async def give_to_player(self, item_id: int, stack: int = 1) -> bool:
        return await self.mod.give_item(item_id, stack)

    async def drop_for_player(self, item_id: int, stack: int = 1) -> bool:
        # 在背包里找到该物品，丢到脚下让玩家拾取
        inv = await self._all_items()
        for it in inv:
            if it.get("id") == item_id and it.get("stack", 0) > 0:
                slot = it.get("inv_slot")
                if slot is None:
                    continue  # equipped 条目无 inv_slot，跳过
                return await self.mod.drop_item(slot, stack)
        return False

    async def use_by_name(self, name: str, resolve) -> bool:
        # resolve(name)->item_id；在背包里找到后选中并使用
        iid = resolve(name)
        if iid < 0:
            return False
        inv = await self._all_items()
        for it in inv:
            if it.get("id") == iid:
                slot = it.get("inv_slot")
                if slot is None:
                    continue
                return await self.mod.use_item_slot(slot)
        return False
