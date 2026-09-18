# nrftest BTP-first 计划

## 1. 状态与约束

- 计划状态：**已批准，Phase 0–4 已完成，Phase 5 进行中**。
- 当前主线：Zephyr Bluetooth Tester + Bluetooth Test Protocol（BTP）+ 跨平台 Python Host。
- 当前阶段：Windows x64 Phase 5 Host gate 已通过；跨平台源码/配置审计已加固全 Host AutoPTS 独占、固定 checkout 导入、DFU identity、setup preflight、Telemetry 和跨平台 Just 入口。Windows VC++ Runtime 已改由 Pixi `win-64` 环境直接管理；Nordic device-lib managed-install receipt 仅记录显式 UAC provisioning 来源，不替代 `host-doctor`/`firmware-flash` 功能证据，也不推翻已通过的 Windows Host gate。Zephyr Tester 修复已集成并 push 到 `VIDLG/zephyr` 的固定 commit，nrftest 已完成 fork 纯净配置的重新构建和 package 验证，尚未刷写并重跑 RF；旧 v4.4.2 RF 结果不能外推到新 commit。macOS Apple Silicon 与 Linux x64 实机门尚未执行；固定 AutoPTS pending-command/stop 无硬截止时间仍需独立设计决策。Phase 5 整体未通过。
- 旧的 Nordic Connectivity、`pc-ble-driver-py` 和 Blatann 实现已完整迁入 `archive/connectivity-blatann-2026-09-06/`，不再是活动实现，也不能用其历史结果证明 BTP 主线通过。
- 本计划只约束 `nrftest`，不修改 BleHub 的公共 API、production backend 或架构文档。经用户明确批准，BleHub 可在自己的仓库中维护独立、通用、参数化的 HIL consumer；这不建立两个项目之间的代码、仓库或进程依赖。
- 如果实施中需要偏离本计划的协议、拓扑、硬件角色、验证门或归档边界，必须停止并通知用户，得到明确批准后再修改计划和继续实施。

---

## 2. 项目定义

`nrftest` 是一个独立、跨平台、PC 可控制的 BLE Peripheral 硬件测试夹具，用于通过真实 BLE RF 测试 BleHub Central。

它由两部分组成：

1. 运行在 nRF52840 上的 Zephyr Bluetooth Tester/BTP Server；
2. 运行在 Windows、macOS 或 Linux 上、优先复用 AutoPTS `pybtp` 的 Python Host 和测试夹具 API。

`nrftest` 的职责是：

- 配置并运行一个真实 BLE Peripheral；
- 动态创建测试所需的 GATT database；
- 控制广告、值、Notification、Indication、断连和恢复；
- 提供独立于 BleHub 的 Peripheral 侧事实；
- 让同一个测试夹具可被不同平台上的 BleHub Central 使用。

`nrftest` 不是 BleHub 的 backend，也不是 BleHub Central 的 controller。BleHub 不通过 USB、串口或 BTP 调用 nRF；BleHub 与 nRF 之间只有真实 BLE RF 通信。

---

## 3. 已确定的架构决策

### 3.1 BTP 是最终自动化控制协议

设备控制面采用 Zephyr Bluetooth Tester 已实现的 BTP，不再自行定义设备侧 NDJSON、文本 Shell 协议或 Nordic RPC 协议。

BTP 的基本帧由以下字段组成：

```text
service | opcode | index | payload_length | payload
```

具体字段布局、字节序、命令和事件定义必须来自同一个被固定 revision 的 Zephyr/AutoPTS BTP 定义，不能依靠手写的非版本化常量。

### 3.2 Shell 只作为可选诊断入口

Zephyr Shell 可以用于：

- 固件 bring-up；
- 人工查看状态；
- 临时诊断 BLE 或 USB；
- 在 BTP 不工作时辅助定位。

Shell 不是自动化测试的事实协议，自动化代码不得解析普通 Shell 日志来判断测试通过。

如果启用 Shell 或日志：

- 不得向 BTP 二进制流注入文本；
- 优先使用独立 UART、独立 USB CDC endpoint 或 RTT；
- 如果 PCA10059 资源不能可靠隔离，正式测试固件可以禁用 Shell 和普通串口日志。

### 3.3 Python Host 优先复用现有 BTP Client

Host 端优先直接复用固定 revision 的 AutoPTS `pybtp`，通过系统串口链路访问 nRF：

```text
Windows: COMx
macOS:   USB CDC 串口设备
Linux:   ttyACM/ttyUSB 类设备
```

Phase 0 先使用 AutoPTS 的现有 Core/GAP/GATT API 完成无 nrftest 协议封装的直接探针。证明其可脱离 PTS 用例运行后，正式代码只增加 Profile、Fixture、Telemetry 和 Recovery 薄适配，不重新实现 BTP framing、parser、response/event worker 或 Core/GAP/GATT codec。

AutoPTS 当前串口接入、`socat`、socket worker、全局 `Stack`/`IutCtl` 和 PTS 框架耦合必须通过固定 revision 的本地 Spike 核对，不能预先假定它是可直接安装的独立 SDK。

只有直接复用确实被耦合、平台或许可证阻塞时，才可以提出独立最小 Client。到达该边界必须停止并通知用户，修改计划并重新批准；不得一边实施一边静默重写 `pybtp`。

Host 不加载平台 Bluetooth API，不接管 USB HCI interface，也不依赖 `pc-ble-driver-py`、Blatann 或平台特定 BLE native wheel。

### 3.4 Zephyr Bluetooth Tester 是上游基线，不等于已完成产品

优先复用上游 Tester 的 Core、GAP 和 GATT BTP 实现。是否可以保持上游代码零修改，必须由硬件和语义验证决定。

禁止在验证前宣称：

- PCA10059 已可直接构建 Tester；
- 一个 USB CDC 通道足够；
- 动态数据库可无限次重建；
- BTP response 等于 BLE 对端已收到数据；
- 当前 Tester 已提供每条 Indication 的确认事件；
- Windows、macOS 和 Linux 已全部通过。

### 3.5 不发明第二套设备控制协议

如标准 BTP 足以满足测试，不增加项目私有 opcode。

只有当已记录的 BTP 缺口阻塞验收时，才可以提出最小扩展。任何扩展必须先提交计划变更，说明：

- 标准 BTP 为什么不能满足；
- 是否已有上游命令或待合并设计；
- 扩展的 service/opcode 命名空间；
- Host 与 firmware 的版本协商；
- 是否准备向上游贡献；
- 对兼容性和维护成本的影响。

未经批准不得直接实现私有 BTP 扩展。

---

## 4. 目标与非目标

### 4.1 目标

第一版应实现：

- PCA10059/nRF52840 Dongle 运行 Zephyr Bluetooth Tester 或基于它裁剪的固件；
- PC 通过 USB CDC/UART 与设备交换 BTP command、response 和 event；
- 同一个 Python Host API 在 Windows、macOS 和 Linux 上工作；
- BTP Core capability handshake；
- BTP GAP controller information、power、advertising 和 disconnect；
- BTP GATT 动态 Service、Characteristic、Descriptor、CCC 和初始值；
- BleHub Central 的 scan、connect、GATT discovery、read 和 write 测试；
- Central write 在 Peripheral 侧产生可判定的 attribute value changed event；
- Notification 和 Indication 的单包测试；
- 订阅、取消订阅及取消后静默测试；
- Peripheral 主动断连及恢复；
- 重复场景和 soak；
- 每次运行记录 firmware、BTP、硬件、Host 和 DUT 环境；
- 明确区分 Peripheral 控制结果和 BleHub Central 观察结果；
- 在通过 parity gate 后，替代 BleHub 日常测试中的 BLESS Peripheral。

### 4.2 非目标

第一版不做：

- 为夹具修改 BleHub 公共 API、production backend 或架构；
- 将 nRF 作为 BleHub Central controller；
- 让 BleHub 通过串口或 BTP 直接调用 nRF；
- 实现通用商业 BLE Peripheral SDK；
- 将 BTP 包装成面向普通应用开发者的跨语言 BLE API；
- 承担 Bluetooth SIG PTS 全量自动化；
- 复制或派生 AutoPTS 的 PTS 编排、MMI、XML-RPC、Windows PTS integration 或全 Profile 集合；
- 在未重新评审计划前自研另一套 BTP framing、session 或 Core/GAP/GATT Client；
- 覆盖所有 BTP service、LE Audio、Mesh 或 Classic Bluetooth；
- 替代 BleHub `FakeBackend` 的确定性 contract/conformance 测试；
- 替代 BlueZ/D-Bus、QEMU USB passthrough、guest reboot 等专项测试；
- 模拟任意 malformed ATT PDU、controller fault 或 RF 干扰；
- 把 nRF 测试结果推断为 BLESS、BlueZ、Android 或其他 provider 的结果。

---

## 5. 角色和拓扑

### 5.1 角色

- **外部 HIL Runner**：位于 `nrftest` 和 DUT 项目之外，协调两个独立进程并关联两侧证据；可以是人工操作、CI job 或独立编排器。
- **nrftest Host**：跨平台 Python 程序，优先复用 AutoPTS `pybtp`，只拥有 Profile 映射、fixture API、Telemetry 和 Recovery 等项目语义。
- **nRF BTP Peripheral**：PCA10059，运行 Zephyr Bluetooth Tester/BTP Server 和 BLE Peripheral Stack。
- **BleHub DUT Central**：被测试的 BleHub Central 实现。
- **Central Controller**：BleHub 所在平台实际使用的蓝牙 controller。

`nrftest` 不定位、配置、导入或启动 BleHub checkout，BleHub 也不定位、配置、导入或启动 `nrftest` checkout。BleHub 自己维护的通用 HIL consumer 只接受 DUT 测试参数，不得认识 BTP、串口、`nrftest` Profile 路径或 checkout。两者分别启动；外部 HIL Runner 只协调时序和报告，双方运行时唯一的数据通信是 BLE RF。

### 5.2 正确拓扑

```text
                              HIL Runner
                         /                  \
                        /                    \
             控制 Peripheral                 驱动/观察 DUT
                      /                        \
                     v                          v
          nrftest Python Host              BleHub Central
                     │                          │
                     │ BTP over USB CDC        │ Central API/backend
                     v                          v
           nRF52840 BTP Peripheral <──── BLE RF ────> Central Controller
```

更严格地表示为：

```text
Control/Telemetry Plane:
HIL Runner → nrftest Python Host → BTP serial → nRF Peripheral

DUT Control/Observation Plane:
HIL Runner → BleHub public test entry → BleHub events

BLE Data Plane:
BleHub Central controller ↔ BLE RF ↔ nRF Peripheral
```

不存在以下调用链：

```text
BleHub → nrftest Host → nRF
```

Control Plane 不得：

- 伪造 BleHub event；
- 调用 BleHub backend event ingress；
- 绕过 RF 回答 BleHub 的读写；
- 用 Python 的预期值代替 nRF callback/event；
- 把 BTP command success 当成 BleHub 已观察到结果。

---

## 6. 为什么选择 BTP

### 6.1 相对于 Nordic Bluetooth RPC

Nordic Bluetooth RPC 主要用于 nRF 多核 MCU 环境中的 Bluetooth API 转发。当前 PCA10059 是单核 nRF52840，PC 端也没有已确认可直接使用的官方通用 Python RPC Runtime。

BTP 的定位则是：

```text
测试控制端 → Bluetooth IUT/Tester
```

与本项目的自动化测试目标直接一致。

### 6.2 相对于纯 Shell

Shell 和 BTP 都可以运行在跨平台串口上，但 BTP 更适合自动化：

| 维度 | BTP | Shell |
|---|---|---|
| 目标用户 | 自动化测试程序 | 人工调试 |
| 编码 | 二进制结构化消息 | 文本命令和日志 |
| 消息边界 | header + length | prompt/换行/文本规则 |
| 响应与事件 | 明确区分 | 容易与日志交错 |
| capability discovery | 支持 | 通常依赖帮助文本/配置 |
| 动态 GATT | Tester 已实现基础命令 | Shell 命令未必是通用 Profile builder |
| Python 控制 | 优先复用 AutoPTS `pybtp` | 串口库直接发送文本 |
| 长期自动化稳定性 | 更合适 | 更易受输出格式变化影响 |

因此：

- BTP 是正式自动化路径；
- Shell 是辅助诊断路径；
- 二者共享 USB/UART 的跨平台优势，但不得共享一个会混入文本的 BTP 字节流。

---

