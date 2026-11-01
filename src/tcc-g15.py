# (c) github.com/AlexIII
# GPLv3
# 中文本地化 + 增强：github.com/ybq9430/tcc-g15-zh

import sys

from Backend.AppLog import setupLogging


# @main_requires_admin
def main():
    # 安装模式：把当前目录镜像到 Program Files 后退出，不启动界面
    if "--install" in sys.argv:
        from Backend.Installer import install, INSTALL_DIR
        args = sys.argv[sys.argv.index("--install") + 1:]
        target = INSTALL_DIR
        for i, a in enumerate(args):
            if a == "--to" and i + 1 < len(args):
                target = args[i + 1]
        return install(target, launch="--no-launch" not in sys.argv)

    # 尽量早接管输出：后面任何 import 阶段的崩溃也要留下现场
    setupLogging()
    print("Starting")

    from GUI.AppGUI import runApp

    startMinimized = "--minimized" in sys.argv
    return runApp(startMinimized)


if __name__ == "__main__":
    sys.exit(main())
