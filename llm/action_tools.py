"""猫娘动作工具：warp / summon / mine / craft / give / equip。

重构说明：
- 移除 bot.warp 等直接调用
- 改为通过 ModLink 实现
"""

from typing import Any, Dict

from plugin.sdk.plugin import Ok

from ..bridge.executor import SRC_OWNER


class ActionToolsMixin:
    _agent: Any
    async def llm_warp(self, *, x: int, y: int, **_) -> Dict[str, Any]:
        ok = await self._agent.mod.warp_to(x, y)
        return Ok({"output": f"已传送到 ({x},{y})" if ok else "传送失败"})

    async def llm_mine(self, *, item: str, amount: int = 10, **_) -> Dict[str, Any]:
        # fire-and-forget：发起挖矿（executor 后台任务执行），立即返回 ack，
        # 完成时由 brain 的 task_done 回调把结果推回 LLM（参照 Minecraft 插件做法）
        # —— LLM 回合不被长任务阻塞，猫娘可以边挖边陪主人聊天
        import asyncio

        async def _mine(info):
            _iid, mined = await self._agent.mining.mine_target(item, amount)
            return {"ok": True, "output": f"挖了 {mined} 个 {item}"}

        ex = self._agent.executor
        try:
            asyncio.get_running_loop().create_task(
                ex.run(f"挖{amount}个{item}", _mine, source=SRC_OWNER))
        except Exception as e:
            return Ok({"output": f"挖矿发起失败喵（{e}）"})
        return Ok({"output": f"✅ 已受理：开始挖 {amount} 个 {item}。\n"
                             f"⏳ 这条消息只代表任务下达成功，挖矿仍在执行中，"
                             f"完成后会汇报喵~"})

    async def llm_craft(self, *, item: str, amount: int = 1, **_) -> Dict[str, Any]:
        iid = self._agent.resolve_item(item)
        if iid < 0:
            return Ok({"output": f"我不认识 {item} 哦"})
        n = await self._agent.mod.craft(item_id=iid, amount=amount)
        if n <= 0:
            return Ok({"output": f"{item} 合成失败（材料不足或缺少合成站）"})
        return Ok({"output": f"合成了 {n} 个 {item}"})

    async def llm_give(self, *, item: str, stack: int = 1, **_) -> Dict[str, Any]:
        ok = await self._agent.equip.give_to_player(
            self._agent.resolve_item(item), stack)
        return Ok({"output": "已给玩家" if ok else "给物品失败"})

    async def llm_give_to_me(self, *, item: str, stack: int = 1, **_) -> Dict[str, Any]:
        ok = await self._agent.hand_to_player(item, stack)
        return Ok({"output": f"已丢下 {stack} 个 {item}，快捡~" if ok
                   else "我背包里没有这个哦"})

    async def llm_use_item(self, *, item: str, **_) -> Dict[str, Any]:
        ok = await self._agent.use_item_by_name(item)
        return Ok({"output": f"用了 {item}" if ok else "用不了这个"})

    async def llm_list_inventory(self, **_) -> Dict[str, Any]:
        inv = await self._agent.get_inventory()
        items = []
        for kind in ("hotbar", "equipped", "inventory"):
            for it in inv.get(kind, []):
                items.append(f"{it.get('name', '?')}x{it.get('stack', 0)}")
        return Ok({"output": "我的物品：" + "、".join(items) if items
                   else "我现在是空的~"})

    async def llm_where_is(self, *, item: str, **_) -> Dict[str, Any]:
        await self._agent.refresh_inventory()  # 按需刷新背包，保证查到新物品
        loc = self._agent.locate_item(item)
        if not loc.get("found"):
            return Ok({"output": f"我哪里都没有 {item} 哦"})
        where = loc["where"]
        if where == "chest":
            c = loc["chest"]
            return Ok({"output": f"{item} 在箱子({c['x']},{c['y']})里，共 {loc['stack']} 个"})
        name_map = {"inventory": "背包", "hotbar": "手持栏", "equipped": "装备栏"}
        return Ok({"output": f"{item} 在我{name_map.get(where, where)}里，共 {loc['stack']} 个"})

    async def llm_find_items(self, *, tag: str, **_) -> Dict[str, Any]:
        out_names = []
        for mod, items in self._agent.registry.mods.items():
            t = self._agent.registry.tags.get(mod, {})
            for name, iid in items.items():
                if tag in t.get(name, []):
                    out_names.append(name)
        if not out_names:
            return Ok({"output": f"我不认识任何用于「{tag}」的物品哦"})
        return Ok({"output": f"用于「{tag}」的物品有：" + "、".join(out_names[:15])})

    async def llm_store(self, *, item: str, stack: int = 1,
                        chest_x: int = -1, chest_y: int = -1, **_) -> Dict[str, Any]:
        await self._agent.refresh_inventory()  # 按需刷新背包，找到物品槽位
        if chest_x >= 0 and chest_y >= 0:
            chest = {"x": chest_x, "y": chest_y}
        else:
            chest = await self._agent.nearest_chest_with(item)
            if chest is None:
                chests = await self._agent.mod.enum_chests()
                chest = chests[0] if chests else None
        if not chest:
            return Ok({"output": "附近没有箱子，先放不进去哦"})
        ok = await self._agent.store_to_chest(item, chest, stack)
        return Ok({"output": f"已放进箱子({chest['x']},{chest['y']})" if ok
                   else "放不进去（可能不在箱子旁或背包没有）"})

    async def llm_take(self, *, item: str, stack: int = 1,
                       chest_x: int = -1, chest_y: int = -1, **_) -> Dict[str, Any]:
        if chest_x >= 0 and chest_y >= 0:
            chest = {"x": chest_x, "y": chest_y}
        else:
            chest = await self._agent.nearest_chest_with(item)
        if not chest:
            return Ok({"output": "附近没有含这个的箱子哦"})
        ok = await self._agent.take_from_chest(item, chest, stack)
        return Ok({"output": f"已从箱子({chest['x']},{chest['y']})取出 {item}" if ok
                   else "取不出来"})

    async def llm_task(self, *, steps: list, goal: str = "", **_) -> Dict[str, Any]:
        # fire-and-forget：三阶段（想→处理→做）后台执行，立即返回 ack，
        # 完成/失败由 executor 回调推回 LLM（参照 Minecraft 插件做法）
        import asyncio
        try:
            asyncio.get_running_loop().create_task(
                self._agent.run_complex_task(steps, goal, source=SRC_OWNER))
        except Exception as e:
            return Ok({"output": f"任务发起失败喵（{e}）"})
        return Ok({"output": f"✅ 已受理：开始执行任务（{goal or '多步任务'}）。\n"
                             f"⏳ 这条消息只代表任务下达成功，任务仍在执行中，"
                             f"完成后会汇报喵~"})

    async def llm_chain(self, *, steps: list, **_) -> Dict[str, Any]:
        from ..bridge.task_chain import Goal
        if not steps:
            return Ok({"output": "步骤列表是空的喵~"})
        goals = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            goals.append(Goal(
                goal_type=str(s.get("goal_type", "mine")),
                target=str(s.get("target", "") or ""),
                amount=int(s.get("amount", 10) or 10),
                craft_first=bool(s.get("craft_first")),
                deliver_to_player=bool(s.get("deliver_to_player")),
                reason="主人下达的多步任务",
            ))
        if not goals:
            return Ok({"output": "步骤格式不对喵~"})
        # fire-and-forget：链后台执行，完成/失败由 executor/task 回调推回
        import asyncio
        try:
            asyncio.get_running_loop().create_task(
                self._agent.tasks.submit_sequence(goals))
        except Exception as e:
            return Ok({"output": f"任务链发起失败喵（{e}）"})
        names = " → ".join(f"{g.goal_type}:{g.target}" for g in goals)
        return Ok({"output": f"✅ 已受理：开始按顺序执行 {names}。\n"
                             f"⏳ 这条消息只代表任务下达成功，任务仍在执行中，"
                             f"完成后会汇报喵~"})

    async def llm_assess(self, *, steps: list, goal: str = "", **_) -> Dict[str, Any]:
        res = await self._agent.run_complex_task(steps, goal, dry_run=True)
        out = res.get("output", "")
        fixes = res.get("fixes") or []
        if fixes:
            out += "（我会先自己解决：" + "、".join(fixes) + "）"
        if res.get("need"):
            out += f"（{res['need']}）"
        return Ok({"output": out})

    async def llm_recipe(self, *, item: str, **_) -> Dict[str, Any]:
        book = self._agent.recipe_book
        await book.refresh()
        r = book.find(item)
        if r is None:
            uses = book.used_in(item)
            if uses:
                names = "、".join(x.name for x in uses[:5])
                return Ok({"output": f"{item}做不出来，不过它能用来做：{names}"})
            return Ok({"output": f"我不知道{item}怎么做，可能配方还没同步~"})
        src = f"（{r.mod} 的东西）" if r.is_modded() else ""
        tail = "，现在就能做" if r.available else "，不过现在还差点条件"
        return Ok({"output": f"{r.say()}{src}{tail}"})

    async def llm_why(self, **_) -> Dict[str, Any]:
        a = self._agent.brain.last_assessment()
        if a is None:
            return Ok({"output": "我还没想过什么复杂任务呢~"})
        body = a.explain() or a.say()
        return Ok({"output": f"我是这么想的：\n{body}"})

    async def llm_task_status(self, **_) -> Dict[str, Any]:
        cur = self._agent.executor.current()
        longs = self._agent.longterm.active()
        parts = []
        if cur:
            src = "主人交代的" if cur["source"] == "owner" else "我自己找的活"
            parts.append(f"正在做「{cur['name']}」（{src}），进度 {cur['step']}，"
                         f"当前：{cur['note']}，已经 {cur['elapsed']} 秒了")
        for t in longs:
            parts.append(t["say"])
        if not parts:
            last = self._agent.executor.last_result()
            if last:
                return Ok({"output": f"现在空着呢~ 上一件事：{last.get('output', '')}"})
            return Ok({"output": "现在空着呢，随时可以派活给我~"})
        return Ok({"output": "；".join(parts)})

    async def llm_keep_doing(self, *, kind: str, target: str = "",
                             amount: int = 0, **_) -> Dict[str, Any]:
        res = await self._agent.start_longterm(kind, target=target, amount=amount,
                                               reason="主人的长期要求")
        out = res.get("output", "")
        return Ok({"output": f"{out}\n"
                             f"✅ 已受理：这是长期任务，只代表任务下达成功，"
                             f"会一直执行到主人喊停喵~"})

    async def llm_stop_doing(self, *, kind: str = "", **_) -> Dict[str, Any]:
        res = await self._agent.stop_longterm(kind)
        return Ok({"output": res.get("output", "")})

    async def llm_climb(self, *, x: int, y: int, **_) -> Dict[str, Any]:
        ok = await self._agent.climb_to(x, y)
        return Ok({"output": f"已经爬到 ({x},{y}) 啦~" if ok else "这次没爬上去，我停在原地了"})

    async def llm_plan_climb(self, *, x: int, y: int, **_) -> Dict[str, Any]:
        plan = await self._agent.planner.plan_climb(x, y)
        head = "能上去" if plan.feasible else "上不去"
        return Ok({"output": f"{head}：{plan.describe()}"})

    async def llm_capabilities(self, **_) -> Dict[str, Any]:
        caps = await self._agent.capability.refresh()
        if not caps:
            return Ok({"output": "我还不清楚自己能力，稍等"})
        parts = []
        parts.append("钩锁：" + ("有" if caps.get("has_hook") else "无"))
        parts.append(f"土块：{caps.get('dirt_count', 0)}")
        parts.append("镐子：" + ("有" if caps.get("has_pickaxe") else "无"))
        parts.append(f"绳/梯：{caps.get('rope_count', 0)}")
        return Ok({"output": "我当前能力 → " + "、".join(parts)})

    async def llm_status(self, **_) -> Dict[str, Any]:
        state = self._agent.get_state()
        world = getattr(self._agent, "_world_info", {}) or {}
        inv = await self._agent.get_inventory()

        hp = state.get("hp", 0)
        max_hp = state.get("max_life", 0) or 100
        mp = state.get("mp", 0)
        max_mp = state.get("max_mp", 0) or 20
        # mod 返回 tile 坐标（mod_link.get_state），不是像素 x/y
        tx = int(state.get("tile_x", 0) or 0)
        ty = int(state.get("tile_y", 0) or 0)

        out_parts = [
            f"HP:{hp}/{max_hp} MP:{mp}/{max_mp}",
            f"位置:Tile({tx},{ty})",
            f"世界:{world.get('world_name','未知')} 难度:{world.get('difficulty','?')} 时段:{state.get('time_of_day', '?')}",
        ]

        nearby = state.get("nearby_players", [])
        if nearby:
            pstrs = [f"{p.get('name','?')}@({p.get('tile_x',0)},{p.get('tile_y',0)})"
                     for p in nearby[:3]]
            out_parts.append("附近:" + "、".join(pstrs))
        else:
            out_parts.append("附近:无其他玩家")

        hotbar = inv.get("hotbar", [])
        if hotbar:
            items = []
            for s in hotbar:
                name = s.get("name", "")
                stack = s.get("stack", 0)
                if name and stack:
                    items.append(f"{name}x{stack}")
            if items:
                out_parts.append("快捷栏:" + " ".join(items))

        return Ok({"output": "\n".join(out_parts)})
