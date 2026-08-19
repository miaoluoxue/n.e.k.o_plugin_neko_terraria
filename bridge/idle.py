"""空闲自驱引擎（v0.11 A4）：没人在指挥时猫娘也自己找事干，提供"陪玩感"。

参照 Lumi_Nox bridge._run_survival_loop 的 multiplayer fallback：
- 主人走远 → 追上去（迟滞带，防抖）
- 周期就近挖矿（find_ore → dig_tile → 收集，背包计数确认）
- 低亮度 → 掏火把照亮
- 偶尔给主人塞个火把

与 autonomous/motivation 的区别：
  motivation 是 LLM/情绪驱动且会抢，这里是纯规则、不吃前台槽位；
  只在 _state_loop 里每 1s 轮询时顺手做一步（动作本身是 45s 周期的慢节律），
  随时可被主人任务打断（coordinator busy 时完全不进本函数）。
"""

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

# 追随迟滞带（与 standing_jobs 同套语义）
FOLLOW_TRIGGER_DIST = 60
FOLLOW_STOP_DIST = 18
# 矿井循环节律（慢周期，不打扰主人）
MINE_SCAN_INTERVAL = 45  # 每 N 秒扫一次矿
MINE_TARGETS = ["铁矿", "铜矿", "锡矿", "银矿", "钨矿", "金矿"]  # 优先顺序
MINE_SLOTS = 3  # 每轮最多点 N 个目标
MINE_MAX_DIST = 30  # 矿点离我超过 30 格不开挖（远距离不导航）
# 照明节律
LIGHT_CHECK_INTERVAL = 35
# 各模式声明冷却（避免刷屏）
ANNOUNCE_COOLDOWNS = {
    "follow": 40,
    "mine": 25,
    "light": 45,
}


def _me(st) -> Tuple[int, int]:
    return int(st.get("tile_x", 0) or 0), int(st.get("tile_y", 0) or 0)


def _owner(st) -> Optional[Tuple[int, int]]:
    """最近的非自身玩家（残留槽位过滤）。"""
    players = st.get("nearby_players", []) or []
    best, best_d = None, 10**9
    mx, my = _me(st)
    for p in players:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        if name == st.get("player_name", ""):
            continue  # 过滤自身（get_state 不含自身，但 state 可能来自推送含自身）
        x = int(p.get("tile_x", 0) or 0)
        y = int(p.get("tile_y", 0) or 0)
        if x == 0 and y == 0:
            continue
        d = (x - mx) ** 2 + (y - my) ** 2
        if d < best_d:
            best_d = d
            best = (x, y)
    return best


def _cooldown_ok(ctx: Dict[str, Any], mode: str) -> bool:
    last = ctx.get(f"last_say_{mode}", 0.0)
    return time.time() - last > ANNOUNCE_COOLDOWNS[mode]


def _should_talk(ctx: Dict[str, Any], mode: str, text: str) -> bool:
    """同一句话（同模式+同摘要）只在冷却结束后才说，避免洗碗刷屏。"""
    if not _cooldown_ok(ctx, mode):
        return False
    ctx[f"last_say_{mode}"] = time.time()
    ctx["last_say_slug"] = f"{mode}:{text[:24]}"
    return True


async def _pack_torch(agent) -> None:
    """确保背包有火把并丢给主人（1 个/次，冷却 90s）。"""
    ctx = agent._idle_ctx
    if time.time() - ctx.get("last_give_torch", 0) < 90:
        return
    try:
        iid = agent.resolve_item("火把")
        if iid < 0:
            return
        ok = await agent.equip.drop_for_player(iid, 1)
        if ok:
            ctx["last_give_torch"] = time.time()
            agent.log("给主人塞了个火把", "give")
    except Exception:
        pass


