"""视觉感知：截图理解 + 游戏状态融合 → 交互引擎事件。

v2.0: 实现了实际的 LLM Vision 分析流程。
  1. core/vision.py 节流截图（每 6s 一帧）
  2. 图片变化检测 → 有意义变化时才起 LLM Vision
  3. LLM Vision 分析 → 结构化感知报告
  4. 与 game_state 数据交叉验证
  5. 注入交互引擎 inject_event()（发现宝箱/稀有物/地形变化/危险）
"""

import asyncio
from typing import Any, Dict, List, Optional

from ..core.context import build_user_context


# ── LLM Vision 分析 prompt（v0.7：画面理解 + 情感）──
# 猫娘通过主人窗口画面"看"主人在干什么，输出结构化事实 + 自己的心情与想说的话。
# 台词归属角色 LLM：这里的 want_to_say 只是画面触发的第一反应，最终表达由
# 主程序 LLM 以人设润色（同战雷插件的"事实行+要求行"模式）。

VISION_ANALYSIS_PROMPT = """你是猫娘，你在看主人玩泰拉瑞亚的游戏画面（主玩家视角）。
请用 JSON 格式报告你看到的内容和你的感受：

{{
    "scene_type": "underground|cave|surface|boss_fight|ocean|dungeon|unknown",
    "owner_action": "build|mine|fight|explore|idle|unknown",
    "visible_threats": ["怪物名"...],
    "owner_hp_visible": "high|medium|low|unknown",
    "day_or_night": "day|night|unknown",
    "mood": "curious|excited|worried|afraid|proud|bored|cozy",
    "summary": "一句话画面描述（猫娘语气，不超过20字）",
    "want_to_say": "你想对主人说的话（可为空字符串，不超过15字，猫娘语气）"
}}

只输出 JSON，不要废话。"""


