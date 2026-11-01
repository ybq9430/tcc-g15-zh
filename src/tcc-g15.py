# (c) github.com/AlexIII
# GPLv3
# 中文本地化 + 增强：github.com/ybq9430/tcc-g15-zh

import sys

from Backend.AppLog import setupLogging


# @main_requires_admin
def main():
    # 尽量早接管输出：后面任何 import 阶段的崩溃也要留下现场
    setupLogging()
    print("Starting")

    from GUI.AppGUI import runApp

    startMinimized = "--minimized" in sys.argv
    return runApp(startMinimized)


if __name__ == "__main__":
    sys.exit(main())