async def idle_drudge(agent, st: Dict[str, Any]) -> None:
    """每轮状态刷新时跑一次。st 为最新状态快照。"""
    ctx = agent._idle_ctx
    if not st:
        return
    # 自爆保护：血量过低不主动搞事
    hp = int(st.get("hp", 0) or 0)
    max_hp = int(st.get("max_life", 100) or 100) or 100
    if max_hp > 0 and hp / max_hp < 0.35:
        return

    ctx["cycle"] += 1
    owner = _owner(st)
    if owner is None:
        return  # 单人模式不主动搞事（等主人）

    mx, my = _me(st)
    ox, oy = owner
    dist = ((ox - mx) ** 2 + (oy - my) ** 2) ** 0.5

    # ── P-1 基地（低频）：首次初始化 + 背包满回家存 ──
    base = getattr(agent, "base", None)
    if base is not None and ctx["cycle"] % 30 == 0:  # 每 30s
        try:
            if base.base_pos is None:
                await base.init_base()
            else:
                await base.resupply()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            agent.log(f"基地逻辑异常: {e}", "warn")

    # ── P0 追随（迟滞带，防抖） ──
    if not ctx.get("_following"):
        if dist >= FOLLOW_TRIGGER_DIST:
            ctx["_following"] = True
            if _should_talk(ctx, "follow", "追"):
                await agent.send_chat("主人走远了，等等我喵！")
    else:
        if dist <= FOLLOW_STOP_DIST:
            ctx["_following"] = False

    if ctx.get("_following"):
        try:
            await agent.mod.navigate_stream_fire(ox, oy)
        except Exception:
            pass
        return  # 本秒在追，不干别的

    # ── P1 周期矿井：就近扫矿，挖 1-3 块（进度由背包增量确认） ──
    if ctx["cycle"] % MINE_SCAN_INTERVAL == 0 and dist < 40:
        try:
            await _mine_job(agent, st, mx, my)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            agent.log(f"空闲挖矿异常: {e}", "warn")
        return

    # ── P1.5 周期捡掉落物（探索的奖励：掉落物/战利品主动收集） ──
    if ctx["cycle"] % 15 == 0 and dist < 40:
        try:
            collected = await agent.mod.collect_items(radius=500)
            if collected > 0 and ctx["cycle"] % 60 == 0:
                agent.log(f"捡了 {collected} 个掉落物", "item")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        return

    # ── P1.6 陪伴式生活小动作：砍树/钓鱼（什么任务用什么工具） ──
    life = getattr(agent, "life", None)
    if life is not None and ctx["cycle"] % 30 == 0 and dist < 30:
        try:
            await life.do_something()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            agent.log(f"生活小动作异常: {e}", "warn")
        return

    # ── P2 照明：低亮度 → 掏火把照亮 ──
    if ctx["cycle"] % LIGHT_CHECK_INTERVAL == 0:
        b = st.get("brightness", 1.0)
        if b is not None and b < 0.45:
            try:
                await _light_up(agent, st)
            except Exception:
                pass
            return

    # ── P3 给主人塞火把（低频） ──
    if ctx["cycle"] % 60 == 0 and dist < 20:
        try:
            await _pack_torch(agent)
        except Exception:
            pass


async def _mine_job(agent, st: Dict[str, Any], mx: int, my: int) -> None:
    """扫一圈矿，挖 1-3 块目标矿石（进度由背包增量确认，杜绝假完成）。"""
    n = 0
    for ore in MINE_TARGETS:
        iid = agent.resolve_item(ore)
        if iid < 0:
            continue
        try:
            from .item_npc_dict import tile_type_of
            ores = await agent.mod.find_ore(radius=40, tile_type=tile_type_of(ore, iid))
        except Exception:
            ores = []
        if not ores:
            continue
        # 命中断言：最近矿离我不超 30（远距离不开导航）
        mine = ores[0]
        d = abs(mine.get("x", 0) - mx) + abs(mine.get("y", 0) - my)
        if d > MINE_MAX_DIST:
            continue
        try:
            got = await agent.mining.mine_ore_inplace(ore, mine["x"], mine["y"])
        except Exception as e:
            got = None
            agent.log(f"挖{ore}失败: {e}", "warn")
        if got:
            n += 1
            if _should_talk(agent._idle_ctx, "mine", f"挖到{got}个{ore}"):
                await agent.send_chat(f"挖到几块{ore}啦喵～")
        if n >= MINE_SLOTS:
            break
    # 挖完顺手把掉落物捡了（不报数，安静点）
    try:
        await agent.mod.collect_items(radius=400)
    except Exception:
        pass


async def _light_up(agent, st: Dict[str, Any]) -> None:
    """低亮度：持火把发光。"""
    slots = await _torch_slots(agent)
    if not slots:
        return
    slot = slots[0]
    try:
        await agent.mod.select_item(slot)
        await asyncio.sleep(0.2)
        await agent.mod.use_item_slot(slot)
    except Exception:
        pass


async def _torch_slots(agent) -> list:
    iid = agent.resolve_item("火把")
    if iid < 0:
        return []
    inv = agent.get_inventory_sync()
    out = []
    for it in (inv.get("inventory", []) or []) + (inv.get("hotbar", []) or []):
        if it.get("id") == iid and it.get("inv_slot") is not None:
            out.append(it["inv_slot"])
    return out