## 7. 已核对的上游证据与待验证边界

### 7.1 当前已从上游源码确认

截至 2026-09-06，对 Zephyr `main` 的只读核对确认：

- `tests/bluetooth/tester/README.rst` 将 Tester 定义为面向自动化测试的二进制协议应用；
- README 明确写有 Tester 需要两个 serial port，第一路用于 BTP；
- `tests/bluetooth/tester/prj.conf` 启用了 `CONFIG_UART_PIPE` 和 `CONFIG_BT_GATT_DYNAMIC_DB`；
- `src/btp_core.c` 支持 capability、service register 和 unregister；
- `src/btp_gap.c` 支持 controller information、power、advertising、connect/disconnect 和 GAP events；
- `src/btp_gatt.c` 支持动态添加 Service、Characteristic、Descriptor、Included Service、Set Value 和 Start Server；
- 动态 Service 最终调用 `bt_gatt_service_register()`；
- remove 路径调用 `bt_gatt_service_unregister()`；
- Central 写入动态 attribute 时会产生 `BTP_GATT_EV_ATTR_VALUE_CHANGED`；
- 对带 CCC 的 attribute 执行 Set Value 时，当前实现会根据 CCC 状态调用 `bt_gatt_notify()` 或 `bt_gatt_indicate()`；
- AutoPTS 当前包含 Python `pybtp`，已有 BTP header codec、response/event worker 以及 Core、GAP、GATT command wrapper；
- `pybtp` 已有动态 GATT Server 所需的 add service/characteristic/descriptor、set value、start server、get attributes 和 remove handle 调用；
- AutoPTS 当前物理串口路径使用 serial → `socat` → Unix/TCP socket → BTP worker，而不是一个独立的 `pyserial` SDK；
- 当前 `pybtp` 仍与 AutoPTS 全局 `Stack`、MMI、`IutCtl`、board control 和测试框架存在耦合；
- AutoPTS 源码 header 和 `COPYING` 标示 GNU GPL v2，不能仅凭“测试用途”推断没有许可证义务。

官方入口：

- <https://github.com/zephyrproject-rtos/zephyr/tree/main/tests/bluetooth/tester>
- <https://github.com/zephyrproject-rtos/zephyr/blob/main/tests/bluetooth/tester/src/btp_gatt.c>
- <https://github.com/zephyrproject-rtos/zephyr/blob/main/tests/bluetooth/tester/src/btp_gap.c>
- <https://github.com/auto-pts/auto-pts>

这些在线结论只用于制定计划。开始实现时必须固定具体 Zephyr/AutoPTS revision，并优先在本地 checkout 中审计和构建，不能持续跟随未固定的 `main`。

### 7.2 当前 Tester 的已知约束

当前 GATT Tester 源码包含有限资源，例如：

```text
最多 10 个动态 Service
最多 50 个动态 Attribute
动态 server buffer 2048 bytes
最多 2 个 CCC tracking slot
单个 attribute value 最多 512 bytes
```

这些限制对于基础 BleHub profile 可能足够，但尚未通过实际 Profile 构建证明。

当前实现还表现出以下需要验证的边界：

- 动态 GATT 添加操作有顺序约束；
- service unregister 不等于动态分配空间已回收；
- `tester_unregister_gatt()` 当前不能视为完整 database reset；
- repeated profile rebuild 是否必须重启设备尚未证明；
- stock Set Value 的 BTP success 不等于 native send 已接受或 Notification/Indication 已被 Central 接收；Phase 3 project patch 只把 immediate native success/failure 纳入现有 BTP status，delivery 仍由 Central 证明；
- 当前 Indication callback 主要记录日志，没有对应的逐包 BTP confirmation event；Phase 3 已把它记录为明确可观测性边界而没有扩展 BTP；
- burst、逐包 pacing 和高吞吐 telemetry 不是当前通用 GATT Set Value 命令的明确保证；
- Tester README 的双串口要求如何映射到 PCA10059 尚未证明；
- stock `tests.yaml` 的 `platform_allow` 包含 `nrf52840dk/nrf52840`，但不包含 `nrf52840dongle/nrf52840`；
- Tester 的 `boards/` 目录有 nRF52840 DK 专用配置，但没有 PCA10059 专用 config/overlay；
- stock `prj.conf` 启用了大量与基础 Peripheral 无关的能力，可能需要裁剪后才能适合 nRF52840 的资源。

### 7.3 必须通过 Spike 回答的问题

1. 选定 Zephyr revision 是否存在并支持 PCA10059 对应 board target？
2. Tester 是否能在该 target 上编译、链接和运行？
3. 使用 nRF52840 integrated controller 时实际需要几路 serial？
4. BTP 能否通过 PCA10059 USB CDC ACM 稳定收发？
5. 哪个 AutoPTS revision 与选定 Zephyr Tester/BTP revision 匹配？
6. AutoPTS `pybtp` 的 Core/GAP/GATT API 能否脱离 PTS、MMI 和 XML-RPC 用例直接运行？
7. 当前 `socat` + socket transport 是否可在 Windows、macOS 和 Linux 上稳定控制串口，还是需要可上游化的 transport 解耦？
8. nrftest 能否只依赖 AutoPTS，而不复制或派生其 GPL 源码？项目内部使用、CI 使用和对外分发分别需要什么许可证措施？
9. Windows、macOS、Linux 是否都能无平台 BLE Runtime 地打开同一个 BTP transport？
10. 动态基础 Profile 是否满足 service/characteristic/CCC 数量和内存限制？
11. 同一固件 boot 中能否清理并重建 Profile？
12. Notification 的 Peripheral 侧可观测终点是什么？
13. Indication confirmation 是否需要扩展 BTP event？
14. 1000 包功能 soak 是否能通过标准命令完成？
15. throughput 测试是否会被 BTP serial 往返限制？
16. reset 后 USB 设备是否可按 serial identity 重新发现？

任何问题未验证时只能标记为未验证，不能推断为通过。若第 6 至第 8 项阻止直接复用，应停止并向用户报告可选路径，不得自动转入独立 BTP Client 实现。

---

## 8. 硬件、软件和版本基线

### 8.1 初始硬件

| 项目 | 初始选择 |
|---|---|
| Board | Nordic nRF52840 Dongle，PCA10059 |
| SoC | nRF52840，单核 |
| Device role | 完整 BLE Peripheral，而不是纯 HCI adapter |
| Physical connection | PCA10059 原生 USB；必要时外接 USB-UART adapter |
| BTP transport | 首选 USB CDC ACM 虚拟串口；必要时使用板级 UART |
| BLE Host | Zephyr Bluetooth Host，在 nRF52840 上运行 GAP/GATT Server |
| BLE Controller | Zephyr Bluetooth Controller/Link Layer，在同一 nRF52840 上驱动 Radio |
| Firmware control | Zephyr Bluetooth Tester/BTP Server |

这里的 `USB CDC ACM` 是通过 USB 枚举出的串行端口：

```text
Python Host
    → Windows/macOS/Linux serial API
    → USB CDC ACM virtual serial
    → nRF BTP Server
```

它不属于此前排除的直接 HCI USB 接管路径：

```text
PC Bluetooth stack/BTstack
    → libusb claim Bluetooth HCI interface
    → USB HCI controller
```

因此首选方案仍然是“Python 走串口控制 BTP”；只是在 PCA10059 上，这条串口的物理承载优先使用原生 USB CDC ACM。它不要求 Python 使用 libusb，也不把 nRF 的 HCI interface 暴露给 PC。macOS 对 CDC 枚举、打开、重置和重枚举仍须实机验证，不能仅根据架构推断通过。

nRF52840 内部确实承担 BLE Controller 角色，但同时也运行 Zephyr Bluetooth Host、GAP/GATT Server 和 BTP Server。从测试拓扑看，它是一台完整的 BLE Peripheral，而不是只向 PC 提供 HCI 的 controller adapter。BleHub Central 继续使用自己的 Central Controller；两块 controller 通过真实 BLE RF 通信：

```text
BleHub Host + Central Controller
            ↕ BLE RF
nRF Zephyr Host + Peripheral Controller/Radio
```

### 8.2 固件来源、构建与交付责任

截至 2026-09-06，上游提供的是可复用源码和协议实现，不是可直接交付给 PCA10059 用户的已验证固件：

- Zephyr Project 提供 `tests/bluetooth/tester` 的 Tester/BTP Server 源码、通用配置和构建机制，但 Zephyr release 不提供 PCA10059 专用的预编译 Tester HEX 或 DFU ZIP；
- AutoPTS 提供 BTP header/spec、`pybtp` 和 Host/client 代码，不提供 PCA10059 板级固件；
- stock Tester 的测试平台清单和 board-specific 配置覆盖 nRF52840 DK，不覆盖 PCA10059，因此不能把“上游有 Tester 源码”等同于“Dongle 已有现成可刷镜像”。

正式责任边界如下：

| 层次 | 责任方 |
|---|---|
| Tester/BTP Server 上游源码 | Zephyr Project |
| BTP header/spec 和 Host/client | AutoPTS |
| 固定 Zephyr/AutoPTS revision | `nrftest` 项目 |
| PCA10059 最小 config/overlay；Tester 修复的 fork 集成维护 | `nrftest` 项目 / `VIDLG/zephyr` 专用 fork |
| 可复现 firmware build/package recipe | `nrftest` 项目 |
| 硬件验证、image SHA-256、许可证记录和发布 | `nrftest` 项目维护者/CI |
| 向指定设备执行刷写 | 用户或硬件 CI，通过显式 `firmware-flash` recipe |

首个固件候选必须来自固定 Zephyr revision 的：

```text
tests/bluetooth/tester
    + board target nrf52840dongle/nrf52840
    + nrftest 维护的最小 PCA10059 config/overlay
```

默认刷写候选是保留 PCA10059 原厂 Nordic nRF5 SDK USB bootloader，并按照 Zephyr board 文档使用 nRF Util 的 `nrf5sdk-tools` command package 生成和刷入 DFU ZIP：

```text
nrfutil install nrf5sdk-tools
nrfutil nrf5sdk-tools pkg generate \
    --hw-version 52 \
    --sd-req=0x00 \
    --application build/zephyr/zephyr.hex \
    --application-version <VERSION> \
    tester.zip
nrfutil nrf5sdk-tools dfu usb-serial -pkg tester.zip -p <PORT>
```

这是 Phase 0 要实机验证的首选路径，不是尚未执行就宣称通过的结论。`nrfutil` 只属于显式 firmware package/flash 流程，不是 BTP Host 运行时依赖，也不得放入普通无副作用 `setup`。需要从空 Host 复现 DFU package 时，先从 `nrftest.local.example.toml` 创建本机 `nrftest.local.toml` 并显式选择安装路径，再使用包含受限 Nordic 工具安装的 `setup-firmware` 或 `firmware-reproduce`；两者都不刷写设备。Zephyr 此路径要求的是 `nrfutil nrf5sdk-tools`；不得将归档方案使用过的 `nrfutil device` 误当成同一个 command package。

当前固定入口为 nRF Util CLI `8.2.1` 和官方 `nrf5sdk-tools` command package `1.1.0`。该 command package 内部报告 `pc-nrfutil 6.1.7`，这是 Nordic 为旧 nRF5 SDK Secure DFU 格式保留的兼容引擎，不是项目主动回退到旧版全局 CLI。CLI 和 command package 均按 `LicenseRef-Nordic-1-Clause` 管理，只能用于 Nordic 芯片且禁止由本项目重新分发二进制；项目仅跟踪下载 URL、版本、commit、SHA-256、许可证政策和显式安装脚本。

MCUboot + `mcumgr` serial recovery 可以作为后续升级方案，但首次安装 MCUboot 仍需经过原厂 bootloader 或外部 probe。若使用外部 SWD/J-Link **刷写**，必须切换到与之匹配的 `nrf52840dongle/nrf52840/bare` 变体，并会改变 partition layout、放弃原厂 USB bootloader；Phase 4 仅通过已存在固件执行 J-Link target reset，不做 SWD erase/program，因此不改变当前 board variant、partition 或原厂 bootloader。任何刷写路径切换都必须显式记录 bootloader、board variant、partition layout、恢复方法和产物 identity，不能由 recipe 静默决定。

