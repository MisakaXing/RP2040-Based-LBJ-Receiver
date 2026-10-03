# LBJ 普通版 / W 版独立内部检测

本目录与主程序、`pico_updater` 分开。`gui.py` 把诊断固件加载到普通版 RP2040 Pico 或 W 版 Waveshare RP2350B-Plus-W 的 RAM，不覆盖原系统文件。机器脱离电脑运行正式一小时测试；**最终报告和明细日志只写入 SD 卡**，GUI 只从插在电脑上的 SD 卡读取。

GUI 连接或刷新串口后读取板型、SN、MicroPython 和原系统版本。默认“自动识别”沿用 `pico_updater/solder_check.py` 的规则：RP2350 识别为 W 版，RP2040 识别为普通版。下拉菜单可手动改为“普通版”或“W 版”，自动识别结果仍显示，手动选择会用于本次所有按键、采样和预检。普通 Raspberry Pi Pico W / Pico 2 W 不等于本项目的 Waveshare W 版接线。RP2040 无 GP41/42，不能选择本项目 W 版。

| 检测型号 | POWER | 电池 ADC | USB 电源检测 | 温度 ADC | Precheck |
| --- | --- | --- | --- | --- | --- |
| 普通版 | GP28 | GP27 | GP24 | CORE_TEMP（旧接口为 4） | RTC、电池、射频 |
| W 版 | GP42 | GP41 | WL_GPIO2 | CORE_TEMP（旧接口为 8） | RTC、电池、射频、CYW43 |

