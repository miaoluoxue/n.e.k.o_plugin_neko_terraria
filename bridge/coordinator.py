"""任务协调中枢：一句话进来，决定谁去做、要不要让路、怎么回话。"""

from __future__ import annotations

import asyncio
import json
import time
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from . import intent as intent_mod                  # 保留旧 intent 作为 fallback
from .executor import SRC_OWNER, SRC_AUTO
from ..polish.human_timing import HumanTiming
from ..core.context import build_anchor_msg

if TYPE_CHECKING:
    from ..llm.intent_parser import LLMIntentParser, IntentResult

log = logging.getLogger(__name__)


class TaskCoordinator:
    """统一入口：主人说什么，这里决定怎么落地。"""

    def __init__(self, agent, name: str = "neko") -> None:
        from ..llm.intent_parser import LLMIntentParser  # 延迟导入打破循环

        self.agent = agent
        # LLM 意图解析器（llm_call 未设置时走 fallback 正则解析）
        self._intent_parser = LLMIntentParser(agent, name=name)
        self.timing = HumanTiming()  # v2.1: 人类化延迟
        # 长期任务去重：同类指令在窗口内重复触发（双路径/重复下发）不重启
        self._last_longterm: tuple = (None, None, 0.0)

    def set_llm_call(self, llm_call) -> None:
        """设置 LLM 调用函数，启用 LLM 意图解析。"""
        self._intent_parser._llm_call = llm_call

    @property
    def lt(self):
        return self.agent.longterm

    @property
    def jobs(self):
        return self.agent.jobs

    @property
    def executor(self):
        return self.agent.executor

    # ---------------- 主入口 ----------------
    async def handle(self, text: str, source: str = SRC_OWNER) -> Dict[str, Any]:
        """解析并派发一句指令。"""
        # 陪伴：主人开口了 → 通知交互引擎刷新静默计时
        if source == SRC_OWNER:
            try:
                from ..autonomous.event_bus import get_event_bus
                get_event_bus().fire("owner_spoke", {"text": text})
            except Exception:
                pass

        # v2.1: 先检查是否是对待决询问的回答（任务中询问闭环）
        # 命中 → 不算新指令，直接记录答案并让猫娘回应
        inq_mgr = getattr(self.agent, "inquiry", None)
        if source == SRC_OWNER and inq_mgr and inq_mgr.has_pending:
            answered = inq_mgr.match_answer(text)
            if answered:
                await self._announce_inquiry_answered(answered)
                return {"ok": True, "status": "inquiry_answered",
                        "question": answered.question, "answer": answered.answer,
                        "output": answered.answer}

        # v0.7: 指令情感反馈（夸/凶）——主人语气影响依恋值与情绪
        try:
            from ..autonomous.heart import Heart
            aff = Heart.classify_affection(text)
            if aff:
                brain = getattr(self.agent, "brain", None)
                heart = getattr(brain, "heart", None) if brain else None
                if aff == "praise":
                    if heart:
                        heart.on_praise(text)
                    if brain and getattr(brain, "interaction", None):
                        brain.interaction.mood.trigger("excitement", 0.5)
                        await brain.interaction.push_speech(
                            f"[被夸] 主人夸我了：{text}\n开心地回应（1句话，15字内）",
                            behavior="respond")
                elif aff == "scold":
                    if heart:
                        heart.on_scold(text)
                    if brain and getattr(brain, "interaction", None):
                        brain.interaction.mood.trigger("tired", 0.45)
                        await brain.interaction.push_speech(
                            f"[被凶] 主人凶我了：{text}\n委屈地回应（1句话，15字内）",
                            behavior="respond")
        except Exception:
            pass

        result = await self._intent_parser.parse(text)
        self.agent.log(f"[coordinator.handle] 📋 解析结果: mode={result.mode}, kind={result.kind}, target={result.target}", "info")

        # 记录到上下文（用于"继续"/"再来点"等指代理解）
        self._intent_parser.record_success(text, result)

        # 先回话（LLM 解析出的 pre_reply，如"好的主人~"）
        if result.pre_reply:
            try:
                await self.agent.send_chat(result.pre_reply)
            except Exception:
                pass

        # 处理打断级别
        if result.interrupt_level > 0 and result.mode != "chat":
            await self._handle_interrupt(result)

        # 推给主程序 LLM，以完整角色人设自然回应
        if result.mode == "chat":
            await self._do_chat(text, result)
            return {"ok": True, "status": "chat", "mode": "chat",
                    "output": result.pre_reply,
                    "intent": result.to_dict()}

        # 分发（兼容旧 Intent 接口字段）
        if result.mode == "stop":
            self.agent.log(f"[coordinator.handle] 🛑 执行 stop", "info")
            return await self._do_stop(result)
        if result.mode == "longterm":
            self.agent.log(f"[coordinator.handle] ⏳ 执行 longterm", "info")
            return await self._do_longterm(result)
        if result.mode == "finite":
            self.agent.log(f"[coordinator.handle] 📝 执行 finite", "info")
            return await self._do_finite(result, source)

        # 认不出来：根据置信度决定是反问还是拒绝
        return await self._handle_unknown(text, result)

    # ---------------- 闲聊处理 ----------------

    async def _do_chat(self, text: str, it: IntentResult) -> None:
        """陪伴式闲聊：把主人的话题交给主程序 LLM，以角色人设完整回应。"""
        try:
            plugin = getattr(self.agent, "plugin", None)
            push = getattr(plugin, "push_message", None)
            if not push:
                return
            # 当前游戏状态一句话（防 LLM 编造）
            try:
                anchor = build_anchor_msg(self.agent)
            except Exception:
                anchor = ""
            mood_note = ""
            brain = getattr(plugin, "_autonomous_brain", None)
            inter = getattr(brain, "interaction", None) if brain else None
            if inter:
                style = inter.mood.primary_style()
                if style:
                    mood_note = f"\n你此刻的心情：{json.dumps(style, ensure_ascii=False)}"
            await push(
                parts=[{"type": "text", "text": (
                    f"[主人找你聊天] 主人说：『{text}』{mood_note}"
                    f"\n{anchor}\n"
                    f"用猫娘语气自然回应主人的话（闲聊，1-2句，20字内，"
                    f"不要做任务、不要提'执行/完成'）")}],
                ai_behavior="respond")
        except Exception:
            pass  # 闲聊推送失败不阻塞

    # ---------------- 询问回答处理 ----------------

    async def _announce_inquiry_answered(self, answered) -> None:
        """主人回答了询问 → 猫娘回应一句 + 通知交互引擎。"""
        try:
            await self.agent.send_chat("好的主人~")
        except Exception:
            pass
        try:
            from ..autonomous.event_bus import get_event_bus
            await get_event_bus().publish("inquiry_answered", {
                "question": answered.question, "answer": answered.answer})
        except Exception:
            pass

    # ---------------- 打断处理 ----------------

    async def _handle_interrupt(self, it: IntentResult) -> None:
        """统一委托 brain 的四级打断（走事件总线，避免双轨逻辑）。"""
        level = it.interrupt_level
        if level >= 3:
            bus_level = 4
        elif level >= 2:
            bus_level = 3
        else:
            bus_level = 1

        from ..autonomous.event_bus import get_event_bus
        await get_event_bus().publish("interrupt", {
            "level": bus_level,
            "reason": it.reason or it.pre_reply or "主人有新指令",
            "task_name": it.target or "",
        })

    # ---------------- 停止 ----------------
    async def _do_stop(self, it) -> Dict[str, Any]:
        if it.kind:
            t = self.lt.get(it.kind)
            if t is None:
                # 停的可能是前台有限任务（如"挖10个铁"进行中）→ 校验名称匹配再停
                cur = self.executor.current()
                if cur and self._stop_matches(it, cur):
                    await self.agent.interrupt_current("主人喊停")
                    return {"ok": True, "output": "好的，我不做了~"}
                return {"ok": True, "output": "我本来就没在做这个呀~"}
            name = t.name
            await self.lt.stop(it.kind, "主人喊停")
            return {"ok": True, "output": f"好的，不{name}了~"}
        # 没指明停什么：全停
        names = await self.lt.stop_all("主人喊停")
        if self.executor.current():
            await self.agent.interrupt_current("主人喊停")
        if names:
            return {"ok": True, "output": "好的，" + "、".join(names) + " 都停下了~"}
        return {"ok": True, "output": "我现在什么都没在做哦~"}

    @staticmethod
    def _stop_matches(it, cur: Dict[str, Any]) -> bool:
        """前台任务名是否与停止意图的 kind 匹配，避免"别守了"误停挖矿。"""
        kind = it.kind or ""
        name = str(cur.get("name", "") or "")
        table = {"follow": ("跟", "跟随"), "mine": ("挖", "矿", "采"),
                 "guard": ("守",)}
        kw = table.get(kind)
        if not kw:
            return True  # kind 未知：保守停前台
        return any(w in name for w in kw)

    # ---------------- 长期任务 ----------------
    async def _do_longterm(self, it) -> Dict[str, Any]:
        # 去重：同类指令 8 秒内重复触发（双路径/重复下发）→ 不重启，返回已在进行
        now = time.time()
        last_kind, last_target, last_ts = self._last_longterm
        if (last_kind == it.kind and last_target == it.target
                and now - last_ts < 8.0
                and self.lt.get(it.kind) is not None):
            self.agent.log(f"[coordinator] ⏸ 长期任务去重: kind={it.kind} "
                           f"target={it.target}（{now - last_ts:.1f}s 内重复）", "info")
            return {"ok": True, "status": "running",
                    "mode": "longterm",
                    "output": f"我已经在{it.kind}了，不用重复说~"}
        self._last_longterm = (it.kind, it.target, now)

        self.agent.log(f"[coordinator] 🟢 _do_longterm() 开始: kind={it.kind}, target={it.target}", "info")
        await asyncio.sleep(self.timing.command_delay())
        res = await self.jobs.start(it.kind, target=it.target,
                                    amount=it.amount, reason=it.reason)
        try:
            self.agent.remember(
                "主人偏好-长期任务",
                f"主人让我长期{it.kind}({it.target or '无目标'})",
                category="preference")
        except Exception:
            pass
        self.agent.log(f"[coordinator] 🟢 _do_longterm() 完成: {res}", "info")
        res["mode"] = "longterm"
        res["intent"] = it.snapshot()
        return res

    # ---------------- 有限任务 ----------------
    async def _do_finite(self, it, source: str) -> Dict[str, Any]:
        steps = list(it.steps or [])
        if not steps:
            # LLM 可能没产出 steps：从 kind/target/amount 自动生成
            steps = self._auto_steps(it)
        if not steps:
            log.warning("finite 任务无 steps，无法执行: %s", it.to_dict())
            return {"ok": False, "status": "not_understood",
                    "output": "喵？这个我没太明白，主人再说清楚点嘛~"}
        # 统一规范化：LLM 兜底可能产出字符串步骤，task_brain 需要 dict
        from ..llm.intent_parser import LLMIntentParser
        it.steps = LLMIntentParser._normalize_steps(steps)
        # 人类化延迟：pre_reply 已发，"好的喵~"说了 → 停顿一下再动手
        await asyncio.sleep(self.timing.command_delay())
        return await self.run_foreground(it.steps, it.reason or it.raw, source)

    @staticmethod
    def _auto_steps(it: IntentResult) -> List[Dict[str, Any]]:

        kind = it.kind
        target = it.target or ""
        amount = it.amount
        kind_map = {
            "mine":    ("mine",   "挖掘"),
            "chop":    ("gather", "砍伐"),
            "dig":     ("mine",   "挖掘"),
            "fish":    ("gather", "钓鱼"),
            "hunt":    ("goto",   "猎杀"),
            "collect": ("gather", "收集"),
            "place":   ("goto",   "放置"),
            "craft":   ("craft",  "制作"),
        }
        action, _ = kind_map.get(kind, ("goto", "执行"))
        item = target or "矿"
        if action == "goto" and item == "矿" and kind != "hunt":
            item = "目标"
        if target and amount:
            return [{"action": action, "item": item, "amount": max(1, amount)}]
        if target:
            return [{"action": action, "item": item, "amount": 1}]
        if amount:
            return [{"action": "gather", "item": "物品", "amount": max(1, amount)}]
        return [{"action": action, "item": item, "amount": 1}]

    async def run_foreground(self, steps: List[str], goal_text: str = "",
                             source: str = SRC_OWNER,
                             dry_run: bool = False) -> Dict[str, Any]:
        """跑前台有限任务。让路/恢复由执行器统一负责，这里不重复处理。"""
        return await self.agent.run_complex_task(
            steps, goal_text, source, dry_run=dry_run)

    # ---------------- 状态汇报 ----------------
    def status(self) -> Dict[str, Any]:
        cur = self.executor.current()
        longs = self.lt.active()
        return {
            "foreground": cur,
            "longterm": longs,
            "busy": bool(cur) or bool(longs),
            "say": self.say(),
        }

    def say(self) -> str:
        """一句话说清"我在干嘛"。"""
        cur = self.executor.current()
        longs = self.lt.say_all()
        if cur and longs:
            return f"正在{cur.get('name', '做任务')}，同时{longs}"
        if cur:
            return f"正在{cur.get('name', '做任务')}"
        if longs:
            return longs
        return "我现在闲着呢，主人有什么要我做的吗~"

    # ---------------- 意图不明处理（反问澄清）----------------

    async def _handle_unknown(self, text: str, result: IntentResult) -> Dict[str, Any]:
        """处理无法识别的意图：根据置信度决定反问还是直接拒绝。"""
        confidence = result.confidence

        # 语义匹配成功（confidence >= 0.7）→ 确认后执行
        if result.source == "semantic" and confidence >= 0.7:
            inq_mgr = getattr(self.agent, "inquiry", None)
            if inq_mgr and not inq_mgr.has_pending:
                # 反问确认
                inq = inq_mgr.ask(
                    f"喵～是不是像之前那样，{result.reason}呀？",
                    options=["是的", "不是"],
                    timeout=30.0
                )
                if inq:
                    try:
                        await self.agent.send_chat(inq.question)
                        # 等待回答（非阻塞，由主循环处理）
                        return {"ok": False, "status": "confirming",
                                "mode": "unknown", "output": inq.question,
                                "intent": result.to_dict()}
                    except Exception:
                        pass

        # 置信度较低（< 0.5）→ 开放式反问
        if confidence < 0.5:
            inq_mgr = getattr(self.agent, "inquiry", None)
            if inq_mgr and not inq_mgr.has_pending:
                inq = inq_mgr.ask(
                    "喵？主人是想让我做任务还是在跟我聊天呀～",
                    options=["做任务", "聊天"],
                    timeout=30.0
                )
                if inq:
                    try:
                        await self.agent.send_chat(inq.question)
                        return {"ok": False, "status": "need_clarification",
                                "mode": "unknown", "output": inq.question,
                                "intent": result.to_dict()}
                    except Exception:
                        pass

        # 默认：陪伴式回应（不是客服提示）
        await self._do_chat(text, result)
        return {"ok": False, "status": "not_understood", "mode": "unknown",
                "output": "喵？主人说的我有点懵，换个说法跟我说嘛~",
                "intent": result.to_dict()}