`firmware-build` 生成和验证 Zephyr ELF/HEX/BIN，并记录 Zephyr/toolchain revision、board variant、config/overlay、许可证、生成配置、地址范围和 SHA-256 的 build manifest；独立的 `firmware-package` 再生成与原厂 bootloader 匹配的 DFU ZIP 并把 package identity 写入 manifest。Nordic 内部兼容引擎会把当前时间写入 ZIP entry metadata，因此 `firmware-package` 在不改动 payload 的前提下将 entry 顺序和时间规范化为固定值 `1980-01-01T00:00:00`，再调用 Nordic 自己的 `pkg display` 重新解析验证。Windows 上该兼容引擎返回后曾短暂保留 ZIP 句柄，使同进程 `os.replace()` 报 `WinError 5`；正式入口现用顺序独立的 `generate` 与 `normalize-validate` Python 子进程建立句柄释放边界，外部 Just 命令保持不变。普通用户应使用 `nrftest` 发布的经验证产物或由同一固定 recipe 构建的产物，而不是自行猜测配置或临时从 Zephyr `main` 构建。未固定 revision、未记录 manifest 或未通过对应硬件门的镜像不得作为推荐固件。

当前活动目录的 `firmware/app/pca10059.conf` 明确设置 `CONFIG_TEST_LOGGING_DEFAULTS=n`、`CONFIG_LOG=n`、`CONFIG_CONSOLE=n` 和 `CONFIG_PRINTK=n`；fork 的 Tester `prj.conf` 不再强制 Debug log choice，正式 BTP CDC 不输出普通 logging/console 文本。断言仍启用但无 verbose 文本；调试日志必须使用独立配置和独立 transport。Phase 0 stock 基线为 Flash `393484 B`、RAM `95256 B`，HEX SHA-256 `4a1a79fc123082a3ec987df10e566b4432a401aeee0bda351f7a50d9dff20308`，DFU ZIP SHA-256 `d53fa63143ad862de23cff4dcc3af68e538f94feafa1fcadb37e295029b25326`；它保留为控制面和 stock 行为对照。

Phase 4 的 project-managed candidate 使用固定 upstream 加组合 patch；该 patch 及对应产物仅作为历史证据保留。当前默认 `firmware-build`、`firmware-package` 和 `firmware-flash` 指向 `VIDLG/zephyr` 固定集成 commit `f530afbe09cb3b3d96dee432676aaafd56a8a93d`，构建时不复制 Tester、不应用本地 patch；manifest 记录 repository、commit 和 `tester_patch=null`。集成内容保持 BTP wire 不变，包含 single-subscriber/native status 修复、Service 0-based slot 修正和容量保护。切换 fork 后必须重新生成并验证 HEX/DFU identity，旧 v4.4.2 产物和 RF 证据不能外推。历史 patch 位于 [`firmware/patches/archive/`](../firmware/patches/archive/)，迁移说明见 [`2026-09-11-zephyr-fork-integration.md`](research/2026-09-11-zephyr-fork-integration.md)，纯净配置验证见 [`2026-09-11-zephyr-fork-pure-configuration.md`](research/2026-09-11-zephyr-fork-pure-configuration.md)。

**nRF52840 DK（PCA10056）第二硬件路径（2026-09-18 实机验证通过）。** 这是与 PCA10059 主线并列的显式 board variant，不是主线的替换：`firmware-build-dk` 使用 `nrf52840dk/nrf52840`，应用从 `0x0` 链接、无原厂 bootloader（`BOARD_HAS_NRF5_BOOTLOADER`/`FLASH_LOAD_OFFSET` 约束不适用），镜像上限为完整 1MB flash；固件输入为 `firmware/app/pca10056.conf/.overlay`（内容对齐上游 Tester 的 `boards/nrf52840dk_nrf52840`，BTP 走 UART0 → 板载 SEGGER J-Link OB VCOM，115200 + hw-flow-control）。烧写不经 DFU：`firmware-flash-dk` 通过 pylink + 本机 `JLink_x64.dll`（64 位 DLL，32 位 `JLinkARM.dll` 配 64 位 Python 报 WinError 193）以 SWD 刷 `pca10056-tester` 的 `zephyr.hex`，刷前强制 manifest board/HEX SHA-256 校验与 `--confirm-sha256` 显式确认，产物 identity 记录于 build manifest 和 `firmware-flash-dk` telemetry 报告。恢复方法：重跑 `firmware-build-dk` + `firmware-flash-dk`，或 J-Link 全片擦除后刷任意合法镜像。DK 基线（2026-09-18，fork commit `f530afb`）：Flash `345200 B`（32.92%）、RAM `92480 B`、HEX SHA-256 `eb09c98dcc015e23e551944021d64f3dccd8f93ca836985659b9de8c6fc712d5`。Host 侧 USB 身份门禁参数化：`select_application_port` 增加 `transport`（`dongle`=2FE3:0004 / `dk`=J-Link OB VCOM 1366:1061），`btp-doctor`、`host-doctor`、`btp-gap-rf-fixture` 以 `--transport dk` 显式选择；DK 上 BTP 口为本机 COM11。DK 的 target reset 语义与 Dongle 不同（J-Link VCOM 在复位时不消失）：`btp-target-reset ... dk` 以"VCOM 身份稳定 + 全新 BTP Core/GAP 握手成功"为恢复门，替代 Dongle 的 USB 消失/重枚举观测；报告场景为 `target-reset-dk`。2026-09-18 实机验证：reset≈0.4s、BTP 重握手≈2.5s（1 次尝试），`btp-target-recover ... dk` 完整恢复链（reset → doctor → Profile 重建）通过。

官方入口：

- <https://github.com/VIDLG/zephyr/tree/main/tests/bluetooth/tester>
- <https://github.com/VIDLG/zephyr/blob/main/boards/nordic/nrf52840dongle/doc/index.rst>
- <https://github.com/zephyrproject-rtos/zephyr/tree/main/tests/bluetooth/tester>（上游参考）

### 8.3 版本固定规则

当前正式基线使用 `VIDLG/zephyr` 专用 fork，不使用 NCS 所携带的 Zephyr，也不在 nrftest 构建时应用本地 patch：

```text
Zephyr repository: https://github.com/VIDLG/zephyr.git
Zephyr commit:     f530afbe09cb3b3d96dee432676aaafd56a8a93d
Zephyr base:       upstream Zephyr main at fork creation (not a runtime pin)
Zephyr SDK:        1.0.1
GNU target:        arm-zephyr-eabi
AutoPTS repository:https://github.com/auto-pts/auto-pts.git
AutoPTS commit:    54e81c7f3495bce72e5f688e9c996b85b8272799
```

Zephyr、SDK 和 AutoPTS 固定值记录在 `upstream.lock.toml`；Windows portable Host 工具的版本、来源、下载 URL、SHA-256、安装内相对路径和许可证记录在 `host-tools.lock.toml`；Nordic nRF Util CLI 与 `nrf5sdk-tools` command package 的三平台下载、版本、commit、SHA-256、许可证和禁止重新分发政策记录在 `firmware-tools.lock.toml`。当前 Zephyr 固定为 `VIDLG/zephyr` commit `f530afbe09cb3b3d96dee432676aaafd56a8a93d`；fork 中已集成 nrftest 所需 Tester 修复，nrftest 默认不再应用本地 patch。切换 fork 后必须重新完成 build/package/flash、BTP Core、GAP/GATT 和 RF 验证；旧 upstream v4.4.2 结果只保留作历史对照。当前 Windows 机器保留已有 `E:\dev\v3.4.0` NCS workspace，但 setup 不读取或修改它的 Zephyr source。

Zephyr SDK installer 按执行主机选择基础包：Windows x64 使用 Windows x64 minimal，macOS Apple Silicon 使用 macOS AArch64 minimal，Linux x64 使用 Linux x64 minimal；三者再安装各自主机可执行、但目标均为 nRF52840 ARM 的 `arm-zephyr-eabi` 交叉编译器。普通 build/test 不自动安装，只有显式 setup recipe 执行下载。

实施前必须固定:

- Zephyr repository URL 和固定 commit；
- 如使用 NCS，固定 NCS manifest revision 和实际 Zephyr revision；
- Zephyr SDK/toolchain version；
- BTP header/spec 来源 revision；
- 与其匹配的 AutoPTS repository、revision 和取得方式；
- Python version 范围；
- AutoPTS、`socat`、serial/socket transport 及其他 Host dependency 版本；
- AutoPTS 和其他第三方组件的许可证、notice、源码提供与分发策略；
- firmware image 和可刷写 package 的 SHA-256；
- PCA10059 config/overlay revision；
- board variant、bootloader、partition layout 和 flash 方法；
- 如采用默认 DFU 路径，nRF Util CLI 和 `nrf5sdk-tools` command package version。

不能复制一份未标注 revision 的 BTP header 后长期独立演化。

### 8.4 Host 平台基线

第一轮跨平台门：

```text
Windows x64
macOS Apple Silicon
Linux x64
```

Linux ARM64 和 macOS x64 可在核心三平台通过后按实际需求追加，不能预先宣称支持。

### 8.5 BleHub DUT 平台

`nrftest` 应允许以下 BleHub Central 在各自实现可用后接入：

```text
Windows
Android
macOS
iOS
Linux
```

这不意味着所有平台在 `nrftest` Phase 0 就必须同时通过。每个平台都需要独立 RF 报告，且一个平台的结果不能证明另一个平台。DUT 项目可自行提供独立、参数化的 HIL consumer；`nrftest` 不构建、配置或启动这些入口。

### 8.6 Pixi 和 Just 是统一项目入口

活动主线继续使用 Pixi 管理跨平台开发环境，并由 Pixi 安装和固定 Just。开发者和 CI 统一通过以下形式运行项目命令：

```text
pixi run just <recipe>
```

职责边界：

- `pixi.toml`/`pixi.lock` 固定 Python、Just、pytest、格式化/静态检查工具以及能由 Pixi 可靠提供的 Host dependency；
- `justfile` 提供稳定、可发现的项目工作流入口和依赖顺序；
- 复杂的跨平台逻辑优先由 Pixi 环境中的 Python 脚本实现，Just 负责编排，不在 recipe 中堆叠难以维护的 Windows/POSIX shell 分支；
- 必须使用 PowerShell、udev、系统 driver、bootloader 或 vendor programmer 的操作放在明确的平台脚本和显式 recipe 中；
- West、Zephyr SDK/NCS、AutoPTS 和 firmware revision 继续由各自 manifest/lock 固定，不能只依赖某台机器当前安装状态。

初始 Pixi lock 至少覆盖：

```text
win-64
osx-arm64
linux-64
```

`osx-64` 和 `linux-aarch64` 只有在依赖可解析并经过对应验证后再加入支持矩阵。Pixi 能解析环境不等于硬件测试已通过。

计划中的稳定 recipe 包括：

```text
pixi run just require-local-config
pixi run just setup
pixi run just setup-firmware
pixi run just firmware-reproduce
pixi run just upstream-status
pixi run just setup-upstream-sources
pixi run just update-upstream-sources
pixi run just setup-zephyr-sdk
pixi run just verify-upstream
pixi run just host-tools-status
pixi run just setup-host-tools
pixi run just verify-host-tools
pixi run just firmware-tools-status
pixi run just setup-firmware-tools
pixi run just verify-firmware-tools
pixi run just platform-provisioning-status
pixi run just setup-platform-provisioning
pixi run just verify-platform-provisioning
pixi run just toolchain-paths
pixi run just check-tools
pixi run just fmt
pixi run just lint
pixi run just test
pixi run just profile-check
pixi run just firmware-build
pixi run just firmware-package
pixi run just firmware-flash <明确设备选择参数>
pixi run just btp-doctor <可选端口/设备选择参数>
pixi run just btp-gap-rf-fixture <显式硬件参数>
```

具体 recipe 可在实施时根据已验证工具调整名称或拆分，但必须保持以下原则：

- 普通 build、test、lint 和 doctor 不静默下载 SDK、安装 driver、修改 udev、请求 UAC/sudo、接受许可证或刷写设备；
- `setup`、`setup-firmware`、`firmware-reproduce`、`setup-upstream-sources`、`setup-zephyr-sdk`、`setup-host-tools`、`setup-platform-provisioning`、平台权限配置和 flash 是显式动作；安装/build/package/flash 入口必须先通过 `require-local-config`，`firmware-reproduce` 可以联网安装固定依赖并完成 build/package，但绝不刷写设备；只有 `setup-platform-provisioning` 可以请求 UAC/sudo，`setup-firmware` 不隐式进入该 recipe；
- flash 必须显示目标 board、hardware serial/port 和 image identity，不凭“第一个设备”选择；
- hardware test 不混入普通 unit test，必须通过显式 recipe 触发；
- 跨平台 recipe 的语义一致，平台限制应明确失败或 skip，不能静默执行不同测试。

