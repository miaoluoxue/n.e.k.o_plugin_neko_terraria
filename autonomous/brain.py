"""AutonomousBrain：四层思考 + 事件驱动打断 + 自主执行 + 交互引擎。"""

import asyncio
import random
import time
from typing import Any, Dict

from ..bridge.executor import SRC_AUTO
from ..bridge.task_chain import Goal
from ..core.context import build_user_context
from ..polish.attention import AttentionDrift
from ..polish.human_timing import HumanTiming
from .event_bus import get_event_bus

# v2.0: 交互引擎
from .interaction_engine import InteractionEngine
from .internal_state import InternalState
from .motivation import MotivationSystem

# ── LLM 自主决策的触发文本 ──

LLM_THINK_PROMPT = (
    "你是{name}，一只在泰拉瑞亚世界里的猫娘——你会采矿、砍树、战斗、探索。\n"
    "你现在闲着，用游戏里的角色看看周围该做什么：\n"
    "{context}\n\n"
    "选项：\n"
    "1. 跟着主人（主人在附近就跟着）\n"
    "2. 挖矿/采集（镐子+地下的方向）\n"
    "3. 探索（附近没探过的区域）\n"
    "4. 回血/整理（血量低或背包乱）\n"
    "5. 继续发呆（什么都不做也行）\n\n"
    "用 terraria_chat 简短说说你想干什么，然后用对应的工具行动。"
    "别长篇大论，1-2 句就够了！"
)