class VisualPerception:


    def __init__(self, vision_bridge, agent=None) -> None:
        self.vision = vision_bridge
        self.agent = agent
        self._llm_vision = None             # async (b64: str, prompt: str) -> str
        self._last_frame_hash = None        # 上次截图的简单哈希
        self._last_analysis_ts = 0.0        # 上次 LLM Vision 分析时间
        self._min_interval = 15.0           # LLM Vision 最低间隔（秒）
        self._last_report: Dict[str, Any] = {}
        self._pending_tasks: List[asyncio.Task] = []  # 追踪 fire-and-forget 任务

    def set_llm_vision(self, llm_vision) -> None:
        """设置 LLM Vision 调用函数。

        llm_vision: async (b64_image: str, prompt: str) -> str
        不设置时只做截图转发，不做 LLM 分析。
        """
        self._llm_vision = llm_vision

    async def feed(self, b64: str, mime: str = "image/jpeg") -> None:

        # 始终转发截图到视觉管线
        await self.vision.on_frame(b64, mime)

        # 无 LLM Vision 回调：跳过分析
        if not self._llm_vision:
            return

        # 变化检测
        if not self._should_analyze(b64):
            return

        # LLM Vision 分析
        await self._analyze_frame(b64)

    def _should_analyze(self, b64: str) -> bool:

        import hashlib
        import time

        now = time.monotonic()
        if now - self._last_analysis_ts < self._min_interval:
            return False

        # 简单图片哈希（取中间段）
        mid = len(b64) // 2
        sample = b64[mid:mid + 200]
        h = hashlib.md5(sample.encode()).hexdigest()

        if h == self._last_frame_hash:
            return False

        self._last_frame_hash = h
        return True

    async def _analyze_frame(self, b64: str) -> None:

        import time

        try:
            raw = await self._llm_vision(b64, VISION_ANALYSIS_PROMPT)
        except Exception:
            return

        self._last_analysis_ts = time.monotonic()

        report = self._parse_vision_response(raw)
        if not report:
            return

        # 与 game_state 交叉验证
        if self.agent:
            await self._cross_validate(report)

        self._last_report = report
        self._emit_events(report)

    @staticmethod
    def _parse_vision_response(raw: str) -> Optional[Dict[str, Any]]:
        """解析 LLM Vision 返回的 JSON。"""
        import json
        cleaned = raw.strip()
        for fence in ("```json", "```"):
            cleaned = cleaned.replace(fence, "").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    pass
        return None

    async def _cross_validate(self, report: Dict[str, Any]) -> None:

        try:
            state = self.agent.get_state()
        except Exception:
            return

        # 威胁交叉验证：get_state 实际键名是 nearby_npcs（v0.6 修正，
        # 原 nearby_enemies 永远为空导致 enemy_spotted 从不触发）
        visions_threats = report.get("visible_threats", []) or []
        game_enemies = state.get("nearby_npcs", []) or state.get("nearby_enemies", []) or []
        game_enemy_names = [str(n.get("name", "")).lower() for n in game_enemies]

        confirmed_threats = []
        for vt in visions_threats:
            vt_lower = vt.lower()
            if any(vt_lower in gn for gn in game_enemy_names):
                confirmed_threats.append(vt)
        if confirmed_threats:
            report["confirmed_threats"] = confirmed_threats

    def _emit_events(self, report: Dict[str, Any]) -> None:

        if not self.agent:
            return
        try:
            brain = getattr(self.agent, "brain", None)
            if not brain or not getattr(brain, "interaction", None):
                return
            inject = brain.interaction.inject_event
        except Exception:
            return

        summary = report.get("summary", "")

        # 清理已完成的任务
        self._pending_tasks = [t for t in self._pending_tasks if not t.done()]

        def _fire(coro):
            """创建并追踪一个 fire-and-forget 任务。"""
            task = asyncio.create_task(coro)
            self._pending_tasks.append(task)
            task.add_done_callback(lambda t: self._pending_tasks.remove(t)
                                   if t in self._pending_tasks else None)

        # 敌人（画面看到 + 游戏数据确认）
        confirmed = report.get("confirmed_threats", []) or report.get("visible_threats", []) or []
        if confirmed:
            _fire(inject("enemy_spotted", 0.3,
                         f"看到敌人: {', '.join(confirmed)}。{summary}"))

        # 主人动作变化（v0.7：画面理解→情感事件）
        owner_action = report.get("owner_action", "")
        prev_action = self._last_report.get("owner_action", "")
        if owner_action and owner_action != prev_action:
            action_map = {
                "fight": ("owner_fighting", 0.5,
                          f"主人正在战斗！{summary}"),
                "mine": ("owner_mining", 0.2,
                         f"主人在挖矿。{summary}"),
                "build": ("owner_building", 0.2,
                          f"主人在盖东西。{summary}"),
                "explore": ("owner_exploring", 0.3,
                            f"主人在探索！{summary}"),
                "idle": ("owner_idle", 0.1,
                         f"主人好像闲下来了。{summary}"),
            }
            if owner_action in action_map:
                evt, lv, desc = action_map[owner_action]
                _fire(inject(evt, lv, desc))

        # 画面心情（v0.7：猫娘对画面的第一反应 → 情绪弧线 + 想说的话）
        mood = report.get("mood", "")
        if mood:
            mood_map = {"excited": "excitement", "worried": "fear",
                        "afraid": "fear", "proud": "proud",
                        "curious": "curiosity", "cozy": "excitement"}
            emo = mood_map.get(mood)
            if emo:
                try:
                    brain.interaction.mood.trigger(emo, 0.4)
                except Exception:
                    pass
        want = (report.get("want_to_say") or "").strip()
        if want:
            _fire(inject("scene_say", 0.25, want))

        # 场景变化
        scene = report.get("scene_type", "")
        prev_scene = self._last_report.get("scene_type", "")
        if scene and scene != prev_scene and prev_scene:
            _fire(inject("terrain_changed", 0.2,
                         f"场景切换: {prev_scene} → {scene}。{summary}"))

    @property
    def last_report(self) -> Dict[str, Any]:
        return self._last_report
