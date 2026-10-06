# LBJ Manager

PicoUpdater 与 LogViewer 的整合版，版本 `3.0.3-preview`。一个窗口、一个设备选择器，包含「设备管理」和「历史记录」两页。仓库内原来的两个工具目录已由本目录替代。

## 使用

安装 Python 3.11 或更高版本及依赖，运行唯一的主入口：

```sh
python3 -m pip install -r lbj_manager/requirements.txt
python3 lbj_manager/lbj_manager.py
```

Apple Silicon 版 `LBJ Manager.app` 内嵌 Python 和依赖，可直接双击。没有开发者证书签名或公证；其他 Mac 的安全设置可能要求手动允许打开。复制整个 `.app`，不要只复制里面的可执行文件，也不要破坏 Framework 的符号链接。

## 工作流程

- 接收器接入 USB 后点击「扫描设备」：唯一 Pico 自动选中，在顶部设备选择器旁显示绿色「✓ 已自动选中 Pico」和端口，底部也显示扫描结果。启动时自动选中同样有提示；后台扫描不抹掉确认，不切换设备。多台设备时手动选择。没有检测到 Pico 或扫描失败时不能刷入、读取或自检。
- 设备管理：选择普通版或 W 版，在线更新、强制刷入、离线 ZIP 刷入和硬件检查单。刷入前复核硬件兼容性。**刷入会清除设备历史，先到历史页导出备份。**
- 历史记录：读取设备、导出原始 JSONL、导入本地日志，按车次、车型、时间筛选，查看坐标、地图与原始报文。9999 条记录分批显示，未知速度保持 `---`。
- 更新、自检、读取和导出共用串口锁，不能同时操作同一台或另一台接收器。预检取消后尝试重启恢复接收；恢复失败、USB 拔出和部分写入会明确提示，不宣称设备已经恢复。
- 读取设备历史会暂时停止接收并在结束后恢复。硬件检查也会暂停接收。界面导航和设备扫描本身不会停止接收。

## 拖拽导入

- ZIP：将一个固件 ZIP 拖到设备管理页的「拖入固件 ZIP」区域，或「从 ZIP 刷入」按钮。软件只在电脑上校验包内版本、当前分支、运行文件与 DMA / 保护依赖；通过后显示文件名和版本。点击「刷入已载入 ZIP」才进入原有硬件预检与清空数据确认流程。**拖入本身不访问串口、不刷写、不清历史。**切换固件分支会清除已载入的 ZIP，刷入前会再次校验。
- 日志：将一个 `.json`、`.jsonl`、`.txt` 或 `.log` 历史记录文件拖到历史页的「拖入日志文件」区域，或「导入日志」按钮，直接开始后台解析。无效文件不覆盖当前已载入的记录。
- 支持中文、空格与花括号路径。多文件、文件夹、拖错区域和任务进行中的拖入会给出底部提示。
- 原有文件选择按钮保留。拖拽使用 [TkinterDnD2 的原生 TkDND 支持](https://github.com/Eliav2/tkinterdnd2)，Apple Silicon App 已内嵌 arm64 库；源码环境缺依赖时会明确提示并回退到按钮。

## DMA / 新增文件

两个分支的在线清单与离线 ZIP 都识别 `pio_dma_rx.py` 和 `device_protection.py`，依赖文件先写、`main.py` 最后写。W 版另含 `history_store.py`、`wireless_portal.py`。

整合版额外解析包内 Python 文件的本地模块依赖：新固件如果导入 DMA / 保护模块，ZIP 或在线下载必须包含对应文件；语法错误、缺文件、无效 UTF-8 均在刷入前阻止。没有新增依赖的旧固件仍可使用。**这不是设备端逐字节刷写校验**：复制仍依赖 mpremote 的写入结果；历史导出则使用长度与 SHA-256 校验。

W 版核心自检已按当前硬件读取 GP46 的 VSYS/3，不再沿用旧的 GP41 电池测量；普通版保留 GP27 外部电池检测。

## 开发与测试

`lbj_manager.py` 是直接维护的唯一主入口，已内联两套主程序和历史传输引擎，不依赖旧工具目录或生成脚本。硬件自检使用同目录的 `solder_check.py` 与 `diagnostic_drivers/`。原有更新、检查单和历史传输的回归测试已迁入本目录，直接测试整合版，不需要恢复旧源码。

```sh
python3 -m unittest discover -s lbj_manager/tests -v
python3 lbj_manager/tests/gui_smoke.py /private/tmp/lbj-manager-gui
python3 lbj_manager/tests/drag_drop_smoke.py /private/tmp/lbj-manager-drop
```

GUI 测试创建真实 Tk 窗口，但 USB、下载与自检传输使用模拟对象，不会操作真实接收器。测试详情与边界见 `TEST_REPORT.md`。

原随附的 RTC 校时脚本保留在 `rtc_sync_gui.py`，可单独运行 `python3 lbj_manager/rtc_sync_gui.py`。它不在主 App 的导航里；使用时不要同时在 Manager 中对同一设备执行串口任务。

打包：在 arm64 Python 环境运行 `python3 -m PyInstaller "lbj_manager/LBJ Manager.spec"`。