### 8.7 本机配置和工具解析

跟踪 `nrftest.local.example.toml`，开发者必须先复制为被版本控制忽略的 `nrftest.local.toml`，并显式选择 Zephyr、SDK、AutoPTS 和 Host tools 安装根目录；项目不得为这些大体积或许可证受限资产猜测固定磁盘或用户缓存路径。缺少本机配置时，安装/build/package/flash Just 入口必须在下载或修改文件前明确失败。CI 可通过 `NRFTEST_CONFIG` 指向由 CI 管理的同结构配置文件。本机值按以下优先级解析：

1. 显式 Just/CLI 参数；
2. `NRFTEST_*` 环境变量；
3. `nrftest.local.toml`；
4. 无歧义且非敏感的默认值。

本机配置只保存机器相关内容，例如：

- Zephyr/NCS、AutoPTS 和 SDK checkout/root；
- programmer、`socat` 或其他外部工具路径覆盖；
- 可选外部 J-Link native library 路径和 debugger hardware serial；
- PCA10059 hardware serial/port selector；
- build、cache 和报告输出目录。

版本、repository URL、commit、下载 URL、SHA-256、许可证政策和 Profile 定义必须保存在跟踪的 manifest/lock/源码中，不能藏在本机配置。`toolchain-paths` 和 `check-tools` 必须打印最终解析来源、路径和版本，但不得输出 secret。

如果某个平台无法由 Pixi 提供 `socat`、Zephyr SDK 或 programmer，项目必须通过显式配置和 doctor 报告外部前置条件；普通命令不得自行安装系统软件。

Zephyr SDK `1.0.1` 当前不提供 Windows host-tools bundle。CMake、Ninja、Git、West、7-Zip 和 `gperf` 由 Pixi 管理；DTC 采用按宿主平台分流但入口一致的方案：

- Windows：`setup-host-tools` 读取 `host-tools.lock.toml`，下载固定的 portable DTC `1.6.1` ZIP，先验证 SHA-256，再安全解压到本机配置的 `host_tools_root`；当前安装路径是 `E:\dev\nrftest-upstream\host-tools\dtc-1.6.1\usr\bin\dtc.exe`。脚本验证精确版本和 Zephyr 要求的最低版本 `1.4.6`，不读取或依赖已有 NCS 工具目录；
- macOS Apple Silicon/Linux x64：DTC 由对应 Pixi target dependency 提供，`setup-host-tools` 不下载系统包，只验证 Pixi 环境中的可执行文件及最低版本；
- `host-tools-status` 只报告状态，`verify-host-tools` 只验证，只有显式 `setup-host-tools` 可以下载/安装 Windows portable 工具。

DTC 只是 Host 端 devicetree 编译工具，不改变 upstream firmware source。下载缓存路径和安装根目录是机器本地配置；版本、URL、SHA-256、安装内相对路径和许可证是跟踪的 lock 数据。普通 build/test 不静默下载 DTC，也不假设任意机器预装 NCS、系统 DTC 或固定盘符。

当前默认原厂 bootloader + USB CDC `nrf5sdk-tools dfu usb-serial` 路径不需要 J-Link；Host runtime、系统 provisioning 和连接设备的功能证据必须分开：

- Windows：VC++ Runtime 由 `pixi.toml` 的 `win-64` 直接依赖和联合 `pixi.lock` 管理，不再查询注册表、下载或安装全局 VC Redistributable；`platform-prerequisites.lock.toml` 只固定 Nordic nRF device-lib installer 的 URL/SHA-256，由显式 `setup-platform-provisioning` 请求 UAC。device-lib 覆盖 `nrfutil device`/恢复及未经配置的新 Host；它是否是 `nrf5sdk-tools usb-serial` 的硬依赖仍由实机验证确认；
- Debian 系 Linux：固定 `nrf-udev 1.0.1` `.deb` 及 SHA-256，由显式 provisioning recipe 请求 sudo 安装；其他发行版必须明确失败并由用户提供等效 udev/串口权限配置；
- macOS Apple Silicon：当前不安装额外系统包，只检查 USB CDC 串口和重枚举；
- SEGGER J-Link driver 仅属于外部 SWD/J-Link 或 `/bare` 恢复路径，不进入默认 `setup-firmware`。

`setup-firmware` 只准备 Zephyr/SDK/Host tools 和 nRF Util/`nrf5sdk-tools`，不隐式修改系统。`platform-provisioning-status`/`verify-platform-provisioning` 只报告或验证受管 driver/udev 安装来源；Windows receipt 只证明固定 installer 成功退出，不能证明连接设备功能，也不能用 receipt 缺失否定一台已通过 `host-doctor` 或实际 `firmware-flash` 的机器。旧的 `*-platform-prereqs` recipe 名仅作为兼容 alias。普通 build/test/doctor 不静默提权。

---

## 9. 目标软件架构

计划中的活动源码结构如下；在对应 Phase 获批前不提前创建空模块：

```text
nrftest/
├── pixi.toml                     # 跨平台项目环境和固定工具
├── pixi.lock                     # win-64/osx-arm64/linux-64 联合锁
├── justfile                      # 所有开发/构建/硬件流程的稳定入口
├── pyproject.toml                # pytest/Ruff 活动源码配置
├── pyrightconfig.json            # 编辑器使用 Pixi default environment
├── nrftest.local.example.toml    # 可复制的本机路径/设备配置模板
├── upstream.lock.toml            # Zephyr/SDK/AutoPTS 精确版本和来源
├── host-tools.lock.toml          # Windows portable Host 工具版本、URL、SHA 和许可证
├── firmware-tools.lock.toml      # Nordic DFU 工具三平台版本、URL、SHA 和许可证
├── platform-prerequisites.lock.toml # 系统 driver/udev 版本、URL、SHA 和许可证
├── tools/                        # Pixi Python 下运行的跨平台编排脚本
│   ├── config.py                 # 配置优先级、路径和设备选择
│   ├── setup_upstream.py         # 安全、幂等的 source/SDK setup 与验证
│   ├── setup_host_tools.py       # 按宿主平台安装/验证 DTC
│   ├── setup_firmware_tools.py   # 显式安装/验证 Nordic DFU 工具
│   ├── setup_platform_prerequisites.py # 显式安装/验证 driver/udev provisioning
│   ├── build_firmware.py         # 固定 Tester build、配置/地址审计和 manifest
│   ├── package_firmware.py       # 可重复 DFU package 和 Nordic parser 复验
│   ├── flash_firmware.py         # 双重显式选择 port/package identity 后刷写
│   └── ...                       # doctor 所需最小脚本
├── docs/
│   ├── PLAN.md
│   ├── research/
│   └── testing/reports/
├── archive/
│   └── connectivity-blatann-2026-09-06/
├── firmware/
│   ├── package.toml              # unsigned 开发/HIL DFU package 固定参数
│   └── app/
│       ├── pca10059.conf         # bootloader、USB/BTP、日志和 identity 配置
│       └── pca10059.overlay      # 将现有 CDC ACM 绑定为 zephyr,uart-pipe
├── host/
│   └── nrftest/
│       ├── autopts_adapter.py    # pybtp 生命周期和最薄兼容边界
│       ├── profile.py           # Profile → pybtp dynamic DB 调用/handle 映射
│       ├── fixture.py           # HIL 使用的高层 Peripheral API
│       ├── telemetry.py         # Peripheral 侧结构化证据
│       ├── recovery.py          # reset/re-enumeration/reopen
│       └── cli.py               # doctor 和人工控制入口
├── profiles/
│   └── blehub-nrf-basic-v1.json
└── tests/
    ├── unit/
    ├── protocol/
    └── hardware/
```

这只是目标边界，不要求严格使用以上每一个文件。实现应保持最小、可审计，不为简单字段拆分过多模块。默认不创建 `framing.py`、`session.py`、`core.py`、`gap.py` 或 `gatt.py`；这些职责属于复用的 AutoPTS `pybtp`。若最终不得不拥有其中任何职责，必须先停止并重新评审计划。

---

## 10. Host BTP 复用与薄适配设计

### 10.1 复用优先顺序

Host 按以下顺序决策：

1. 使用固定 revision 的 AutoPTS `pybtp` 直接调用 Core/GAP/GATT API；
2. 只增加隔离 AutoPTS 生命周期、全局状态和内部命名的极薄 adapter；
3. 优先向上游推动或复用可独立安装的 `pybtp` package/transport 解耦；
4. 只有前三项被实证的耦合、平台或许可证问题阻塞时，才提出独立最小 Client。

第 4 项不是本计划已批准的实施内容。触发时必须停止、记录证据和备选方案，并由用户重新批准。不得复制 GPL `pybtp` 源码后改名为 nrftest 实现，也不得发明另一套设备协议。

### 10.2 Phase 0 直接探针

Phase 0 不先设计 nrftest Client abstraction。使用固定 AutoPTS checkout 的原始 API，完成：

```text
transport 启动
→ BTP Core handshake
→ capability discovery
→ GAP controller information/power
→ 最小 advertising
```

该探针用于验证“现成 API 能操作设备”，可以直接依赖 AutoPTS 的初始化方式和低层函数，不承诺成为最终公共 API。只有探针通过后才编写薄 adapter。

### 10.3 薄 adapter 的职责边界

`autopts_adapter.py` 只允许负责：

- 建立和释放固定 revision 所要求的 AutoPTS transport/worker；
- 隔离 AutoPTS 全局 `Stack`、`IutCtl` 或内部模块命名；
- 将已存在的 Core/GAP/GATT 调用暴露给 Profile/Fixture 层；
- 将现有 response/event 转交 Telemetry；
- 将 AutoPTS 异常归入 nrftest 的错误分类；
- 在单个 HIL session 内维持明确的生命周期和命令所有权；
- 区分 Host transport 与 nRF BTP service 生命周期：先用现有 `READ_SUPPORTED_COMMANDS` 探测常驻 handler，存在时 attach，明确 fresh boot 时才 register。

它不重新编码 BTP payload，不复制 parser，不重新定义 opcode，不实现平行的 response/event worker。Profile role 到动态 handle 的映射属于 `profile.py`，测试场景语义属于 `fixture.py`。

### 10.4 Transport、消息和并发验证

当前 AutoPTS 使用 serial → `socat` → socket → BTP worker。Phase 0/1 必须验证：

- Windows、macOS 和 Linux 上所需 `socat` 能否可靠取得、启动、停止和报告错误；
- COM/tty 参数、波特率和 raw mode 是否正确；
- reset/re-enumeration 后能否重建 transport；
- BTP 二进制流不会混入普通日志；
- response 前后到达的异步 event 不会丢失或错误相关；
- AutoPTS 是否保证同一 session 最多一个 pending command，或 nrftest adapter 是否需要在调用入口串行化。

BTP header 没有调用方生成的通用 request ID。nrftest 不得并发发送无法可靠相关的命令。如果发现 AutoPTS parser/session 缺陷，应优先固定上游 revision、提交最小上游修复或维护经批准的明确 patch，不得静默实现第二个 Client。

### 10.5 Timeout 和错误分类

Host 负责场景 timeout 和恢复策略。错误至少分类为：

- `invalid_request`：Profile、参数或调用顺序错误；
- `protocol`：非法 BTP frame、unexpected response、版本/能力不匹配；
- `environment`：设备、USB、权限、串口、`socat`、flash 或 firmware 不可用；
- `upstream_client`：AutoPTS 初始化、内部状态或兼容性失败；
- `capability_skip`：固定版本明确不支持所需能力；
- `scenario`：环境和控制面正常，但 RF/DUT 结果不符合预期；
- `not_executed`：未运行。

### 10.6 对外 API

测试代码使用高层 Fixture API，不直接构造裸 BTP payload，也不依赖 AutoPTS 的全局对象和 handle 细节：

```python
fixture.doctor()
fixture.load_profile(profile)
fixture.start_advertising()
fixture.set_value(role, payload)
fixture.disconnect_peer()
fixture.wait_for_write(...)
fixture.reset()
```

该 API 可以是同步 Python API。是否增加异步包装由实际 HIL runner 决定，不在没有使用方证据时引入 runtime 或复杂并发框架。

可以提供 CLI 方便人工和外部 runner 调用，但 CLI 只是 Host 入口；设备上的最终控制协议仍是 BTP，不再定义第二套设备协议。

