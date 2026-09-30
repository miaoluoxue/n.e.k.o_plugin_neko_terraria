"""世界推演：在脑子里把任务先跑一遍，再决定要不要动手。

原来的评估是"逐步独立检查"，会犯一个很蠢的错：
    ["挖10个铁", "合成铁镐"]
    → 检查第2步时问"现在有铁吗"，答"没有"，于是拒绝。
    可第1步明明就会挖到铁。

所以要有一份"虚拟背包"：从现在的真实背包出发，
一步步推演每步的产出与消耗，让后面的步骤看得到前面的成果。
这样才谈得上"会想"。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class VirtualInventory:
    """推演用的虚拟背包：真实库存的一份可涂改的副本。"""

    counts: Dict[str, int] = field(default_factory=dict)
    has_pickaxe: bool = False
    has_axe: bool = False
    has_rod: bool = False
    has_hook: bool = False
    rope: int = 0
    dirt: int = 0

    registry: Any = None

    def __post_init__(self) -> None:
        original = self.counts
        self.counts = {}
        for name, count in original.items():
            key = name if str(name).startswith("id:") else self.key(name)
            self.counts[key] = self.counts.get(key, 0) + count

    def copy(self) -> "VirtualInventory":
        return VirtualInventory(dict(self.counts), self.has_pickaxe,
                                self.has_axe, self.has_rod, self.has_hook,
                                self.rope, self.dirt, self.registry)

    def key(self, item) -> str:
        from .item_npc_dict import item_id
        from .recipe_book import CN_EN, _norm
        if str(item).isdecimal() and int(item) > 0:
            return f"id:{int(item)}"
        iid = item_id(item, self.registry)
        return f"id:{iid}" if iid > 0 else _norm(CN_EN.get(str(item), str(item)))

    def count(self, item: str) -> int:
        return int(self.counts.get(self.key(item), 0))

    def add(self, item: str, n: int) -> None:
        if not item or n <= 0:
            return
        key = self.key(item)
        self.counts[key] = self.counts.get(key, 0) + n
        info = self.registry.describe(item) if self.registry is not None else {}
        tags = info.get("tags", [])
        name = str(item).casefold()
        if "pickaxe" in tags or "镐" in name or "pickaxe" in name:
            self.has_pickaxe = True
        if "axe" in tags or "斧" in name or ("axe" in name and "pickaxe" not in name):
            self.has_axe = True
        if "fishing" in tags or "钓竿" in name or "fishingpole" in name.replace(" ", ""):
            self.has_rod = True
        if "hook" in tags or "钩" in name or "hook" in name:
            self.has_hook = True

    def take(self, item: str, n: int) -> bool:
        if n < 0 or self.count(item) < n:
            return False
        self.counts[self.key(item)] = self.count(item) - n
        return True


@dataclass
class SimStep:
    """一步推演的结果。"""

    index: int
    desc: str
    ok: bool = True
    gap: str = ""            # 缺什么（人话）
    need_item: str = ""      # 缺的物品，供补救用
    need_amount: int = 0
    produces: str = ""       # 这步会产出什么
    note: str = ""


@dataclass
class SimResult:
    """整段推演结果。"""

    steps: List[SimStep] = field(default_factory=list)
    final: Optional[VirtualInventory] = None
    ok: bool = True

    def first_gap(self) -> Optional[SimStep]:
        for s in self.steps:
            if not s.ok:
                return s
        return None

    def gaps(self) -> List[SimStep]:
        return [s for s in self.steps if not s.ok]


# 挖矿类动作默认产出自身
MINE_ACTIONS = ("mine", "gather")


class WorldModel:
    """从真实状态出发，推演一串步骤能不能顺利做完。"""

    def __init__(self, agent, book=None) -> None:
        self.agent = agent
        # 只使用当前游戏的真实配方，不用常识表猜模组配方。
        self.book = book if book is not None else getattr(
            agent, "recipe_book", None)

    # ---------- 快照 ----------
    async def snapshot(self) -> VirtualInventory:
        """把当前真实背包与能力拍成虚拟背包。"""
        vi = VirtualInventory(registry=getattr(self.agent, "registry", None))
        cap = self.agent.capability
        try:
            await cap.refresh()
            vi.has_pickaxe = cap.has_pickaxe()
            vi.has_axe = cap.has_axe()
            vi.has_rod = cap.has_rod()
            vi.has_hook = cap.has_hook()
            vi.rope = cap.rope_count()
            vi.dirt = cap.dirt_count()
        except Exception:
            pass

        try:
            inv = await self.agent.mod.get_inventory()
            self.agent._inv_full = inv
            seen = set()
            for kind in ("inventory", "hotbar"):
                for it in inv.get(kind, []):
                    name = it.get("name") or ""
                    slot = it.get("inv_slot")
                    if slot is not None and slot in seen:
                        continue
                    if slot is not None:
                        seen.add(slot)
                    if name:
                        identity = str(it["id"]) if int(it.get("id", 0)) > 0 else name
                        vi.add(identity, int(it.get("stack", 0) or 0))
        except Exception:
            pass
        return vi

    # ---------- 配方 ----------
    async def recipe_of(self, item: str) -> List[Tuple[str, int]]:
        recipe = await self.recipe_for(item)
        return list(recipe.materials) if recipe else []

    async def recipe_for(self, item, inventory=None, amount=1, recipe_index=None):
        if self.book is None:
            return None
        await self.book.refresh()
        return self.book.find(item, inventory, amount, recipe_index)

    async def stations_of(self, item: str) -> List[str]:
        recipe = await self.recipe_for(item)
        return list(recipe.stations) if recipe else []

    async def station_ready(self, item: str, recipe=None) -> Tuple[bool, str]:
        recipe = recipe or await self.recipe_for(item)
        if recipe is None:
            return False, "未同步到真实配方"
        if recipe.environment_ready:
            return True, ""
        from .recipe_book import station_cn
        reasons = [station_cn(s) for s in recipe.stations]
        reasons.extend(c.get("name", "") for c in recipe.conditions if not c.get("met"))
        return False, "、".join(reasons) or "未确认合成环境条件"

    # ---------- 推演 ----------
    async def simulate(self, steps: List[Dict[str, Any]],
                       start: Optional[VirtualInventory] = None) -> SimResult:
        """按顺序推演每一步，让后面的步骤看得到前面的产出。"""
        vi = (start or await self.snapshot()).copy()
        res = SimResult(steps=[], final=vi)
        if self.book is not None and any(s.get("action") == "craft" for s in steps):
            await self.book.refresh_availability()

        for i, s in enumerate(steps):
            action = str(s.get("action", "")).lower()
            item = str(s.get("item", "") or "")
            amt = int(s.get("amount", 1) or 1)
            st = SimStep(index=i + 1, desc=self._desc(action, item, amt))

            if action in MINE_ACTIONS:
                if not vi.has_pickaxe:
                    st.ok = False
                    st.gap = "没有镐子，挖不了"
                    st.need_item = "镐"
                else:
                    have = vi.count(item)
                    if have >= amt:
                        # 已经够了，这步其实可以省掉
                        st.note = f"背包里已经有 {have} 个{item}，够了"
                    vi.add(item, amt)
                    st.produces = item

            elif action == "craft":
                recipe = await self.recipe_for(item, vi, amt, s.get("recipe_index"))
                if recipe is None:
                    st.ok = False
                    st.gap = f"没有同步到{item}的唯一真实配方"
                    st.need_item, st.need_amount = item, amt
                else:
                    s["recipe_index"] = recipe.recipe_index
                    takes, missing = recipe.requirements(amt, vi)
                    ok_station, lack_station = await self.station_ready(item, recipe)
                    if missing:
                        st.ok = False
                        st.gap = "、".join(f"缺 {n} 个{m}" for m, n in missing)
                        st.need_item, st.need_amount = missing[0]
                    elif not ok_station:
                        st.ok = False
                        st.gap = f"合成条件未满足：{lack_station}"
                    else:
                        for name, count in takes:
                            vi.take(name, count)
                        vi.add(item, recipe.amount * recipe.batches(amt))
                        st.produces = item
                        st.note = recipe.say()

            elif action == "fetch":
                # 取箱子里的东西：能不能取到得问真实世界
                chest = None
                try:
                    chest = await self.agent.nearest_chest_with(item)
                except Exception:
                    chest = None
                if chest is None:
                    st.ok = False
                    st.gap = f"附近箱子里没有 {item}"
                    st.need_item = item
                    st.need_amount = amt
                else:
                    vi.add(item, amt)
                    st.produces = item
                    st.note = f"在箱子({chest.get('x')},{chest.get('y')})"

            elif action == "chop":
                # 砍树：得有斧头（能力物品）
                if not vi.has_axe:
                    st.ok = False
                    st.gap = "没有斧头，砍不了树"
                    st.need_item = "斧"
                    st.need_amount = 1
                else:
                    st.produces = item or "木材"
                    vi.add(st.produces, amt)
                    st.note = "用斧头砍"

            elif action == "fish":
                # 钓鱼：得有钓竿
                if not vi.has_rod:
                    st.ok = False
                    st.gap = "没有钓竿，钓不了鱼"
                    st.need_item = "钓竿"
                    st.need_amount = 1
                else:
                    st.produces = item or "鱼"
                    st.note = "用钓竿钓"

            elif action == "give":
                if vi.count(item) < amt:
                    st.ok = False
                    st.gap = f"身上只有 {vi.count(item)} 个{item}，不够给"
                    st.need_item = item
                    st.need_amount = amt - vi.count(item)
                else:
                    vi.take(item, amt)

            elif action in ("climb", "goto"):
                tx, ty = int(s.get("x", 0) or 0), int(s.get("y", 0) or 0)
                try:
                    plan = await self.agent.planner.plan_climb(tx, ty)
                    if plan.feasible:
                        st.note = f"要{len(plan.legs)}段：{plan.describe()}"
                    else:
                        st.ok = False
                        st.gap = plan.blocked_reason or "上不去"
                        st.need_item = "钩爪"
                except Exception:
                    st.note = "路线待定"

            res.steps.append(st)

        res.final = vi
        res.ok = all(s.ok for s in res.steps)
        return res

    @staticmethod
    def _desc(action: str, item: str, amt: int) -> str:
        table = {"mine": "挖", "gather": "挖", "craft": "合成",
                 "fetch": "取", "give": "给主人", "climb": "爬到",
                 "goto": "走到", "follow": "回到主人身边",
                 "chop": "砍", "fish": "钓"}
        head = table.get(action, action)
        if action in ("climb", "goto", "follow"):
            return head
        return f"{head}{item}x{amt}" if item else head
