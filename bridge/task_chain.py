"""任务链引擎：将目标编排为多步行为（挖矿→合成→给玩家/自穿）。

v0.11（A3）：诚实化——
  - explore 不再"走100格就算完成"：真实找洞→下挖→记录坐标→标记探索
  - gather 按"真捡到物品数"判定
  - 失败不再静默：return (ok, reason) 外，能丢信息的都丢进 agent.log，
    上游 run_complex_task 保证有说话
"""

import asyncio
from dataclasses import dataclass
from typing import List, Optional

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
    report_fail: str = ""  # 步骤失败时向主人汇报的话


class TaskChain:
    def __init__(self, mining: MiningEngine, mod: ModLink, equip: EquipmentManager, agent=None) -> None:
        self.mining = mining
        self.mod = mod
        self.equip = equip
        self.agent = agent
        self._queue: asyncio.Queue[Goal] = asyncio.Queue(maxsize=1)
        self._current: Optional[Goal] = None
        self._chain: List[str] = []  # 决策链：每步结果，供解释
        self._step: str = ""  # 当前进度 "2/4"
        self._last_ok: bool = True  # 上一步是否成功，用于中止后续

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
            self._step = f"{i + 1}/{total}"
            await self.submit(g)
            # 等这一步被取走并执行完毕（队列空 且 无正在执行的目标）
            while not self._queue.empty() or self._current is not None:
                await asyncio.sleep(0.2)
            if not self._last_ok:
                self._chain.append(f"{i + 1}.{g.goal_type}:{g.target} 失败，中止")
                if self.agent:
                    self.agent.log(f"多步任务在第{i + 1}/{total}步中止：{g.goal_type} {g.target}", "warn")
                return
            self._chain.append(f"{i + 1}.{g.goal_type}:{g.target} 完成")
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
                    res = await self.agent.start_longterm("follow", reason=goal.reason or "跟着主人")
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
            return await self._explore(goal)

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

        # gather：收集掉落物（v0.11：按真捡到数判定）
        if goal.goal_type == "gather":
            try:
                collected = await self.mod.collect_items(radius=600)
                ok = collected > 0
                if not ok and self.agent:
                    self.agent.log("gather：附近没有可捡的掉落物", "warn")
                return ok
            except Exception:
                return False

        # chop：真砍树（什么任务用什么工具 → 斧头）
        if goal.goal_type == "chop":
            try:
                life = getattr(self.agent, "life", None)
                if life:
                    amount = goal.amount or 10
                    got = await life.chop_wood(target=amount)
                    return got > 0
                return False
            except Exception:
                return False

        # fish：真钓鱼（工具 → 钓竿）
        if goal.goal_type == "fish":
            try:
                life = getattr(self.agent, "life", None)
                if life:
                    return await life.fish(attempts=3)
                return False
            except Exception:
                return False

        # social 无具体物品目标：交给自主大脑处理，这里放行
        if goal.goal_type == "social":
            return True

        # 爬升/移动：target 形如 "x,y" 走坐标；否则按目标名（方向词/某物）处理
        if goal.goal_type in ("climb", "goto"):
            tgt = goal.target or ""
            if "," in tgt:
                try:
                    sx, sy = tgt.split(",")
                    tx, ty = int(sx.strip()), int(sy.strip())
                except (ValueError, AttributeError):
                    tx = ty = None
                if tx is not None and ty is not None:
                    if goal.goal_type == "climb":
                        return await self.agent.climb_to(tx, ty)
                    return await self.agent.navigate_to(tx, ty)
            # #4: 非坐标目标 → 方向词走 explore 语义，其他尝试找物
            return await self._goto_by_name(goal, tgt)

        # 去箱子取物：先找到含该物的最近箱子，再取
        if goal.goal_type == "fetch":
            chest = await self.agent.nearest_chest_with(goal.target)
            if chest is None:
                if self.agent:
                    self.agent.log(f"fetch：附近没有含 {goal.target} 的箱子", "warn")
                return False
            return await self.agent.take_from_chest(goal.target, chest, goal.amount)

        # 默认：挖矿/合成流程
        iid = self.agent.resolve_item(goal.target) if self.agent else -1
        if iid < 0:
            if self.agent:
                self.agent.log(f"不认识目标物品：{goal.target}", "warn")
            return False
        if goal.craft_first:
            crafted = await self.mod.craft(item_id=iid, amount=goal.amount)
            if crafted <= 0:
                if self.agent:
                    self.agent.log(f"craft：{goal.target} 材料不足或合成失败", "warn")
                return False  # 材料不足/合成失败，不假装成功
            return True
        mined_iid, mined = await self.mining.mine_target(goal.target, goal.amount)
        if mined <= 0:
            if self.agent:
                self.agent.log(f"mine：附近没找到 {goal.target} 或挖不到", "warn")
            return False
        if goal.deliver_to_player:
            await self.equip.give_to_player(mined_iid, mined)
        elif goal.equip_self:
            await self.equip.auto_equip()
        return True

    # ---------------- 按目标名移动（#4 goto 无坐标时） ----------------

    async def _goto_by_name(self, goal: Goal, tgt: str) -> bool:
        """goto/climb 目标不是坐标时：方向词走 explore，其他找物导航。"""
        try:
            st = self.agent.get_state()
            sx = int(st.get("tile_x", 0) or 0)
            sy = int(st.get("tile_y", 0) or 0)
        except Exception:
            sx, sy = 0, 0

        t = (tgt or "").strip()
        # 方向词
        if t in ("左", "左边", "left", "west"):
            tx = sx + 100
            return bool(await self.agent.navigate_to(tx, sy, timeout=30))
        if t in ("右", "右边", "right", "east"):
            tx = sx + 100
            return bool(await self.agent.navigate_to(tx, sy, timeout=30))
        if t in ("地下", "下方", "down", "underground"):
            # 复用探索的下挖逻辑
            return await self._explore(Goal(goal_type="explore", target="地下",
                                            reason=goal.reason))
        if t in ("上方", "上面", "up"):
            ty = sy - 30
            return bool(await self.agent.navigate_to(sx, ty, timeout=30))
        # 目标物：找最近的该物品箱子/矿点导航过去
        try:
            ores = await self.agent.mod.find_ore(radius=60)
            if ores:
                # 尽量匹配目标物对应的矿（tile 类型），匹配不到就取最近的矿
                from .item_npc_dict import tile_type_of
                want = tile_type_of(t)
                pick = None
                for o in ores:
                    if want and int(o.get("type", 0) or 0) == want:
                        pick = o
                        break
                if pick is None and not want:
                    pick = ores[0]
                if pick is not None:
                    return bool(await self.agent.navigate_to(
                        pick["x"], pick["y"], timeout=30))
        except Exception:
            pass
        # 兜底：向最近的玩家/出生方向挪动，至少不是 (0,0)
        try:
            players = st.get("nearby_players", []) or []
            if players:
                px = int(players[0].get("tile_x", sx) or sx)
                py = int(players[0].get("tile_y", sy) or sy)
                return bool(await self.agent.navigate_to(px, py, timeout=30))
        except Exception:
            pass
        return False

    # ---------------- 探索（A3 诚实化） ----------------

    async def _explore(self, goal: Goal) -> bool:
        """真实探索：找洞→下挖→记录→标记，不再"走100格就完成"。

        方向探索走到目标附近并确认有移动就算完成（导航成功即真的到了）；
        地下探索真正往下挖一段并记录坐标；
        目标性探索（找某物）真的去挖一次并计数。
        """
        target = goal.target or ""
        try:
            st = self.agent.get_state()
            sx = int(st.get("tile_x", 0) or 0)
            sy = int(st.get("tile_y", 0) or 0)
        except Exception:
            sx, sy = 0, 0

        if target in ("left", "right", "左", "右"):
            direction = -1 if target in ("left", "左") else 1
            tx = sx + direction * 100
            ok = bool(await self.agent.navigate_to(tx, sy, timeout=30))
            if self.agent:
                self.agent.log(
                    f"探索：向{'左' if direction < 0 else '右'}走到 ({tx},{sy}) {'成功' if ok else '失败'}", "nav"
                )
            return ok

        if target in ("地下", "下方", "underground"):
            # v0.5: 完整地下探索闭环（找洞→下挖→挖矿→回家），替代单纯下挖 25 格
            explorer = getattr(self.agent, "explorer", None)
            if explorer is not None:
                try:
                    return await explorer.explore(direction=1, max_time=180.0)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if self.agent:
                        self.agent.log(f"地下探索异常: {e}", "warn")
                    return False
            # 兜底：真下挖（有镐才挖，否则诚实失败）
            down = 0
            for _ in range(5):
                if self.agent.executor and self.agent.executor.should_stop():
                    break
                st = await self.agent.refresh_state()
                mx = int(st.get("tile_x", 0) or 0)
                my = int(st.get("tile_y", 0) or 0)
                moved = False
                for dy in range(1, 4):
                    try:
                        if await self.mod.break_tile(mx, my + dy):
                            down += 1
                            moved = True
                            break
                    except Exception:
                        break
                if not moved:
                    break
                await asyncio.sleep(0.6)
            if self.agent:
                self.agent.log(f"探索：向下挖了 {down} 格", "nav")
            return down > 0

        # 目标性探索：找某物 → 真的挖一次
        try:
            _iid, mined = await self.mining.mine_target(target, 1)
            return mined > 0
        except Exception:
            return False