### 10.7 AutoPTS 许可证边界

AutoPTS 当前源码标示 GNU GPL v2。测试用途不自动构成许可证例外，内部执行、CI 使用、复制源码、修改、打包和向用户分发是不同场景。本计划不作法律结论，但要求：

- Phase 0 可以使用用户提供的固定本地 checkout 做内部技术验证；
- 在仓库内复制、vendor、patch 或派生 AutoPTS 源码前，先记录许可证影响并获得批准；
- 在发布 Host package、CI image 或可再分发测试套件前，确认 GPL 对应的 source、notice、修改说明和分发方式；
- 若项目不接受相应义务，先评估独立进程边界、上游拆包或其他已有兼容 Client，不能通过换名复制规避许可证。

许可证核对是发布门，不阻止在不对外分发的本地 Spike 中验证技术可行性。

---

## 11. Firmware 设计和上游策略

### 11.1 最小固件原则

固件只启用本项目需要的能力：

- BTP Core；
- GAP Peripheral；
- GATT dynamic database；
- Notification/Indication；
- 必要连接和安全能力；
- BTP UART/USB transport；
- 必要的 reset/recovery 支持。

不因为 stock Tester 支持大量 LE Audio、Mesh、OTS、Central 或 ISO 功能就全部启用。裁剪必须保留与上游 Tester 可对照的配置和理由。

### 11.2 上游优先级

按以下顺序决策：

1. 原样构建上游 Tester；
2. 只通过 Kconfig/overlay 裁剪；
3. 建立项目 app，复用上游 Tester 源；
4. 维护最小 patch；
5. 最后才考虑私有 BTP 扩展。

每下降一级都必须记录为什么上一级不可行。

Phase 3 已按该顺序执行到第 4 级：原样 Tester 的 Notification 首轮通过，但 Indication 的 `bt_gatt_indicate(NULL, ...)` 在固定单连接实机上同步失败，且 Set Value 路径丢弃 allocation/native send status；只修 Indication 后，跨模式复跑又暴露 Notification `conn=NULL` failure。Kconfig/overlay 不能修复该 C 逻辑，另建完整 project app 会复制大量 Tester 实现。因此先保留历史对照补丁 [`firmware/patches/archive/zephyr-v4.4.2-tester-update-single-subscriber.patch`](../firmware/patches/archive/zephyr-v4.4.2-tester-update-single-subscriber.patch)，并已将最终修复移植到 `VIDLG/zephyr` 的集成 commit；nrftest 默认构建不再应用 patch。fork 集成保持两种 update 都显式选择唯一模式匹配连接，不进入第 5 级私有 BTP 扩展。补丁原因、四组固件对照和旧版 RF 证据见 [`docs/research/2026-09-09-phase3-notification-indication-rf.md`](research/2026-09-09-phase3-notification-indication-rf.md)。

Phase 4 发现的 stock Tester 问题是：第一个 Service 注册索引错误，且最大 Service 数量存在越界风险。修复已集成进 `VIDLG/zephyr` commit `f530afbe09cb3b3d96dee432676aaafd56a8a93d`；它不试图回收 Tester append-only backing state，也没有新增私有 BTP opcode。v4.4.2 的一次 removal/rebuild RF 证据仍见 [`docs/research/2026-09-09-phase4-database-lifecycle-and-reset.md`](research/2026-09-09-phase4-database-lifecycle-and-reset.md)，fork 候选必须重新验证。

### 11.3 BTP 与日志隔离

BTP transport 上只允许 BTP frame。

调试信息优先级：

1. RTT；
2. 独立 UART/CDC channel；
3. 构建时关闭普通日志；
4. 不允许把文本日志混入 BTP stream 后由 Host 猜测过滤。

### 11.4 动态数据库生命周期

正式生命周期必须验证：

```text
MCU boot
→ register Core/GAP/GATT 一次
→ 按 JSON provision 一个固定 Profile
→ 后续 Host transport/session 识别 resident handlers 并 attach
→ 重新核对 UUID/role/handle mapping
→ 按 JSON 恢复 Characteristic 初值
→ advertise/run
→ disable/disconnect/stop/close transport
→ 下一轮重新 attach，不 remove/rebuild database
```

截至 2026-09-09，一次 Profile A removal 后构建不同 Profile B 已通过，但固定 Tester 的 attributes、Service slots、value buffer 和 CCC bookkeeping 仍不回收；Host 自动分配的旧 handles 被清零后，标准 BTP `Set Value` 仍以 `server_db[0].handle` 计算数组偏移，无法再可靠控制新 Profile B。因此该实验只保留为 one-shot 能力边界证据，不作为普通 testcase；实验后必须在 suite 边界重新启动 MCU，不能直接继续日常 RF 场景。

经用户批准，正式 BleHub 生命周期在一个 MCU boot 内只使用一个固定 Profile；每轮独立清理 connection、CCC、advertising 和 Host transport，并重新恢复初值、核对实际 mapping。确需切换 topology 时，在 suite/设备边界使用另一根已 provision 的设备，或执行显式人工/可选自动 target reset。外部 J-Link/SWD 薄层是灾难恢复后端，不是普通 testcase 或 Phase 4 退出门。是否增加 `reset database` 或私有 reboot 扩展仍属于计划变更，不得在实现中偷偷加入。

---

## 12. Profile 设计与 BTP 映射

### 12.1 复用归档 Profile schema v1

归档中的：

```text
archive/connectivity-blatann-2026-09-06/profiles/nrf52840-basic-gatt-v1.json
```

已经把 BleHub 基础 Peripheral 的平台无关语义声明为 JSON。新主线保留该 schema v1 和语义，不让 Profile 绑定 Blatann 或 BTP：

| JSON 字段 | 新主线职责 |
|---|---|
| `schema_version` | Host schema 兼容和迁移门 |
| `profile_id` | provider/run identity 和报告关联 |
| `advertising.local_name` | 映射到 BTP GAP advertising data |
| `advertising.require_local_name` | Host/BleHub 场景断言策略 |
| `advertising.service_uuids` | 映射到 advertising service UUID data |
| `services[].uuid/primary/advertise` | 映射到动态 GATT Service 和 advertising 选择 |
| `characteristics[].role` | 稳定逻辑名称，映射动态 value/CCC handle |
| `characteristics[].uuid` | 映射到动态 Characteristic UUID |
| `characteristics[].properties` | 映射 BTP property/permission bit；v1 使用无安全要求的最小权限政策 |
| `characteristics[].initial_value_hex` | 解码后通过现有 BTP Set Value 设置 |

实施时将该 Profile 作为有来源记录的语义基线迁移到活动目录：

```text
profiles/blehub-nrf-basic-v1.json
```

不得从 `archive/` 运行时 import 或直接作为测试输入。活动 Profile、validator、semantic signature 和 BTP 映射必须在新主线重新测试；归档的 Blatann 通过结果不证明 BTP 映射正确。

schema v1 第一版继续保持一个 advertised primary root service 和现有四个 role，不为通用性提前扩展。当前 JSON 没有显式 Descriptor、security permission、maximum length 或 included service：

- 普通 read/write permission 可由 v1 properties 按已记录规则派生；
- notify/indicate 所需 CCC 是由薄层按固定 Tester/AutoPTS revision 显式添加还是由上游 helper 生成，必须由 Phase 2 源码审计和实测决定；
- 旧 Blatann 的 `max_length`、`variable_length`、`prefer_indications` 等 provider policy 不得未经验证原样搬到 BTP；
- 2026-09-08 经用户明确批准，active canonical Profile 的 `write-only.initial_value_hex` 设为 `"00"`，为固定 Tester 分配 1-byte value buffer；这仍是 schema v1，不引入隐式 provider 默认值，Phase 2 只验证等长 1-byte 写；
- 只有真实验收场景需要新增声明能力时才设计 schema v2，并保留 v1 兼容测试。

### 12.2 Provider identity

nRF provider 使用独立 identity：

- 独立 `profile_id`；
- 独立 local name；
- 独立 advertised/root service UUID；
- 与其他 provider 语义对齐但 UUID 不冲突；
- Central 使用 provider-specific service filter；
- 不依赖 RSSI、发现顺序或固定 Bluetooth address 选择设备。

### 12.3 基础 GATT role

第一版至少包含：

| Role | Properties | 用途 |
|---|---|---|
| `read-write` | read/write/write without response | 读、写、确定性回读和 write ledger |
| `updates` | read/notify/indicate | Notification、Indication 和取消订阅后静默 |
| `read-only` | read | 固定信息和只读路径 |
| `write-only` | write/write without response | 写入和无响应写压力 |

### 12.4 Profile 到 BTP 的构建过程

```text
validate profile
→ Core capability handshake
→ register GAP/GATT BTP services
→ configure controller
→ Add Service
→ Add Characteristic
→ Add Descriptor/CCC
→ Set initial values
→ Start Server
→ query/record actual handles
→ start advertising
```

Profile builder 必须记录逻辑 role 到实际 attribute handle 的映射，不能假定句柄永远固定。

### 12.5 Profile 校验

至少校验：

- schema/version；
- UUID 类型和唯一性；
- characteristic properties；
- read/write permissions；
- notify/indicate 与 CCC 配置一致；
- 初始值大小不超过当前 Tester 限制；
- service/attribute/CCC 数量不超过固件 capability；
- provider identity 不与 Android、QEMU/BLESS 等 fixture 冲突；
- semantic signature 和 source revision/checksum。

---

## 13. Peripheral 事实与结果语义

### 13.1 三类事实必须分开

```text
Host/BTP fact:
BTP command 是否发送、收到 success/failure response

Peripheral BLE fact:
Zephyr callback/event 是否表示连接、写入、断开或确认

BleHub DUT fact:
BleHub 是否观察到 scan/connect/read/write/update/disconnect 结果
```

任何一类都不能替代另一类。

### 13.2 Notification

至少区分：

- BTP Set Value response；
- nRF 是否处于已订阅状态；
- Zephyr 是否接受 notify 调用；
- BleHub 是否收到对应 payload；
- burst ledger 是否 missing、duplicate、out-of-order 或 foreign-batch。

如果标准 Tester 不提供逐包发送完成事件，报告必须如实标记 Peripheral 侧可观测终点，不能把 BTP success 命名为 `notification_delivered`。

### 13.3 Indication

严格 Indication 测试要求：

- BleHub 以 Indication 模式写入 CCC；
- nRF 发出一个 payload；
- BleHub 收到该 payload；
- 下一条 Indication 不得在前一条仍未完成时无约束发送；
- Peripheral confirmation 的证据边界必须明确。

当前上游 Tester 的 indication callback 主要写日志。Phase 3 必须决定：

1. 仅依靠单条/保守 pacing 和 BleHub 接收事实是否足以满足基础测试；或
2. 是否需要新增结构化 BTP confirmation event。

第 2 项属于协议扩展，必须单独批准。

### 13.4 Central write ledger

Central 对动态 attribute 的 write 应由 `BTP_GATT_EV_ATTR_VALUE_CHANGED` 或等价结构化事件记录：

- connection/peer identity（在协议可得范围内）；
- attribute handle 和逻辑 role；
- write payload；
- Host 接收 sequence；
- test batch/sequence payload 解码结果。

Write with response 和 write without response 的 Central 终点不能只靠 Peripheral event 区分时，必须结合 BleHub 命令类型和两侧时间线报告，不能伪造 Peripheral 不提供的语义。

---

## 14. 生命周期和状态机

目标状态机：

```text
Absent
  ↓ serial discovery
TransportReady
  ↓ BTP Core handshake
BtpReady
  ↓ probe GAP/GATT command handlers
  ├─ resident and responsive → attach
  └─ explicit unknown-command on fresh boot → register once
ServicesReady
  ↓ dynamic database build
ProfileReady
  ↓ advertising start
Advertising
  ↓ Central connects
Connected
  ├─ read/write/update events
  ├─ disconnect → ProfileReady/Advertising
  ├─ logical reset → restore values; retain resident Profile
  └─ USB/firmware fault or requested topology change → Recovering
NormalClose
  ↓ stop active work; retain BTP services; close Host transport
Absent
Recovering
  ↓ release Host transport + rediscover + handshake + validate resident Profile
  ├─ known Profile responsive → BtpReady/ProfileReady
  └─ unknown/dead target → fail with optional external reset or manual recovery
```

要求：

