"""任务链引擎：将目标编排为多步行为（挖矿→合成→给玩家/自穿）。"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .equipment import EquipmentManager
from .mining import MiningEngine
from .mod_link import ModLink


@dataclass
class Goal:
    goal_type: str
    target: str
    reason: str = ""
    amount: int = 10
    deliver_to_player: bool = False
    craft_first: bool = False
    equip_self: bool = False
    interrupt: bool = False
    report_fail: str = ""   # 步骤失败时向主人汇报的话


class TaskChain:
    def __init__(self, mining: MiningEngine, mod: ModLink,
                 equip: EquipmentManager, agent=None) -> None:
        self.mining = mining
        self.mod = mod
        self.equip = equip
        self.agent = agent
        self._queue: asyncio.Queue[Goal] = asyncio.Queue(maxsize=1)
        self._current: Optional[Goal] = None
        self._chain: List[str] = []      # 决策链：每步结果，供解释
        self._step: str = ""             # 当前进度 "2/4"
        self._last_ok: bool = True       # 上一步是否成功，用于中止后续

    async def submit(self, goal: Goal) -> None:
        if goal.interrupt and self._current:
            self.mining.cancel()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self._queue.put(goal)

    async def submit_sequence(self, goals: List[Goal]) -> None:
        # 串行多步骤：等上一步真正执行完再下一步；任一步失败则中止后续（不自动重试）
        self._chain = []
        total = len(goals)
        for i, g in enumerate(goals):
            self._step = f"{i+1}/{total}"
            await self.submit(g)
            # 等这一步被取走并执行完毕（队列空 且 无正在执行的目标）
            while not self._queue.empty() or self._current is not None:
                await asyncio.sleep(0.2)
            if not self._last_ok:
                self._chain.append(f"{i+1}.{g.goal_type}:{g.target} 失败，中止")
                if self.agent:
                    self.agent.log(
                        f"多步任务在第{i+1}/{total}步中止：{g.goal_type} {g.target}", "warn")
                return
            self._chain.append(f"{i+1}.{g.goal_type}:{g.target} 完成")
        self._step = ""

    def chain(self) -> List[str]:
        # 决策链：每步做了什么、在哪步停的，供 UI/汇报解释
        return list(self._chain)

    async def run_one(self, goal: Goal) -> bool:
        """直接执行单个目标并返回结果（不经队列），供执行器逐步驱动。"""
        self._current = goal
        try:
            self.mining.reset()
            return await self._execute(goal)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.agent:
                self.agent.log(f"步骤异常：{goal.goal_type} {goal.target} → {e}", "warn")
            return False
        finally:
            self._current = None

    async def run_loop(self) -> None:
        while True:
            goal = await self._queue.get()
            self._current = goal
            try:
                ok = await self._execute(goal)
            except asyncio.CancelledError:
                self._current = None  # 取消时也要复位，防止 submit_sequence 死等
                raise
            except Exception as e:
                ok = False
                if self.agent:
                    self.agent.log(f"任务异常：{e}", "warn")
            self._last_ok = ok
            if not ok and goal.report_fail and self.agent:
                await self.agent.send_chat(goal.report_fail)
            self._current = None

    async def _execute(self, goal: Goal) -> bool:
        # follow：启动长期跟随，等待其真正结束（被主动停止）才算完成
        if goal.goal_type == "follow":
            try:
                if self.agent:
                    res = await self.agent.start_longterm(
                        "follow", reason=goal.reason or "跟着主人")
                    if not res.get("ok"):
                        return False
                    # 等待长期任务的 runner 结束（被 stop 或异常退出）
                    lt = self.agent.longterm
                    runner = lt._runners.get("follow")
                    if runner:
                        await runner  # 阻塞到 follow_loop 退出
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            return False

        # explore：探索（"去地下看看"、"帮我找铁矿"）
        if goal.goal_type == "explore":
            # 如果 target 是方向（left/right/地下/上方）
            if goal.target in ("left", "right", "左", "右"):
                direction = -1 if goal.target in ("left", "左") else 1
                # 向某方向探索一段距离（v0.8：检查导航结果，失败不算完成）
                try:
                    st = self.agent.get_state()
                    start_x = st.get("tile_x", 0)
                    target_x = start_x + direction * 100
                    return bool(await self.agent.navigate_to(target_x, timeout=30))
                except Exception:
                    return False
            elif goal.target in ("地下", "下方", "underground"):
                # 向下探索
                try:
                    st = self.agent.get_state()
                    start_y = st.get("tile_y", 0)
                    target_y = start_y + 50
                    return bool(await self.agent.navigate_to(
                        st.get("tile_x", 0), target_y, timeout=30))
                except Exception:
                    return False
            else:
                # 目标性探索：找某物 → 尝试挖矿引擎找到并挖（v0.8：不再直接算成功）
                try:
                    _iid, mined = await self.mining.mine_target(goal.target, 1)
                    return mined > 0
                except Exception:
                    return False

        # give：给玩家物品
        if goal.goal_type == "give":
            iid = self.agent.resolve_item(goal.target) if self.agent else -1
            if iid < 0:
                return False
            await self.equip.give_to_player(iid, goal.amount)
            return True

        # wait：等待 N 秒
        if goal.goal_type == "wait":
            wait_time = goal.amount or 5
            await asyncio.sleep(wait_time)
            return True

        # combat：战斗（打最近的敌对怪；v0.8：按 fight_nearest 真实结果判定，不再无条件成功）
        if goal.goal_type == "combat":
            try:
                combat = getattr(self.agent, "combat", None)
                if combat:
                    st = {}
                    try:
                        st = self.agent.get_state()
                    except Exception:
                        pass
                    return await combat.fight_nearest(st, timeout=30)
                return False
            except Exception:
                return False

        # gather：收集掉落物（v0.8：按收集数量判定）
        if goal.goal_type == "gather":
            try:
                collected = await self.mod.collect_items(radius=600)
                return collected > 0
            except Exception:
                return False

        # social 无具体物品目标：交给自主大脑处理，这里放行
        if goal.goal_type == "social":
            return True

        # 爬升/移动：target 形如 "x,y"，走分段规划（中途平台多次钩锁）
        if goal.goal_type in ("climb", "goto"):
            try:
                sx, sy = goal.target.split(",")
                tx, ty = int(sx.strip()), int(sy.strip())
            except (ValueError, AttributeError):
                return False
            if goal.goal_type == "climb":
                return await self.agent.climb_to(tx, ty)
            return await self.agent.navigate_to(tx, ty)

        # 去箱子取物：先找到含该物的最近箱子，再取
        if goal.goal_type == "fetch":
            chest = await self.agent.nearest_chest_with(goal.target)
            if chest is None:
                return False
            return await self.agent.take_from_chest(goal.target, chest, goal.amount)

        # 默认：挖矿/合成流程
        iid = self.agent.resolve_item(goal.target) if self.agent else -1
        if iid < 0:
            return False
        if goal.craft_first:
            crafted = await self.mod.craft(item_id=iid, amount=goal.amount)
            if crafted <= 0:
                return False  # 材料不足/合成失败，不假装成功
            return True
        mined_iid, mined = await self.mining.mine_target(goal.target, goal.amount)
        if mined <= 0:
            return False
        if goal.deliver_to_player:
            await self.equip.give_to_player(mined_iid, mined)
        elif goal.equip_self:
            await self.equip.auto_equip()
        return True
