import sys, os, time, datetime
from enum import Enum
from typing import Callable, Literal, Optional, Tuple, List
from PySide6 import QtCore, QtGui, QtWidgets
from windows_toasts import WindowsToaster, Toast, ToastDuration, ToastDisplayImage
from Backend.AWCCThermal import AWCCThermal, NoAWCCWMIClass, CannotInstAWCCWMI
from GUI.QRadioButtonSet import QRadioButtonSet
from GUI.AppColors import Colors
from GUI.ThermalUnitWidget import ThermalUnitWidget
from GUI.QGaugeTrayIcon import QGaugeTrayIcon
from GUI import HotKey
from Backend.DetectHardware import DetectHardware

GUI_ICON = 'icons/gaugeIcon.png'

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

    
class SettingsKey(Enum):
    Mode = "app/mode"
    CPUFanSpeed = "app/fan/cpu/speed"
    CPUThresholdTemp = "app/fan/cpu/threshold_temp"
    GPUFanSpeed = "app/fan/gpu/speed"
    GPUThresholdTemp = "app/fan/gpu/threshold_temp"
    FailSafeIsOnFlag = "app/failsafe_is_on_flag"
    MinimizeOnCloseFlag = "app/minimize_on_close_flag"

def errorExit(message: str, message2: Optional[str] = None) -> None:
    if not QtWidgets.QApplication.instance():
         QtWidgets.QApplication([])
    alert("出错了", message, QtWidgets.QMessageBox.Icon.Critical, message2 = message2)
    sys.exit(1)