- 当前固定 AutoPTS 使用 Host-global socket/全局状态，因此整台 Host 同时只允许一个 AutoPTS session；在上游 socket/port 隔离得到验证前不得仅按 COM/tty 放宽，多设备并发另行设计；
- 同一 session 同时只允许一个 BTP command 等待 response；
- stock GAP/GATT service 在同一 nRF boot 内常驻，正常关闭只停止活动并释放 Host transport，不调用不完整的 upstream service unregister；
- 新 Host session 必须先通过现有 service `READ_SUPPORTED_COMMANDS` 识别 resident handler；成功则 attach，明确 unknown-command 才视为 fresh boot 并 register，timeout 不得盲目 register；
- `close` 和 recovery 幂等；
- transport failure 不伪装成 scenario failure；
- 普通 testcase 不执行 database remove/rebuild 或 target reset；同一 boot 复用一个已验证 Profile；
- 每轮 attach 后按 JSON 恢复 Characteristic 初值，并重新验证实际 Profile mapping；
- 若执行可选 USB/target reset，必须按硬件 serial identity 恢复，不依赖旧 COM/tty 路径；
- recovery 后重新执行 capability handshake；
- 不跨连接复用旧 connection、CCC 或 pending command；BleHub 每次重新 discovery，nrftest 每次重新核对实际 handle mapping；
- Host 进程退出允许留下可被下一 session 主动识别的常驻 BTP service，不得留下无法识别的隐式状态。

---

## 15. 测试场景

### 15.1 `doctor`

验证：

```text
发现目标设备
→ 打开 serial
→ 读取 BTP Core capability
→ 注册/查询必要 service
→ 读取 controller information
→ 关闭 session
```

输出至少包含：

- board/USB serial；
- port；
- firmware build identity；
- Zephyr revision；
- BTP definition revision；
- supported services/commands；
- Host OS/Python/dependency versions。

### 15.2 `basic`

```text
build profile
→ advertise
→ BleHub scan/filter
→ connect
→ discover GATT
→ read
→ write with response
→ write without response
→ compare Peripheral events and BleHub observations
→ disconnect
```

### 15.3 `notifications`

```text
connect
→ enable Notification
→ prove CCC state/effect
→ send one deterministic payload
→ verify BleHub event
→ send paced sequence
→ verify ledger
→ disable
→ attempt/update after disable
→ verify stale-route silence
```

### 15.4 `indications`

```text
connect
→ enable strict Indication
→ send one deterministic payload
→ verify BleHub event
→ establish Peripheral confirmation evidence boundary
→ send paced sequence
→ verify ordering and no overlap violation
→ disable
```

### 15.5 `passive_disconnect`

```text
connect and subscribe
→ trigger a documented Peripheral-side connection loss through standard BTP GAP
   ├─ direct DISCONNECT where the fixed Tester can resolve the peer
   └─ SET_POWERED(false) radio-stack fault when direct DISCONNECT fails
→ BleHub observes passive Disconnected with command_id=None
→ if powered off, SET_POWERED(true)
→ a new Host validates BTP and the same resident Profile
→ restore JSON initial values and advertise again
→ BleHub reconnects and performs fresh discovery
```

必须记录实际 trigger，不能把 `SET_POWERED(false)` 写成逻辑 GAP disconnect。不得为此新增私有 BTP opcode。

### 15.6 `profile_rebuild`

该场景只保留为 Tester 能力边界实验，不属于 BleHub 日常 testcase 或 Phase 4 正常生命周期门：

```text
Profile A build/run
→ clean stop
→ one-shot standard BTP removal
→ distinct Profile B build/local verification
→ record append-only resource boundary
→ do not continue repeated rebuild in the same boot
```

普通测试使用一个 boot-time provisioned Profile；确需切换 topology 时使用另一根已 provision 的设备，或在 suite 边界执行显式人工/可选自动 target reset。

### 15.7 `stress`

至少覆盖：

- 10 次 scan/connect/disconnect；
- 1000 个 Notification payload；
- 1000 个 Indication payload，在 confirmation 语义可证明后执行；
- 多批 write ledger；
- Host process restart；
- unknown/dead target 的 fail-fast，以及已配置时的可选 reset/re-enumeration recovery；
- disable 后无 stale update；
- 每批 missing、duplicate、out-of-order、out-of-range、foreign-batch 和 malformed 检查。

### 15.8 throughput 与功能 soak 分离

BTP serial command rate可能限制由 PC 逐包触发的吞吐。必须分别报告：

- 功能正确性和 lossless soak；
- BTP control-plane rate；
- BLE application-boundary throughput；
- BleHub Central 侧观察 rate。

不能把 BTP 串口瓶颈解释为 BLE controller 性能，也不能在没有设备侧 burst 支持时声称完成最大吞吐测试。

如果吞吐验收确实需要 device-local burst generator，应先提出最小 BTP 扩展计划并经批准。

---

## 16. 跨平台验证矩阵

### 16.1 nrftest Host

每个平台独立验证：

| 能力 | Windows x64 | macOS Apple Silicon | Linux x64 |
|---|---:|---:|---:|
| USB 枚举 | 必须 | 必须 | 必须 |
| serial identity discovery | 必须 | 必须 | 必须 |
| BTP Core handshake | 必须 | 必须 | 必须 |
| boot-time Profile provision + resident attach | 必须 | 必须 | 必须 |
| runtime value restore | 必须 | 必须 | 必须 |
| advertise/connect | 必须 | 必须 | 必须 |
| read/write telemetry | 必须 | 必须 | 必须 |
| Notification/Indication | 必须 | 必须 | 必须 |
| optional reset/re-enumeration backend | 可选能力 | 可选能力 | 可选能力 |
| repeated resident lifecycle | 必须 | 必须 | 必须 |

Linux 权限、Windows driver 和 macOS USB CDC 行为属于各自环境门，不得由其他平台结果代替。

### 16.2 BleHub DUT

每个 DUT 平台独立保存：

- BleHub candidate revision/artifact；
- OS 和版本；
- Central controller、driver/firmware；
- nrftest Host 平台；
- nRF firmware identity；
- Profile 和 run ID；
- 两侧原始事件；
- pass/fail/skip/not-executed。

macOS 结果不证明 iOS，Windows 结果不证明 Android，任何 nRF 结果也不证明 BLESS/BlueZ provider。

---

## 17. 开发阶段

### Phase 0：上游固定与 PCA10059 可行性

截至 2026-09-08，Phase 0-A/0-B 和正式控制面门已完成。活动 Pixi manifest/联合 lock、Pixi 管理的 Just、无副作用 recipe、本机配置模板和解析器、Ruff/Pytest 配置均已建立；固定 upstream Zephyr `v4.4.2`/commit、68 个 West projects、AutoPTS commit 和 Zephyr SDK `1.0.1` + `arm-zephyr-eabi` 已安装到当前 Windows 机器的 `E:\dev\nrftest-upstream` 并通过精确验证。Windows Host DTC 由 `host-tools.lock.toml` 管理为 portable DTC `1.6.1`，不依赖已有 NCS；`E:\dev\v3.4.0` NCS workspace 未被正式 setup/build 修改。

分层实验确认原 `GetCommState` 失败来自 assertions 启用时 zero-backend deferred logging process thread 在 `subsys/logging/log_core.c:956` 触发 assertion。NCS T12 仅关闭该 thread 后通过，T13 进一步在保持 assertions、Tester source、DTS、USB/BTP/Bluetooth 边界时验证 `CONFIG_LOG=n` 通过。正式 upstream config 随后迁入 `CONFIG_TEST_LOGGING_DEFAULTS=n` 和 `CONFIG_LOG=n`，不修改或 vendor Tester source。upstream `prj.conf` 先选择 debug log-level、extra config 再关闭 logging 会留下一个隐藏 choice Kconfig warning；最终 `.config` 和 build manifest 已确认 logging 关闭。复制整份 upstream `prj.conf` 只为消除提示会扩大维护面，因此不采用。

正式 `firmware-build` 生成 Flash `393484 B`、RAM `95256 B` 的 ELF/HEX/BIN，HEX 段 `[0x1000, 0x6110c)` 未覆盖 MBR 或 stock bootloader。`firmware-package` 已改为独立 `generate`/`normalize-validate` 子进程，消除 Windows 上 Nordic legacy 兼容引擎返回后的 ZIP handle 时序；Nordic parser 复验通过，package SHA-256 为 `d53fa63143ad862de23cff4dcc3af68e538f94feafa1fcadb37e295029b25326`。该包经明确 `1915:521F`/hardware serial/port 和 package SHA 双重校验刷写，随后以 `2FE3:0004`、serial `DBDBE94A2CED8C63` 重枚举。Win32 `CreateFile/GetCommState(115200)/CloseHandle` 与固定 AutoPTS `IutCtl`/`BTPSocketSrv`/`BTPWorker`/`pybtp` Core direct probe 均通过；cleanup 仍只有已记录的 upstream service-unregister status defect。Phase 0 只在 Windows PCA10059 控制面边界通过，RF、具体 GAP/GATT、macOS/Linux 仍未执行。

工作：

1. 建立覆盖 `win-64`、`osx-arm64`、`linux-64` 的活动 Pixi manifest/lock，并由 Pixi 固定 Just；
2. 建立 `nrftest.local.example.toml`、配置优先级、`toolchain-paths`、`check-tools`、`setup-host-tools` 和 `verify-host-tools` recipe；
3. 获取用户提供或批准的 upstream Zephyr 和 AutoPTS 本地 checkout；
4. 记录两个 repository、固定 revision、BTP 兼容关系和许可证；
5. 记录 Zephyr/AutoPTS 不提供 PCA10059 预编译 Tester 镜像，以及 stock Tester 只列入 nRF52840 DK、没有 PCA10059 专用 config/overlay 的上游边界；
6. 审计 Tester、BTP header、board support、USB/UART、bootloader、partition layout 和 flash 路径；
7. 审计 AutoPTS `pybtp` 初始化、Core/GAP/GATT API、worker、`socat` transport 和框架耦合；
8. 确认 `nrf52840dongle/nrf52840` board target，并维护构建所需的最小 PCA10059 config/overlay；
9. 通过 `pixi run just firmware-build` 从固定 Zephyr revision 构建 Tester，生成 ELF/HEX/BIN 和包含 revision、配置、许可证、生成配置、地址范围、SHA-256 的 build manifest；
10. 固定 nRF Util CLI 与 `nrf5sdk-tools` command package 后，通过独立 `firmware-package` 生成 DFU ZIP 并记录 package identity；优先验证保留原厂 bootloader 的 USB DFU 路径，通过显式 flash recipe 选择设备并刷写，不自动 erase/recover；
11. 确认 USB 枚举和实际 serial topology，并证明 reset 后可按稳定 identity 重新发现；
12. 通过 doctor/direct probe recipe 直接使用固定 AutoPTS `pybtp` 完成 BTP Core handshake，不实现 nrftest framing/parser；
13. 只有上述门通过后，才把对应固件 identity 标记为项目验证候选并允许发布；失败或未执行时不得向普通用户推荐该镜像。

退出条件：

```text
固定 Zephyr revision + PCA10059 config/overlay
    → 可重复 build + package
    → 显式 nrf5sdk-tools DFU flash
    → enumerate/re-enumerate
且产物 manifest/SHA-256 完整
且固定 AutoPTS pybtp 能读取合法 BTP Core response
```

如果默认 DFU 路径无法通过，停止并报告 MCUboot 或 SWD/J-Link 备选方案及其 bootloader/partition 影响，不得静默切换。如果整体无法通过，不进入 Host API 实现；尤其不得把 AutoPTS 复用失败自动转换成自研 BTP Client。

当前状态：**2026-09-08 已通过**。通过范围为 Windows Host + PCA10059 + 固定 upstream Tester 的 build/package/flash/re-enumeration/Win32 serial/BTP Core；不包含 Phase 1 的 GAP 或 BLE RF。

### Phase 1：Core/GAP 和第一条真实 RF