W 版无线检测逐步移植自 picoupdater：兼容 `network.WLAN.IF_STA` 和 `network.STA_IF`，启用 STA，最多等 1.5 秒确认 `active()`，随后关闭 STA。启动或关闭失败都令 Precheck 失败，POWER 重试，不能继续后续检测。不扫描热点、不连接网络。USB 检测按 [Waveshare 原理图](https://files.waveshare.com/wiki/RP2350B-Plus-W/RP2350B-Plus-W.pdf)使用 CYW43 GPIO2；温度优先使用 [MicroPython 的 ADC.CORE_TEMP](https://github.com/micropython/micropython/blob/master/ports/rp2/machine_adc.c)。W 版四项结果和下方拔线、插卡提示保持在同一屏。

型号、自动识别结果、自动/手动选择和实际引脚会存入 SD 报告及电脑端加载记录；GUI 读取时按报告型号显示，旧报告也可读取。W 版目前仅完成本地模拟和代码检查，尚未进行实机验证。

## 正式测试流程

1. 电池供电并用 USB 连接设备，在 Mac 终端输入 `picotest` 打开 GUI，选择串口，检查自动识别型号，必要时手动修改，再点击“加载检测固件到 RAM”。`/opt/homebrew/bin/picotest` 是指向本目录 `gui.py` 的软连接。加载前工具再次读取板型和原系统版本。
2. 设备先显示 **PRECHECK**，依次检测 RTC、电池、射频 SPI 与时钟/数据线；W 版另检查 CYW43 无线模块。失败时按 POWER 重试。全部通过后，同一页面底部出现“UNPLUG USB”和“INSERT SD CARD”两行提示，检测结果保持显示。拔线并插卡后按右上 **POWER** 做一次简单的卡识别和扇区读取；未识别到卡就在该页面持续显示红色错误，至少保留两秒，并等待 POWER 重试。
3. 识别到卡后进入独立 **SD CARD** 页，按 POWER 做 FAT 挂载及写入回读验证。失败时 POWER 只重试；未通过不能进入后续检测。**此硬件的 USB 与 SD 卡冲突，不要在 SD 检测和压力测试期间重新连接 USB。**
4. SD 检查通过后进入全屏红、绿、蓝、白、黑色块测试。蜂鸣器页每按一次左上 **OK** 响一次；听到后按 POWER。五键同页，左侧从上到下 OK、UP、DOWN、MENU，右上 POWER。全部按过变绿后，按 POWER 继续。
5. 随后设备启动一小时射频测试，在 `LBJ_DIAG/<时间-UID>/` 逐步写入事件、错误和分钟快照。设备约每秒检查一次 SD 卡；拔卡或读写失败立即终止射频测试并显示错误。为保存最终摘要，可重新插卡，按 POWER 重试保存。
6. 显示 `RESULT` 后关闭设备，取出 SD 卡接到电脑。在 GUI 点击“从 SD 卡读取报告”，选择 `LBJ_DIAG/<时间-UID>/report.json`。GUI 不通过 USB 采集报告。

读取后会自动打开大屏结果总览：顶部直接显示结论、失败原因、电压和电量变化、平均 RSSI、不可纠错比例、FIFO 与恢复次数、每分钟最低剩余内存；下方同屏展示全部分钟折线图和 RX TYPE 柱状图。“详细数据与日志”按检查项、分钟数据、接收事件、错误统计和完整原始报告分开查看。也可在终端输入 `picotest /Volumes/<SD卡名>/LBJ_DIAG/<运行目录>/report.json` 直接打开该 SD 报告。

趋势图把本分钟新增量或当前值放在左侧纵轴，把累计值或历史最低值放在右侧纵轴；两侧分别缩放，并在图例标明使用哪侧。没有累计量的图只显示左轴，只有累计量的图只显示右轴。机内固件在 SD 挂载前遇到低剩余内存时会先执行一次垃圾回收，再决定是否省略操作日志；屏幕、蜂鸣器和五键测试开始前已经挂载 SD，操作日志可以直接写卡。已生成报告的结论不会重算。

结果总览右上角的“接收车次”打开独立页面。页面按每条接收回调列出测试时间、RX TYPE、车次、机车、速度、公里标、RSSI、RIC 和坐标；可搜索、筛选有车次号的记录，点选查看完整解码字段与原始接收内容。顶部同时显示已保存 RX 记录数、接收计数、不同车次数以及日志缺失/详情截短提示。SD 日志记录全部成功解码并送到回调的消息；若队列满或内存不足导致省略，报告和页面会显示缺少条数。它不等于记录了每个原始射频码字。

## SD 报告

每次运行目录含 `report.json`、`events.jsonl`、`errors.jsonl`、`snapshots.jsonl`；完成后另写 `complete.json` 作为最终摘要备份。报告先写入运行中摘要，测试结束后更新最终摘要；若中途拔卡且未能重新保存，GUI 会把未完成的摘要标为 `INCOMPLETE`。GUI 将 JSONL 合并显示详细报告、每分钟趋势和 RX TYPE 柱状图。日志队列最多暂存 8 条；排队满时继续累计接收数与 RSSI，并记录省略数，结果标为 `INCOMPLETE`。单条事件详情最多 2048 字符，截短数也会记录。

最终电量数据只给出射频测试起止的电压降低值，以及按原主程序公式换算的电量降低百分点。平均接收 RSSI 按每条有有效 RSSI 的接收记录计算；分钟 RSSI 快照只用于趋势图。

新版判定规则：FIFO 满计数达到 10 次，或不可纠错码字比例严格超过 50% 时判 FAIL；射频恢复次数记录为提示，不单独造成 FAIL。每分钟快照保存采样时剩余内存和当时记录到的历史最低剩余内存；摘要提供这些分钟样本的最低、最高和平均值，GUI 展示折线图。旧版报告保留生成时的原判定，不会被重新裁决。

SD 检测会写入并读回一个临时验证文件，不会格式化卡。拔卡发生在写入时可能使 FAT 文件系统受损，因此测试时保持卡稳定插入。完全脱机的一小时正式测试和拔卡恢复流程仍须在实机完成。

开发用 USB 短测命令：`python3 flash_and_report.py start --seconds 5 --development-short-run --skip-interactive`。此模式不会访问 SD，也不能作为成品合格证据。

命令行用 `probe --port <串口>` 读取身份，`start --variant auto|standard|wireless` 指定型号；省略时自动识别。身份读取会暂停原程序，查询结束后用软重启恢复原程序。
