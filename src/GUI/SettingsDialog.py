"""设置对话框：风扇曲线 / 自动化 / 诊断。"""

import os
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from Backend.AppLog import logDir, logPath
from Backend.GameDetector import DEFAULT_EXCLUSIONS
from GUI.AppColors import Colors
from GUI.CurveEditorWidget import CurveEditorWidget
from GUI.FanCurve import FanCurve, BIAS_LIMIT
from GUI.HistoryChartWidget import HistoryBuffer, HistoryChartWidget
from GUI.SettingsKeys import SettingsKey

TAB_CURVE, TAB_AUTOMATION, TAB_DIAGNOSTICS = 0, 1, 2


def vkName(vk: int) -> str:
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x70 + 1}"
    return f"0x{vk:02X}"


def qtKeyToVk(key: int) -> Optional[int]:
    if QtCore.Qt.Key_A <= key <= QtCore.Qt.Key_Z:
        return key
    if QtCore.Qt.Key_0 <= key <= QtCore.Qt.Key_9:
        return key
    if QtCore.Qt.Key_F1 <= key <= QtCore.Qt.Key_F35:
        return 0x70 + (key - QtCore.Qt.Key_F1)
    return None


class HotKeyButton(QtWidgets.QPushButton):
    """点一下进入“按键捕捉”状态，把按下的一次按键转成 Win32 虚拟键码"""

    captured = QtCore.Signal(int)

    def __init__(self, vk: int, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(vkName(vk), parent)
        self._capturing = False
        self._idleText = vkName(vk)
        self.setMinimumWidth(110)
        self.clicked.connect(self._startCapture)

    def setIdleVk(self, vk: int) -> None:
        self._idleText = vkName(vk)
        if not self._capturing:
            self.setText(self._idleText)

    def _startCapture(self) -> None:
        self._capturing = True
        self.setText("请按键…")
        self.setFocus()

    def keyPressEvent(self, event) -> None:
        if not self._capturing:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == QtCore.Qt.Key_Escape:
            self._capturing = False
            self.setText(self._idleText)
            return
        vk = qtKeyToVk(key)
        self._capturing = False
        if vk is None:
            self.setText("不支持，重试")
            QtCore.QTimer.singleShot(1200, lambda: self.setText(self._idleText))
            return
        self._idleText = vkName(vk)
        self.setText(self._idleText)
        self.captured.emit(vk)


class SettingsDialog(QtWidgets.QDialog):
    curveChanged = QtCore.Signal()
    automationChanged = QtCore.Signal()
    hotKeyChanged = QtCore.Signal(int)

    def __init__(self, curves: Dict[str, FanCurve], settings: QtCore.QSettings,
                 history: HistoryBuffer,
                 tempsProvider: Callable[[], Tuple[Optional[int], Optional[int]]],
                 sensorScanner: Callable[[], List[Tuple[int, int]]],
                 parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._curves = curves
        self._settings = settings
        self._history = history
        self._tempsProvider = tempsProvider
        self._sensorScanner = sensorScanner
        self._sensorRows: Optional[List[Tuple[int, int]]] = None
        self._scanned = False
        # 初始化控件时 setChecked/setPlainText 会触发保存回调，用它挡住
        self._loading = True

        self.setWindowTitle("设置")
        self.setMinimumSize(740, 600)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        self._tabs = QtWidgets.QTabWidget()
        self._tabs.addTab(self._buildCurveTab(), "风扇曲线")
        self._tabs.addTab(self._buildAutomationTab(), "自动化")
        self._tabs.addTab(self._buildDiagnosticsTab(), "诊断")

        closeBtn = QtWidgets.QPushButton("关闭")
        closeBtn.clicked.connect(self.close)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(closeBtn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._tabs)
        layout.addLayout(bottom)

        self._loading = False

        self._liveTimer = QtCore.QTimer(self)
        self._liveTimer.setInterval(1000)
        self._liveTimer.timeout.connect(self._refreshLive)
        self._liveTimer.start()
        self._refreshLive()
        # 让窗口先画出来，再跑阻塞式的传感器扫描（只跑一次，之后复用结果）
        QtCore.QTimer.singleShot(150, self._runSensorScan)

    # ================= 风扇曲线 =================

    def _activeRole(self) -> str:
        return self._roleCombo.currentData() or 'gpu'

    def _activeCurve(self) -> FanCurve:
        return self._curves[self._activeRole()]

    def _otherCurve(self) -> FanCurve:
        return self._curves['cpu' if self._activeRole() == 'gpu' else 'gpu']

    def _buildCurveTab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        roleRow = QtWidgets.QHBoxLayout()
        roleRow.addWidget(QtWidgets.QLabel("正在编辑："))
        self._roleCombo = QtWidgets.QComboBox()
        self._roleCombo.addItem("GPU 曲线", "gpu")
        self._roleCombo.addItem("CPU 曲线", "cpu")
        self._roleCombo.currentIndexChanged.connect(self._onRoleChanged)
        roleRow.addWidget(self._roleCombo)
        roleRow.addStretch(1)
        layout.addLayout(roleRow)

        hint = QtWidgets.QLabel(
            "只在「静音模式」下生效。拖动竖线改温度阈值，拖动圆点改该档转速；"
            "绿色 / 黄色虚线是当前 GPU / CPU 实测温度。"
            "CPU 温度基线通常比 GPU 高，所以两条曲线分开设定，虚线是另一颗芯片的对照。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {Colors.GREY.value};")
        layout.addWidget(hint)

        self._editor = CurveEditorWidget(self._curves['gpu'], secondary=self._curves['cpu'])
        self._editor.curveChanged.connect(self._onCurveEdited)
        layout.addWidget(self._editor, 1)

        self._curveDesc = QtWidgets.QLabel()
        self._curveDesc.setWordWrap(True)
        self._curveDesc.setStyleSheet(f"color: {Colors.WHITE.value};")
        layout.addWidget(self._curveDesc)

        sliderRow = QtWidgets.QHBoxLayout()
        sliderRow.addWidget(QtWidgets.QLabel("更安静"))
        self._biasSlider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._biasSlider.setRange(0, BIAS_LIMIT * 2)
        self._biasSlider.setSingleStep(1)
        self._biasSlider.setPageStep(2)
        self._biasSlider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self._biasSlider.setTickInterval(4)
        self._biasSlider.valueChanged.connect(self._onBiasChanged)
        sliderRow.addWidget(self._biasSlider, 1)
        sliderRow.addWidget(QtWidgets.QLabel("更凉快"))
        layout.addLayout(sliderRow)

        self._biasLabel = QtWidgets.QLabel()
        self._biasLabel.setStyleSheet(f"color: {Colors.GREY.value};")
        layout.addWidget(self._biasLabel)

        resetBtn = QtWidgets.QPushButton("恢复默认曲线")
        resetBtn.setToolTip("只恢复当前正在编辑的这条曲线")
        resetBtn.clicked.connect(self._onResetCurve)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(resetBtn)
        layout.addLayout(row)

        self._syncCurveUi()
        return page

    def _onRoleChanged(self, *_) -> None:
        self._editor.setCurve(self._activeCurve())
        self._editor.setSecondaryCurve(self._otherCurve())
        self._syncCurveUi()

    def _syncCurveUi(self) -> None:
        curve = self._activeCurve()
        name = 'CPU' if curve.role == 'cpu' else 'GPU'
        self._curveDesc.setText(f"{name} 当前曲线：" + curve.describe())
        # 滑块 0..2*BIAS_LIMIT 反向映射：左端最安静（bias 为正，阈值上移）
        self._biasSlider.blockSignals(True)
        self._biasSlider.setValue(BIAS_LIMIT - curve.bias)
        self._biasSlider.blockSignals(False)
        if curve.bias == 0:
            self._biasLabel.setText(f"{name} 整体偏移：0°C")
        elif curve.bias > 0:
            self._biasLabel.setText(f"{name} 整体偏移：+{curve.bias}°C（更晚提速，更安静）")
        else:
            self._biasLabel.setText(f"{name} 整体偏移：{curve.bias}°C（更早提速，更凉快）")

    def _onBiasChanged(self, value: int) -> None:
        self._activeCurve().setBias(BIAS_LIMIT - value)
        self._syncCurveUi()
        self._editor.update()
        self.curveChanged.emit()

    def _onCurveEdited(self) -> None:
        self._syncCurveUi()
        self.curveChanged.emit()

    def _onResetCurve(self) -> None:
        self._activeCurve().reset()
        self._syncCurveUi()
        self._editor.update()
        self._editor.setSecondaryCurve(self._otherCurve())
        self.curveChanged.emit()

    def _refreshLive(self) -> None:
        gpu, cpu = self._tempsProvider()
        self._editor.setLiveTemps(gpu, cpu)

    # ================= 自动化 =================

    def _buildAutomationTab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        def section(text: str) -> QtWidgets.QLabel:
            lb = QtWidgets.QLabel(text)
            f = lb.font()
            f.setBold(True)
            lb.setFont(f)
            return lb

        def note(text: str) -> QtWidgets.QLabel:
            lb = QtWidgets.QLabel(text)
            lb.setWordWrap(True)
            lb.setStyleSheet(f"color: {Colors.GREY.value};")
            return lb

        layout.addWidget(section("电源联动"))
        self._powerLinkCB = QtWidgets.QCheckBox("根据供电状态自动切换模式")
        self._powerLinkCB.setChecked(self._getBool(SettingsKey.PowerLinkFlag, False))
        self._powerLinkCB.toggled.connect(self._saveAutomation)
        layout.addWidget(self._powerLinkCB)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("使用电池时切到"))
        self._batteryModeCombo = QtWidgets.QComboBox()
        self._batteryModeCombo.addItems(["静音模式", "平衡模式", "G 模式"])
        self._batteryModeCombo.setCurrentIndex(
            int(self._settings.value(SettingsKey.PowerLinkBatteryMode.value) or 0))
        self._batteryModeCombo.currentIndexChanged.connect(self._saveAutomation)
        row.addWidget(self._batteryModeCombo)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(note("插上电源时自动切回平衡模式。程序启动时如果正在用电池，也会立刻应用一次。"
                              "手动切模式后，自动化会等到下一次供电状态变化才再动手。"))

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet(f"color: {Colors.GREY.value};")
        layout.addWidget(line)

        layout.addWidget(section("游戏检测"))
        self._gameDetectCB = QtWidgets.QCheckBox("检测到全屏游戏时自动切到 G 模式")
        self._gameDetectCB.setChecked(self._getBool(SettingsKey.GameDetectFlag, False))
        self._gameDetectCB.toggled.connect(self._saveAutomation)
        layout.addWidget(self._gameDetectCB)
        layout.addWidget(note(
            "判定方式是「前台窗口铺满整个屏幕，且进程不在下面的排除表里」——"
            "Dell 的接口不告诉我们当前跑的是什么程序，只能这样猜。"
            "浏览器按 F11 全屏、视频播放器全屏都会被误判，把它们的进程名加进排除表即可。"))
        self._exclusionsEdit = QtWidgets.QPlainTextEdit()
        self._exclusionsEdit.setPlaceholderText("每行一个进程名，例如：chrome")
        self._exclusionsEdit.setMaximumHeight(118)
        savedExcl = self._settings.value(SettingsKey.GameExclusions.value)
        self._exclusionsEdit.setPlainText(str(savedExcl) if savedExcl else "\n".join(DEFAULT_EXCLUSIONS))
        self._exclusionsEdit.textChanged.connect(self._saveAutomation)
        layout.addWidget(self._exclusionsEdit)

        line2 = QtWidgets.QFrame()
        line2.setFrameShape(QtWidgets.QFrame.HLine)
        line2.setStyleSheet(f"color: {Colors.GREY.value};")
        layout.addWidget(line2)

        layout.addWidget(section("全局热键"))
        hkRow = QtWidgets.QHBoxLayout()
        hkRow.addWidget(QtWidgets.QLabel("G 模式切换键："))
        curVk = int(self._settings.value(SettingsKey.HotKeyVk.value) or 0x80)
        self._hotKeyBtn = HotKeyButton(curVk)
        self._hotKeyBtn.captured.connect(self._onHotKeyCaptured)
        hkRow.addWidget(self._hotKeyBtn)
        hkRow.addStretch(1)
        layout.addLayout(hkRow)
        layout.addWidget(note("点一下按钮，然后按下想用的键。只支持单个按键（字母、数字、F1~F24），不支持组合键。改完立即生效。"))

        layout.addStretch(1)
        return page

    def _getBool(self, key: SettingsKey, default: bool) -> bool:
        v = self._settings.value(key.value)
        if v is None:
            return default
        return str(v).lower() == "true"

    def _saveAutomation(self) -> None:
        if self._loading:
            return
        self._settings.setValue(SettingsKey.PowerLinkFlag.value, self._powerLinkCB.isChecked())
        self._settings.setValue(SettingsKey.PowerLinkBatteryMode.value, self._batteryModeCombo.currentIndex())
        self._settings.setValue(SettingsKey.GameDetectFlag.value, self._gameDetectCB.isChecked())
        self._settings.setValue(SettingsKey.GameExclusions.value, self._exclusionsEdit.toPlainText())
        self._settings.sync()
        self.automationChanged.emit()

    def _onHotKeyCaptured(self, vk: int) -> None:
        self._settings.setValue(SettingsKey.HotKeyVk.value, vk)
        self._settings.sync()
        self.hotKeyChanged.emit(vk)

    def exclusions(self) -> List[str]:
        raw = self._exclusionsEdit.toPlainText().replace(",", "\n")
        return [x.strip().lower() for x in raw.splitlines() if x.strip()]

    # ================= 诊断 =================

    def _buildDiagnosticsTab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        layout.addWidget(QtWidgets.QLabel("温度与转速历史"))
        self._chart = HistoryChartWidget(self._history)
        layout.addWidget(self._chart, 1)

        btnRow = QtWidgets.QHBoxLayout()
        exportBtn = QtWidgets.QPushButton("导出 CSV")
        exportBtn.clicked.connect(self._onExportCsv)
        clearBtn = QtWidgets.QPushButton("清空历史")
        clearBtn.clicked.connect(self._onClearHistory)
        logBtn = QtWidgets.QPushButton("打开日志目录")
        logBtn.setToolTip(f"排查问题用：{logPath()}")
        logBtn.clicked.connect(self._onOpenLogDir)
        btnRow.addStretch(1)
        btnRow.addWidget(logBtn)
        btnRow.addWidget(exportBtn)
        btnRow.addWidget(clearBtn)
        layout.addLayout(btnRow)

        self._csvHint = QtWidgets.QLabel("")
        self._csvHint.setStyleSheet(f"color: {Colors.GREY.value};")
        self._csvHint.setWordWrap(True)
        layout.addWidget(self._csvHint)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet(f"color: {Colors.GREY.value};")
        layout.addWidget(line)

        headRow = QtWidgets.QHBoxLayout()
        headRow.addWidget(QtWidgets.QLabel("传感器"))
        self._scanBtn = QtWidgets.QPushButton("重新扫描")
        self._scanBtn.clicked.connect(lambda: self._runSensorScan(force=True))
        headRow.addStretch(1)
        headRow.addWidget(self._scanBtn)
        layout.addLayout(headRow)

        self._sensorTable = QtWidgets.QTableWidget(0, 3)
        self._sensorTable.setHorizontalHeaderLabels(["编号", "温度", "说明"])
        self._sensorTable.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self._sensorTable.verticalHeader().setVisible(False)
        self._sensorTable.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._sensorTable.setMaximumHeight(150)
        layout.addWidget(self._sensorTable)

        layout.addWidget(QtWidgets.QLabel(
            "说明：编号是 Dell WMI 的传感器 ID。标着「风扇关联」的是界面主视图用的那两路；"
            "其余是驱动里有读数、但没有绑定到风扇的传感器。"))

        self._chartTimer = QtCore.QTimer(self)
        self._chartTimer.setInterval(1000)
        self._chartTimer.timeout.connect(self._chart.refresh)
        self._chartTimer.start()
        return page

    def _runSensorScan(self, force: bool = False) -> None:
        """同步扫描。WMI 的 COM 对象是在主线程创建的，放到别的线程调用会直接失败，
        所以这里不走线程——13 个传感器大约 1 秒，比拿不到数据好。

        结果会缓存，重新打开设置窗口时直接复用，不再让用户白等一秒。
        """
        if self._scanned and not force and self._sensorRows is not None:
            self._populateSensorTable()
            return
        self._scanBtn.setEnabled(False)
        self._scanBtn.setText("扫描中…")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self._sensorRows = self._sensorScanner()
            self._scanned = True
        except Exception as ex:
            self._sensorRows = []
            print(f'传感器扫描失败: {ex}')
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self._scanBtn.setEnabled(True)
            self._scanBtn.setText("重新扫描")
        self._populateSensorTable()

    def _populateSensorTable(self) -> None:
        rows = self._sensorRows or []
        known = {1: "CPU（风扇关联）", 6: "GPU（风扇关联）"}
        self._sensorTable.setRowCount(len(rows))
        for r, (sid, temp) in enumerate(sorted(rows)):
            self._sensorTable.setItem(r, 0, QtWidgets.QTableWidgetItem(f"#{sid}"))
            self._sensorTable.setItem(r, 1, QtWidgets.QTableWidgetItem(f"{temp} °C"))
            self._sensorTable.setItem(r, 2, QtWidgets.QTableWidgetItem(known.get(sid, "未关联风扇")))

    def _onExportCsv(self) -> None:
        defaultName = QtCore.QDateTime.currentDateTime().toString("yyyyMMdd-HHmmss")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出历史记录", f"tcc-history-{defaultName}.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            n = self._history.exportCsv(path)
            self._csvHint.setText(f"已导出 {n} 条记录")
        except Exception as ex:
            self._csvHint.setText(f"导出失败：{ex}")

    def _onClearHistory(self) -> None:
        self._history.clear()
        self._chart.refresh()
        self._csvHint.setText("历史已清空")

    def _onOpenLogDir(self) -> None:
        try:
            os.makedirs(logDir(), exist_ok=True)
            os.startfile(logDir())  # type: ignore[attr-defined]
        except Exception as ex:
            self._csvHint.setText(f"打不开日志目录：{ex}")

    # ================= 生命周期 =================

    def selectTab(self, index: int) -> None:
        self._tabs.setCurrentIndex(index)

    def reloadFromSettings(self) -> None:
        """外部（如“恢复默认设置”）改了配置后，把界面重新同步一遍"""
        self._loading = True
        try:
            self._powerLinkCB.setChecked(self._getBool(SettingsKey.PowerLinkFlag, False))
            self._batteryModeCombo.setCurrentIndex(
                int(self._settings.value(SettingsKey.PowerLinkBatteryMode.value) or 0))
            self._gameDetectCB.setChecked(self._getBool(SettingsKey.GameDetectFlag, False))
            savedExcl = self._settings.value(SettingsKey.GameExclusions.value)
            self._exclusionsEdit.setPlainText(
                str(savedExcl) if savedExcl else "\n".join(DEFAULT_EXCLUSIONS))
            curVk = int(self._settings.value(SettingsKey.HotKeyVk.value) or 0x80)
            self._hotKeyBtn.setIdleVk(curVk)
        finally:
            self._loading = False
        self._syncCurveUi()
        self._editor.update()
        self._editor.setSecondaryCurve(self._otherCurve())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 每次重新显示时补一次同步，避免主界面改了状态而这里还停在旧值
        self._syncCurveUi()
        if not self._liveTimer.isActive():
            self._liveTimer.start()
        if not self._chartTimer.isActive():
            self._chartTimer.start()

    def closeEvent(self, event) -> None:
        self._liveTimer.stop()
        self._chartTimer.stop()
        super().closeEvent(event)