截至 2026-09-08，GAP controller info、power、advertising 和正常 Host transport 生命周期已完成分层验证。同一 session 的 `SET_POWERED(false) → SET_POWERED(true) → 再次 advertising` 通过；stock GAP unregister 后的第二次 register 失败，根因与未清 handler assertion 边界闭环；改为 service 常驻后，两个独立 Python 命令进程、合计 20 个 AutoPTS/socat transport session 全部完成 attach、controller info 和 advertising start/stop。正式 AUTO 协商又分别通过 `GAP attached/GATT registered` 与 `GAP attached/GATT attached` 两种状态；advertising 期间 Host 子进程被强杀后，COM 在 `0.609 s` 内释放，新 Host无 reset attach、观察并清理遗留广播，再完成 start/stop。最后由独立 nrftest fixture 广播 `FDF0`，独立 BleHub Windows Central 使用自己的 controller 完成 scan/connect/disconnect；nRF 侧同步观察到 connected/disconnected，双侧均通过。详见 [`docs/research/2026-09-08-zephyr-tester-service-lifecycle-and-reset.md`](research/2026-09-08-zephyr-tester-service-lifecycle-and-reset.md)和 [`docs/research/2026-09-08-phase1-gap-rf-smoke.md`](research/2026-09-08-phase1-gap-rf-smoke.md)。pending command、固件 assertion/deadlock 和 target reset 尚未完成，不影响本阶段既定退出条件。

工作：

- 使用已验证的 AutoPTS `pybtp` 初始化 Core/GAP 和 event worker；
- 增加最薄的 AutoPTS adapter，并保持一个 HIL session 的命令所有权；
- capability discovery；
- GAP power/controller information；
- start/stop advertising；
- 将已通过的“service 常驻、Host transport 重建”探针收敛为正式协商：resident handler 可响应时 attach，明确 fresh boot 才 register，正常关闭不调用 stock GAP/GATT service unregister；
- 单独验证 Host 强杀、pending command/serial 中断和 target reset fallback；不得把正常 transport attach 结果扩张为异常恢复已通过；
- connection/disconnection events；
- `nrftest` 独立进入 advertising 并等待 Peripheral 侧 connection/disconnection events；
- 由另一个独立进程中的 BleHub 完成 scan、connect、disconnect，任何一方都不配置或启动另一方。

退出条件：

```text
BleHub Central ↔ nRF BTP Peripheral
完成第一条真实 scan/connect/disconnect RF smoke
```

当前状态：**2026-09-08 已通过**。最终干净复验中，nrftest Peripheral 报告为 `peripheral-pass` 且 cleanup clean，BleHub 独立命令输出 `Windows connect smoke: PASS`。`FDF0` 当前只是 advertising selector，不代表动态 GATT service 已创建或 Phase 2 已通过。

### Phase 2：动态 GATT Profile

截至 2026-09-09，active Profile、validator、semantic signature/source checksum、四-role basic contract 和 Profile resource boundary 已完成；固定 AutoPTS 创建的 dynamic root service 为 handle `33`，四个 value handles 为 `35/37/40/42`，updates CCC 为 `38`。fresh build 与独立 Host resident attach 均验证 10 个 attributes 和可读初值；独立 BleHub Windows Central 又通过 root service、四 characteristic properties、CCC、公开 read、1-byte write with response/write without response 和 readback。两轮写入在 nRF 侧均得到 handle `35`、精确 value、`changed_count=1` 的 Attribute Value Changed。固定 Tester 的 128-bit `GET_HANDLE_FROM_UUID`、metadata ATT `0x0c` 和 native prepare-write permission 表示缺陷已在映射层按源码边界处理；未修改 upstream。

工作：

- 将归档 JSON 的 schema v1 和基础 Profile 语义迁移到活动目录，不建立运行时 archive 依赖；
- 为 schema v1 增加 validator、semantic signature 和 Profile contract tests；
- Profile 到 BTP GATT 命令映射；
- Service/Characteristic/Descriptor/CCC；
- actual handle mapping；
- read；
- write with response；
- write without response；
- attribute value changed event；
- Profile resource boundary tests。

退出条件：完整 basic GATT scenario 通过，两侧证据一致。

当前状态：**2026-09-09 已通过**。详见 [`docs/research/2026-09-09-phase2-dynamic-gatt-rf.md`](research/2026-09-09-phase2-dynamic-gatt-rf.md)。该结论不包含 Notification、Indication、dynamic removal/rebuild、异常 reset 或其他 Host/DUT 平台。

### Phase 3：Notification、Indication 和语义缺口决策

截至 2026-09-09，stock Tester 的 Notification 首轮通过，但 Indication 在 CCC=`0200` 后由 `bt_gatt_indicate(NULL, ...)` 返回同步失败，stock handler 又把该失败隐藏成 BTP success。status-only 对照先暴露现有 BTP failure；indication-only 候选完成一轮 Notification/Indication 后，后续 Notification 又稳定暴露 `conn=NULL` failure。最终 wire-compatible update-single-subscriber 最小 patch 让两种模式都显式选择唯一订阅连接并传播 allocation/native send status。active candidate 随后连续通过 Notification `11`、Indication `21`、Notification `31`，每轮均完成 CCC disable=`0000`、after-disable readback `12/22/32` 和 2 秒静默。固定 Tester callback 仍没有 BTP confirmation event，该不可观测终点已明确记录，没有新增私有 opcode。

工作：

- CCC enable/disable；
- 单条 Notification；
- 单条 Indication；
- 明确 Set Value response 的真实语义；
- 明确当前 Tester 的 confirmation 可观测性；
- 验证取消订阅后静默；
- 决定标准 BTP 是否足够。

退出条件：基础 Notification/Indication RF 通过；所有不可观测终点被明确记录。

如果必须扩展 BTP，Phase 3 在实现扩展前暂停，提交计划变更给用户。

当前状态：**2026-09-09 已通过**。详见 [`docs/research/2026-09-09-phase3-notification-indication-rf.md`](research/2026-09-09-phase3-notification-indication-rf.md)。通过范围限单 Central、单 updates characteristic、单 subscriber、单字节单包和 2 秒静默窗；Peripheral 侧逐条 ATT Confirmation telemetry 不在固定 BTP 中，不得宣称已观察。

### Phase 4：固定 Profile 生命周期和恢复

工作：

- one-shot Profile removal/rebuild 能力边界实验；
- boot-time Profile provision 和 resident attach；
- 每轮 JSON initial value restore 与实际 mapping 复核；
- advertising、connection、CCC 和 Host transport cleanup；
- Host restart；
- Peripheral-side disconnect fault 与恢复；
- 故障分类；
- optional target reset backend；
- 10-cycle resident lifecycle。

退出条件：10-cycle resident lifecycle 和一次 Host/Peripheral 主动故障恢复通过；无旧 connection/CCC/value/advertising/transport 泄漏。不要求同一 boot 回收 Tester backing database，也不以 J-Link 实机 reset 作为退出门。

当前状态：**2026-09-09 已通过**。一次 Profile A→standard removal→不同 Profile B 通过，但随后证明 removal 后标准 BTP `Set Value` 无法可靠定位 Profile B；一次人工重新上电退出该破坏性边界实验后，canonical Profile A fresh provision 以及 10-cycle resident lifecycle 通过。10 轮均重新 attach/恢复初值/复核 mapping，并通过交替 Notification/Indication、CCC disable、2 秒 stale-route silence、断连和 clean Host reopen。固定 Tester 的直接 GAP `DISCONNECT` 在当前 Windows peer 上返回 BTP error，未宣称通过；替代门使用明确记录的 BTP `SET_POWERED(false)` radio-stack fault，BleHub 观察到 `command_id=None` passive disconnect 并完成 active-subscription 资源释放，随后 `SET_POWERED(true)`、新 Host attach 和普通 Notification RF 恢复通过。Phase 1 已有 advertising-active Host 强杀/reopen 证据。自动 J-Link reset/re-enumeration 软件层仍是可选恢复能力。详见 [`docs/research/2026-09-09-phase4-database-lifecycle-and-reset.md`](research/2026-09-09-phase4-database-lifecycle-and-reset.md)。

### Phase 5：跨平台 Host

工作：

- Windows x64；
- macOS Apple Silicon；
- Linux x64；
- 三个平台从同一 `pixi.lock` 建立环境并通过一致的 Just recipe；
- 各平台 USB/permission/port discovery；
- 各平台 `socat`/外部工具解析、显式前置条件和失败报告；
- 同一 firmware 和 Profile 的行为对比。

退出条件：三个 Host 平台均完成 doctor、basic GATT、Notification、Indication、Host restart/reopen 和 repeated resident lifecycle；可选 reset backend 单独记录，未执行平台不得标记支持。

当前状态：**进行中**。Windows x64 Host gate 已于 2026-09-10 通过：正式 `PeripheralFixture` 完成 doctor/resident Profile attach；迁移后的 basic GATT fixture 分别通过 Write With Response/Without Response 双侧 RF；迁移后的 subscription fixture 分别通过 Notification、Indication、CCC disable、after-disable stale-route silence和远端断连；随后 10 个独立 nrftest/BleHub 进程轮次交替通过 Notification/Indication、clean close 和 resident attach。整个 Windows 正式 API 验证没有执行 database remove/rebuild、target reset 或人工拔插。跨平台静态审计随后补齐了全 Host AutoPTS OS lock、固定 checkout import origin、独立 bootloader serial、DFU identity 二次确认、setup preflight、统一原子 Telemetry 和无 POSIX env-prefix 的 stock recipe；Windows `host-doctor` 回归仍通过。后续又将 VC++ Runtime 固定为 Pixi `win-64` 直接依赖，把 Nordic driver/udev managed provisioning 与连接设备功能门分离，并保证 `setup-firmware` 不隐式提权。Nordic managed-install receipt 尚未建立，但它不是 Windows Host gate；macOS Apple Silicon 与 Linux x64 实机门尚未通过，因此 Phase 5 整体未通过。详见 [`docs/research/2026-09-10-phase5-windows-host-baseline.md`](research/2026-09-10-phase5-windows-host-baseline.md)和[`docs/research/2026-09-10-cross-platform-source-config-audit.md`](research/2026-09-10-cross-platform-source-config-audit.md)。

### Phase 6：BleHub provider parity 和 soak

工作：

- BleHub 已实现平台的独立 HIL；
- 1000-packet soak；
- deterministic write ledger；
- stale-route silence；
- passive disconnect/recovery；
- 报告 BTP control rate 与 BLE data rate；
- 与既有 BLESS 基础语义对比，但不混合证据。

退出条件：通过第 18 节 parity gate 后，nRF 才可成为 BleHub 日常主力 Peripheral fixture。

### Phase 7：可复现发布

工作：

- 固定 west manifest 和 toolchain；
- 发布 `pixi.toml`/`pixi.lock`、Pixi 管理的 Just 和跨平台 recipe 说明；
- 固定 Python package/lock、AutoPTS revision 和取得方式；
- 发布 `nrftest.local.example.toml`，不发布真实本机路径、端口或 secret；
- 完成 AutoPTS/GPL 及其他第三方依赖的发布合规清单；
- 发布 firmware image、SHA-256 和 source revision；
- 提供显式 build/flash/doctor 命令；
- 提供 Host package 和平台安装说明；
- 普通 unit test 与显式 hardware test 分离；
- 保存 license 和上游 patch 清单。

---

## 18. Provider parity gate

成为 BleHub 日常默认硬件 Peripheral 前必须通过：

1. 固定 firmware、Zephyr 和 BTP revision；
2. PCA10059 build/flash/re-enumeration；
3. Core capability handshake；
4. provider identity 和 Profile semantic signature；
5. scan/filter/advertisement；
6. connect/disconnect；
7. public GATT discovery；
8. read；
9. write with response；
10. write without response；
11. Peripheral attribute changed evidence；
12. Notification；
13. strict Indication，包含明确的 confirmation 证据边界；
14. subscription disable 后 stale-route silence；
15. 明确 trigger 的 Peripheral-side connection loss、BleHub `command_id=None` passive disconnect 和恢复；
16. Host restart/reopen；
17. unknown/dead target fail-fast，以及可选/人工恢复路径文档；
18. boot-time Profile provision、resident attach 和 runtime value restore；
19. 10-cycle resident lifecycle；
20. 1000-packet Notification soak；
21. 1000-packet Indication soak，仅在逐包确认语义可证明后；
22. Windows、macOS、Linux Host 独立验证；
23. Peripheral BTP evidence 与 BleHub event 独立保存并通过 run ID 关联。

任何未执行项均标记 `not_executed`，不能折算为通过。

---

## 19. BLESS 替代范围

通过 parity gate 后，nRF BTP fixture 可替代 BLESS 的日常范围：

- 普通 scan/connect；
- provider-specific service filter；
- GATT discovery/read/write；
- Notification/Indication；
- 主动断连和恢复；
- 重复连接和应用层 soak；
- 不依赖 Linux VM 的独立硬件 Peripheral 互操作；
- 跨 BleHub Central 平台 RF smoke。

