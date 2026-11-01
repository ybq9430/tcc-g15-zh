import sys, os, time, datetime
from enum import Enum
from typing import Callable, Dict, Literal, Optional, Tuple, List
from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets
from windows_toasts import WindowsToaster, Toast, ToastDuration, ToastDisplayImage
from Backend.AWCCThermal import AWCCThermal, NoAWCCWMIClass, CannotInstAWCCWMI
from GUI.QRadioButtonSet import QRadioButtonSet
from GUI.AppColors import Colors
from GUI.ThermalUnitWidget import ThermalUnitWidget
from GUI.QGaugeTrayIcon import QGaugeTrayIcon
from GUI import HotKey
from GUI.FanCurve import FanCurve
from GUI.HistoryChartWidget import HistoryBuffer
from GUI.SettingsDialog import SettingsDialog, TAB_DIAGNOSTICS
from GUI.SettingsKeys import SettingsKey
from Backend import PowerMonitor, GameDetector
from Backend.AppLog import logPath
from Backend.DetectHardware import DetectHardware

GUI_ICON = 'icons/gaugeIcon.png'
SINGLE_INSTANCE_NAME = 'TCC-G15-ZH-SingleInstance'

def resourcePath(relativePath: str = '.'):
    return os.path.join(sys._MEIPASS if hasattr(sys, '_MEIPASS') else os.path.abspath('.'), relativePath)

def autorunTask(action: Literal['add', 'remove']) -> int:
    taskXmlFilePath = resourcePath("tcc_g15_task.xml")

    addCmd = f'schtasks /create /xml "{taskXmlFilePath}" /tn "TCC_G15"'
    removeCmd = 'schtasks /delete /tn "TCC_G15" /f'

    if action == 'add':
        # Patch program path in the xml file
        exeFile = os.path.abspath(sys.argv[0])
        if exeFile.endswith('.exe'):
            with open(taskXmlFilePath, 'r') as f:
                xml = f.read()
            xml = xml.replace('<!--EXE_FILE_PATH-->', exeFile)
            with open(taskXmlFilePath, 'w') as f:
                f.write(xml)
        else:
            return -100
        
        os.system(removeCmd)
        return os.system(addCmd)
    else:
        return os.system(removeCmd)

def alert(title: str, message: str, type: QtWidgets.QMessageBox.Icon = QtWidgets.QMessageBox.Icon.Information, *, message2: Optional[str] = None) -> None:
    msg = QtWidgets.QMessageBox(type, title, message)
    msg.setWindowIcon(QtGui.QIcon(resourcePath(GUI_ICON)))
    if message2: msg.setInformativeText(message2)
    msg.setStandardButtons(QtWidgets.QMessageBox.Ok)
    msg.exec()

def confirm(title: str, message: str, options: Optional[Tuple[str, str]] = None, dontAskAgain: bool = False) -> Tuple[bool, Optional[bool]]:
    msg = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Question, title, message, QtWidgets.QMessageBox.Yes |  QtWidgets.QMessageBox.No)
    msg.setWindowIcon(QtGui.QIcon(resourcePath(GUI_ICON)))

    if options is not None:
        msg.button(QtWidgets.QMessageBox.Yes).setText(options[0])
        msg.button(QtWidgets.QMessageBox.No).setText(options[1])

    cbDontAskAgain = None
    if dontAskAgain:
        cbDontAskAgain = QtWidgets.QCheckBox('不再询问', msg)
        msg.setCheckBox(cbDontAskAgain)

    return (msg.exec_() == QtWidgets.QMessageBox.Yes, cbDontAskAgain is not None and cbDontAskAgain.isChecked() or None)


class QPeriodic:
    def __init__(self, parent: QtCore.QObject, periodMs: int, callback: Callable) -> None:
        self._tmr = QtCore.QTimer(parent)
        self._tmr.setInterval(periodMs)
        self._tmr.setSingleShot(False)
        self._tmr.timeout.connect(callback)
    def start(self):
        self._tmr.start()
    def stop(self):
        self._tmr.stop()

class ThermalMode(Enum):
    Balanced = 'Balanced'
    Silent = 'Silent'      # 本版本新增：软件模拟的静音模式（后端复用 Custom 接管风扇）
    G_Mode = 'G_Mode'
    Custom = 'Custom'

# 后端不认识 Silent，需要映射到它支持的 WMI 模式值
BACKEND_MODE_PASSTHROUGH = {
    ThermalMode.Balanced.value: 'Balanced',
    ThermalMode.G_Mode.value: 'G_Mode',
    ThermalMode.Custom.value: 'Custom',
    ThermalMode.Silent.value: 'Custom',   # 静音 = 切到 Custom 让 EC 交出风扇控制权
}

# 中文显示名（枚举值本身不能改，否则会破坏与后端/配置文件的对应关系）
MODE_NAMES_ZH = {
    ThermalMode.Balanced.value: '平衡模式',
    ThermalMode.Silent.value: '静音模式',
    ThermalMode.G_Mode.value: 'G 模式',
    ThermalMode.Custom.value: '自定义',
}

def modeDisplayName(value: str) -> str:
    return MODE_NAMES_ZH.get(value, value.replace('_', ' '))

    
def errorExit(message: str, message2: Optional[str] = None) -> None:
    if not QtWidgets.QApplication.instance():
         QtWidgets.QApplication([])
    alert("出错了", message, QtWidgets.QMessageBox.Icon.Critical, message2 = message2)
    sys.exit(1)

