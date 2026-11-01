"""
风扇曲线编辑器：直接拖动阶梯图上的控制点。

- 拖动竖直边界：调整温度阈值
- 拖动线段上的圆点：调整该档的风扇百分比
- 灰色竖线表示当前 GPU / CPU 实测温度，方便对着真实温度调参
"""

from typing import Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.AppColors import Colors
from GUI.FanCurve import FanCurve, TEMP_MIN, TEMP_MAX


class CurveEditorWidget(QtWidgets.QWidget):
    curveChanged = QtCore.Signal()

    MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 46, 16, 16, 32
    HIT_RADIUS = 11
    SPEED_SNAP = 5

    def __init__(self, curve: FanCurve, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._curve = curve
        self._drag: Optional[Tuple[str, int]] = None
        self._liveTemps: Tuple[Optional[int], Optional[int]] = (None, None)
        self._hover: Optional[Tuple[str, int]] = None
        self.setMinimumSize(440, 250)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CrossCursor)
        self.setToolTip("拖动竖线改温度阈值，拖动圆点改该档转速")

    # ---------- 对外 ----------

    def setCurve(self, curve: FanCurve) -> None:
        self._curve = curve
        self.update()

    def curve(self) -> FanCurve:
        return self._curve

    def setLiveTemps(self, gpu: Optional[int], cpu: Optional[int]) -> None:
        self._liveTemps = (gpu, cpu)
        self.update()

    # ---------- 坐标换算 ----------

    def _plotRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(
            self.MARGIN_L, self.MARGIN_T,
            max(1.0, self.width() - self.MARGIN_L - self.MARGIN_R),
            max(1.0, self.height() - self.MARGIN_T - self.MARGIN_B),
        )

    def _xForTemp(self, t: float) -> float:
        r = self._plotRect()
        return r.left() + (t - TEMP_MIN) / (TEMP_MAX - TEMP_MIN) * r.width()

    def _tempForX(self, x: float) -> float:
        r = self._plotRect()
        return TEMP_MIN + (x - r.left()) / r.width() * (TEMP_MAX - TEMP_MIN)

    def _yForSpeed(self, s: float) -> float:
        r = self._plotRect()
        return r.bottom() - s / 100.0 * r.height()

    def _speedForY(self, y: float) -> float:
        r = self._plotRect()
        return (r.bottom() - y) / r.height() * 100.0

    # ---------- 几何 ----------

    def _segments(self):
        """[(x起, x止, 转速, 档位索引)]"""
        eb = self._curve.effectiveBoundaries()
        speeds = self._curve.speeds
        out = []
        for i, spd in enumerate(speeds):
            t0 = TEMP_MIN if i == 0 else eb[i - 1]
            t1 = TEMP_MAX if i == len(speeds) - 1 else eb[i]
            out.append((self._xForTemp(t0), self._xForTemp(t1), spd, i))
        return out

    def _handles(self):
        """返回 [(种类, 索引, x, y)]，种类为 'b'(边界) 或 's'(线段)"""
        out = []
        segs = self._segments()
        eb = self._curve.effectiveBoundaries()
        for i, b in enumerate(eb):
            x = self._xForTemp(b)
            y = (self._yForSpeed(segs[i][2]) + self._yForSpeed(segs[i + 1][2])) / 2.0
            out.append(('b', i, x, y))
        for x0, x1, spd, i in segs:
            out.append(('s', i, (x0 + x1) / 2.0, self._yForSpeed(spd)))
        return out

    def _hitTest(self, pos: QtCore.QPointF):
        best = None
        bestDist = self.HIT_RADIUS
        for kind, idx, x, y in self._handles():
            d = ((pos.x() - x) ** 2 + (pos.y() - y) ** 2) ** 0.5
            if d < bestDist:
                bestDist = d
                best = (kind, idx)
        return best

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self._plotRect()

        p.fillRect(self.rect(), QtGui.QColor(Colors.DARK_GREY.value))
        p.fillRect(r, QtGui.QColor('#171717'))

        font = QtGui.QFont('Microsoft YaHei UI', 8)
        p.setFont(font)

        # 网格
        gridPen = QtGui.QPen(QtGui.QColor('#2e2e2e'))
        gridPen.setWidthF(1.0)
        p.setPen(gridPen)
        labelPen = QtGui.QPen(QtGui.QColor(Colors.GREY.value))
        for t in range(TEMP_MIN, TEMP_MAX + 1, 10):
            x = self._xForTemp(t)
            p.setPen(gridPen)
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
            p.setPen(labelPen)
            p.drawText(QtCore.QRectF(x - 18, r.bottom() + 4, 36, 14),
                       QtCore.Qt.AlignCenter, f"{t}°")
        for s in range(0, 101, 20):
            y = self._yForSpeed(s)
            p.setPen(gridPen)
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
            p.setPen(labelPen)
            p.drawText(QtCore.QRectF(2, y - 8, self.MARGIN_L - 8, 16),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, f"{s}%")

        # 曲线下方填充
        segs = self._segments()
        poly = QtGui.QPolygonF()
        poly.append(QtCore.QPointF(segs[0][0], r.bottom()))
        for x0, x1, spd, _ in segs:
            y = self._yForSpeed(spd)
            poly.append(QtCore.QPointF(x0, y))
            poly.append(QtCore.QPointF(x1, y))
        poly.append(QtCore.QPointF(segs[-1][1], r.bottom()))
        fill = QtGui.QColor(Colors.BLUE.value)
        fill.setAlpha(45)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(fill)
        p.drawPolygon(poly)
        p.setBrush(QtCore.Qt.NoBrush)

        # 阶梯线
        curvePen = QtGui.QPen(QtGui.QColor(Colors.BLUE.value).lighter(160))
        curvePen.setWidthF(2.0)
        p.setPen(curvePen)
        for x0, x1, spd, _ in segs:
            y = self._yForSpeed(spd)
            p.drawLine(QtCore.QPointF(x0, y), QtCore.QPointF(x1, y))
        eb = self._curve.effectiveBoundaries()
        for i, b in enumerate(eb):
            x = self._xForTemp(b)
            p.drawLine(QtCore.QPointF(x, self._yForSpeed(segs[i][2])),
                       QtCore.QPointF(x, self._yForSpeed(segs[i + 1][2])))

        # 实时温度标记
        colors = (QtGui.QColor(Colors.GREEN.value), QtGui.QColor(Colors.YELLOW.value))
        for i, t in enumerate(self._liveTemps):
            if t is None:
                continue
            x = self._xForTemp(max(TEMP_MIN, min(TEMP_MAX, t)))
            pen = QtGui.QPen(colors[i])
            pen.setWidthF(1.2)
            pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
            p.setPen(QtGui.QPen(colors[i]))
            label = f"{'GPU' if i == 0 else 'CPU'} {t}°"
            onLeft = x < r.center().x()
            labelRect = QtCore.QRectF(x + 4 if onLeft else x - 74,
                                      r.top() + 2 + i * 14, 70, 14)
            p.drawText(labelRect, QtCore.Qt.AlignLeft if onLeft else QtCore.Qt.AlignRight, label)

        # 控制点
        for kind, idx, x, y in self._handles():
            active = self._hover == (kind, idx)
            radius = 6.5 if active else 5.0
            if kind == 'b':
                p.setPen(QtGui.QPen(QtGui.QColor(Colors.WHITE.value), 1.5))
                p.setBrush(QtGui.QColor('#2f2f2f'))
                p.drawEllipse(QtCore.QPointF(x, y), radius, radius)
                p.setPen(QtGui.QPen(QtGui.QColor(Colors.WHITE.value), 1.5))
                p.drawLine(QtCore.QPointF(x, y - 4), QtCore.QPointF(x, y + 4))
            else:
                p.setPen(QtGui.QPen(QtGui.QColor(Colors.DARK_GREY.value), 1.5))
                p.setBrush(QtGui.QColor(Colors.BLUE.value).lighter(170))
                p.drawEllipse(QtCore.QPointF(x, y), radius, radius)

        p.end()

    # ---------- 交互 ----------

    def mousePressEvent(self, event) -> None:
        if event.button() != QtCore.Qt.LeftButton:
            return
        hit = self._hitTest(event.position())
        if hit is not None:
            self._drag = hit
            self._applyDrag(event.position())

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None:
            self._applyDrag(event.position())
            return
        hit = self._hitTest(event.position())
        if hit != self._hover:
            self._hover = hit
            self.setCursor(QtCore.Qt.SizeHorCursor if hit and hit[0] == 'b'
                           else QtCore.Qt.SizeVerCursor if hit else QtCore.Qt.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()

    def _applyDrag(self, pos: QtCore.QPointF) -> None:
        kind, idx = self._drag
        if kind == 'b':
            target = int(round(self._tempForX(pos.x())))
            self._curve.setBoundary(idx, target - self._curve.bias)
        else:
            raw = self._speedForY(pos.y())
            snapped = int(round(raw / self.SPEED_SNAP) * self.SPEED_SNAP)
            self._curve.setSpeed(idx, snapped)
        self.update()
        self.curveChanged.emit()