它不替代：

- BlueZ D-Bus 行为；
- QEMU USB passthrough；
- guest reboot；
- BlueZ daemon restart；
- BLESS/BlueZ 特有生命周期；
- FakeBackend 的任意事件注入；
- 非法 ATT PDU/controller fault 测试。

---

## 20. 验收证据

每次硬件运行至少保存：

```text
scenario/profile/run_id
nrftest source revision
Zephyr/NCS revision
BTP definition revision
firmware image SHA-256
board and hardware serial
bootloader/flash method
USB VID/PID and current port
Host OS and version
Python, Pixi, Just and dependency versions
resolved tool/config sources with sensitive values redacted
AutoPTS revision and transport mode
active Profile source checksum and semantic signature
supported BTP services/commands
BleHub candidate revision/artifact
DUT OS and Central controller information
BTP command/response/event timeline
Peripheral facts
BleHub command/event timeline
start/end timestamps
reset/recovery actions
failure classification
```

默认报告不得公开不必要的原始 Bluetooth address；设备选择和结果关联优先使用硬件 serial、Profile identity 和 run ID。

结果分类：

- **pass**：该平台、candidate、firmware 和场景实际执行并满足条件；
- **test failure**：环境正常但 DUT/场景语义失败；
- **environment failure**：USB、权限、serial、flash、firmware 或工具环境失败；
- **protocol failure**：BTP frame、response 或 capability 不符合固定协议；
- **capability skip**：固定固件明确不支持；
- **not executed**：没有运行。

---

## 21. 主要风险与应对

| 风险 | 应对 |
|---|---|
| PCA10059 不能直接构建 stock Tester | Phase 0 先做本地源码审计和最小构建；失败即停，不先写完整 Host |
| Tester README 要求两路 serial | 实测 integrated controller 配置；必要时 RTT/独立 UART；不假设单 CDC 已满足 |
| stock 配置超出 nRF52840 资源 | 只通过可审计 Kconfig/overlay 裁剪到 Core/GAP/GATT Peripheral |
| BTP 版本随 Zephyr `main` 演化 | 固定匹配的 Zephyr、AutoPTS/BTP revision，运行时 capability discovery |
| AutoPTS `pybtp` 不是稳定独立 SDK | Phase 0 直接探针验证；正式代码只做薄 adapter；复用失败即停，不自动自研 Client |
| AutoPTS 依赖 `socat`、socket 和全局框架状态 | 三平台验证启动/关闭/错误/恢复；优先上游解耦或经批准的最小 patch |
| AutoPTS 是 GPL-2.0 | 区分内部验证与分发；不未经批准复制/vendor；发布前完成 source、notice 和分发策略核对 |
| 三平台工具版本或 recipe 漂移 | Pixi 联合 lock 固定环境；所有入口使用 `pixi run just`；同名 recipe 保持同一语义 |
| Just 中堆积平台 shell 分支 | Just 只编排，复杂逻辑使用 Pixi Python；必须平台特有的操作放入明确脚本和 recipe |
| 本机绝对路径进入仓库 | 只写入忽略的 `nrftest.local.toml`；固定版本/URL/hash 留在跟踪的 manifest/lock |
| 普通命令意外安装、提权或刷写 | setup、权限配置和 flash 显式分离；普通 build/test/doctor 禁止副作用 |
| SDK 1.0.1 不提供 Windows host tools | CMake/Ninja/gperf 等由 Pixi 管理；Windows 通过固定 URL/SHA 的 portable DTC `1.6.1` 和显式 `setup-host-tools` 管理，macOS/Linux 由 Pixi target dependency 提供；不依赖预装 NCS |
| 把 `arm-zephyr-eabi` 误解成 ARM 宿主包 | 明确它是运行于当前 Windows/macOS/Linux 宿主、为 nRF52840 生成 ARM EABI 固件的交叉编译器 |
| Nordic DFU 工具许可证限制 | `firmware-tools.lock.toml` 固定许可证和 `redistribution = "prohibited"`；只做机器本地显式安装，不把 CLI/command 二进制打入项目或发布物 |
| `nrf5sdk-tools` 显示旧 `6.1.7` | 记录它是官方 command package `1.1.0` 内嵌、用于 PCA10059 原厂 nRF5 SDK Secure DFU 格式的兼容引擎，不误认为项目回退了主 CLI |
| unsigned DFU 被误当生产安全更新 | `firmware/package.toml` 明确 `signed = false`；只用于当前开发/HIL Dongle 和允许 signature-less package 的原厂 bootloader，不宣称安全更新能力 |
| legacy engine 写入当前 ZIP timestamp | 只规范化 ZIP container metadata，不修改 Nordic payload；固定 entry 时间并在规范化后调用 Nordic `pkg display` 重新验证，要求连续构建 SHA-256 一致 |
| Profile 从 archive 直接运行或语义漂移 | 将 schema v1 语义迁入活动目录，保留 validator/signature/checksum，不 import archive |
| BTP 无 request ID | 验证 AutoPTS 命令相关；nrftest 保持一个 session、一个 command owner、最多一个 pending response |
| 文本日志破坏二进制流 | BTP port 禁止文本；日志走 RTT/独立通道或关闭 |
| stock Tester service unregister 不完整 | 正常路径让 GAP/GATT BTP service 在同一 boot 常驻；新 Host transport 先探测并 attach；timeout/不一致状态进入 recovery，不盲目第二次 register |
| 动态 DB unregister 不回收，且 removal 后 `Set Value` 的 handle-offset 假设失效 | 普通 testcase 不 remove/rebuild；每个 boot 使用固定 resident Profile；one-shot rebuild 实验后必须 suite-boundary reset；topology 切换使用另一根已 provision 的设备或可选/人工 reset recovery |
| Indication confirmation 未结构化返回 | Phase 3 已明确证据边界；Central receipt 证明 delivery，但不声称 Peripheral BTP 观察到 confirmation；未来确需 event 时先停并提交计划变更 |
| stock BTP Set Value 隐藏 allocation/native send failure | 固定 update-single-subscriber patch 传播 immediate status，并显式选择唯一模式匹配连接；仍由 Peripheral readback 与独立 BleHub event 分层证明状态和 delivery |
| 逐包 BTP 限制吞吐 | 功能 soak 与 throughput 分离；device-local burst 作为单独批准的扩展 |
| macOS USB 行为变化 | 使用 CDC serial，不 claim HCI；仍在真实 macOS 版本上执行枚举和恢复门 |
| Linux 串口权限 | 显式文档和 doctor，不在普通运行中静默提权或安装规则 |
| 可选 reset 后端口变化 | 仅对启用的 reset backend 按 hardware serial/VID/PID 重新发现，不缓存旧 COM/tty 名；普通 resident lifecycle 不要求 reset |
| fork 集成源码漂移 | 固定 `VIDLG/zephyr` commit；历史 patch 仅保留 provenance；更新 fork 后必须重新 build/package/RF 验证 |
| 把 nRF 结果泛化到其他 provider | 报告按 fixture、DUT 平台和 firmware identity 隔离 |

---

## 22. 归档策略

旧 Connectivity/Blatann 项目完整保存在：

```text
archive/connectivity-blatann-2026-09-06/
```

其中包含：

- 旧 `docs/PLAN.md`；
- Python Host 实现；
- tests 和 Profile；
- Connectivity firmware 和 wheel；
- Pixi/Just/pytest 配置；
- 当时的缓存和本地辅助资产。

归档规则：

- 归档目录不是活动源码；
- 新实现不得从归档路径 import 或运行；
- 不修改归档来假装新主线通过；
- 如需复用某段逻辑，应在新实现中显式移植并重新测试；
- 旧 Windows COM11/Connectivity smoke 只证明旧方案，不证明 BTP 方案；
- 归档删除或压缩必须再次取得用户明确批准。

---

## 23. 当前下一步

Phase 0 和 Phase 1 已于 2026-09-08 通过，Phase 2、Phase 3 和 Phase 4 已于 2026-09-09 通过。当前 fork 集成候选使用 `VIDLG/zephyr` commit `f530afbe09cb3b3d96dee432676aaafd56a8a93d`；其纯净配置 build/package 已通过，RF 尚未执行，旧 upstream v4.4.2 的 unsigned DFU ZIP SHA-256 `3393e36271aa31f4d93a6802765cb364030c482c51524961460539446fbb6715` 仅作历史对照。以下 Phase 4 事实均属于旧 v4.4.2 候选：它曾重跑通过两种 write 和 `Notification → Indication → Notification`。一次 Profile A 标准 removal 后构建不同 Profile B 通过，但 Profile B 随后的标准 BTP `Set Value` 失败，证明该 one-shot 实验后必须退出当前 boot。人工重新上电一次后，canonical Profile A fresh provision 和 10-cycle resident lifecycle 已通过；正常 10 轮之间没有 reset、拔插或 rebuild：

| 顺序 | Phase 4 工作 | 状态 | 关键边界 |
|---:|---|---:|---|
| 1 | one-shot Profile removal/rebuild | 通过一次 | 只证明 effective native service replacement；实验后 `Set Value` handle-offset 失效，必须 suite-boundary reset |
| 2 | Profile A 10-cycle resident lifecycle | **通过** | 10/10 attach、初值恢复、mapping、Notification/Indication、CCC disable、stale silence、disconnect 和 clean Host reopen |
| 3 | Host restart 与 Peripheral-side fault recovery | **通过** | Phase 1 Host 强杀/reopen；本阶段 radio-stack power-off passive disconnect、power-on、new Host attach 和普通 RF 恢复通过 |
| 4 | direct BTP GAP `DISCONNECT` | 未通过 | 当前 Windows peer 返回 BTP error；不影响已批准的 radio-stack fault 等价门，不伪称逻辑 disconnect 已通过 |
| 5 | optional target reset/re-enumeration backend | 软件完成，实机可选 | 仅用于 unknown/dead target、破坏性实验结束或 topology 切换；不阻塞 Phase 4 |

Phase 4 已达到当前退出条件，证据和恢复边界见 [`docs/research/2026-09-09-phase4-database-lifecycle-and-reset.md`](research/2026-09-09-phase4-database-lifecycle-and-reset.md)。未执行的 pending-response interruption、固件 assertion/deadlock、J-Link 实机 reset、soak、throughput 和跨平台 Host 仍保持未验证，不因 Phase 4 通过而外推。

2026-09-10 跨平台源码/config 审计完成后，Phase 5 的直接工作按以下顺序推进；详见[`docs/research/2026-09-10-cross-platform-source-config-audit.md`](research/2026-09-10-cross-platform-source-config-audit.md)：

| 顺序 | Phase 5 工作 | 状态 | 边界 |
|---:|---|---:|---|
| 1 | Windows Host 源码安全加固与回归 | **通过** | 140 unit tests、lint、固定工具 verify、实机 resident `host-doctor`；旧固件证据不覆盖新 fork |
| 2 | `VIDLG/zephyr` fork 集成候选 build/package/RF 回归 | 纯净配置代码/build/package 通过，RF 待执行 | commit `f530afbe09cb3b3d96dee432676aaafd56a8a93d` 已集成；必须刷写新 package 后重跑 BTP/GAP/GATT/Notification/Indication/recovery |
| 3 | Windows managed driver provisioning | 待执行，非 Host gate | VC++ Runtime 已由 Pixi 管理；Nordic receipt 仅由显式 UAC `setup-platform-provisioning` 建立，功能以 `host-doctor`/实际 flash 为准 |
| 4 | Linux x64 Host gate | - | 必须实机验证 udev、串口、socat、RF 和 repeated reopen |
| 5 | macOS Apple Silicon Host gate | - | 必须实机验证 USB CDC、权限、socat、RF 和 repeated reopen |
| 6 | AutoPTS bounded teardown 决策 | 待审 | 子进程隔离会改变 Host 进程结构，不能作为普通修复静默引入 |

后续仍必须遵守：

- 不实现或复制 Python BTP framing/parser/Core/GAP/GATT Client；
- 不把 AutoPTS 源码复制或 vendor 到活动源码；
- 不删除归档的 Connectivity 基线；
- 不修改 BleHub 公共 API、production backend 或架构；BleHub 自有的通用参数化 HIL consumer 不属于 `nrftest` 运行时依赖；
- 不把当前 Windows 单连接结果扩张为 macOS、Linux、多连接、soak 或 throughput 已通过；
- 不增加私有 BTP opcode；
- 不把在线源码阅读替代为本地构建和真实硬件证据。
