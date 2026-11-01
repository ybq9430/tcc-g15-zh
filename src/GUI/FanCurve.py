"""
风扇曲线数据模型。

曲线是「阶梯型」的：给定 N 个温度边界，把 40~100°C 切成 N+1 段，
每一段一个固定风扇百分比。语义与上游 SILENT_CURVE 一致：

    boundaries = [82, 87, 91]
    speeds     = [0, 30, 65, 100]

    temp <  82        -> 0%
    82 <= temp <  87  -> 30%
    87 <= temp <  91  -> 65%
    temp >= 91        -> 100%

额外支持一个「噪音偏移」(bias)：把整条曲线的温度边界统一平移，
让用户只拖一根「安静 ↔ 凉快」的滑块就能微调整体倾向，而不必改每一档。
"""

import json
from typing import List, Optional, Tuple

TEMP_MIN = 40
TEMP_MAX = 100
SPEED_MIN = 0
SPEED_MAX = 100
MIN_GAP = 2          # 相邻温度边界的最小间隔（°C）
BIAS_LIMIT = 8       # 噪音偏移的上下限（°C）

DEFAULT_BOUNDARIES = [82, 87, 91]
DEFAULT_SPEEDS = [0, 30, 65, 100]


def _normalizeSequence(values: List[int], lo: int, hi: int, gap: int) -> List[int]:
    """夹取到 [lo, hi]、保证严格递增且相邻间隔 >= gap。

    先做正向推挤，再做反向修正——只做正向会把末尾顶出上界，
    例如 [200] 夹成 98 后推挤成 [98, 100, 102]。
    """
    out = [max(lo, min(hi, int(v))) for v in values]
    for i in range(1, len(out)):
        if out[i] < out[i - 1] + gap:
            out[i] = out[i - 1] + gap
    if out and out[-1] > hi:
        out[-1] = hi
        for i in range(len(out) - 2, -1, -1):
            if out[i] > out[i + 1] - gap:
                out[i] = out[i + 1] - gap
    return [max(lo, v) for v in out]


class FanCurve:
    def __init__(self, boundaries: Optional[List[int]] = None,
                 speeds: Optional[List[int]] = None, bias: int = 0) -> None:
        if boundaries is None:
            boundaries = list(DEFAULT_BOUNDARIES)
        if speeds is None:
            speeds = list(DEFAULT_SPEEDS)
        self.boundaries: List[int] = [int(b) for b in boundaries]
        self.speeds: List[int] = [int(s) for s in speeds]
        self.bias: int = int(bias)
        if len(self.speeds) != len(self.boundaries) + 1:
            raise ValueError("speeds 必须比 boundaries 多一个元素")
        self.normalize()

    # ---------- 构造 ----------

    @classmethod
    def default(cls) -> "FanCurve":
        return cls()

    @classmethod
    def fromJson(cls, raw) -> "FanCurve":
        """从 QSettings 里取出的 JSON 字符串还原；任何异常都退回默认值"""
        try:
            d = json.loads(raw) if isinstance(raw, str) else dict(raw)
            return cls(d["b"], d["s"], d.get("bias", 0))
        except Exception:
            return cls()

    def toJson(self) -> str:
        return json.dumps({"b": self.boundaries, "s": self.speeds, "bias": self.bias},
                          ensure_ascii=False)

    def copy(self) -> "FanCurve":
        return FanCurve(self.boundaries, self.speeds, self.bias)

    # ---------- 规范化 ----------

    def normalize(self) -> None:
        """把边界夹到合法区间、保证严格递增且间隔达标、转速夹到 0~100"""
        self.bias = max(-BIAS_LIMIT, min(BIAS_LIMIT, int(self.bias)))
        self.boundaries = _normalizeSequence(self.boundaries,
                                            TEMP_MIN + MIN_GAP, TEMP_MAX - MIN_GAP, MIN_GAP)
        self.speeds = [max(SPEED_MIN, min(SPEED_MAX, int(s))) for s in self.speeds]

    # ---------- 求值 ----------

    def effectiveBoundaries(self) -> List[int]:
        """叠加噪音偏移后的实际边界。偏移后仍需保持递增，否则逐项兜住。"""
        return _normalizeSequence([b + self.bias for b in self.boundaries],
                                  TEMP_MIN + MIN_GAP, TEMP_MAX - MIN_GAP, MIN_GAP)

    def bandIndex(self, temp: Optional[int]) -> int:
        """温度落在第几档。读不到温度时按最高档处理，宁可吵不可热"""
        if temp is None:
            return len(self.speeds) - 1
        for i, b in enumerate(self.effectiveBoundaries()):
            if temp < b:
                return i
        return len(self.speeds) - 1

    def speedAt(self, temp: Optional[int]) -> int:
        return self.speeds[self.bandIndex(temp)]

    def bandLowerEdge(self, index: int) -> int:
        """第 index 档的下界温度（用于降档滞环判断）"""
        if index <= 0:
            return -1000
        return self.effectiveBoundaries()[index - 1]

    def levelRanges(self) -> List[Tuple[str, int]]:
        """给界面/提示文本用的 (温度描述, 转速) 列表"""
        eb = self.effectiveBoundaries()
        out: List[Tuple[str, int]] = []
        for i, spd in enumerate(self.speeds):
            if i == 0:
                desc = f"< {eb[0]}°C"
            elif i == len(self.speeds) - 1:
                desc = f"≥ {eb[-1]}°C"
            else:
                desc = f"{eb[i-1]}~{eb[i]}°C"
            out.append((desc, spd))
        return out

    def describe(self) -> str:
        return "　".join(f"{d}·{s}%" for d, s in self.levelRanges())

    # ---------- 修改 ----------

    def setBoundary(self, index: int, value: int) -> None:
        if 0 <= index < len(self.boundaries):
            self.boundaries[index] = int(value)
            self.normalize()

    def setSpeed(self, index: int, value: int) -> None:
        if 0 <= index < len(self.speeds):
            self.speeds[index] = int(value)
            self.normalize()

    def setBias(self, value: int) -> None:
        self.bias = int(value)
        self.normalize()

    def reset(self) -> None:
        self.boundaries = list(DEFAULT_BOUNDARIES)
        self.speeds = list(DEFAULT_SPEEDS)
        self.bias = 0
        self.normalize()

    def __eq__(self, other) -> bool:
        if not isinstance(other, FanCurve):
            return NotImplemented
        return (self.boundaries == other.boundaries and
                self.speeds == other.speeds and
                self.bias == other.bias)