class TCC_GUI(QtWidgets.QWidget):
    TEMP_UPD_PERIOD_MS = 1000
    AUTOMATION_PERIOD_MS = 5000
    FAILSAFE_CPU_TEMP = 95
    FAILSAFE_GPU_TEMP = 85
    FAILSAFE_TRIGGER_DELAY_SEC = 8
    FAILSAFE_RESET_AFTER_TEMP_IS_OK_FOR_SEC = 60
    GAME_TRIGGER_DELAY_SEC = 3      # 全屏持续这么久才认定是游戏，避免切窗口误触发
    GAME_EXIT_DELAY_SEC = 8         # 退出全屏这么久后才恢复原模式
    HISTORY_CAPACITY = 1800         # 历史缓冲 1800 个采样点（约 30 分钟）
    # 静音模式防抖参数（曲线本身可编辑，见 GUI/FanCurve.py）
    SILENT_BAND_MARGIN = 5          # 降档需低于当前档位下界这么多度（档位滞环）
    SILENT_DOWN_COOLDOWN_SEC = 45   # 上次升档后至少等这么久才允许降档，避免风扇反复启停
    SILENT_REASSERT_SEC = 25        # 即使档位没变也定期重发一次，防止 EC 自行接管回自动曲线
    # 转速渐变：一次性从 30% 跳到 65% 会有明显的“呼”声，改成每秒走几步
    SILENT_RAMP_UP_PCT_PER_SEC = 30    # 升温提转速要快：0→100% 约 3.3 秒
    SILENT_RAMP_DOWN_PCT_PER_SEC = 6   # 降速要慢，避免转速骤降后又被拉起来
    APP_NAME = "Thermal Control Center for Dell G15"
    APP_NAME_ZH = "Dell G15 散热控制中心"
    APP_VERSION = "1.6.6-zh"
    APP_DESCRIPTION = "本软件是 Alienware Control Center（AWCC）的开源替代品 "
    APP_URL = "github.com/AlexIII/tcc-g15"          # 同时是 QSettings 的组织名，不能改
    APP_REPO_URL = "github.com/ybq9430/tcc-g15-zh"  # 汉化与增强版仓库
    APP_LOCALIZED_BY = "中文本地化与增强：ybq9430"

    # Green to Yellow and Yellow to Red thresholds
    GPU_COLOR_LIMITS = (72, 85)
    CPU_COLOR_LIMITS = (85, 95)

    # private
    _failsafeTempIsHighTs = 0                           # Last time when the temp was registered to be high
    _failsafeTempIsHighStartTs: Optional[int] = None    # Time when the temp first registered to be high (without going lower than the threshold)
    _failsafeTrippedPrevModeStr: Optional[str] = None   # Mode (Custom, Balanced) before fail-safe tripped, as a string
    _failsafeOn = True
    _prevSavedSettingsValues: list = []
    _silentLastBand: dict = {}                          # 静音模式：上次生效的档位索引（用于滞环）
    _silentLastChangeTs: dict = {}                      # 静音模式：上次改速的时间戳（用于降档冷静期）
    _silentLastWriteTs: dict = {}                       # 静音模式：上次写 EC 的时间戳（用于定期重申）
    _silentCurrentSpeed: dict = {}                      # 静音模式：当前实际转速（浮点，用于渐变）
    _silentLastWrittenSpeed: dict = {}                  # 静音模式：上次真正写进 EC 的整数转速
    _lastCustomSpeed: Optional[Tuple[int, int]] = None  # 上次用户手动设定的 (CPU, GPU) 转速
    _appliedMode: str = ThermalMode.Balanced.value      # 后端当前真正生效的模式
    _suppressModeChange = False                         # True 时不响应模式切换（用于回滚）

    _lastAcState: Optional[bool] = None                 # 上次看到的供电状态，用于检测交流/电池切换
    _gameActive = False                                 # 当前是否判定在跑全屏游戏
    _gamePrevMode: Optional[str] = None                 # 进游戏前的模式，退出时恢复
    _gameCandidateSince = 0.0                           # 首次检测到全屏的时间（防抖）
    _gameIdleSince = 0.0                                # 首次检测到全屏结束的时间（防抖）
    _autoSwitchGuard = False                            # True 表示这次模式变更是自动化触发的，不算用户手选

    _gModeKeySignal = QtCore.Signal()
    _gModeKeyPrevModeStr: Optional[str] = None

    _toaster = WindowsToaster(APP_NAME)

    _modeSwitch: QRadioButtonSet
    _settingsDialog: Optional["SettingsDialog"] = None

    def __init__(self, awcc: AWCCThermal):
        super().__init__()
        self._awcc = awcc

        self.settings = QtCore.QSettings(self.APP_URL, "AWCC")
        print(f'Settings location: {self.settings.fileName()}')

        # 静音曲线：GPU / CPU 各一条，对话框和主界面共享同一份对象。
        # 旧版本只有一条共用曲线（app/silent/curve_json），它是按 GPU 温度调的，
        # 直接套给 CPU 会让 CPU 长期停在 0% 档，所以只迁移给 GPU，CPU 走默认值。
        legacy = self.settings.value(SettingsKey.SilentCurveJsonLegacy.value) or ''
        rawGpu = self.settings.value(SettingsKey.SilentCurveGpuJson.value) or legacy
        rawCpu = self.settings.value(SettingsKey.SilentCurveCpuJson.value) or ''
        self._curveGpu = FanCurve.fromJson(rawGpu, 'gpu')
        self._curveCpu = FanCurve.fromJson(rawCpu, 'cpu')
        self._curves: Dict[str, FanCurve] = {'gpu': self._curveGpu, 'cpu': self._curveCpu}
        self._history = HistoryBuffer(self.HISTORY_CAPACITY)

        # Set main window props
        self.setFixedSize(600, 0)
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.WindowMinimizeButtonHint | QtCore.Qt.WindowCloseButtonHint)
        self.setWindowIcon(QtGui.QIcon(resourcePath(GUI_ICON)))
        self.mouseReleaseEvent = lambda evt: (
            evt.button() == QtCore.Qt.RightButton and
            alert("关于", f"{self.APP_NAME_ZH} v{self.APP_VERSION}",
                  message2=(f"{self.APP_DESCRIPTION}\n"
                            f"上游项目：{self.APP_URL}\n"
                            f"汉化与增强：{self.APP_REPO_URL}\n"
                            f"{self.APP_LOCALIZED_BY}\n\n"
                            f"运行日志：{logPath()}"))
        )

        # Set up tray icon
        self.trayIcon = QGaugeTrayIcon((self.GPU_COLOR_LIMITS, self.CPU_COLOR_LIMITS))
        menu = QtWidgets.QMenu()
        # Mode switch
        menu.addSection("散热模式")
        self._trayMenuModeSwitch = {} # Dict[ThermalMode, QtWidgets.QAction]
        for m in ThermalMode:
            modeAction = menu.addAction("-")
            modeAction.triggered.connect(lambda _, m_value=m.value: self._modeSwitch.setChecked(m_value))
            self._trayMenuModeSwitch[m.value] = modeAction
        # Settings
        menu.addSection("设置")
        showAction = menu.addAction("显示主界面")
        showAction.triggered.connect(self.showNormal)
        settingsAction = menu.addAction("设置…")
        settingsAction.triggered.connect(lambda: self.openSettings())
        historyAction = menu.addAction("查看温度历史…")
        historyAction.triggered.connect(lambda: self.openHistory())
        addToAutorunAction = menu.addAction("启用开机自启")
        def autorunTaskRun(action: Literal['add', 'remove']) -> None:
            err = autorunTask(action)
            if err != 0 and action == 'add':
                self.trayMessage("开机自启", f"创建计划任务失败，错误码 {err}", critical=True)
            else:
                self.trayMessage("开机自启", "已启用" if action == 'add' else "已关闭")
            # When in minimized state, a wired bug causes the app to close if we won't touch some of the `self.show*()` methods
            if self.isMinimized():
                self.showMinimized()
                self.hide()
        addToAutorunAction.triggered.connect(lambda: autorunTaskRun('add'))
        removeFromAutorunAction = menu.addAction("关闭开机自启")
        removeFromAutorunAction.triggered.connect(lambda: autorunTaskRun('remove'))
        restoreAction = menu.addAction("恢复默认设置")
        restoreAction.triggered.connect(self.clearAppSettings)
        exitAction = menu.addAction("退出")
        exitAction.triggered.connect(self.onExit)
        # Setup tray widget
        tray = QtWidgets.QSystemTrayIcon(self)
        tray.setIcon(self.trayIcon)
        tray.setContextMenu(menu)
        tray.show()
        self._tray = tray

        def onTrayIconActivated(trigger):
            if trigger == QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick:
                self.showNormal()
                self.activateWindow()
        self.connect(tray, QtCore.SIGNAL("activated(QSystemTrayIcon::ActivationReason)"), onTrayIconActivated)

        # Set up GUI
        self.setObjectName('QMainWindow')
        self.setWindowTitle(self.APP_NAME_ZH)

        self._thermalGPU = ThermalUnitWidget(self, tempMinMax= (0, 95), tempColorLimits= self.GPU_COLOR_LIMITS, fanMinMax= (0, 5500), sliderMaxAndTick= (100, 20))
        self._thermalGPU.setTitle('GPU')
        self._thermalCPU = ThermalUnitWidget(self, tempMinMax= (0, 110), tempColorLimits= self.CPU_COLOR_LIMITS, fanMinMax= (0, 5500), sliderMaxAndTick= (100, 20))
        self._thermalCPU.setTitle('CPU')

        # Detecting GPU/CPU model is a slow operation, run asynchronously
        class DetectCpuGpuModelsWorker(QtCore.QObject):
            finished = QtCore.Signal(str, str)
            def __init__(self, parent: QtCore.QObject, on_result: Callable[[Optional[str], Optional[str]], None]) -> None:
                super().__init__()
                self._t = QtCore.QThread(parent)
                self.moveToThread(self._t)
                self.finished.connect(self._t.quit)
                self.finished.connect(on_result)
                self._t.started.connect(self._task)
                self._t.start()
            def _task(self):
                print("DetectCpuGpuModelsWorker: started")
                d = DetectHardware()
                gpuModel = d.getHardwareName(d.GPUFanIdx)
                cpuModel = d.getHardwareName(d.CPUFanIdx)
                print(f"DetectCpuGpuModelsWorker: finished: {gpuModel}, {cpuModel}")
                self.finished.emit(gpuModel, cpuModel)
            def start(self):
                self._t.start()
        detect = DetectCpuGpuModelsWorker(self, self.updateGaugeTitles)
        detect.start()

        lTherm = QtWidgets.QHBoxLayout()
        lTherm.addWidget(self._thermalGPU)
        lTherm.addWidget(self._thermalCPU)

        self._modeSwitch = QRadioButtonSet(None, None, list(map(lambda m: (modeDisplayName(m.value), m.value), ThermalMode)))
        self._refreshSilentTooltip()
        self._modeSwitch.setToolTipFor(
            ThermalMode.Custom.value,
            "手动拖动下方滑块，直接指定风扇转速。\n有效范围 0~100，0% 约为硬件最低转速（约 3000 RPM），并非停转。"
        )

        # Fail-safe indicator
        failsafeIndicator = QtWidgets.QLabel()
        def updFailsafeIndicator() -> None:
            color = Colors.GREEN.value if self._failsafeOn else Colors.DARK_GREY.value
            msg = "正常"
            if self._failsafeTempIsHighTs > 0: # Fail-safe have tripped at some point in the past
                color = Colors.YELLOW.value
                timeStr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._failsafeTempIsHighTs))
                msg = f"上次高温：{timeStr}"
                if self._failsafeTrippedPrevModeStr is not None: # Fail-safe is in tripped state now
                    color = Colors.RED.value

            failsafeIndicator.setStyleSheet(f"QLabel {{ min-height: 14px; min-width: 14px; max-height: 14px; max-width: 14px; border: 1px solid {Colors.GREY.value}; border-radius: 7px; background: {color}; }}")
            failsafeIndicator.setToolTip(msg)
        updFailsafeIndicator()

        # Fail-safe temp limits
        self._limitTempGPU = QtWidgets.QComboBox()
        self._limitTempGPU.addItems(list(map(lambda v: str(v), range(50, 91))))
        self._limitTempGPU.setToolTip("GPU 温度阈值")
        self._limitTempCPU = QtWidgets.QComboBox()
        self._limitTempCPU.addItems(list(map(lambda v: str(v), range(50, 101))))
        self._limitTempCPU.setToolTip("CPU 温度阈值")
        def onLimitGPUChange():
            val = self._limitTempGPU.currentText()
            if val.isdigit(): self.FAILSAFE_GPU_TEMP = int(val)
        self._limitTempGPU.currentIndexChanged.connect(onLimitGPUChange)
        def onLimitCPUChange():
            val = self._limitTempCPU.currentText()
            if val.isdigit(): self.FAILSAFE_CPU_TEMP = int(val)
        self._limitTempCPU.currentIndexChanged.connect(onLimitCPUChange)

        # Fail-safe checkbox
        self._failsafeCB = QtWidgets.QCheckBox("温度保护")
        self._failsafeCB.setToolTip(f"当 GPU 温度达到 {self.FAILSAFE_GPU_TEMP}°C 或 CPU 达到 {self.FAILSAFE_CPU_TEMP}°C 时，自动切换到 G 模式（风扇满转）")
        def onFailsafeCB():
            self._failsafeOn = self._failsafeCB.isChecked()
            self._failsafeTempIsHighTs = 0
            self._failsafeTrippedPrevModeStr = None
            self._failsafeTempIsHighStartTs = None
            updFailsafeIndicator()
        self._failsafeCB.toggled.connect(onFailsafeCB)
        self._failsafeCB.setChecked(self._failsafeOn)

        failsafeBox = QtWidgets.QHBoxLayout()
        failsafeBox.addWidget(self._failsafeCB)
        failsafeBox.addWidget(self._limitTempGPU)
        failsafeBox.addWidget(self._limitTempCPU)
        failsafeBox.addWidget(failsafeIndicator)

        modeBox = QtWidgets.QHBoxLayout()
        modeBox.addWidget(self._modeSwitch, alignment= QtCore.Qt.AlignLeft)
        modeBox.addWidget(QtWidgets.QWidget(), alignment= QtCore.Qt.AlignRight) # Insert dummy Widget in order to move the following 'failsafeBox' to the right side
        modeBox.addLayout(failsafeBox)

        # 设置入口单独放一行，避免和模式选项、温度保护挤在一行撑破 600px 宽度
        settingsRow = QtWidgets.QHBoxLayout()
        self._settingsBtn = QtWidgets.QPushButton("设置…")
        self._settingsBtn.setToolTip("编辑静音风扇曲线、配置电源联动与游戏检测、查看温度历史")
        self._settingsBtn.clicked.connect(lambda: self.openSettings())
        settingsRow.addStretch(1)
        settingsRow.addWidget(self._settingsBtn)

        mainLayout = QtWidgets.QVBoxLayout(self)
        mainLayout.addLayout(lTherm)
        mainLayout.addLayout(modeBox)
        mainLayout.addLayout(settingsRow)
        mainLayout.setAlignment(QtCore.Qt.AlignTop)
        mainLayout.setContentsMargins(10, 0, 10, 0)

        # Glue GUI to backend
        self.gModeHotKey = None
        self._updateGaugesTask = None

        def setFanSpeed(fan: Literal['GPU', 'CPU'], speed: int) -> None:
            res = self._awcc.setFanSpeed(self._awcc.GPUFanIdx if fan == 'GPU' else self._awcc.CPUFanIdx, speed)
            print(f'Set {fan} fan speed to {speed}: ' + ('ok' if res else 'fail'))

        def updateFanSpeed():
            if self._modeSwitch.getChecked() != ThermalMode.Custom.value:
                return
            setFanSpeed('GPU', self._thermalGPU.getSpeedSlider())
            setFanSpeed('CPU', self._thermalCPU.getSpeedSlider())
        self._thermalGPU.speedSliderChanged(updateFanSpeed)
        self._thermalCPU.speedSliderChanged(updateFanSpeed)

        def refreshModeControls(val: str) -> None:
            """按模式刷新滑块可用状态和托盘勾选"""
            self._thermalGPU.setSpeedDisabled(val != ThermalMode.Custom.value)
            self._thermalCPU.setSpeedDisabled(val != ThermalMode.Custom.value)
            for m in ThermalMode:
                self._trayMenuModeSwitch[m.value].setText(
                    f"{'•' if m.value == val else ' '} {modeDisplayName(m.value)}")

        def onModeChange(val: str):
            if self._suppressModeChange:
                return
            # 静音模式后端没有对应的 WMI 值，复用 Custom 让 EC 交出风扇控制权，再由软件曲线控速
            backendVal = BACKEND_MODE_PASSTHROUGH.get(val, val)
            res = self._awcc.setMode(self._awcc.Mode[backendVal])
            if not res:
                # 睡眠唤醒后第一次调用 WMI 失败很常见，重试一次再判死刑
                print(f'Set mode {val} (backend {backendVal}) failed, retrying once...')
                time.sleep(0.1)
                res = self._awcc.setMode(self._awcc.Mode[backendVal])
            if not res:
                # 一次瞬时失败不该把整个程序杀掉，回滚 UI 状态并提示
                prev = self._appliedMode
                print(f'Set mode {val} failed twice, keeping {prev}')
                self._suppressModeChange = True
                try:
                    self._modeSwitch.setChecked(prev)
                finally:
                    self._suppressModeChange = False
                refreshModeControls(prev)
                self.trayMessage(
                    "切换失败",
                    f"无法切换到「{modeDisplayName(val)}」，已保持「{modeDisplayName(prev)}」。"
                    "若反复出现，请确认程序以管理员身份运行。",
                    critical=True)
                return

            self._appliedMode = val
            print(f'Set mode {val} (backend {backendVal}): ok')
            refreshModeControls(val)
            self._resetSilentState()
            updateFanSpeed()
            if val != ThermalMode.G_Mode.value:
                self._failsafeTrippedPrevModeStr = None # In case the mode was switched manually
            updFailsafeIndicator()
            # 用户手动改模式时，取消「退出游戏后恢复原模式」的打算，免得覆盖用户的选择
            if not self._autoSwitchGuard and self._gameActive and val != ThermalMode.G_Mode.value:
                self._gamePrevMode = None

        self._modeSwitch.setChecked(ThermalMode.Balanced.value)
        onModeChange(ThermalMode.Balanced.value)
        self._modeSwitch.setOnChange(onModeChange)

        def updateAppState():
            # Get temps and RPMs
            gpuTemp = self._awcc.getFanRelatedTemp(self._awcc.GPUFanIdx)
            gpuRPM = self._awcc.getFanRPM(self._awcc.GPUFanIdx)
            cpuTemp = self._awcc.getFanRelatedTemp(self._awcc.CPUFanIdx)
            cpuRPM = self._awcc.getFanRPM(self._awcc.CPUFanIdx)
            # Update UI gauges
            if gpuTemp is not None: self._thermalGPU.setTemp(gpuTemp)
            if gpuRPM is not None: self._thermalGPU.setFanRPM(gpuRPM)
            if cpuTemp is not None: self._thermalCPU.setTemp(cpuTemp)
            if cpuRPM is not None: self._thermalCPU.setFanRPM(cpuRPM)
            # print(gpuTemp, gpuRPM, cpuTemp, cpuRPM)

            # 静音模式：按温度曲线自动调节风扇转速
            if self._modeSwitch.getChecked() == ThermalMode.Silent.value:
                self._applySilentCurve(gpuTemp, cpuTemp)

            # Handle fail-safe
            tempIsHigh = (
                (gpuTemp is None) or (gpuTemp >= self.FAILSAFE_GPU_TEMP) or
                (cpuTemp is None) or (cpuTemp >= self.FAILSAFE_CPU_TEMP)
            )
            
            if tempIsHigh:
                self._failsafeTempIsHighTs = time.time()

            self._failsafeTempIsHighStartTs = (self._failsafeTempIsHighStartTs or time.time()) if tempIsHigh else None

            # Trip fail-safe
            if (self._failsafeOn and 
                self._modeSwitch.getChecked() != ThermalMode.G_Mode.value and
                tempIsHigh and
                time.time() - self._failsafeTempIsHighStartTs > self.FAILSAFE_TRIGGER_DELAY_SEC
            ):
                self._failsafeTrippedPrevModeStr = self._modeSwitch.getChecked()
                self._modeSwitch.setChecked(ThermalMode.G_Mode.value)
                self._toasterMessageCurrentMode(source='failsafe')
                print(f'Fail-safe tripped at GPU={gpuTemp} CPU={cpuTemp}')

            # Auto-reset failsafe
            if (self._failsafeTrippedPrevModeStr is not None and
                time.time() - self._failsafeTempIsHighTs > self.FAILSAFE_RESET_AFTER_TEMP_IS_OK_FOR_SEC
            ):
                self._modeSwitch.setChecked(self._failsafeTrippedPrevModeStr)
                self._toasterMessageCurrentMode(source='failsafe')
                self._failsafeTrippedPrevModeStr = None
                print('Fail-safe reset')

            # Update tray icon
            self.trayIcon = self.trayIcon.resizeForScreen() or self.trayIcon
            self.trayIcon.update((gpuTemp, cpuTemp), self._modeSwitch.getChecked() == ThermalMode.G_Mode.value)
            tray.setIcon(self.trayIcon)
            tray.setToolTip(f"GPU：    {gpuTemp} °C    {gpuRPM} RPM\nCPU：    {cpuTemp} °C    {cpuRPM} RPM\n模式：    {modeDisplayName(self._modeSwitch.getChecked())}")

            # 记录历史，供设置里的曲线/诊断页使用
            self._history.add(gpuTemp, cpuTemp, gpuRPM, cpuRPM)
            
            # Periodically save app settings
            self._saveAppSettings()

        self._loadAppSettings()

        self._updateGaugesTask = QPeriodic(self, self.TEMP_UPD_PERIOD_MS, updateAppState)
        updateAppState()
        self._updateGaugesTask.start()

        self._automationTask = QPeriodic(self, self.AUTOMATION_PERIOD_MS, self._checkAutomation)
        self._automationTask.start()

        self._gModeKeySignal.connect(self._onGModeHotKeyPressed)
        self._startHotKey()

    def updateGaugeTitles(self, gpuModel, cpuModel):
        if gpuModel: self._thermalGPU.setTitle(gpuModel)
        if cpuModel: self._thermalCPU.setTitle(cpuModel)

    # ---------- 静音曲线 ----------

    def _refreshSilentTooltip(self) -> None:
        self._modeSwitch.setToolTipFor(
            ThermalMode.Silent.value,
            "风扇按温度分档自动调速，日常负载下保持最低转速：\n"
            f"GPU：{self._curveGpu.describe()}\n"
            f"CPU：{self._curveCpu.describe()}\n\n"
            "转速平滑过渡（升快降慢），降温后需等 45 秒才降档，避免风扇反复启停。\n"
            "曲线可在「设置 → 风扇曲线」里拖动调整。\n\n"
            "注意：本机手动转速下限约 2600 RPM，与空载时的自动转速接近，\n"
            "所以静音模式的收益主要在中等负载时不急着提速，而不是让空载更安静。\n"
            "温度保护依然有效，超过阈值仍会切到 G 模式。"
        )

    def _resetSilentState(self) -> None:
        """档位/渐变状态全部作废，下一拍重新按当前曲线下发"""
        self._silentLastBand = {}
        self._silentLastChangeTs = {}
        self._silentLastWriteTs = {}
        self._silentCurrentSpeed = {}
        self._silentLastWrittenSpeed = {}

    def _onCurveChanged(self) -> None:
        self.settings.setValue(SettingsKey.SilentCurveGpuJson.value, self._curveGpu.toJson())
        self.settings.setValue(SettingsKey.SilentCurveCpuJson.value, self._curveCpu.toJson())
        # 旧键保留但清空，避免下次启动又把老的共用曲线当成 GPU 曲线读回来
        self.settings.remove(SettingsKey.SilentCurveJsonLegacy.value)
        self.settings.sync()
        self._refreshSilentTooltip()
        self._resetSilentState()
        print(f'静音曲线已更新 GPU: {self._curveGpu.describe()} | CPU: {self._curveCpu.describe()}')

    # ---------- 设置对话框 ----------

    def openSettings(self, tab: int = 0) -> None:
        # 复用同一个对话框实例：每次新建会累积 QDialog，而且每次都要重跑一遍
        # 阻塞式的传感器扫描（约 1 秒），第二三次打开照样卡。
        if getattr(self, '_settingsDialog', None) is None:
            dlg = SettingsDialog(
                self._curves, self.settings, self._history,
                tempsProvider=lambda: (self._thermalGPU.getTemp(), self._thermalCPU.getTemp()),
                sensorScanner=lambda: self._awcc.probeSensors(12),
                parent=self,
            )
            dlg.curveChanged.connect(self._onCurveChanged)
            dlg.hotKeyChanged.connect(self._onHotKeyChanged)
            dlg.automationChanged.connect(self._onAutomationChanged)
            self._settingsDialog = dlg
        dlg = self._settingsDialog
        dlg.selectTab(tab)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def openHistory(self) -> None:
        """托盘菜单直达温度历史"""
        self.openSettings(TAB_DIAGNOSTICS)

    def trayMessage(self, title: str, message: str, critical: bool = False) -> None:
        """托盘气泡，替代每次点菜单都弹出来的模态对话框"""
        icon = (QtWidgets.QSystemTrayIcon.MessageIcon.Critical if critical
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information)
        try:
            tray = getattr(self, '_tray', None)
            if tray is not None:
                tray.showMessage(title, message, icon, 4000)
            else:
                alert(title, message)
        except Exception as ex:
            print(f'托盘气泡发送失败: {ex}')

    # ---------- 热键 ----------

    def _startHotKey(self) -> None:
        vk = int(self.settings.value(SettingsKey.HotKeyVk.value) or HotKey.G_MODE_KEY)
        if getattr(self, 'gModeHotKey', None) is not None:
            self.gModeHotKey.stop()
            self.gModeHotKey.wait(2000)
        self.gModeHotKey = HotKey.HotKey(vk, self._gModeKeySignal)
        self.gModeHotKey.start()
        print(f'G 模式热键已注册: 0x{vk:02X}')

    def _onHotKeyChanged(self, vk: int) -> None:
        self.settings.setValue(SettingsKey.HotKeyVk.value, vk)
        self.settings.sync()
        self._startHotKey()

    # ---------- 自动化 ----------

    def _flagEnabled(self, key: SettingsKey) -> bool:
        v = self.settings.value(key.value)
        return str(v).lower() == 'true' if v is not None else False

    def _exclusions(self) -> List[str]:
        raw = self.settings.value(SettingsKey.GameExclusions.value)
        if not raw:
            return list(GameDetector.DEFAULT_EXCLUSIONS)
        text = str(raw).replace(',', '\n')
        return [x.strip().lower() for x in text.splitlines() if x.strip()]

    def _autoSetMode(self, mode: str, reason: str) -> None:
        print(f'自动化切换 -> {mode}（{reason}）')
        self._autoSwitchGuard = True
        try:
            self._modeSwitch.setChecked(mode)
        finally:
            self._autoSwitchGuard = False
        self._toasterMessageCurrentMode()

    def _checkAutomation(self) -> None:
        self._checkPowerLink()
        self._checkGame()

    def _checkPowerLink(self) -> None:
        if not self._flagEnabled(SettingsKey.PowerLinkFlag):
            self._lastAcState = None
            return
        onAc, _ = PowerMonitor.powerStatus()
        if onAc is None:
            return
        if self._lastAcState is None:
            self._lastAcState = onAc
            if onAc:
                return      # 插着电启动：不去动它，用户可能就想用 G 模式
            # 电池启动：立即应用一次设定，不必等到拔插一次才生效
        elif onAc == self._lastAcState:
            return
        self._lastAcState = onAc
        if onAc:
            target = ThermalMode.Balanced.value
        else:
            idx = int(self.settings.value(SettingsKey.PowerLinkBatteryMode.value) or 0)
            candidates = [ThermalMode.Silent.value, ThermalMode.Balanced.value, ThermalMode.G_Mode.value]
            target = candidates[max(0, min(len(candidates) - 1, idx))]
        if self._modeSwitch.getChecked() != target:
            self._autoSetMode(target, '接入电源' if onAc else '改用电池供电')

    def _checkGame(self) -> None:
        if not self._flagEnabled(SettingsKey.GameDetectFlag):
            self._gameActive = False
            self._gamePrevMode = None
            self._gameCandidateSince = 0.0
            self._gameIdleSince = 0.0
            return
        isGame, name = GameDetector.detectFullscreenGame(self._exclusions())
        now = time.time()
        if isGame:
            self._gameIdleSince = 0.0
            if self._gameActive:
                return
            if self._gameCandidateSince == 0.0:
                self._gameCandidateSince = now
            elif now - self._gameCandidateSince >= self.GAME_TRIGGER_DELAY_SEC:
                self._gameActive = True
                self._gameCandidateSince = 0.0
                self._gamePrevMode = self._modeSwitch.getChecked()
                if self._gamePrevMode != ThermalMode.G_Mode.value:
                    self._autoSetMode(ThermalMode.G_Mode.value, f'检测到全屏程序 {name}')
        else:
            self._gameCandidateSince = 0.0
            if not self._gameActive:
                return
            if self._gameIdleSince == 0.0:
                self._gameIdleSince = now
            elif now - self._gameIdleSince >= self.GAME_EXIT_DELAY_SEC:
                self._gameActive = False
                self._gameIdleSince = 0.0
                prev = self._gamePrevMode or ThermalMode.Balanced.value
                self._gamePrevMode = None
                if self._modeSwitch.getChecked() != prev:
                    self._autoSetMode(prev, '全屏程序已退出')

    def _onAutomationChanged(self) -> None:
        # 配置变了，重置供电/游戏的判定状态，避免拿旧状态做比较
        self._lastAcState = None
        self._gameActive = False
        self._gamePrevMode = None
        self._gameCandidateSince = 0.0
        self._gameIdleSince = 0.0

    def _applySilentCurve(self, gpuTemp: Optional[int], cpuTemp: Optional[int]) -> None:
        """静音模式核心：按温度查档，升档立即生效，降档必须跌出当前档位下界一段才生效。

        GPU 与 CPU 各用一条曲线（CPU 温度基线更高）。
        转速不是一步跳到目标值，而是按每秒固定幅度渐变——一次性从 30% 跳到 65%
        会有明显的“呼”一声，比温度本身更扰人。升快降慢。
        """
        now = time.time()
        targets = (
            ('GPU', self._thermalGPU, self._awcc.GPUFanIdx, gpuTemp, self._curveGpu),
            ('CPU', self._thermalCPU, self._awcc.CPUFanIdx, cpuTemp, self._curveCpu),
        )
        for name, unit, fanIdx, temp, curve in targets:
            idx = curve.bandIndex(temp)
            last = self._silentLastBand.get(name)
            if last is None or idx > last:
                newIdx = idx                                   # 首次或升温升档：立即执行
            elif idx < last:
                lowerEdge = curve.bandLowerEdge(last)
                cooledDown = (now - self._silentLastChangeTs.get(name, 0)) >= self.SILENT_DOWN_COOLDOWN_SEC
                if cooledDown and temp is not None and temp <= lowerEdge - self.SILENT_BAND_MARGIN:
                    newIdx = max(idx, last - 1)                # 降档每次最多退一档，避免转速骤降后又反弹
                else:
                    newIdx = last
            else:
                newIdx = last

            target = curve.speeds[newIdx]
            cur = self._silentCurrentSpeed.get(name)
            if cur is None:
                # 首帧从当前实际转速出发，避免切进静音模式时转速突然跳变
                cur = float(unit.getSpeedSlider())
                dt = 1.0
            else:
                dt = now - self._silentLastWriteTs.get(name, now)
                dt = max(0.2, min(5.0, dt))
            rate = (self.SILENT_RAMP_UP_PCT_PER_SEC if target > cur
                    else self.SILENT_RAMP_DOWN_PCT_PER_SEC)
            step = rate * dt
            if abs(target - cur) <= step:
                cur = float(target)
            elif target > cur:
                cur += step
            else:
                cur -= step

            speed = int(round(cur))
            lastWritten = self._silentLastWrittenSpeed.get(name)
            steady = (newIdx == last and speed == lastWritten)
            if steady and (now - self._silentLastWriteTs.get(name, 0)) < self.SILENT_REASSERT_SEC:
                # 档位和转速都没变：正常情况下不写 EC，但隔一会儿重申一次，防 EC 接管
                continue

            self._silentLastBand[name] = newIdx
            self._silentCurrentSpeed[name] = cur
            self._silentLastWrittenSpeed[name] = speed
            self._silentLastWriteTs[name] = now
            if newIdx != last:
                self._silentLastChangeTs[name] = now
            unit.setSpeedSliderSilently(speed)
            res = self._awcc.setFanSpeed(fanIdx, speed)
            print(f'静音: {name} {temp}C -> 档位 {newIdx}, 风扇 {speed}% ' + ('ok' if res else 'fail'))

    def closeEvent(self, event):
        minimizeOnClose = self.settings.value(SettingsKey.MinimizeOnCloseFlag.value)
        if minimizeOnClose is not None:
            minimizeOnClose = str(minimizeOnClose).lower() == 'true'
        
        if minimizeOnClose is None:
            # minimizeOnClose is not set, prompt user
            (toExit, dontAskAgain) = confirm("退出", "你要退出程序，还是最小化到系统托盘？", ("退出", "最小化"), True)
            minimizeOnClose = not toExit
            if dontAskAgain:
                self.settings.setValue(SettingsKey.MinimizeOnCloseFlag.value, minimizeOnClose)

        if minimizeOnClose:
            event.ignore()
            self.hide()
        else:
            self.onExit()
        return

    # onExit() connected to systray_Exit
    def onExit(self):
        print("exit")
        # Set mode to Balanced before exit
        prevMode = self._modeSwitch.getChecked()
        self._modeSwitch.setChecked(ThermalMode.Balanced.value)
        if prevMode != ThermalMode.Balanced.value:
            self._toasterMessageCurrentMode()
        self._destroy()
        sys.exit(0)

    def _errorExit(self, message: str, message2: Optional[str] = None) -> None:
        self._destroy()
        errorExit(message, message2)

    def _destroy(self):
        if self.gModeHotKey is not None:
            self.gModeHotKey.stop()
            self.gModeHotKey.wait()
        if self._updateGaugesTask is not None:
            self._updateGaugesTask.stop()
        if getattr(self, '_automationTask', None) is not None:
            self._automationTask.stop()
        if getattr(self, '_settingsDialog', None) is not None:
            try:
                self._settingsDialog.close()
            except Exception:
                pass
        print('Cleanup: done')

    def _onGModeHotKeyPressed(self):
        current = self._modeSwitch.getChecked()
        if current == ThermalMode.G_Mode.value:
            self._modeSwitch.setChecked(self._gModeKeyPrevModeStr or ThermalMode.Balanced.value)
        else:
            self._gModeKeyPrevModeStr = current
            self._modeSwitch.setChecked(ThermalMode.G_Mode.value)
        self._toasterMessageCurrentMode()

    def _toasterMessageCurrentMode(self, source: Optional[Literal['failsafe']] = None) -> None:
        sourceStr = f" [温度保护]" if source == 'failsafe' else ""
        self.toasterMessage(
            [
                modeDisplayName(self._modeSwitch.getChecked()),
                f"GPU: {self._thermalGPU.getTemp()}°C, CPU: {self._thermalCPU.getTemp()}°C",
                "散热模式已切换" + sourceStr
            ],
            source != 'failsafe'
        )

    def toasterMessage(self, message: List[str | None], expire = True) -> None:
        try:
            toast = Toast(duration=ToastDuration.Short, expiration_time= (datetime.datetime.now() + datetime.timedelta(seconds=5)) if expire else None)
            toast.text_fields = message
            # 图标缺失不该让整个程序崩掉——温度保护切换模式也会走到这里
            try:
                toast.AddImage(ToastDisplayImage.fromPath(resourcePath(GUI_ICON)))
            except Exception as ex:
                print(f'通知图标加载失败（忽略）: {ex}')
            self._toaster.show_toast(toast)
        except Exception as ex:
            print(f'系统通知发送失败（忽略）: {ex}')

    def _saveAppSettings(self):
        # 静音模式下滑块会被曲线每秒改写，那个值不是用户选的「自定义转速」。
        # 直接存下去会让用户切回自定义时发现转速莫名变成 0% 或 100%，所以用快照。
        isSilent = self._modeSwitch.getChecked() == ThermalMode.Silent.value
        if isSilent and self._lastCustomSpeed is not None:
            cpuSpeed, gpuSpeed = self._lastCustomSpeed
        else:
            cpuSpeed = self._thermalCPU.getSpeedSlider()
            gpuSpeed = self._thermalGPU.getSpeedSlider()
            if not isSilent:
                self._lastCustomSpeed = (cpuSpeed, gpuSpeed)

        curValues = [
            self._modeSwitch.getChecked(),
            cpuSpeed,
            gpuSpeed,
            self.FAILSAFE_CPU_TEMP,
            self.FAILSAFE_GPU_TEMP,
            self._failsafeOn
        ]
        if curValues == self._prevSavedSettingsValues:
            return
        self._prevSavedSettingsValues = curValues

        self.settings.setValue(SettingsKey.Mode.value, self._modeSwitch.getChecked())
        self.settings.setValue(SettingsKey.CPUFanSpeed.value, cpuSpeed)
        self.settings.setValue(SettingsKey.GPUFanSpeed.value, gpuSpeed)
        self.settings.setValue(SettingsKey.CPUThresholdTemp.value, self.FAILSAFE_CPU_TEMP)
        self.settings.setValue(SettingsKey.GPUThresholdTemp.value, self.FAILSAFE_GPU_TEMP)
        self.settings.setValue(SettingsKey.FailSafeIsOnFlag.value, self._failsafeOn)

    def _loadAppSettings(self):
        savedMode = self.settings.value(SettingsKey.Mode.value)
        if savedMode not in [m.value for m in ThermalMode]:
            savedMode = ThermalMode.Balanced.value
        self._modeSwitch.setChecked(savedMode)
        savedSpeed = self.settings.value(SettingsKey.CPUFanSpeed.value)
        self._thermalCPU.setSpeedSlider(savedSpeed)
        savedSpeed = self.settings.value(SettingsKey.GPUFanSpeed.value)
        self._thermalGPU.setSpeedSlider(savedSpeed)
        savedTemp = self.settings.value(SettingsKey.CPUThresholdTemp.value) or 95
        self._limitTempCPU.setCurrentText(str(savedTemp))
        savedTemp = self.settings.value(SettingsKey.GPUThresholdTemp.value) or 85
        self._limitTempGPU.setCurrentText(str(savedTemp))
        savedFailsafe = self.settings.value(SettingsKey.FailSafeIsOnFlag.value) or 'true'
        self._failsafeCB.setChecked(str(savedFailsafe).lower() == 'true')
        # 记住用户自定义模式的转速，静音模式下 _saveAppSettings 会用这份快照
        self._lastCustomSpeed = (self._thermalCPU.getSpeedSlider(),
                                 self._thermalGPU.getSpeedSlider())
        self._prevSavedSettingsValues = []

    def clearAppSettings(self):
        (isYes, _) = confirm("恢复默认设置", "要把所有设置恢复为默认值吗？\n"
                              "包括模式、转速、温度阈值、风扇曲线、自动化、热键。", ("恢复", "取消"))
        if not isYes: return
        self.settings.clear()
        # 曲线对象是共享引用，必须一起重置，否则界面上还显示旧曲线、
        # 下次保存时又把旧值写回去
        self._curveGpu.reset()
        self._curveCpu.reset()
        self._resetSilentState()
        self._refreshSilentTooltip()
        self._loadAppSettings()
        self._lastAcState = None
        self._onAutomationChanged()
        self._startHotKey()
        if getattr(self, '_settingsDialog', None) is not None:
            try:
                self._settingsDialog.reloadFromSettings()
            except Exception as ex:
                print(f'刷新设置窗口失败: {ex}')
        print('设置已恢复默认')

    def G_Mode_key_Pressed(self, val):
        print("G_Mode_key " + str(val))

