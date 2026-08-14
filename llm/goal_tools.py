"""猫娘目标指令工具：set_goal / interrupt / send_chat。"""

from typing import Any, Dict

from plugin.sdk.plugin import Ok, llm_tool


class GoalToolsMixin:
    _agent: Any
    _autonomous_brain: Any
    async def llm_set_goal(self, *, goal_type: str, target: str,
                           reason: str = "", amount: int = 10,
                           deliver_to_player: bool = False,
                           equip_self: bool = False, **_) -> Dict[str, Any]:
        from ..bridge.task_chain import Goal
        goal = Goal(
            goal_type=goal_type, target=target,
            reason=reason,
            amount=amount,
            deliver_to_player=deliver_to_player,
            equip_self=equip_self,
        )
        await self._agent.submit_goal(goal)
        return Ok({"output": f"已下达目标：{goal_type} {target}"})

    async def llm_interrupt(self, *, reason: str = "", **_) -> Dict[str, Any]:
        why = reason or "主人有新指令"
        cur = self._agent.executor.current()
        stopped = await self._agent.interrupt_current(why)
        # 同步清掉自主大脑的动机残留，避免刚打断又自己跑掉
        # v2.1: 带 level=4 (HARD) —— 该工具语义就是"立即停止一切执行新指令"
        if self._autonomous_brain:
            await self._autonomous_brain.bus.publish(
                "interrupt",
                {"level": 4, "reason": why,
                 "task_name": cur.get("name", "") if cur else ""})
        if stopped and cur:
            return Ok({"output": f"已停下「{cur['name']}」（做到 {cur['step']}），听你的~"})
        return Ok({"output": "现在没有在做的任务，随时听你安排~"})

    async def llm_chat(self, *, text: str, **_) -> Dict[str, Any]:
        await self._agent.send_chat(text)
        return Ok({"output": "已发送"})

    @llm_tool(
        name="terraria_command",
        description="【主人下游戏指令的主入口】把主人用自然语言说的话（中文/英文均可）原样传入，"
                    "由猫娘内置指令解析器判断并执行：挖矿('挖10个铁')、跟随('跟着我')、"
                    "守点('守在这')、停止('别挖了')、以及任何多步任务。只要是游戏内操作指令就调本工具，"
                    "不要调用 terraria_chat。解析失败会返回'没听懂'，成功则开始执行。",
        parameters={"type": "object",
                    "properties": {"text": {"type": "string",
                                            "description": "主人下达的游戏指令原文"}},
                    "required": ["text"]},
    )
    async def llm_command(self, *, text: str, **_) -> Dict[str, Any]:
        # 自然语言直达执行：command -> coordinator.handle -> intent.parse
        # 这是确定性解析，不依赖 LLM 二次判断，确保"说出口就做"
        self._agent.logger.info(f"[llm_command] 📥 收到指令: {text}")
        res = await self._agent.command(text, source="owner")
        self._agent.logger.info(f"[llm_command] 📤 执行结果: {res}")
        mode = res.get("mode", "")
        output = res.get("output", str(res))

        # 长期任务：明确告诉 LLM 这是持续任务，不是已完成
        if mode == "longterm":
            self._agent.logger.info(f"[llm_command] ✅ 识别为长期任务: mode={mode}")
            return Ok({
                "output": output,
                "status": "running",
                "note": "这是长期任务，会在后台持续执行直到主人喊停"
            })

        # 有限任务/其他：正常返回
        self._agent.logger.info(f"[llm_command] ℹ️ 返回结果: mode={mode}")
        return Ok({"output": output})

    async def llm_attack(self, *, target: str = "", timeout: int = 30, **_) -> Dict[str, Any]:
        state = self._agent.get_state()
        combat = self._agent.combat

        if target:
            enemies = state.get("nearby_npcs", [])
            found = False
            for e in enemies:
                if target.lower() in e.get("name", "").lower():
                    found = True
                    break
            if not found:
                return Ok({"output": f"附近没有找到 {target} 喵"})

        nearby = state.get("nearby_npcs", [])
        if not nearby or len(nearby) == 0:
            return Ok({"output": "附近没有敌人呀，很安全喵~"})

        success = await combat.fight_nearest(state, timeout=timeout)
        if success:
            return Ok({"output": "打败了！主人看我厉害吧~"})
        else:
            return Ok({"output": "没打过...可能隔墙了或者太强了喵"})

    async def llm_flee(self, *, distance: int = 30, **_) -> Dict[str, Any]:
        state = self._agent.get_state()
        px = int(state.get("tile_x", 0))
        py = int(state.get("tile_y", 0))

        enemies = state.get("nearby_npcs", [])
        if not enemies:
            return Ok({"output": "这里没有危险呀，不用跑喵~"})

        avg_ex = sum(int(e.get("tile_x", px)) for e in enemies) / len(enemies)
        direction = -1 if avg_ex > px else 1
        target_x = px + direction * distance

        success = await self._agent.navigate_to(target_x, py, timeout=10)
        if success:
            return Ok({"output": f"呼~逃出来了，主人快看我跑了{distance}格！"})
        else:
            return Ok({"output": "跑的时候被卡住了...主人救命喵！"})

    async def llm_explore(self, *, direction: str = "random", distance: int = 50, **_) -> Dict[str, Any]:
        state = self._agent.get_state()
        px = int(state.get("tile_x", 0))
        py = int(state.get("tile_y", 0))

        if direction == "left":
            target_x, target_y = px - distance, py
        elif direction == "right":
            target_x, target_y = px + distance, py
        elif direction == "up":
            target_x, target_y = px, py - distance
        elif direction == "down" or direction == "underground":
            target_x, target_y = px, py + distance
        else:
            import random
            dx = random.choice([-1, 1]) * distance
            target_x, target_y = px + dx, py

        success = await self._agent.navigate_to(target_x, target_y, timeout=30)
        if success:
            new_state = self._agent.get_state()
            biome = new_state.get("biome", "未知地带")
            return Ok({"output": f"探索到{biome}了！主人快来看~"})
        else:
            return Ok({"output": f"向{direction}走了一段，但被地形卡住了喵"})

    async def llm_move(self, *, x: int = None, y: int = None,
                       direction: str = "", distance: int = 10, **_) -> Dict[str, Any]:
        state = self._agent.get_state()
        px = int(state.get("tile_x", 0))
        py = int(state.get("tile_y", 0))

        if x is not None and y is not None:
            target_x, target_y = x, y
        elif direction:
            if direction == "left":
                target_x, target_y = px - distance, py
            elif direction == "right":
                target_x, target_y = px + distance, py
            elif direction == "up":
                target_x, target_y = px, py - distance
            elif direction == "down":
                target_x, target_y = px, py + distance
            else:
                return Ok({"output": "方向不对劲...是left/right/up/down其中一个吗？"})
        else:
            return Ok({"output": "主人要我去哪呀？说个坐标或方向吧~"})

        success = await self._agent.navigate_to(target_x, target_y, timeout=20)
        if success:
            return Ok({"output": f"到了喵！现在在({target_x}, {target_y})"})
        else:
            return Ok({"output": "走不过去...路被挡住了或者太远了喵"})

    async def llm_guard(self, *, range: int = 15, **_) -> Dict[str, Any]:
        res = await self._agent.start_longterm("guard", reason=f"守护半径{range}格", range=range)
        return Ok({
            "output": res.get("output", "好的，我会守在这里的！"),
            "status": "running",
            "note": "这是长期任务，会持续守护直到主人喊停"
        })

