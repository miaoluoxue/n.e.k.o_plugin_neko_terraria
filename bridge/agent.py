"""TerrariaAgent：有头客户端架构（全渲染，非无头协议模拟）。
- 通过 GameLauncher 启动完整的 tModLoader 图形客户端
- 移动/战斗控制通过 ModLink (TCP 9877) 实现
- 登录/心跳由游戏原生网络栈处理，不自行模拟协议

"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..autonomous.event_bus import get_event_bus
from .capability import Capability
from .combat import CombatEngine
from .connection import Connection
from .coordinator import TaskCoordinator
from .equipment import EquipmentManager
from .executor import SRC_AUTO, SRC_OWNER, TaskExecutor
from .inventory_ops import InventoryOps
from .launcher import GameLauncher
from .longterm import LongTermManager
from .mining import MiningEngine
from .mod_link import ModLink
from .mod_registry import ModItemRegistry
from .planner import Planner
from .recipe_book import RecipeBook
from .standing_jobs import StandingJobs
from .task_brain import TaskBrain
from .task_chain import Goal, TaskChain
from .task_inquiry import TaskInquiry

# ── 联机模式检测阈值（参照 Lumi_Nox） ──
# 附近玩家距离阈值：存在非自身玩家且距离 < 该值 → 视为联机模式
MULTIPLAYER_DIST_THRESHOLD = 200  # tile


class TerrariaAgent:
    def __init__(self, plugin) -> None:
        from ..core.config_store import PLUGIN_ROOT  # 延迟导入打断循环

        self.plugin = plugin
        self.cfg = plugin._config
        # 标准 logging logger（供各引擎 agent.logger.xxx 使用；agent.log 是内存环）
        self.logger = logging.getLogger("plugin.plugins.neko_terraria.agent")
        self.conn = Connection(self.cfg["mod_host"], self.cfg["mod_port"])
        self.launcher = GameLauncher(self.cfg)
        self.mod = ModLink(self.conn)
        self.combat = CombatEngine(self.mod, self)
        self.mining = MiningEngine(self.mod, self)
        self.equip = EquipmentManager(self.mod)
        self.tasks = TaskChain(self.mining, self.mod, self.equip, self)
        self.registry = ModItemRegistry(PLUGIN_ROOT)
        self.capability = Capability(self)
        self.planner = Planner(self)
        self.executor = TaskExecutor(self)
        self.recipe_book = RecipeBook(self)
        from .base import BaseManager
        from .explore import UndergroundExplorer
        from .life import LifeEngine

        self.base = BaseManager(self)
        self.explorer = UndergroundExplorer(self)
        self.life = LifeEngine(self)
        self.brain = TaskBrain(self)
        self.longterm = LongTermManager(self)
        self.jobs = StandingJobs(self)
        self.coordinator = TaskCoordinator(self, name=self._character_name())
        self.inquiry = TaskInquiry(self)  # v2.1: 任务中决策询问
        self.items = InventoryOps(self)
        from .events import EventResponder

        self.events = EventResponder(self)
        # v2.1: 视觉管线（截图源在 lifecycle 注入；无源时自动降级）
        from ..core.vision import VisionPipeline

        self.vision = VisionPipeline(self.cfg, agent=self, push_message=getattr(plugin, "push_message", None))
        # v0.10: 英雄成长引擎（周期扫配方合成升级）
        from .upgrade import UpgradeEngine

        self.upgrade = UpgradeEngine(self)
        self._state: Dict[str, Any] = {}
        self._inv_full: Dict[str, Any] = {"hotbar": [], "equipped": [], "inventory": []}
        self._chests: List[Dict[str, Any]] = []
        self._world_info: Dict[str, Any] = {}
        self._log: List[Dict[str, Any]] = []
        self._running = False

        # ── 死亡/复活状态（参照 Lumi_Nox bridge._is_dead） ──
        self._is_dead: bool = False
        self._death_message: str = ""  # 最后一条死亡信息
        self._death_position: Optional[tuple] = None  # (tile_x, tile_y) 死亡位置
        self._death_count: int = 0  # 累计死亡次数
        self._last_hp: int = 0  # 上一帧血量（用于暴跌检测）

        # ── 复活回调 ──
        self.respawn_callbacks: list = []  # 复活时触发，供 brain 注册

        # ── 联机模式 ──
        self._multiplayer_mode: bool = False  # 检测到附近有玩家 → 联机模式

        # ── idle 心跳状态：随 _state_loop 1s 轮询更新（不独立开线程） ──
        self._idle_ctx: Dict[str, Any] = {
            "cycle": 0,  # 状态轮询计数（= 秒）
            "last_mode": None,  # idle 模式：patrol/mine/chest/gather/light
            "mode_n": 0,  # 当前模式连续秒数（同一模式不做反复声明）
            "patrol_dir": 1,  # 巡逻方向 ±1
            "last_event": "",  # 最近宣布过的事（去重播报，防刷屏）
            "home": None,  # (x, y) 基地坐标；在 host 附近开箱/被叫停后兜底 = 出生点
            "last_resupply": 0.0,  # 上次补给时间戳（补给冷却 90s）
            "gave_torch": 0,  # 上次传递火把计数（随 fire_and_forget 发送）
        }

    async def start(self) -> bool:
        """启动 AI 客户端 → 加入游戏。

        所有默认值统一由 config_store.DEFAULTS 管理，此处直接从 self.cfg 取，
        不再写死第二份 fallback。
        """
        server_host: str = self.cfg["server_host"]
        server_port: int = self.cfg["server_port"]
        server_password: str = self.cfg["server_password"]
        character_name: str = self._character_name()

        if not await self.launcher.launch():
            return False

        # launcher 已等待 3s；Mod TCP 监听器约在进程启动后 10~12s 就绪
        # 此处补偿等待，避免无效重试
        await asyncio.sleep(8.0)

        # 连接 AI Mod（只有 AI 装了 NekoTerrariaLink → 独占 9877）
        if not await self._ensure_mod_connected():
            self.launcher._log("无法连接到 AI 的 NekoTerrariaLink Mod，检查 AI 客户端是否正常加载")
            return False

        # 注册 mod 事件回调（死亡/Boss/入侵等主动推送事件）
        self.conn.on_message(self._handle_mod_event)

        # 等待游戏加载到主菜单（此时 DrawMenu hook 已在跑自动选角色）
        await asyncio.sleep(1.0)

        # ===== 1) 通知 mod 选定角色（统一从前端 character_name 读取） =====
        # Mod 自身也有 AutoSelect 机制，即使 Python 命令失败也能兜底
        if character_name:
            ok = await self.mod.select_character(name=character_name)
            if not ok:
                self.launcher._log(f"指定角色 '{character_name}' 命令失败（Mod AutoSelect 兜底）")
            else:
                self.launcher._log(f"已选中角色 '{character_name}'")
        else:
            await self.mod.select_character()

        # ===== 2) 加入服务器 —— 使用状态机轮询，模仿 Terraria-Bot =====
        # join_server(wait_confirm=True) 会在 mod 侧等待真实连接确认（netMode→1 + player.active）
        self.launcher._log(f"正在连接服务器 {server_host}:{server_port}...")
        for retry in range(5):
            # 先确保 Mod TCP 连通
            if not await self._ensure_mod_connected():
                self.launcher._log(f"Mod 连接丢失 (第{retry + 1}次重试)，正在重连...")
                if retry < 4:
                    await asyncio.sleep(1.0)
                continue

            self.launcher._log(f"发送 join_server 命令 (第{retry + 1}次)...")
            ok = await self.mod.join_server(
                server_host,
                server_port,
                server_password,
                character_name=character_name,
                wait_confirm=True,
                confirm_timeout=25,
            )
            if ok:
                self.launcher._log("AI 成功进入服务器！")
                break
            # 未确认入服：多半是 TCP 半开（AI 客户端已断但本地未检测），
            # 强制断开让下一次 _ensure_mod_connected 真正重连
            self.launcher._log(f"join_server 未确认入服 (第{retry + 1}次)，重试...")
            self.conn.close()
            if retry < 4:
                await asyncio.sleep(1.0)
        else:
            self.launcher._log("AI 加入服务器失败，请确认服务器已开启且配置正确")
            return False

        self._running = True
        self.events.bind()
        asyncio.create_task(self.tasks.run_loop())
        asyncio.create_task(self._state_loop())
        asyncio.create_task(self._auto_register())
        return True

    async def _ensure_mod_connected(self) -> bool:
        """确保已连接到 AI Mod 的 TCP 端口，未连接则尝试重连。"""
        if self.conn.is_mod_connected():
            return True
        for retry in range(10):
            if await self.conn.connect_mod(retry_ports=(retry >= 3)):
                return True
            if retry < 9:
                await asyncio.sleep(1.0)
        return False

    def _character_name(self) -> str:
        """直接从配置文件读取角色名，零 cfg 传播依赖。"""
        from ..core.config_store import load_user_config

        return load_user_config().get("character_name", "Neko")

    async def refresh_state(self) -> Dict[str, Any]:
        """立即刷新状态"""
        player_name = self._character_name()
        st = await self.mod.get_state(player_name)
        if st:
            self._state = st
        return self._state

    def log(self, msg: str, kind: str = "info") -> None:
        # 内存环形日志，供前端静默刷新展示
        from time import time

        self._log.append({"t": time(), "msg": msg, "kind": kind})
        if len(self._log) > 100:
            self._log = self._log[-100:]

    async def _auto_register(self) -> None:
        """进游戏时增量同步 mod 物品。

        enum_items + get_recipes 是一次性重请求（C# 侧遍历全物品/全配方），
        启动瞬间并发会占用命令锁，把推送事件/导航命令堵住。
        延迟 auto_register_delay_seconds 再跑，等入服推送稳定。
        """
        await asyncio.sleep(self.cfg.get("auto_register_delay_seconds", 25.0))
        try:
            mods = await self.mod.enum_items()
            diff = self.registry.sync_from_enum(mods)
            if diff["added"]:
                self.log(f"认识了新 mod：{', '.join(diff['added'])}", "info")
            if diff["removed"]:
                self.log(f"这些 mod 不见了：{', '.join(diff['removed'])}", "warn")
        except Exception:
            pass
        try:
            n = await self.recipe_book.refresh(force=True)
            if n:
                self.log(f"学会了 {n} 条配方", "item")
        except Exception:
            pass

    async def stop(self) -> None:
        self._running = False
        self.launcher.close()
        self.conn.close()

    async def _state_loop(self) -> None:
        """定期刷新状态 + 死亡/复活/联机模式检测（参照 Lumi_Nox）"""
        player_name = self._character_name()
        bus = get_event_bus()
        self.log(f"_state_loop 启动, player_name='{player_name}', running={self._running}")
        self.plugin.logger.info(f"[_state_loop] 启动 player_name='{player_name}'")
        loop_count = 0
        while self._running:
            loop_count += 1
            try:
                # 轮询式：每秒主动拉状态（请求内读流，响应可靠收到）
                st = await self.mod.get_state(player_name)
                if st:
                    self._state = st
                    # ── 死亡/复活检测（基于推送的缓存血量） ──
                    hp = st.get("hp", -1)

                    # 检测死亡：hp==0 且之前未标记死亡
                    if hp == 0 and not self._is_dead:
                        self._is_dead = True
                        self._death_count += 1
                        tx = st.get("tile_x", st.get("x", 0))
                        ty = st.get("tile_y", st.get("y", 0))
                        self._death_position = (tx, ty)
                        self._death_message = f"角色在 tile=({tx},{ty}) 死亡 (第{self._death_count}次)"
                        print(f"[agent] 💀 {self._death_message}")
                        bus.fire(
                            "player_died",
                            {
                                "count": self._death_count,
                                "position": self._death_position,
                                "message": self._death_message,
                            },
                        )

                    # 检测复活：hp>0 且之前标记为死亡
                    if hp > 0 and self._is_dead:
                        self._is_dead = False
                        tx = st.get("tile_x", st.get("x", 0))
                        ty = st.get("tile_y", st.get("y", 0))
                        print(f"[agent] ✨ 角色复活！hp={hp} pos=({tx},{ty}) 累计死亡={self._death_count}")
                        bus.fire(
                            "player_respawned",
                            {
                                "hp": hp,
                                "position": {"tile_x": tx, "tile_y": ty},
                                "death_count": self._death_count,
                            },
                        )
                        # 触发复活回调（brain 注册的自动寻路等）
                        for cb in self.respawn_callbacks:
                            try:
                                if asyncio.iscoroutinefunction(cb):
                                    asyncio.ensure_future(cb())
                                else:
                                    cb()
                            except Exception:
                                pass

                    # 更新上一帧血量（供 service 暴跌检测）
                    self._last_hp = hp

                    # ── 联机模式检测（参照 Lumi_Nox） ──
                    nearby = st.get("nearby_players", []) or []
                    has_other = False
                    if nearby and isinstance(nearby, list):
                        me_x = int(st.get("tile_x", 0) or 0)
                        me_y = int(st.get("tile_y", 0) or 0)
                        for p in nearby:
                            if isinstance(p, dict):
                                name = p.get("name", "")
                                px = int(p.get("tile_x", 0) or 0)
                                py = int(p.get("tile_y", 0) or 0)
                                dist = ((px - me_x) ** 2 + (py - me_y) ** 2) ** 0.5
                                if name and name != player_name and dist < MULTIPLAYER_DIST_THRESHOLD:
                                    has_other = True
                                    break
                    if has_other != self._multiplayer_mode:
                        self._multiplayer_mode = has_other
                        mode_str = "联机" if self._multiplayer_mode else "单人"
                        print(f"[agent] 🔄 模式切换 → {mode_str}模式")
                        bus.fire("multiplayer_mode_changed", {"mode": mode_str})

            except Exception as e:
                self.log(f"_state_loop 处理异常: {e}", "error")

            # 背包完全按需：挖矿/查询/装备时主动 get_inventory，这里不轮询。
            # 箱子缓存低频刷新（变化慢），供取物/存物使用
            if loop_count % 30 == 0:
                try:
                    self._chests = await self.mod.enum_chests()
                    await self.events.report_new_chests(self._chests)
                except Exception as e:
                    self.log(f"enum_chests 异常: {e}", "error")

            if not self._world_info:
                try:
                    self._world_info = await self.mod.get_server_info()
                    self.log(f"get_server_info: keys={list(self._world_info.keys())}")
                except Exception as e:
                    self.log(f"get_server_info 异常: {e}", "error")

            # 空闲小忙（v0.11 A4）：没人在指挥时也会自己找事干，贴身陪玩感
            try:
                from .idle import idle_drudge
            except Exception:
                idle_drudge = None
            busy = self.coordinator.status().get("busy")
            if idle_drudge is not None and not busy:
                try:
                    await idle_drudge(self, st)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.log(f"idle_drudge 异常: {e}", "warn")

            # 英雄成长（v0.10）：周期扫配方合成升级装备
            try:
                if not busy:
                    await self.upgrade.consider()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"upgrade 异常: {e}", "warn")

            await asyncio.sleep(self.cfg.get("state_tick_interval_seconds", 1.0))

    # ── 事件处理（参照 Lumi_Nox bridge._on_combat_event） ──

    def _handle_mod_event(self, msg: dict) -> None:
        """处理模组主动推送的事件消息。

        事件格式：{"type":"event","event":"player_died","message":"..."}
        可能事件：player_died, boss_spawned, boss_killed, invasion_start, npc_arrived 等
        """
        event = msg.get("event", "")
        bus = get_event_bus()
        print(f"[agent] 📨 mod事件: {event} msg={msg.get('message', '')[:80]}")

        if event == "player_died":
            # 事件推送的死亡（优先于轮询，更及时）
            if not self._is_dead:
                self._is_dead = True
                self._death_count += 1
                self._death_message = msg.get("message", f"角色死了 (第{self._death_count}次)")
                print(f"[agent] 💀 {self._death_message}")
                bus.fire(
                    "player_died",
                    {
                        "count": self._death_count,
                        "message": self._death_message,
                        "source": "mod_event",
                    },
                )

        elif event == "boss_spawned":
            bus.fire("boss_spawned", {"name": msg.get("boss_name", msg.get("message", "未知Boss"))})

        elif event == "boss_killed":
            bus.fire("boss_killed", {"name": msg.get("boss_name", msg.get("message", "未知Boss"))})

        elif event == "invasion_start":
            bus.fire("invasion_start", {"message": msg.get("message", "")})

        elif event == "invasion_end":
            bus.fire("invasion_end", {"message": msg.get("message", "")})

        # v3.0: 导航状态流事件（nav_moving/nav_arrived/nav_stuck/nav_timeout）
        # → 转发给 mod_link 的导航监听（navigate_async）
        elif event.startswith("nav_"):
            try:
                self.mod._on_nav_event(msg)
            except Exception:
                pass

        # v3.0: mod 主动汇报血量/位置（1s 一次）→ 更新 _state 缓存，
        # 不依赖 get_state 轮询的响应解析（避免 hp=0 问题）
        elif event == "player_status":
            try:
                self._state["hp"] = int(msg.get("hp", self._state.get("hp", 0)))
                self._state["max_life"] = int(msg.get("max_hp", self._state.get("max_life", 100)))
                self._state["tile_x"] = int(msg.get("x", self._state.get("tile_x", 0)))
                self._state["tile_y"] = int(msg.get("y", self._state.get("tile_y", 0)))
                if msg.get("alive") is False and not self._is_dead:
                    self._is_dead = True
            except Exception:
                pass

        # v3.0: mod 统一推送全量游戏状态（血量/位置/背包/敌人/玩家/时间，2s 一次）
        elif event == "game_state":
            try:
                pl = msg.get("player", {}) or {}
                self._state["hp"] = int(pl.get("hp", self._state.get("hp", 0)))
                self._state["max_life"] = int(pl.get("max_life", self._state.get("max_life", 100)))
                self._state["tile_x"] = int(pl.get("tile_x", self._state.get("tile_x", 0)))
                self._state["tile_y"] = int(pl.get("tile_y", self._state.get("tile_y", 0)))
                if "alive" in pl:
                    self._state["alive"] = bool(pl.get("alive"))
                # 背包
                inv = msg.get("inventory", {}) or {}
                if inv:
                    self._inv_full = {
                        "hotbar": inv.get("hotbar", []),
                        "equipped": inv.get("equipped", []),
                        "inventory": inv.get("inventory", []),
                        "selected_slot": pl.get("selected_slot", 0),
                    }
                # 附近敌人/玩家/时间
                if "nearby_npcs" in msg:
                    npcs = msg.get("nearby_npcs", []) or []
                    # C# 推流 npcs 用 tileX/tileY（camelCase），归一化供战斗/大脑读取
                    for n in npcs:
                        if isinstance(n, dict) and "tileX" in n and "tile_x" not in n:
                            n["tile_x"] = n.get("tileX", 0)
                            n["tile_y"] = n.get("tileY", 0)
                    self._state["nearby_npcs"] = npcs
                if "nearby_players" in msg:
                    self._state["nearby_players"] = msg.get("nearby_players", [])
                if "time_of_day" in msg:
                    self._state["time_of_day"] = msg.get("time_of_day", "")
            except Exception:
                pass

        # 其他事件统一转发
        elif event:
            bus.fire(event, msg)

    def on_respawn(self, callback) -> None:
        """注册复活回调。复活时自动调用，用于 brain 注册自动寻路等。"""
        self.respawn_callbacks.append(callback)

    async def submit_goal(self, goal: Goal) -> None:
        await self.tasks.submit(goal)

    async def run_complex_task(
        self,
        steps: List[Dict[str, Any]],
        goal_text: str = "",
        source: str = SRC_OWNER,
        dry_run: bool = False,
        _retried: bool = False,
    ) -> Dict[str, Any]:
        """收到任务 → 想(评估) → 处理(规划) → 做(执行)。主人的任务可打断自主行为。"""
        # 想：推演整串步骤，缺东西就自己想补救办法
        assess = await self.brain.think(steps)
        self.log(f"想：{assess.say()}", "task")
        for t in assess.thoughts:
            self.log(f"  思考：{t}", "task")
        if not assess.doable:
            await self.send_chat(f"主人，{assess.say()}~")
            # v2.1: 需要主人提供信息（缺材料/目标模糊）→ 发起决策询问，带答案重试一次
            if not _retried and self.inquiry and assess.need_from_owner:
                inq = self.inquiry.ask(
                    question=f"主人，{assess.say()}，怎么办？",
                    options=["你来决定", "先不做"],
                    context={"need": assess.need_from_owner, "goal": goal_text},
                    timeout=30.0,
                )
                if inq:
                    await self.send_chat(inq.question)
                    ans = await self.inquiry.wait_answer(inq)
                    if ans not in ("auto", "hold", "timeout"):
                        self.log(f"主人回答：{ans}，带答案重试任务", "task")
                        return await self.run_complex_task(
                            steps, f"{goal_text}（主人说：{ans}）", source, dry_run=dry_run, _retried=True
                        )
            return {
                "ok": False,
                "status": "not_doable",
                "phase": "think",
                "output": assess.say(),
                "why": assess.explain(),
                "need": assess.need_from_owner,
            }
        # 想到了补救办法就先说一声，让主人知道我在动脑子
        if assess.fixes:
            await self.send_chat(f"主人，{assess.say()}~")

        # 处理：用补全前置后的步骤编译，顺手合并冗余
        plan = self.brain.plan(steps, goal_text, assess)
        self.log(f"处理：{plan.say()}", "task")
        if not plan.goals:
            return {"ok": False, "status": "empty_plan", "phase": "plan", "output": "没解析出可执行步骤"}
        if dry_run:
            return {
                "ok": True,
                "status": "planned",
                "phase": "plan",
                "output": f"我会这样做：{plan.say()}",
                "why": assess.explain(),
                "fixes": assess.fixes,
                "skipped": plan.skipped,
            }

        # 做：交给执行器（单槽位 + 可被主人打断）
        async def _work(info):
            info.phase = "act"
            for i, g in enumerate(plan.goals):
                if self.executor.should_stop():
                    return {"ok": False, "status": "cancelled", "output": f"做到第{i + 1}步被叫停了"}
                info.step_index = i + 1
                info.note = plan.outline[i] if i < len(plan.outline) else g.goal_type
                self.log(f"做：第{i + 1}/{len(plan.goals)}步 {info.note}", "task")
                # 每步开工前说一声（A5：过程不静默，猫娘有存在感，主人知道在忙）
                try:
                    await self.send_chat(f"好，先来做第{i + 1}步：{info.note}~")
                except Exception:
                    pass
                ok = await self.tasks.run_one(g)
                if not ok:
                    msg = g.report_fail or f"第{i + 1}步没做成"
                    await self.send_chat(f"主人，{msg}，我停下来等你~")
                    return {
                        "ok": False,
                        "status": "step_failed",
                        "phase": "act",
                        "output": f"在第{i + 1}步「{info.note}」停下：{msg}",
                    }
                # 每步成功后也吱一声（有真实结果才说，不空喊）
                try:
                    await self._announce_step_done(self.tasks.chain(), g)
                except Exception:
                    pass
            return {"ok": True, "status": "ok", "phase": "act", "output": f"做完啦：{plan.say()}"}

        return await self.executor.run(goal_text or "多步任务", _work, source=source, steps=plan.outline)

    async def _announce_step_done(self, chain, goal: "Goal") -> None:
        """A5：步骤成功后的播报（有真实结果才说，不空喊）。"""
        try:
            last = chain[-1] if chain else ""
            await self.send_chat(f"{last} 哦~")
        except Exception:
            pass

    async def command(self, text: str, source: str = SRC_OWNER) -> Dict[str, Any]:
        """自然语言统一入口：自动判断长期任务/有限任务/喊停并派发。"""
        return await self.coordinator.handle(text, source)

    async def start_longterm(
        self, kind: str, target: str = "", amount: int = 0, reason: str = "", **params
    ) -> Dict[str, Any]:
        """直接起一个长期任务（跟随/挖矿/守点）。"""
        return await self.jobs.start(kind, target=target, amount=amount, reason=reason, **params)

    async def stop_longterm(self, kind: str = "", why: str = "主人喊停") -> Dict[str, Any]:
        """停某个或全部长期任务。"""
        if kind:
            ok = await self.longterm.stop(kind, why)
            return {"ok": ok, "output": "停下了~" if ok else "我没在做这个呀~"}
        names = await self.longterm.stop_all(why)
        return {"ok": bool(names), "output": ("、".join(names) + " 都停下了~") if names else "我现在闲着呢~"}

    def task_status(self) -> Dict[str, Any]:
        """我在干嘛：前台 + 后台一起报。"""
        return self.coordinator.status()

    async def interrupt_current(self, why: str = "主人有新指令") -> bool:
        # 主人随时可打断：停掉正在执行的前台任务（长期任务不受影响，继续跟着）
        return await self.executor.cancel_current(why)

    async def stop_everything(self, why: str = "主人喊停") -> Dict[str, Any]:
        """全停：前台任务 + 所有长期任务。"""
        fg = await self.executor.cancel_current(why)
        names = await self.longterm.stop_all(why)
        return {"ok": True, "foreground_cancelled": fg, "longterm_stopped": names}

    async def send_chat(self, text: str) -> None:
        """通过 Mod 发送聊天消息（A5：加保护，发不出去不炸线程）"""
        try:
            await self.mod.send_chat(text)
        except Exception as e:
            self.log(f"发送聊天失败: {e}", "warn")

    async def speak(self, text: str, ai_behavior: str = "respond") -> None:
        """播报一句话（A5）：优先走宿主 LLM 语气回复，不可用时游戏内聊天兜底，保证不静默。

        - ai_behavior="respond"：猫娘语气回复（主人听的到）
        - ai_behavior="read"：静默通知（不打断主人，只给 LLM 知道）
        """
        plugin = getattr(self, "plugin", None)
        push = getattr(plugin, "push_message", None)
        if push:
            try:
                await push(parts=[{"type": "text", "text": text}], ai_behavior=ai_behavior)
                return
            except Exception:
                pass
        # 兜底：游戏内聊天直接说（不带 LLM 语气，但保证有声音）
        if ai_behavior != "read":
            try:
                await self.send_chat(text)
            except Exception:
                pass

    def get_state(self) -> Dict[str, Any]:
        return self._state

    def remember(self, key: str, value: str, category: str = "fact") -> None:
        try:
            plugin = getattr(self, "plugin", None)
            store_fn = getattr(plugin, "_memory_store", None)
            if store_fn:
                store_fn().remember(key, value, category=category)
        except Exception:
            pass

    @property
    def current_goal(self) -> str:
        g = getattr(self.tasks, "_current", None)
        return g.goal_type if g else ""

    def resolve_item(self, name: str) -> int:
        from .item_npc_dict import item_id

        return item_id(name, self.registry)

    async def follow_player(self, player_pos: tuple) -> None:
        """自动寻路走到玩家身边"""
        px, py = player_pos
        await self.executor.run("跟随玩家", lambda info: self.navigate_to(px, py, timeout=8), source=SRC_AUTO)

    async def heal_self(self) -> bool:
        # 用注册表中标记为 heal（加血）的物品自愈：
        # 先确保背包有（没有就 give 一个），再选中并使用
        heals = self.registry.find_by_tag("heal")
        for pid in heals:
            try:
                inv = await self.mod.get_inventory()
                slot_id = None
                for slot in (inv.get("hotbar", []) or []) + (inv.get("inventory", []) or []):
                    if slot.get("id") == pid:
                        slot_id = slot.get("inv_slot", 0)
                        break
                if slot_id is None:
                    ok = await self.mod.give_item(pid, 1)
                    if not ok:
                        continue
                    inv2 = await self.mod.get_inventory()
                    for slot in (inv2.get("hotbar", []) or []) + (inv2.get("inventory", []) or []):
                        if slot.get("id") == pid:
                            slot_id = slot.get("inv_slot", 0)
                            break
                if slot_id is None:
                    continue
                await self.mod.select_item(slot_id)
                await self.mod.use_item_slot(slot_id)
                self.log("喝了加血物品", "item")
                return True
            except Exception:
                continue
        return False

    async def use_item_on_self(self, name: str) -> bool:
        iid = self.resolve_item(name)
        if iid < 0:
            return False
        return await self.mod.give_item(iid, 1)

    async def navigate_to(self, x: int, y: int, timeout: int = 25) -> bool:
        """自动寻路走到坐标（v3.0: 流式导航——C# BFS 寻路 + 状态流，可中断）"""
        await self.capability.refresh()
        st = self._state
        cur_y = st.get("tile_y", 0)
        height_diff = cur_y - y
        if height_diff > 3 and not self.capability.can_climb(height_diff):
            self.log(f"落差{height_diff}格单次上不去，尝试分段爬升", "nav")
            return await self.climb_to(x, y)

        ok = await self.mod.navigate_async(x, y, timeout)
        self.log(f"走到 ({x},{y}) " + ("成功" if ok else "失败/超时"), "nav")
        if not ok:
            await self.send_chat(f"主人，我过不去 ({x},{y})，卡住了，等你想想办法~")
        return ok

    async def climb_to(self, x: int, y: int, _round: int = 1) -> bool:
        """复杂垂直移动（深坑回地面）：先规划分段，再逐段执行"""
        if _round > 5:
            self.log("爬升重规划超过5轮，放弃", "warn")
            await self.send_chat("主人，我试了好几次都上不去，你来帮帮我吧~")
            return False
        start_y = self._state.get("tile_y", 0)
        plan = await self.planner.plan_climb(x, y)
        if not plan.legs:
            self.log("爬升规划失败：" + plan.blocked_reason, "warn")
            await self.send_chat(f"主人，{plan.blocked_reason}，我上不去，等你命令~")
            return False
        self.log(f"爬升计划（{len(plan.legs)}段）：{plan.describe()}", "nav")
        if not plan.feasible:
            await self.send_chat(f"主人，我先爬{len(plan.legs)}段试试，不过{plan.blocked_reason}，可能到不了顶~")

        for i, leg in enumerate(plan.legs):
            if self.executor.should_stop():
                self.log("爬升被打断", "warn")
                return False
            self.log(f"第{i + 1}/{len(plan.legs)}段：{leg.method} → ({leg.tx},{leg.ty})", "nav")
            ok = await self.mod.navigate_to(leg.tx, leg.ty, timeout=1)
            if not ok:
                await self.send_chat(f"主人，我卡在第{i + 1}段了（{leg.method}到 {leg.tx},{leg.ty}），上不去啦~")
                self.log(f"第{i + 1}段失败，中止爬升", "warn")
                return False
            await self.capability.refresh()

        if plan.feasible:
            self.log("爬升完成", "nav")
            return True

        await self.refresh_state()
        now_y = self._state.get("tile_y", 0)
        if start_y - now_y < 1:
            self.log("本轮没有上升，停止重规划", "warn")
            await self.send_chat("主人，我卡在这上不去了，等你想想办法~")
            return False
        return await self.climb_to(x, y, _round + 1)

    # --- 物品/箱子：实现在 InventoryOps，这里保留原有调用签名 ---
    async def get_inventory(self) -> Dict[str, Any]:
        return await self.items.get_inventory()

    async def refresh_inventory(self) -> Dict[str, Any]:
        """按需刷新背包缓存（C# 不推背包，需要时主动拉取）。"""
        try:
            self._inv_full = await self.mod.get_inventory()
        except Exception:
            pass
        return self._inv_full

    def get_inventory_sync(self) -> Dict[str, Any]:
        return self._inv_full

    def locate_item(self, name: str) -> Dict[str, Any]:
        return self.items.locate_item(name)

    def count_item(self, name: str) -> int:
        return self.items.count_item(name)

    async def nearest_chest_with(self, name: str) -> Optional[Dict[str, Any]]:
        return await self.items.nearest_chest_with(name)

    async def store_to_chest(self, name: str, chest: Dict[str, Any], stack: int = 1) -> bool:
        return await self.items.store_to_chest(name, chest, stack)

    async def take_from_chest(self, name: str, chest: Dict[str, Any], stack: int = 1) -> bool:
        return await self.items.take_from_chest(name, chest, stack)

    async def hand_to_player(self, name: str, stack: int = 1) -> bool:
        return await self.items.hand_to_player(name, stack)

    async def use_item_by_name(self, name: str) -> bool:
        return await self.items.use_item_by_name(name)

    def get_log_sync(self) -> List[Dict[str, Any]]:
        return self._log

    def get_chests_sync(self) -> List[Dict[str, Any]]:
        return self._chests

    async def mine_then_fetch(self, ore: str, surface_item: str, ore_amount: int = 10) -> bool:
        # 深坑挖矿场景：先挖矿，再回地面拿某物，最后回到坑底玩家身边
        # 第1步：挖矿（在坑底进行）
        await self.submit_goal(
            Goal(goal_type="gather", target=ore, amount=ore_amount, reason="坑底挖矿", report_fail="挖不到矿石，主人")
        )
        # 第2步：回地面取物（用 navigate_to 会自动预判能力并汇报）
        chest = await self.nearest_chest_with(surface_item)
        if chest is None:
            chests = await self.mod.enum_chests()
            chest = chests[0] if chests else None
        if chest:
            if not await self.navigate_to(chest["x"], chest["y"]):
                await self.send_chat("回地面拿东西上不去，等我拿到钩锁/土块再说~")
                return False
            await self.take_from_chest(surface_item, chest)
        else:
            await self.send_chat("附近没有箱子，不知道去哪拿" + surface_item)
            return False
        # 第3步：回到玩家身边（坑底）
        st = self._state
        players = st.get("nearby_players", [])
        if players:
            await self.navigate_to(players[0]["tile_x"], players[0]["tile_y"])
        return True

    @property
    def running(self) -> bool:
        return self._running
