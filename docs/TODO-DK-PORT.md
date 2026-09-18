# nrftest × nRF52840 DK（PCA10056）移植待办

> 创建：2026-09-18。目的：把主线面向 PCA10059 Dongle 的 nrftest 夹具跑在已接入的
> nRF52840 DK 上。下次会话直接按本文档顺序执行即可。
>
> **✅ 全部完成（2026-09-18，T1–T4 含实机验证）。本文档转为 DK 路径参考保留。**
>
> 硬件命令速查（本机）：
> - 构建/刷写：`just firmware-build-dk` → `just firmware-flash-dk <sha256>`
> - 探测：`just btp-doctor COM11 dk`、`just host-doctor COM11 <profile> dk`
> - 复位/恢复：`just btp-target-reset COM11 10 30 dk`、
>   `just btp-target-recover COM11 <profile> 10 30 dk`
>
> 本机 `nrftest.local.toml` 要点：COM11 为 BTP 口、`debugger_serial = "1050252028"`、
> `jlink_library` 必须指向 **x64** DLL（32 位 JLinkARM.dll 配 64 位 Python 报
> WinError 193）。

## 0. 当前已完成的基础（无需重做）

- pixi 环境就绪，`just check-tools / test / lint` 全绿（140 个单元测试通过）。
- 上游源码与工具统一安装在 `D:\dk\nrftest-upstream\`：
  - `zephyrproject\zephyr`（VIDLG fork，commit `f530afb`，69 个 west 项目校验通过）
  - `zephyr-sdk-1.0.1`（arm-zephyr-eabi，绕过 GitHub API 限流手动安装，项目校验通过）
  - `auto-pts`（固定 commit）
  - `host-tools\`（dtc 1.6.1 / socat 1.7.3.2）
- 本机配置 `nrftest.local.toml` 已建好；`jlink_library = "D:/jlink/JLink_V924a/JLinkARM.dll"`。
- 硬件已接入：DK 板载 J-Link OB 枚举出 COM3、COM11（VID 1366:1061，序列号 001050252028）。
- 旧版工具已清理（SDK 0.17.4、NCS 3.3.4，共 17.7 GB）。

## 1. 环境备忘（每次新会话都可能用到）

- **网络**：直连 GitHub 被墙。任何联网命令前加临时代理：
  `export HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897`
  （Clash Verge 混合端口；不要写入全局 git 配置，按需临时使用。）
- **已知仓库缺陷（已修，未提交）**：`tests/unit/test_profile.py:20` 的
  `ACTIVE_PROFILE_SHA256` 在原提交 `d64b80c` 里就与 profile 文件对不上，
  已改为文件真实哈希 `2c315af0092c...e26924`。可自行决定是否提交。
- `west sdk install` 走 GitHub API 会被限流；SDK 已手动装好，无需再动。
  若将来重装，参考本次做法：直链下载
  `zephyr-sdk-<ver>_windows-x86_64_minimal.7z` + `sha256.sum`（Release 附件直链），
  7z 解压后 `setup.cmd /c` 再 `setup.cmd /t arm-zephyr-eabi /h`。

## 2. 为什么现状跑不通（兼容性结论）

| 环节 | 现有实现（PCA10059） | DK（PCA10056）实际 | 结论 |
|---|---|---|---|
| 构建目标 | `tools/build_firmware.py:20` 固定 `nrf52840dongle/nrf52840` | 应为 `nrf52840dk/nrf52840` | ❌ |
| Flash 布局 | app 从 0x1000 起、0xE0000 以上是原厂 bootloader；`REQUIRED_CONFIG` 强制 `BOARD_HAS_NRF5_BOOTLOADER=y`、`FLASH_LOAD_OFFSET=0x1000`；`validate_flash_segments()` 拒绝 0x1000 以下/0xE0000 以上 | 无该 bootloader，app 从 0x0 链接 | ❌ |
| 打包/烧写 | `package_firmware.py` + `flash_firmware.py`：nrfutil DFU 包、强制端口 1915:521F、`bootloader_serial` | 板载 SEGGER J-Link（1366:1061）SWD 刷写 | ❌ |
| BTP 传输 | Dongle 原生 USB CDC（`pca10059.overlay` → `board_cdc_acm_uart`） | 自然路径：UART0 → J-Link VCOM（COM3/COM11）；DK 自带 USB 口走 CDC 也可行但需验证 DTS | ⚠️ |
| Target reset | `target_reset.py` 依赖 USB CDC 消失/重枚举 | DK 复位时 VCOM 不会消失，逻辑需改 | ⚠️ |

有利条件：**DK 是上游 Tester 官方支持目标**（`tests.yaml` 的 `platform_allow` 含 DK；
Tester 自带 `boards/nrf52840dk_nrf52840.conf/.overlay`）。2026-09-06 调查报告已用
NCS 组合把官方 DK Tester 构建出 ELF/HEX/BIN（仅未刷入实机验证）。Host 侧
（btp-doctor / host-doctor / fixtures）只依赖 pyserial，对 VCOM 透明，基本不用改。

## 3. 待办任务（按顺序）

### T1 新增 DK 构建入口（不动 PCA10059 主线）
- [x] `tools/build_firmware.py` 增加 `--board {dongle,dk}`（BoardVariant 数据类，
      模块级 Dongle 常量保持不变，package/flash 主线零影响）：
  - `nrf52840dk/nrf52840`，输出目录 `pca10056-tester`
  - DK 专用 REQUIRED_CONFIG：去掉 `BOARD_HAS_NRF5_BOOTLOADER`、`FLASH_LOAD_OFFSET`；
    保留 `UART_PIPE=y`、`UART_INTERRUPT_DRIVEN=y`、`CONSOLE=n`、`LOG=n` 等
  - flash 段校验放宽：允许 0x0 起始、上限完整 1MB（0x100000）
  - 固件输入为自建的 `firmware/app/pca10056.conf/.overlay`（内容对齐上游
    `boards/nrf52840dk_nrf52840`，BTP 走 UART0 + hw-flow-control，但保持仓库
    自包含可哈希的输入文件）
- [x] 传输层决策：UART0 → J-Link VCOM（COM11 实测有 BTP 响应；COM3 无）
- [x] justfile `firmware-build-dk`

### T2 J-Link 刷写入口
- [x] `tools/flash_firmware_dk.py`：pylink + `jlink_library` + `debugger_serial`
      SWD 刷 `zephyr.hex`（`NRF52840_XXAA`），manifest board/HEX sha256 双重校验 +
      `--confirm-sha256` 显式确认，telemetry 报告 `firmware-flash-dk`
- [x] justfile `firmware-flash-dk <sha256>`
- [x] 注意：64 位 pixi Python 必须用 `JLink_x64.dll`（local toml 已改）

### T2b Host 侧传输身份参数化（T3 中发现的新增工作）
- [x] `host/nrftest/autopts_adapter.py`：`select_application_port` 增加
      `transport` 参数（`dongle`=2FE3:0004 / `dk`=1366:1061，默认 dongle 不变）
- [x] `btp_core_probe`、`host.nrftest.cli doctor`、`btp_gap_rf_fixture` 加
      `--transport {dongle,dk}`；justfile 对应 recipe 加 `transport` 参数（默认 dongle）
- [x] 其余全部硬件探针补齐 `--transport`（gap-control / gap-lifecycle /
      gap-transport-reuse / gap-host-crash 含子进程命令透传 / gatt-profile /
      gatt-profile-rebuild / gatt-subscription / gatt-passive-disconnect）；
      justfile 全部对应 recipe 与 `btp-target-recover` 链均支持

### T3 实机验证（顺序执行）
- [x] `pixi run just firmware-build-dk` 构建成功（FLASH 32.92%），Kconfig + 段校验通过
- [x] SWD 刷入 DK 成功
- [x] COM11 承载 UART0/BTP（COM3 是另一个 CDC 接口，无 BTP 响应）
- [x] `pixi run just btp-doctor COM11 dk`：CORE/GAP/GATT 全部注册，mask=0x2000000f
- [x] `pixi run just host-doctor COM11 profiles/blehub-nrf-basic-v1.json dk`：
      outcome=pass，Profile built（10 属性，service_handle=39），cleanup=clean
- [x] `pixi run just btp-gap-rf-fixture COM11 NrftestDK fdf0 120 dk`：
      `NRFTEST_FIXTURE_READY`（地址 dc:26:e2:e1:5b:69）→ 真实 Central
      `5b:d0:1e:03:da:41` 连接并断开 → fixture PASS，nRF 侧 RF 事实已记录

### T4 后续完善（T3 通过后再做）
- [x] `target_reset.py` DK 模式（`--transport dk`）：`reset_dk_target` J-Link
      reset-and-halt → 释放 → 校验 VCOM 身份稳定（不做"USB 消失/重枚举"校验），
      随后以全新 `AutoPtsSession` 的 Core/GAP 握手为恢复门（带超时重试）；
      报告场景 `target-reset-dk`。实机验证：COM11 稳定，reset≈0.4s，
      BTP 重握手≈2.5s（1 次尝试）
- [x] `btp-target-recover ... dk` 完整恢复链实机验证通过：
      reset → btp-doctor（GAP attached / GATT registered）→ GATT Profile PASS（重建）
- [x] 在 `docs/PLAN.md` 8.2 节记录 DK 路径的 board variant / partition / 烧写与
      恢复方式变更（2026-09-18 T3 完成时已记录；T4 的 target-reset 语义已在
      PLAN.md 该段注明）
- [x] 本文档状态更新并归档为 DK 路径参考（2026-09-18 T4 完成时）

## 4. 参考文件索引

- 构建入口：`tools/build_firmware.py`（BOARD 常量、REQUIRED_CONFIG、flash 段校验都在这）
- 刷写（Dongle 版，DK 不用）：`tools/flash_firmware.py`、`tools/package_firmware.py`
- J-Link 复位（DK 可参考的打开方式）：`host/nrftest/target_reset.py`
- 上游 Tester DK 配置：`D:\dk\nrftest-upstream\zephyrproject\zephyr\tests\bluetooth\tester\boards\`
- 兼容性调查历史：`docs/research/2026-09-06-btp-ble-layered-investigation.md`（418-420 行）
- 项目决策规矩：`docs/PLAN.md`（约 419 行：刷写路径切换必须显式记录）