def installChineseTranslator(app: QtWidgets.QApplication) -> None:
    """加载 Qt 自带的中文翻译，让消息框的“确定/取消”等系统按钮也显示中文"""
    candidates = [
        resourcePath('translations'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'translations'),
    ]
    for path in candidates:
        tr = QtCore.QTranslator()
        if tr.load('qtbase_zh_CN', path):
            app.installTranslator(tr)
            app._zhTranslator = tr   # 必须保留引用，否则翻译器被回收后失效
            print(f'Qt zh_CN translator loaded from {path}')
            return
    print('Qt zh_CN translator NOT found')

def applyAppStylesheet(widget: QtWidgets.QWidget) -> None:
    widget.setStyleSheet(f"""
        QGauge {{
            border: 1px solid gray;
            border-radius: 3px;
            background-color: {Colors.GREY.value};
        }}
        QGauge::chunk {{
            background-color: {Colors.BLUE.value};
        }}
        * {{
            color: {Colors.WHITE.value};
            background-color: {Colors.DARK_GREY.value};
        }}
        QToolTip {{
            background-color: black; 
            color: {Colors.WHITE.value};
            border: 1px solid {Colors.DARK_GREY.value};
            border-radius: 3;
        }}
        QComboBox {{
            border: 1px solid gray;
            border-radius: 3px;
            padding: 1px 0.6em 1px 3px;
        }}
        QComboBox::disabled {{
            color: {Colors.GREY.value};
        }}
        QTabWidget::pane {{
            border: 1px solid {Colors.GREY.value};
            border-radius: 3px;
        }}
        QTabBar::tab {{
            background: {Colors.DARK_GREY.value};
            border: 1px solid {Colors.GREY.value};
            border-bottom: none;
            padding: 5px 16px;
            margin-right: 2px;
        }}
        QTabBar::tab:selected {{
            background: #2c2c2c;
        }}
        QPushButton {{
            border: 1px solid gray;
            border-radius: 3px;
            padding: 3px 12px;
            background-color: #2c2c2c;
        }}
        QPushButton:hover {{
            border-color: {Colors.WHITE.value};
        }}
        QPlainTextEdit, QTableWidget {{
            border: 1px solid gray;
            border-radius: 3px;
            background-color: #171717;
        }}
        QHeaderView::section {{
            background-color: #2c2c2c;
            border: none;
            border-bottom: 1px solid {Colors.GREY.value};
            padding: 3px;
        }}
        QSlider::groove:horizontal {{
            height: 4px;
            background: {Colors.GREY.value};
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            width: 14px;
            margin: -6px 0;
            border-radius: 7px;
            background: {Colors.BLUE.value};
        }}
    """)

