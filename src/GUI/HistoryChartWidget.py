"""
温度 / 转速历史记录。

拆成两部分：
- HistoryBuffer：只管存数据，主界面每秒往里塞一个采样点，与界面是否可见无关
- HistoryChartWidget：只负责画，从 buffer 里读

这样设置窗口关掉再打开不会丢历史，也不用在对话框里定时采样。
用 QPainter 直接画折线，不引 QtCharts（那会多十几 MB 打包体积）。
"""

import collections
import csv
import time
from typing import Deque, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.AppColors import Colors

Sample = Tuple[float, Optional[int], Optional[int], Optional[int], Optional[int]]


class HistoryBuffer:
    def __init__(self, capacity: int = 1800) -> None:
        self.capacity = capacity
        self.samples: Deque[Sample] = collections.deque(maxlen=capacity)

    def add(self, gpuTemp: Optional[int], cpuTemp: Optional[int],
            gpuRpm: Optional[int], cpuRpm: Optional[int]) -> None:
        self.samples.append((time.time(), gpuTemp, cpuTemp, gpuRpm, cpuRpm))

    def clear(self) -> None:
        self.samples.clear()

    def __len__(self) -> int:
        return len(self.samples)

    def exportCsv(self, path: str) -> int:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["时间", "GPU温度(°C)", "CPU温度(°C)", "GPU转速(RPM)", "CPU转速(RPM)"])
            for ts, gt, ct, gr, cr in self.samples:
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                            gt if gt is not None else "",
                            ct if ct is not None else "",
                            gr if gr is not None else "",
                            cr if cr is not None else ""])
        return len(self.samples)


class HistoryChartWidget(QtWidgets.QWidget):
    MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 42, 52, 22, 28
    MAX_TEMP = 105
    MAX_RPM = 6000

    def __init__(self, buffer: HistoryBuffer,
                 parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._buf = buffer
        self.setMinimumSize(460, 190)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def refresh(self) -> None:
        self.update()

    def _plotRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(
            self.MARGIN_L, self.MARGIN_T,
            max(1.0, self.width() - self.MARGIN_L - self.MARGIN_R),
            max(1.0, self.height() - self.MARGIN_T - self.MARGIN_B),
        )

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self._plotRect()

        p.fillRect(self.rect(), QtGui.QColor(Colors.DARK_GREY.value))
        p.fillRect(r, QtGui.QColor('#171717'))

        p.setFont(QtGui.QFont('Microsoft YaHei UI', 8))

        gridPen = QtGui.QPen(QtGui.QColor('#2e2e2e'))
        gridPen.setWidthF(1.0)
        labelPen = QtGui.QPen(QtGui.QColor(Colors.GREY.value))

        for t in range(0, self.MAX_TEMP + 1, 20):
            y = r.bottom() - t / self.MAX_TEMP * r.height()
            p.setPen(gridPen)
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
            p.setPen(labelPen)
            p.drawText(QtCore.QRectF(2, y - 8, self.MARGIN_L - 8, 16),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, f"{t}°")
        for rpm in range(0, self.MAX_RPM + 1, 1500):
            y = r.bottom() - rpm / self.MAX_RPM * r.height()
            p.setPen(labelPen)
            p.drawText(QtCore.QRectF(r.right() + 4, y - 8, self.MARGIN_R - 6, 16),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, f"{rpm}")

        samples = self._buf.samples
        if len(samples) < 2:
            p.setPen(labelPen)
            p.drawText(r, QtCore.Qt.AlignCenter, "正在采集数据…")
            p.end()
            return

        count = len(samples)

        def xAt(index: int) -> float:
            # 按当前已有样本数铺满宽度；缓冲区满了以后自然变成滑动窗口。
            # 早期样本少时如果按 capacity 映射，曲线会挤在最右边看不出东西。
            denom = max(count - 1, 1)
            return r.left() + index / denom * r.width()

        gpuColor = QtGui.QColor(Colors.GREEN.value)
        cpuColor = QtGui.QColor(Colors.YELLOW.value)

        def drawSeries(getter, color, maxVal, width, alpha, dashed=False):
            col = QtGui.QColor(color)
            col.setAlpha(alpha)
            pen = QtGui.QPen(col)
            pen.setWidthF(width)
            if dashed:
                pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            prev = None
            for i, s in enumerate(samples):
                v = getter(s)
                if v is None:
                    prev = None
                    continue
                cur = QtCore.QPointF(xAt(i), r.bottom() - v / maxVal * r.height())
                if prev is not None:
                    p.drawLine(prev, cur)
                prev = cur

        drawSeries(lambda s: s[3], gpuColor, self.MAX_RPM, 1.2, 110)
        drawSeries(lambda s: s[4], cpuColor, self.MAX_RPM, 1.2, 110)
        drawSeries(lambda s: s[1], gpuColor, self.MAX_TEMP, 2.0, 255)
        drawSeries(lambda s: s[2], cpuColor, self.MAX_TEMP, 2.0, 255)

        legend = [("GPU 温度", gpuColor, 255), ("CPU 温度", cpuColor, 255),
                  ("GPU 转速", gpuColor, 110), ("CPU 转速", cpuColor, 110)]
        x = r.left() + 8
        for text, color, alpha in legend:
            col = QtGui.QColor(color)
            col.setAlpha(alpha)
            p.setPen(QtGui.QPen(col, 2.0))
            p.drawLine(QtCore.QPointF(x, r.top() - 12), QtCore.QPointF(x + 14, r.top() - 12))
            p.setPen(QtGui.QPen(QtGui.QColor(Colors.WHITE.value)))
            p.drawText(QtCore.QRectF(x + 18, r.top() - 20, 72, 16), QtCore.Qt.AlignLeft, text)
            x += 88

        span = int(samples[-1][0] - samples[0][0])
        p.setPen(labelPen)
        p.drawText(QtCore.QRectF(r.left(), r.bottom() + 6, r.width(), 16),
                   QtCore.Qt.AlignCenter, f"最近 {span} 秒 · {count} 个采样")
        p.end()
