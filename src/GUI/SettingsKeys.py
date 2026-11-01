"""QSettings 键名集中定义，主界面和设置对话框共用，避免两边写错字符串。"""

from enum import Enum


class SettingsKey(Enum):
    # 主界面
    Mode = "app/mode"
    CPUFanSpeed = "app/fan/cpu/speed"
    CPUThresholdTemp = "app/fan/cpu/threshold_temp"
    GPUFanSpeed = "app/fan/gpu/speed"
    GPUThresholdTemp = "app/fan/gpu/threshold_temp"
    FailSafeIsOnFlag = "app/failsafe_is_on_flag"
    MinimizeOnCloseFlag = "app/minimize_on_close_flag"
    # 静音曲线
    SilentCurveJson = "app/silent/curve_json"
    # 自动化
    PowerLinkFlag = "app/auto/power_link"
    PowerLinkBatteryMode = "app/auto/power_link_battery_mode"
    GameDetectFlag = "app/auto/game_detect"
    GameExclusions = "app/auto/game_exclusions"
    # 热键
    HotKeyVk = "app/hotkey/vk"