_singleInstanceMemory: Optional[QtCore.QSharedMemory] = None


def _acquireSingleInstance() -> bool:
    """原子地判断是不是第一个实例。

    用共享内存而不是临时文件：进程退出（包括崩溃）时系统自动回收，
    不会有残留的锁文件把下次启动挡住。必须保留引用，否则段被回收后
    第二个实例又能创建成功。
    """
    global _singleInstanceMemory
    mem = QtCore.QSharedMemory(SINGLE_INSTANCE_NAME)
    if mem.create(1):
        _singleInstanceMemory = mem
        return True
    if mem.error() == QtCore.QSharedMemory.SharedMemoryError.AlreadyExists:
        print('检测到已有实例在运行')
        return False
    # 其它错误（权限等）一律放行，别把程序锁死
    print(f'单实例检测异常（忽略）: {mem.errorString()}')
    return True


def _notifyRunningInstance() -> bool:
    """请已经运行的实例把窗口显示出来。做不到就返回 False，由调用方给提示。"""
    try:
        sock = QtNetwork.QLocalSocket()
        sock.connectToServer(SINGLE_INSTANCE_NAME)
        if not sock.waitForConnected(400):
            return False
        sock.write(b'show\n')
        sock.flush()
        sock.waitForBytesWritten(400)
        sock.disconnectFromServer()
        return True
    except Exception as ex:
        print(f'唤醒已有实例失败: {ex}')
        return False


