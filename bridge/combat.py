"""真人级战斗：走位拉扯 + 垫土 + 钩锁 + 黑名单，基于 mod 状态决策。
- 所有移动通过 mod.navigate_to 实现
- 伤害通过 mod.damage_npc 实现

v0.11（A1）：战斗守卫不吞任务。fight_nearest 增加 yield 能力：
  check_task 存在时，一旦目标不可达/被隔墙/超时就返回 False（打不死就不打），
  由守卫方决定是否退回主线（不占前台任务槽）。
"""

import asyncio
import time
from typing import Any, Callable, Dict, Optional

from .mod_link import ModLink


class CombatEngine:
    def __init__(self, mod: ModLink, agent: Any = None) -> None:
        self.mod = mod
        self.agent = agent
        self._blacklist: Dict[tuple, float] = {}
        self.blacklist_secs = 30
        self.no_dmg_timeout = 4
        # ── 风筝参数（参照 Lumi_Nox KITE_IDEAL_DIST/KITE_TOO_CLOSE） ──
        self.kite_ideal_dist = 3.0  # 理想距离（曼哈顿）：站桩打
        self.kite_too_close = 1.0  # 过近：后退
        self.kite_too_far = 8.0  # 过远：追击靠近
        self.max_height_gap = 10  # 超过此垂直差视为不可达（跳过）
        self.retreat_hp_ratio = 0.30  # 自身血量低于此比例 → 逃跑保命

    def _blacklist_enemy(self, name: str, x: int, y: int, reason: str) -> None:
        self._blacklist[(name, x, y)] = time.time() + self.blacklist_secs

    def _is_blacklisted(self, name: str, x: int, y: int) -> bool:
        key = (name, x, y)
        exp = self._blacklist.get(key, 0)
        if exp and time.time() < exp:
            return True
        if key in self._blacklist:
            del self._blacklist[key]
        return False

    async def fight_nearest(
        self, state: Dict[str, Any], timeout: int = 10, check_task: Optional[Callable[[], bool]] = None
    ) -> bool:
        """战斗最近的敌人（风筝走位 + 黑名单 + 隔墙判定 + 低血保命）。

        check_task: 外部提供的"是否有更重要的前台任务"谓词。
          为真 → 战斗中放弃（打不死就不打，不占任务槽，交给守卫方调度）。
          视频对照 Lumi_P1：战斗优先但可让路。
        """
        px0 = int(state.get("tile_x", 0) or 0)
        py0 = int(state.get("tile_y", 0) or 0)
        target = self._pick_target(state, px0, py0)
        if not target:
            return False

        slot = int(target.get("slot", 0) or 0)
        start = time.time()
        last_change = time.time()
        while time.time() - start < timeout:
            # 重要任务优先：主人要求做的事 > 打小怪（不打折的主线）
            if check_task is not None and check_task():
                await self.mod.navigate_to(px0, py0, timeout=1)  # 先归位，别飘太远
                return False

            # ★ 每轮刷新状态：目标血量/位置是动态的（旧实现用静态快照，
            #   目标血量永不更新 → 隔墙判定永不触发，只能等超时退出）
            try:
                state = self.agent.get_state() if self.agent else state
            except Exception:
                pass
            px = int(state.get("tile_x", px0) or px0)
            py = int(state.get("tile_y", py0) or py0)

            # 从最新状态找目标（按 slot），死亡/消失 → 胜利
            cur = None
            for e in state.get("nearby_npcs", []) or []:
                if int(e.get("slot", -1) or -1) == slot:
                    cur = e
                    break
            if cur is None or int(cur.get("life", 0) or 0) <= 0:
                # 战斗胜利，收集掉落物（打完不抢任务，掉落让主线收）
                try:
                    collected = await self.mod.collect_items(radius=400)
                    if self.agent and collected > 0:
                        self.agent.logger.info(f"[战斗] 拾取了 {collected} 个掉落物")
                except Exception:
                    pass
                return True
            tx = int(cur.get("tile_x", 0) or 0)
            ty = int(cur.get("tile_y", 0) or 0)
            hp = int(cur.get("life", 0) or 0)

            # 自身低血 → 保命（不恋战）
            my_hp = int(state.get("hp", 100) or 100)
            my_max = int(state.get("max_life", 100) or 100) or 100
            if my_hp / my_max < self.retreat_hp_ratio:
                if self.agent is not None:
                    await self.agent.heal_self()
                return False  # 撤，交给 brain._guard_check 的逃跑逻辑

            dx = tx - px
            dy = ty - py
            dist = abs(dx) + abs(dy)

            # ── 风筝走位 ──
            if dist < self.kite_too_close:
                # 过近 → 后退 3 格
                away = -1 if dx > 0 else 1
                await self.mod.navigate_to(px + away * 3, py, timeout=1)
            elif dist > self.kite_too_far:
                # 过远 → 追击靠近
                await self.mod.navigate_to(tx, ty, timeout=1)
            else:
                # 理想距离：侧面站位打（目标上方则偏下，防止隔墙）
                offset = -2 if tx > px else 2
                target_y = ty + 1 if dy < -2 else ty
                await self.mod.navigate_to(tx + offset, target_y, timeout=1)
            await self.mod.damage_npc(slot, 50)

            # 伤害后刷新状态读最新血量（联机伤害由服务器结算，缓存不刷新会拿到旧值）
            new_hp = hp
            try:
                if self.agent:
                    await self.agent.refresh_state()
                    st2 = self.agent.get_state()
                else:
                    st2 = state
                for e in st2.get("nearby_npcs", []) or []:
                    if int(e.get("slot", -1) or -1) == slot:
                        new_hp = int(e.get("life", 0) or 0)
                        break
            except Exception:
                pass
            if new_hp <= 0:
                return True
            # 隔墙判定：持续无伤 → 黑名单弃战
            if new_hp < hp:
                last_change = time.time()
            if time.time() - last_change > self.no_dmg_timeout:
                self._blacklist_enemy(cur.get("name", ""), tx, ty, "隔墙")
                return False

            await self._maybe_cover(px, py, ty)
            await asyncio.sleep(0.3)
        return False

    def _pick_target(self, state: Dict[str, Any], px: int, py: int) -> Optional[Dict[str, Any]]:
        """选最近的可达敌人（跳过黑名单/高差过大的）。"""
        enemies = state.get("nearby_npcs", []) or []
        best = None
        best_dist = 10**9
        for e in enemies:
            if int(e.get("damage", 0) or 0) <= 0:
                continue
            if int(e.get("life", 0) or 0) <= 0:
                continue
            ex = int(e.get("tile_x", 0) or 0)
            ey = int(e.get("tile_y", 0) or 0)
            if self._is_blacklisted(e.get("name", ""), ex, ey):
                continue
            # 可达性：垂直差过大（悬崖上/深坑里）跳过
            if abs(ey - py) > self.max_height_gap:
                continue
            d = abs(ex - px) + abs(ey - py)
            if d < best_dist:
                best_dist = d
                best = e
        return best

    async def _maybe_cover(self, px: int, py: int, ty: int) -> None:
        """坠落风险时在脚下垫土保命（x 用玩家当前位置，不能写死 0）"""
        if abs(ty - py) > 10:
            await self.mod.place_tile(px, py + 1, 0)
