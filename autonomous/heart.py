"""Heart（v0.7）：长期情感层——依恋值（主人关系，跨会话持久化）。

- 陪伴：与主人同屏时间增长（brain tick 喂入）
- 被夸：+3（"真乖""好棒"）；被凶：-4（"笨蛋""笨猫"）
- 冷落：连续 30 分钟无互动缓慢衰减
- 持久化：<插件目录>/data/bond.json（与 recipe_book 同层惯例）

输出：
- stickiness()：黏人指数 0-1 —— 跟随距离/贴贴行为调制
"""

import json
import time
from pathlib import Path
from typing import Optional

BOND_FILE = "data/bond.json"
COMPANION_GAIN_PER_HOUR = 4.0     # 同屏陪伴增长（/小时）
PRAISE_GAIN = 3.0                 # 被夸
SCOLD_LOSS = 4.0                  # 被凶
NEGLECT_DECAY_PER_HOUR = 2.0      # 冷落衰减（/小时）
NEGLECT_AFTER_SECONDS = 1800.0    # 30 分钟无互动算冷落

_PRAISE_WORDS = ("真乖", "好乖", "好棒", "真棒", "乖", "厉害", "聪明", "能干", "可爱")
_SCOLD_WORDS = ("笨蛋", "笨猫", "笨", "没用", "蠢", "废物", "傻猫")


class Heart:
    def __init__(self, agent=None, base_dir: str = "") -> None:
        self.agent = agent
        base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
        self._file = base / BOND_FILE
        self.bond = 50.0                       # 0-100
        self._last_interact_ts = time.time()
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            if self._file.exists():
                d = json.loads(self._file.read_text(encoding="utf-8"))
                self.bond = float(d.get("bond", 50.0))
                self._last_interact_ts = float(d.get("last_interact", time.time()))
        except Exception:
            pass
        self.bond = max(0.0, min(100.0, self.bond))

    def save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps(
                {"bond": round(self.bond, 1), "last_interact": self._last_interact_ts},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---------- 事件 ----------

    def on_praise(self, text: str = "") -> None:
        self._touch()
        self.bond = min(100.0, self.bond + PRAISE_GAIN)
        self.save()

    def on_scold(self, text: str = "") -> None:
        self._touch()
        self.bond = max(0.0, self.bond - SCOLD_LOSS)
        self.save()

    def on_companion(self, dt: float) -> None:
        """与主人同屏陪伴（brain tick 喂入时间增量秒）。"""
        self._touch()
        self.bond = min(100.0, self.bond + COMPANION_GAIN_PER_HOUR * dt / 3600.0)

    def on_neglect_tick(self, now: Optional[float] = None) -> None:
        """冷落衰减：长时间无互动（定期调用，低频）。"""
        now = now or time.time()
        idle = now - self._last_interact_ts
        if idle > NEGLECT_AFTER_SECONDS:
            self.bond = max(0.0, self.bond - NEGLECT_DECAY_PER_HOUR * idle / 3600.0)
            self.save()

    def _touch(self) -> None:
        self._last_interact_ts = time.time()

    # ---------- 输出 ----------

    def stickiness(self) -> float:
        """黏人指数 0-1：bond 50→0.3（正常），100→1.0（黏人）。"""
        return max(0.1, min(1.0, 0.3 + (self.bond - 50.0) / 100.0))

    @staticmethod
    def classify_affection(text: str) -> str:
        """指令情感分类：praise / scold / ""。"""
        t = (text or "").lower()
        for w in _PRAISE_WORDS:
            if w in t:
                return "praise"
        for w in _SCOLD_WORDS:
            if w in t:
                return "scold"
        return ""