def _startInstanceServer(window: QtWidgets.QWidget) -> Optional[QtNetwork.QLocalServer]:
    """监听第二个实例发来的“显示窗口”请求"""
    server = QtNetwork.QLocalServer(window)
    if not server.listen(SINGLE_INSTANCE_NAME):
        # 这里刻意不做 removeServer 重试：万一是另一个实例正在监听，
        # 删名字会把它的管道搞坏，等于两个实例互相打架。
        print(f'实例监听未启动（不影响使用）: {server.errorString()}')
        return None

    def onNewConnection() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is not None:
                conn.disconnectFromServer()
                conn.deleteLater()
        window.showNormal()
        window.activateWindow()
        window.raise_()

    server.newConnection.connect(onNewConnection)
    return server


def runApp(startMinimized = False) -> int:
    app = QtWidgets.QApplication([])

    if not _acquireSingleInstance():
        if not _notifyRunningInstance():
            alert("程序已在运行", "散热控制中心已经启动过了，请在系统托盘里找到它。")
        return 0

    installChineseTranslator(app)

    # Setup backend
    try:
        awcc = AWCCThermal()
    except NoAWCCWMIClass:
        errorExit("系统中未找到 AWCC WMI 类。", "可能是缺少相关驱动，或你的机型不受支持。")
    except CannotInstAWCCWMI:
        errorExit("无法实例化 AWCC WMI 类。", "请确认程序是以管理员身份运行的。")

    mainWindow = TCC_GUI(awcc)
    applyAppStylesheet(mainWindow)
    mainWindow._instanceServer = _startInstanceServer(mainWindow)

    if startMinimized:
        mainWindow.showMinimized()
        mainWindow.hide()
    else:
        mainWindow.show()

    return app.exec()