class AutonomousBrain:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.agent = plugin._agent
        self.cfg = plugin._config
        self.state = InternalState()
        self.motivation = MotivationSystem()
        self.bus = get_event_bus()
        self.timing = HumanTiming()
        self.attention = AttentionDrift()
        self.running = False
        self._tasks: list[asyncio.Task] = []
        self._busy = False
        self._last_llm_think = 0.0  # 上次 LLM 思考时间戳

        # v2.0: 交互引擎接管对话交互（直接传整个 cfg——
        # 之前取 cfg["interaction"] 子字典恒为空，导致 interaction_tick 等配置读不到）
        try:
            self.interaction = InteractionEngine(self.agent, plugin, self.cfg or {})
        except Exception:
            self.interaction = None

    async def start(self) -> None:
        self.running = True
        self._tasks = [
            asyncio.create_task(self._state_tick()),
            asyncio.create_task(self._fast_think()),
            asyncio.create_task(self._llm_think()),
        ]
        self.bus.subscribe("interrupt", self._on_interrupt)
        self.bus.subscribe("combat_hit", self._on_combat_hit)
        # 注册复活回调：复活后自动寻路找主人（参照 Lumi_Nox）
        self.agent.on_respawn(self._on_respawn)

        # v0.7: 处境融合层（身体感×画面感×记忆 → 心情/台词/行为）
        try:
            from .situation import SituationEngine

            coord = getattr(self.agent, "coordinator", None)
            llm = None
            if coord:
                llm = getattr(getattr(coord, "_intent_parser", None), "_llm_call", None)
            self.situation = SituationEngine(self.agent, self, llm_call=llm)
            await self.situation.start()
            self.plugin.logger.info(f"[brain] 处境融合层已启动 (llm={'有' if llm else '无，规则兜底'})")
        except Exception:
            self.situation = None
            self.plugin.logger.warning("[brain] 处境融合层启动失败")

        # v0.7: Heart 依恋值（主人关系跨会话）
        try:
            from .heart import Heart

            self.heart = Heart(self.agent)
            self.plugin.logger.info(f"[brain] Heart 依恋层已启动 (bond={self.heart.bond:.0f})")
        except Exception:
            self.heart = None
            self.plugin.logger.warning("[brain] Heart 依恋层启动失败")

        # v2.0: 启动交互引擎
        if self.interaction:
            await self.interaction.start()
            # 注册 executor 回调 → 干活汇报 / 任务打断
            ex = getattr(self.agent, "executor", None)
            if ex:
                ex.on("task_done", self._on_executor_task_done)
                ex.on("task_started", self._on_executor_task_started)
                ex.on("interrupted", self._on_executor_interrupted)
                ex.on("step_done", self._on_executor_step)

    async def stop(self) -> None:
        self.running = False
        for t in self._tasks:
            t.cancel()
        # v0.7: 停止处境融合层
        if getattr(self, "situation", None):
            await self.situation.stop()
        # v0.7: Heart 依恋值落盘
        if getattr(self, "heart", None):
            try:
                self.heart.save()
            except Exception:
                pass
        # v2.0: 停止交互引擎
        if self.interaction:
            await self.interaction.stop()

    def occupied(self) -> bool:
        """有任务在跑就算占用：自主行为必须让位，不打断正在执行的任务。

        包含两类：前台有限任务（executor）与后台长期任务（longterm）。
        主人说了"跟着我"，自主行为就别再自作主张乱跑。
        """
        if self._busy:
            return True
        ex = getattr(self.agent, "executor", None)
        if ex and ex.busy():
            return True
        lt = getattr(self.agent, "longterm", None)
        return bool(lt and lt.busy_kinds())

    async def _state_tick(self) -> None:
        interval = self.cfg.get("state_tick_interval_seconds", 1.0)
        while self.running:
            try:
                # 刺激源只看前台任务——长期任务（跟随/挖矿）是常态陪伴，不算"刺激"；
                # 否则跟随中 boredom 永远下降，_llm_think 永不触发，自主思考/情感交互全停。
                ex = getattr(self.agent, "executor", None)
                has_stimulus = bool(ex and ex.busy())
                self.state.tick(has_stimulus=has_stimulus)
                # v0.7: Heart 依恋值——主人同屏陪伴增长 / 冷落衰减（每 30s 一次冷落检查）
                heart = getattr(self, "heart", None)
                if heart:
                    try:
                        state = self.agent.get_state()
                        if state.get("nearby_players"):
                            heart.on_companion(interval)
                        elif int(time.time()) % 30 == 0:
                            heart.on_neglect_tick()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.plugin.logger.warning(f"[brain] _state_tick 异常: {e}")
            await asyncio.sleep(interval)

    async def _fast_think(self) -> None:
        interval = self.cfg.get("fast_think_interval_seconds", 5.0)
        while self.running:
            try:
                state = self.agent.get_state()
                # P0 优先级守卫：自保（喝药/逃跑）无条件优先 + 长期任务中遇怪战斗
                # 参照 Lumi_Nox 主循环 P0 自保 > P1 战斗（不被任务占用抑制）
                if await self._guard_check(state):
                    await asyncio.sleep(interval)
                    continue
                # 有前台任务在执行时其余自主行为让位（避免抢控制权）
                # 长期任务（跟随/挖矿）不阻塞自主行为——否则跟随中猫娘不会自己打架/挖矿
                ex = getattr(self.agent, "executor", None)
                if ex and ex.busy():
                    await asyncio.sleep(interval)
                    continue
                drive = self.motivation.update(state, self.state.boredom)
                await self._act_on_drive(drive, state)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.plugin.logger.warning(f"[brain] _fast_think 异常: {e}")
            await asyncio.sleep(interval)

    async def _guard_check(self, state: Dict[str, Any]) -> bool:
        """无条件优先级守卫。返回 True 表示本轮已处理（跳过其余自主行为）。

        v0.11（A2）：比照 Lumi 生存循环 P0/P1——
          P0 自保：HP<50% 喝药（独立动作，不打断任务）
          P1 战斗：无前台任务时打怪；有前台任务则只提醒不打断
        不再有敌就无限占 fast_think（那会吞掉主人的 finite 任务）。
        """
        if not state:
            return False
        hp = int(state.get("hp", 100) or 100)
        max_hp = int(state.get("max_life", 100) or 100) or 100
        ratio = hp / max_hp if max_hp > 0 else 1.0
        enemies = [e for e in (state.get("nearby_npcs", []) or []) if e.get("damage", 0) > 0 and e.get("life", 0) > 0]

        handled = False
        # ── P0 自保 1：血量 <50% 先喝药（独立动作，不打断任务） ──
        if 0 < ratio < 0.5:
            try:
                if await self.agent.heal_self():
                    self.agent.log("自保：血量低，喝药恢复", "item")
                    handled = True
            except Exception:
                pass

        # ── P0 自保 2：血量 <30% 且附近有敌 → 向反方向拉开 8 格 ──
        if ratio < 0.3 and enemies and not handled:
            try:
                me_x = int(state.get("tile_x", 0) or 0)
                me_y = int(state.get("tile_y", 0) or 0)
                nearest = min(
                    enemies,
                    key=lambda e: abs(int(e.get("tile_x", 0) or 0) - me_x) + abs(int(e.get("tile_y", 0) or 0) - me_y),
                )
                dx = me_x - int(nearest.get("tile_x", me_x) or me_x)
                dy = me_y - int(nearest.get("tile_y", me_y) or me_y)
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < 12:
                    step = max(abs(dx), abs(dy), 1)
                    tx = me_x + (dx // step) * 8
                    ty = me_y + (dy // step) * 8
                    await self.agent.navigate_to(tx, ty, timeout=3)
                    self.agent.log("自保：血量危急，拉开距离", "warn")
                    handled = True
                    try:
                        inter = getattr(self, "interaction", None)
                        if inter:
                            await inter.push_speech("血好少，先跑开躲一下喵！", behavior="respond")
                    except Exception:
                        pass
            except Exception:
                pass

        # ── P1 战斗：无前台任务才打（参照 Lumi P1 优先级，但可让路） ──
        if enemies and not handled:
            ex = getattr(self.agent, "executor", None)
            fg_busy = bool(ex and ex.busy())
            if fg_busy:
                # 有主线任务：不打断，最多一句话提醒（交给交互引擎）
                try:
                    if self.interaction and self.state.boredom > 0.2:
                        pass  # 避免抢话：任务的 step/完成回调自会说话
                except Exception:
                    pass
                return handled  # 不占 fast_think
            # 无任务：打（带 check_task，战斗中被主人新任务打断则放弃）
            lt = getattr(self.agent, "longterm", None)
            try:
                if lt:
                    lt.request_yield()  # 长期任务让路，避免抢操作权
                self._busy = True
                try:
                    await self.agent.combat.fight_nearest(
                        state, timeout=8, check_task=lambda: ex.busy() if ex else False
                    )
                finally:
                    self._busy = False
                handled = True
            except Exception:
                pass
            finally:
                if lt:
                    lt.release_yield()
        return handled

    async def _act_on_drive(self, drive: str, state: Dict[str, Any]) -> None:
        await asyncio.sleep(self.timing.reaction_delay())
        # 只被前台任务阻塞——长期任务（跟随/挖矿）不拦截自主行为
        ex = getattr(self.agent, "executor", None)
        if ex and ex.busy():
            return
        if self.attention.should_drift():
            return

        players = state.get("nearby_players", [])
        # #96: 只有社交驱动才跟随主人——否则有主人在场时 gather/explore/comfort
        # 全被 follow_player 抢占（前台任务占用 executor），挖矿/探索自主动机永不执行。
        if players and drive == "social":
            ppos = (players[0]["tile_x"], players[0]["tile_y"])
            await self.agent.follow_player(ppos)
            if self.occupied():
                return

        if drive == "combat":
            self._busy = True
            try:
                await self.agent.combat.fight_nearest(state)
            finally:
                self._busy = False
        elif drive == "comfort":
            hp = state.get("hp", 100)
            max_hp = state.get("max_life", 100) or 100
            if hp < max_hp * 0.5:
                if await self.agent.heal_self():
                    await self.agent.send_chat("血量有点低，喝口药~")
                else:
                    await self.agent.send_chat("血量低，先躲一下")
        elif drive == "gather":
            await self._auto_task("自主储备材料", [{"action": "gather", "item": "wood", "amount": 15}])
        elif drive == "explore" and self.state.boredom > 0.55:
            # v3.0 巡逻兜底（参照 Lumi P4）：主人 30 格内陪伴优先不巡逻
            near_owner = False
            me = (state.get("tile_x", 0), state.get("tile_y", 0))
            for p in state.get("nearby_players", []) or []:
                if abs(int(p.get("tile_x", 0) or 0) - me[0]) < 30 and abs(int(p.get("tile_y", 0) or 0) - me[1]) < 30:
                    near_owner = True
                    break
            if not near_owner:
                # #15: target 不能是 "nearby"（会落到 mine_target("nearby") 挖空气）——
                # 改为随机方向探索，或地下。
                await self.agent.send_chat("有点无聊，我去周围转转~")
                tgt = random.choice(["left", "right", "地下"])
                await self.agent.submit_goal(Goal(goal_type="explore", target=tgt, reason="无聊探索"))
        elif drive == "social" and players and random.random() < 0.2:
            await self.agent.send_chat("主人在这呀，我跟着你~")

    async def _auto_task(self, why: str, steps: list) -> None:
        run = getattr(self.agent, "run_complex_task", None)
        if run is None:
            return
        await run(steps, why, source=SRC_AUTO)

    # ── LLM 自主决策层（新增） ──

    async def _llm_think(self) -> None:

        enabled = self.cfg.get("llm_autonomous_enabled", True)
        if not enabled:
            return

        lo = self.cfg.get("llm_think_min_seconds", 60)
        hi = self.cfg.get("llm_think_max_seconds", 120)

        # 首轮延迟：入服后等 10 秒再让 LLM 思考
        await asyncio.sleep(10)

        while self.running:
            try:
                await asyncio.sleep(random.uniform(lo, hi))
                if not self.running:
                    break

                # 有前台任务在执行 → 不打扰（长期任务/跟随是常态，不阻塞自主思考）
                ex = getattr(self.agent, "executor", None)
                if ex and ex.busy():
                    continue

                # 无聊度太低也没必要
                if self.state.boredom < 0.3:
                    continue

                # 获取上下文
                ctx = build_user_context(self.agent)
                if not ctx:
                    continue

                import time

                self._last_llm_think = time.monotonic()

                # 构造 LLM 思考请求
                char_name = self.cfg.get("character_name", "neko") if self.cfg else "neko"
                prompt = LLM_THINK_PROMPT.format(name=char_name, context=ctx)

                # v3.0: 统一走宿主 LLM 会话（mc 插件 nudge 模式）——
                # 宿主 LLM 既能调工具又能说话，自主决策/解说三合一，
                # 不需要插件自带 LLM
                try:
                    # 全局限流检查
                    from ..llm.throttle import get_throttle

                    throttle = get_throttle()
                    if not throttle.acquire(source="brain_think", priority="low"):
                        self.plugin.logger.info("[brain] LLM 自主思考被限流，跳过本次")
                        continue

                    push = getattr(self.plugin, "push_message", None)
                    if push:
                        await push(parts=[{"type": "text", "text": prompt}], ai_behavior="respond")
                        self.plugin.logger.info(f"[brain] LLM 自主思考已推送，boredom={self.state.boredom:.2f}")
                    else:
                        # 无 LLM 通道：规则兜底
                        await self._deep_boredom_fallback()
                except Exception as e:
                    self.plugin.logger.warning(f"[brain] LLM 自主思考推送失败: {e}")
                    await self._deep_boredom_fallback()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.plugin.logger.warning(f"[brain] _llm_think 异常: {e}")

    # ── 深层思考（v2.0: 委托给交互引擎） ──

    async def _deep_boredom_fallback(self) -> None:
        """极无聊时的资源收集兜底（规则层，不依赖 LLM）。"""
        if self.occupied():
            return
        if self.state.boredom > 0.9:
            await self._auto_task("无聊储备", [{"action": "gather", "item": "wood", "amount": 20}])

    # ── 复活后自动寻路找主人（参照 Lumi_Nox 死亡复活重置目标） ──

    def _on_respawn(self) -> None:

        auto_return = self.cfg.get("auto_return_after_respawn", True)
        if not auto_return:
            return

        async def _navigate_back():
            # 等 agent 状态刷新一帧
            await asyncio.sleep(0.5)
            st = self.agent.get_state()
            players = st.get("nearby_players", []) or []
            if not players:
                return  # 单人模式，不需要找

            owner = players[0]
            ox, oy = owner.get("tile_x", 0), owner.get("tile_y", 0)
            dist = owner.get("distance", 9999)
            name = owner.get("name", "主人")

            print(f"[brain] ✨ 复活后检测到玩家 {name} 距离={dist}，自动寻路回去")
            self.plugin.logger.info(f"[brain] 复活后自动寻路 → {name} pos=({ox},{oy}) dist={dist}")

            try:
                # 先推一条消息给 LLM 知会
                push = getattr(self.plugin, "push_message", None)
                if push:
                    await push(
                        content=f"我复活了！检测到 {name} 在附近({int(dist)}格)，我马上回去～", ai_behavior="read"
                    )

                await self.agent.navigate_to(ox, oy, timeout=30)
                self.agent.log("复活后回到主人身边了", "info")
            except Exception as e:
                self.plugin.logger.warning(f"[brain] 复活后寻路失败: {e}")

        # 在事件循环中调度（回调在 _state_loop 线程内，ensure_future 安全）
        asyncio.ensure_future(_navigate_back())

    async def _on_combat_hit(self, data: Any) -> None:
        """受击即时响应：C# 推 combat_hit → 交互引擎立即惊呼。"""
        if self.interaction:
            text = data.get("message", "受到伤害") if isinstance(data, dict) else "受到伤害"
            await self.interaction.inject_event("combat_hit", intensity=0.6, description=text)

    async def _on_interrupt(self, data: Any) -> None:
        """v2.0 分级打断：根据中断级别区别处理。

        data 格式：{"level": 1-4, "reason": "...", "task_name": "..."}
        """
        self._busy = False
        self.state.boredom = 0.0
        self.motivation.scores.clear()

        level = 1
        why = "主人有新指令"
        task_name = ""
        if isinstance(data, dict):
            level = int(data.get("level", 1) or data.get("interrupt_level", 1))
            why = str(data.get("reason", "") or data.get("why", "") or why)
            task_name = str(data.get("task_name", "") or data.get("name", ""))

        # 级别 4: HARD — 清空一切
        if level >= 4:
            # 记忆被中断的任务（用于后续恢复询问）
            if task_name and self.interaction:
                self.interaction.remember_interrupted_task(task_name)
            await self.agent.interrupt_current(why)
            lt = getattr(self.agent, "longterm", None)
            if lt:
                await lt.stop_all(why)
            return

        # 级别 3: EMERGENCY — 立即喊+切
        if level >= 3:
            await self.agent.interrupt_current(why)
            return

        # 级别 2: CONCERN — 先关心再评估
        if level >= 2:
            # 先注入关心，给一小段对话窗口
            if self.interaction:
                await self.interaction.inject_event("danger_found", intensity=0.7, description=why)
            await self.agent.interrupt_current(why)
            return

        # 级别 1: SOFT — 先回应主人，当前小步自然完成后切换
        # 不强制打断前台任务（主人"换个任务"这种软指令，让当前一小步自然结束）
        lt = getattr(self.agent, "longterm", None)
        if lt:
            await lt.stop_all(why)  # 长期任务让路（跟随/挖矿这类无终点的）
        self._busy = False

    @property
    def _emitter(self):
        svc = getattr(self.plugin, "_service", None)
        return svc.event_emitter if svc else None

    async def _on_executor_task_done(self, data: Dict) -> None:
        name = data.get("name", "任务")
        status = data.get("status", "ok")
        desc = f"「{name}」{'' if status == 'ok' else f'({status})'}完成了~"

        # v3.0: fire-and-forget 完成 cue —— 把任务结果推回宿主 LLM，
        # 让六月喵知道结果并向主人汇报（参照 Minecraft 插件 task_finished 分档）
        result = data.get("result") or {}
        out = ""
        if isinstance(result, dict):
            out = str(result.get("output", "") or "")
        if out and out in (name, f"「{name}」"):
            out = ""
        try:
            if status == "ok":
                cue = f"[任务结果] 「{name}」完成了" + (f"：{out}" if out else "") + "。"
                await self.agent.speak(cue, ai_behavior="read")
            else:
                cue = f"[任务结果] 「{name}」没完成（{status}）" + (f"：{out}" if out else "") + "。"
                await self.agent.speak(
                    cue + "\n用猫娘语气向主人汇报这个结果（1-2句，不要添加不存在的细节）", ai_behavior="respond"
                )
        except Exception:
            pass

        if self._emitter:
            goal_data = data.get("goal", {})
            gtype = goal_data.get("type", "")
            gtarget = goal_data.get("target", "")
            if gtype and gtarget:
                self._emitter.on_goal_completed(gtype, gtarget)

        if self.interaction:
            await self.interaction.inject_event("task_done", intensity=0.5, description=desc, data=data)

    async def _on_executor_task_started(self, data: Dict) -> None:
        name = data.get("name", "新任务")

        if self._emitter:
            goal_data = data.get("goal", {})
            gtype = goal_data.get("type", "")
            gtarget = goal_data.get("target", "")
            if gtype and gtarget:
                self._emitter.on_goal_set(gtype, gtarget, goal_data.get("reason", ""))

        # 任务开始直推主 LLM（read 模式，绕开交互引擎说话冷却）：
        # 让猫娘知道任务真的开始了、还在执行中——这是"任务没完成"认知的关键一环
        try:
            await self.agent.speak(
                f"[任务状态] 开始执行「{name}」。这是任务开始通知，任务仍在进行中，完成后会汇报。",
                ai_behavior="read",
            )
        except Exception:
            pass

        if self.interaction:
            await self.interaction.inject_event(
                "task_started", intensity=0.1, description=f"开始做「{name}」", data=data
            )

    async def _on_executor_interrupted(self, data: Dict) -> None:
        name = data.get("name", "任务")
        reason = data.get("reason", "不明原因")

        if self._emitter:
            goal_data = data.get("goal", {})
            gtype = goal_data.get("type", "")
            gtarget = goal_data.get("target", "")
            if gtype and gtarget:
                self._emitter.on_goal_failed(gtype, gtarget, reason)

        # 任务被中断直推主 LLM：猫娘必须知道任务没有完成
        try:
            await self.agent.speak(
                f"[任务状态] 「{name}」被中断了（{reason}）。这是状态通知，任务没有完成。",
                ai_behavior="respond",
            )
        except Exception:
            pass
        except Exception:
            pass

        if self.interaction:
            self.interaction.remember_interrupted_task(name)
            desc = f"「{name}」被中断了（{reason}）"
            await self.interaction.inject_event("task_interrupted", intensity=0.6, description=desc, data=data)

    async def _on_executor_step(self, data: Dict) -> None:
        kind = data.get("kind", "task")
        desc = data.get("desc", f"{kind} 一步完成")
        # 步骤进度直推主 LLM（read）：任务进行中的持续证据，猫娘不会误以为已完成
        try:
            await self.agent.speak(f"[任务进度] {desc}。任务仍在执行中。", ai_behavior="read")
        except Exception:
            pass

        if self.interaction:
            await self.interaction.inject_event("step_done", intensity=0.15, description=desc, data=data)