class TCC_GUI(QtWidgets.QWidget):
    TEMP_UPD_PERIOD_MS = 1000
    FAILSAFE_CPU_TEMP = 95
    FAILSAFE_GPU_TEMP = 85
    FAILSAFE_TRIGGER_DELAY_SEC = 8
    FAILSAFE_RESET_AFTER_TEMP_IS_OK_FOR_SEC = 60
    # 静音模式风扇曲线：(温度上限 °C, 风扇百分比)，取第一个满足 temp < 上限 的档位
    # 本机 A/B 实测（空载）：平衡模式 2607/2827RPM ≈ 手动 0% 2605/2815RPM，手动 5% 已升到 3084/3241
    # 所以底档必须是 0%，且边界要高于空载温度（约 64-70°C），否则会形成极限环、风扇反复启停
    SILENT_CURVE = ((82, 0), (87, 30), (91, 65), (999, 100))
    SILENT_BAND_MARGIN = 5          # 降档需低于当前档位下界这么多度（档位滞环）
    SILENT_DOWN_COOLDOWN_SEC = 45   # 上次升档后至少等这么久才允许降档，避免风扇反复启停
    SILENT_REASSERT_SEC = 25        # 即使档位没变也定期重发一次，防止 EC 自行接管回自动曲线
    APP_NAME = "Thermal Control Center for Dell G15"
    APP_NAME_ZH = "Dell G15 散热控制中心"
    APP_VERSION = "1.6.5"
    APP_DESCRIPTION = "本软件是 Alienware Control Center（AWCC）的开源替代品 "
    APP_URL = "github.com/AlexIII/tcc-g15"
    APP_LOCALIZED_BY = "中文本地化：WorkBuddy"

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

    _gModeKeySignal = QtCore.Signal()
    _gModeKeyPrevModeStr: Optional[str] = None

    _toaster = WindowsToaster(APP_NAME)

    _modeSwitch: QRadioButtonSet

    def __init__(self, awcc: AWCCThermal):
        super().__init__()
        self._awcc = awcc

        self.settings = QtCore.QSettings(self.APP_URL, "AWCC")
        print(f'Settings location: {self.settings.fileName()}')

        # Set main window props
        self.setFixedSize(600, 0)
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.WindowMinimizeButtonHint | QtCore.Qt.WindowCloseButtonHint)
        self.setWindowIcon(QtGui.QIcon(resourcePath(GUI_ICON)))
        self.mouseReleaseEvent = lambda evt: (
            evt.button() == QtCore.Qt.RightButton and
            alert("关于", f"{self.APP_NAME_ZH} v{self.APP_VERSION}", message2 = f"{self.APP_DESCRIPTION}\n{self.APP_URL}\n{self.APP_LOCALIZED_BY}")
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
        addToAutorunAction = menu.addAction("启用开机自启")
        def autorunTaskRun(action: Literal['add', 'remove']) -> None:
            err = autorunTask(action)
            if err != 0 and action == 'add':
                alert("错误", f"创建开机自启任务失败。错误码={err}", QtWidgets.QMessageBox.Icon.Critical)
            else:
                alert("成功", f"开机自启已{'启用' if action == 'add' else '关闭'}")
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
        self._modeSwitch.setToolTipFor(
            ThermalMode.Silent.value,
            "风扇按温度分档自动调速，日常负载下保持最低转速：\n"
            "82°C 以下·最低　87°C·30%　91°C·65%　91°C 以上·满转\n"
            "降温后需等 45 秒才降档，避免风扇反复启停。\n\n"
            "空载噪音与平衡模式相同，收益在于中等负载时不急着把风扇拉高。\n"
            "注意：本机手动转速下限约 3000 RPM，与空载时的自动转速接近，\n"
            "所以静音模式的收益主要在中等负载时不急着提速，而不是让空载更安静。\n"
            "温度保护依然有效，超过阈值仍会切到 G 模式。"
        )
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

        mainLayout = QtWidgets.QVBoxLayout(self)
        mainLayout.addLayout(lTherm)
        mainLayout.addLayout(modeBox)
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

        def onModeChange(val: str):
            self._thermalGPU.setSpeedDisabled(val != ThermalMode.Custom.value)
            self._thermalCPU.setSpeedDisabled(val != ThermalMode.Custom.value)
            self._silentLastBand = {}
            self._silentLastChangeTs = {}
            self._silentLastWriteTs = {}
            # 静音模式后端没有对应的 WMI 值，复用 Custom 让 EC 交出风扇控制权，再由软件曲线控速
            backendVal = BACKEND_MODE_PASSTHROUGH.get(val, val)
            res = self._awcc.setMode(self._awcc.Mode[backendVal])
            print(f'Set mode {val} (backend {backendVal}): ' + ('ok' if res else 'fail'))
            if not res:
                self._errorExit(f"切换到「{modeDisplayName(val)}」失败", "程序即将退出")
            updateFanSpeed()
            if val != ThermalMode.G_Mode.value:
                self._failsafeTrippedPrevModeStr = None # In case the mode was switched manually
            updFailsafeIndicator()
            for m in ThermalMode:
                self._trayMenuModeSwitch[m.value].setText(f"{'•' if m.value == val else ' '} {modeDisplayName(m.value)}")

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
            
            # Periodically save app settings
            self._saveAppSettings()

        self._loadAppSettings()

        self._updateGaugesTask = QPeriodic(self, self.TEMP_UPD_PERIOD_MS, updateAppState)
        updateAppState()
        self._updateGaugesTask.start()

        self.gModeHotKey = HotKey.HotKey(HotKey.G_MODE_KEY, self._gModeKeySignal)
        self._gModeKeySignal.connect(self._onGModeHotKeyPressed)
        self.gModeHotKey.start()

    def updateGaugeTitles(self, gpuModel, cpuModel):
        if gpuModel: self._thermalGPU.setTitle(gpuModel)
        if cpuModel: self._thermalCPU.setTitle(cpuModel)

    def _silentBandIndex(self, temp: Optional[int]) -> int:
        """温度落在曲线第几档；读不到温度时按最高档处理，宁可吵不可热"""
        if temp is None:
            return len(self.SILENT_CURVE) - 1
        for i, (limit, _) in enumerate(self.SILENT_CURVE):
            if temp < limit:
                return i
        return len(self.SILENT_CURVE) - 1

    def _applySilentCurve(self, gpuTemp: Optional[int], cpuTemp: Optional[int]) -> None:
        """静音模式核心：按温度查档，升档立即生效，降档必须跌出当前档位下界一段才生效"""
        targets = (
            ('GPU', self._thermalGPU, self._awcc.GPUFanIdx, gpuTemp),
            ('CPU', self._thermalCPU, self._awcc.CPUFanIdx, cpuTemp),
        )
        for name, unit, fanIdx, temp in targets:
            idx = self._silentBandIndex(temp)
            last = self._silentLastBand.get(name)
            if last is None or idx > last:
                newIdx = idx                                   # 首次或升温升档：立即执行
            elif idx < last:
                if last > 0:
                    lowerEdge = self.SILENT_CURVE[last - 1][0]
                else:
                    lowerEdge = -1000
                cooledDown = (time.time() - self._silentLastChangeTs.get(name, 0)) >= self.SILENT_DOWN_COOLDOWN_SEC
                if cooledDown and temp is not None and temp <= lowerEdge - self.SILENT_BAND_MARGIN:
                    newIdx = max(idx, last - 1)                # 降档每次最多退一档，避免转速骤降后又反弹
                else:
                    newIdx = last
            else:
                newIdx = last
            if last is not None and newIdx == last:
                # 档位没变：正常情况下不写 EC，但定期重申一次，防止被 EC 自动曲线接管
                if (time.time() - self._silentLastWriteTs.get(name, 0)) < self.SILENT_REASSERT_SEC:
                    continue
            speed = self.SILENT_CURVE[newIdx][1]
            self._silentLastBand[name] = newIdx
            self._silentLastWriteTs[name] = time.time()
            if newIdx != last:
                self._silentLastChangeTs[name] = time.time()
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
        if self.gModeHotKey is not None:
            self._updateGaugesTask.stop()
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
        toast = Toast(duration=ToastDuration.Short, expiration_time= (datetime.datetime.now() + datetime.timedelta(seconds=5)) if expire else None)
        toast.text_fields = message
        toast.AddImage(ToastDisplayImage.fromPath(resourcePath(GUI_ICON)))
        self._toaster.show_toast(toast)

    def _saveAppSettings(self):
        curValues = [
            self._modeSwitch.getChecked(),
            self._thermalCPU.getSpeedSlider(),
            self._thermalGPU.getSpeedSlider(),
            self.FAILSAFE_CPU_TEMP,
            self.FAILSAFE_GPU_TEMP,
            self._failsafeOn
        ]
        if curValues == self._prevSavedSettingsValues:
            return
        self._prevSavedSettingsValues = curValues

        self.settings.setValue(SettingsKey.Mode.value, self._modeSwitch.getChecked())
        self.settings.setValue(SettingsKey.CPUFanSpeed.value, self._thermalCPU.getSpeedSlider())
        self.settings.setValue(SettingsKey.GPUFanSpeed.value, self._thermalGPU.getSpeedSlider())
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

    def clearAppSettings(self):
        (isYes, _) = confirm("恢复默认设置", "要把所有设置恢复为默认值吗？", ("恢复", "取消"))
        if not isYes: return
        self.settings.clear()
        self._loadAppSettings()

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

def runApp(startMinimized = False) -> int:
    app = QtWidgets.QApplication([])
    installChineseTranslator(app)

    # Setup backend
    try:
        awcc = AWCCThermal()
    except NoAWCCWMIClass:
        errorExit("系统中未找到 AWCC WMI 类。", "可能是缺少相关驱动，或你的机型不受支持。")
    except CannotInstAWCCWMI:
        errorExit("无法实例化 AWCC WMI 类。", "请确认程序是以管理员身份运行的。")

    mainWindow = TCC_GUI(awcc)
    mainWindow.setStyleSheet(f"""
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
    """)

    if startMinimized:
        mainWindow.showMinimized()
        mainWindow.hide()
    else:
        mainWindow.show()

    return app.exec()