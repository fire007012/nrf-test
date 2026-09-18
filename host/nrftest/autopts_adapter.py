from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Lock
from types import ModuleType, SimpleNamespace
from typing import Protocol, cast, final

from serial.tools import list_ports

from host.nrftest.interprocess_lock import InterprocessFileLock, InterprocessLockError

APPLICATION_USB_VID = 0x2FE3
APPLICATION_USB_PID = 0x0004
# DK（PCA10056）板载 SEGGER J-Link OB 的 CDC VCOM，BTP 经 UART0 到达该端口。
DK_JLINK_USB_VID = 0x1366
DK_JLINK_USB_PID = 0x1061
CONTROLLER_INDEX = 0
UUID128_ALL_AD_TYPE = 0x07

UsbIdentity = tuple[int, int]

TRANSPORT_USB_IDENTITIES: Mapping[str, tuple[UsbIdentity, ...]] = {
    "dongle": ((APPLICATION_USB_VID, APPLICATION_USB_PID),),
    "dk": ((DK_JLINK_USB_VID, DK_JLINK_USB_PID),),
}


def transport_usb_identities(transport: str = "dongle") -> tuple[UsbIdentity, ...]:
    identities = TRANSPORT_USB_IDENTITIES.get(transport)
    if identities is None:
        known = ", ".join(sorted(TRANSPORT_USB_IDENTITIES))
        raise AutoPtsAdapterError(f"unknown transport {transport!r}; known transports: {known}")
    return identities


class AutoPtsAdapterError(RuntimeError):
    """Raised when the fixed AutoPTS adapter cannot complete safely."""


class AutoPtsEventTimeout(AutoPtsAdapterError):
    """Raised when an expected AutoPTS-backed BTP event does not arrive."""


class ServiceStartupMode(StrEnum):
    AUTO = "auto"
    REGISTER = "register"
    ATTACH = "attach"


class AutoPtsProperty(Protocol):
    data: object


class AutoPtsConnection(Protocol):
    addr_type: int
    sec_level: int


class AutoPtsGapState(Protocol):
    current_settings: AutoPtsProperty
    iut_bd_addr: AutoPtsProperty
    connections: Mapping[str, AutoPtsConnection]

    def wait_for_connection(
        self, timeout: float, conn_count: int = 1, addr: str | None = None
    ) -> bool: ...

    def wait_for_disconnection(self, timeout: float, addr: str | None = None) -> bool: ...


class AutoPtsGattState(Protocol):
    def attr_value_clr_changed(self, handle: int) -> None: ...

    def attr_value_get(self, handle: int) -> object: ...

    def attr_value_get_changed_cnt(self, handle: int) -> object: ...

    def wait_attr_value_changed(self, handle: int, timeout: float | None = None) -> object: ...


class AutoPtsStack(Protocol):
    supported_svcs: int
    supported_cmds: dict[str, int]
    gap: AutoPtsGapState
    gatt: AutoPtsGattState

    def core_init(self) -> None: ...

    def gap_init(self, name: str | bytes | None = None) -> None: ...

    def gatt_init(self) -> None: ...

    def is_svc_supported(self, service: str) -> bool: ...


class AutoPtsController(Protocol):
    def get_stack(self) -> AutoPtsStack: ...

    def start(self, test_case: object) -> None: ...

    def stop(self) -> None: ...


class AutoPtsControllerFactory(Protocol):
    def __call__(self, arguments: object) -> AutoPtsController: ...


class AutoPtsBtp(Protocol):
    def init(self, get_iut: Callable[[], AutoPtsController]) -> None: ...

    def read_supp_svcs(self) -> None: ...

    def read_supported_commands(self, service: str) -> None: ...

    def core_reg_svc_gap(self) -> None: ...

    def core_reg_svc_gatt(self) -> None: ...

    def core_unreg_svc_gap(self) -> None: ...

    def core_unreg_svc_gatt(self) -> None: ...

    def gap_read_controller_info(self) -> None: ...

    def gap_set_powered_on(self) -> None: ...

    def gap_set_powered_off(self) -> None: ...

    def gap_set_connectable(self) -> None: ...

    def gap_set_non_connectable(self) -> None: ...

    def gap_set_general_discoverable(self) -> None: ...

    def gap_set_non_discoverable(self) -> None: ...

    def gap_start_advertising(
        self, ad: dict[int, bytes], sd: dict[int, bytes] | None = None
    ) -> None: ...

    def gap_stop_advertising(self) -> None: ...

    def gap_disconnect(self, bd_addr: str, bd_addr_type: int) -> None: ...

    def set_pts_addr(self, addr: str, addr_type: int) -> None: ...

    def gatts_add_svc(self, svc_type: int, uuid: str) -> None: ...

    def gatts_add_char(self, hdl: int, prop: int, perm: int, uuid: str) -> None: ...

    def gatts_set_val(self, hdl: int, val: str) -> None: ...

    def gatts_add_desc(self, hdl: int, perm: int, uuid: str) -> None: ...

    def gatts_start_server(self) -> None: ...

    def gatts_get_attrs(
        self,
        start_handle: int = 0x0001,
        end_handle: int = 0xFFFF,
        type_uuid: str | None = None,
    ) -> object: ...

    def gatts_get_attr_val(self, bd_addr_type: int, bd_addr: str, handle: int) -> object: ...

    def gatts_get_handle_from_uuid(self, uuid: str) -> object: ...

    def remove_handle_from_db(self, handle: int) -> None: ...


class AutoPtsAdType(Protocol):
    name_full: int
    uuid16_all: int


class AutoPtsAddrType(Protocol):
    le_public: int
    le_random: int


class SerialPort(Protocol):
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    serial_number: str | None


@dataclass(frozen=True)
class LoadedAutoPts:
    controller_factory: AutoPtsControllerFactory
    btp: AutoPtsBtp
    btp_error_type: type[Exception]
    ad_type: AutoPtsAdType
    addr_type: AutoPtsAddrType
    uuid_to_le_bytes: Callable[[int | str], bytes]


@dataclass(frozen=True)
class SerialIdentity:
    port: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    serial_number: str | None


@dataclass(frozen=True)
class CapabilitySnapshot:
    supported_services_mask: int
    supported_services: tuple[str, ...]
    supported_command_masks: dict[str, int]
    service_actions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionSnapshot:
    address: str
    address_type: int
    address_type_name: str
    security_level: int


@dataclass(frozen=True)
class GapSnapshot:
    controller_index: int
    address: str | None
    address_type: int | None
    address_type_name: str | None
    current_settings: dict[str, bool]
    connections: tuple[ConnectionSnapshot, ...]


@dataclass(frozen=True)
class GattAttributeSnapshot:
    handle: int
    permissions: int
    type_uuid: str


@dataclass(frozen=True)
class GattAttributeValueSnapshot:
    att_response: int
    value: bytes


@dataclass(frozen=True)
class GattValueChangedSnapshot:
    handle: int
    value: bytes
    changed_count: int


AUTOPTS_STATUS_ERROR = "Error opcode in response!"

KNOWN_UPSTREAM_UNREGISTER_ERRORS = {
    "GATT unregister: BTPError: Error opcode in response!",
    "GAP unregister: BTPError: Unexpected response received!",
}

_PROCESS_SESSION_LOCK = Lock()
_AUTOPTS_IMPORT_LOCK = Lock()


def cleanup_classification(errors: Sequence[str]) -> str:
    if not errors:
        return "clean"
    if set(errors) <= KNOWN_UPSTREAM_UNREGISTER_ERRORS:
        return "known-upstream-unregister-status-defect"
    return "unexpected"


def autopts_tty_file(port: str, *, platform: str = sys.platform) -> str:
    if platform != "win32":
        return port

    normalized = port.upper()
    number = normalized.removeprefix("COM")
    if not normalized.startswith("COM") or not number.isdecimal() or int(number) < 1:
        raise AutoPtsAdapterError(f"invalid Windows COM port for AutoPTS socat: {port!r}")
    return f"/dev/ttyS{int(number) - 1}"


def iut_arguments(port: str, *, platform: str = sys.platform) -> SimpleNamespace:
    return SimpleNamespace(
        iut_target_name="nrftest-pca10059",
        iut_mode="tty",
        pylink_reset=False,
        device_core=None,
        debugger_snr=None,
        kernel_image=None,
        kernel_cpu=None,
        tty_file=autopts_tty_file(port, platform=platform),
        net_tty_file=None,
        tty_baudrate=115200,
        btattach_bin=None,
        btattach_at_every_test_case=False,
        btproxy_bin=None,
        qemu_bin=None,
        qemu_options="",
        btmgmt_bin=None,
        hid_vid=None,
        hid_pid=None,
        hid_serial=None,
        hci=None,
        gdb=True,
        board_name=None,
        btmon=False,
        rtt_log_syncto=False,
        rtt_log=False,
        rtscts=False,
    )


def _serial_identity(port: SerialPort) -> SerialIdentity:
    return SerialIdentity(
        port=port.device,
        description=port.description,
        hwid=port.hwid,
        vid=port.vid,
        pid=port.pid,
        serial_number=port.serial_number,
    )


def identity_document(identity: SerialIdentity) -> dict[str, object]:
    return {
        "port": identity.port,
        "description": identity.description,
        "hwid": identity.hwid,
        "vid": f"0x{identity.vid:04x}" if identity.vid is not None else None,
        "pid": f"0x{identity.pid:04x}" if identity.pid is not None else None,
        "serial_number": identity.serial_number,
    }


def select_application_port(
    *,
    port_name: str | None = None,
    serial_number: str | None = None,
    ports: Iterable[SerialPort] | None = None,
    transport: str = "dongle",
) -> SerialIdentity:
    available = list(list_ports.comports() if ports is None else ports)
    accepted = transport_usb_identities(transport)
    application_ports = [port for port in available if (port.vid, port.pid) in accepted]
    application_identity = " or ".join(f"{vid:04X}:{pid:04X}" for vid, pid in accepted)

    if port_name is not None:
        named = [port for port in available if port.device.casefold() == port_name.casefold()]
        if len(named) != 1:
            names = ", ".join(port.device for port in available) or "<none>"
            raise AutoPtsAdapterError(
                f"explicit port {port_name!r} is not uniquely present; available ports: {names}"
            )
        selected = named[0]
        if selected not in application_ports:
            raise AutoPtsAdapterError(
                f"explicit port {port_name!r} is not the nrftest application identity "
                + application_identity
            )
        if serial_number is not None and selected.serial_number != serial_number:
            raise AutoPtsAdapterError(
                f"explicit port {port_name!r} has serial {selected.serial_number!r}, "
                + f"expected {serial_number!r}"
            )
        return _serial_identity(selected)

    candidates = application_ports
    if serial_number is not None:
        candidates = [port for port in candidates if port.serial_number == serial_number]
    if len(candidates) != 1:
        identities = (
            ", ".join(
                f"{port.device}({port.serial_number or '<no-serial>'})"
                for port in application_ports
            )
            or "<none>"
        )
        selector = f" with serial {serial_number!r}" if serial_number is not None else ""
        raise AutoPtsAdapterError(
            "nrftest application port selection is ambiguous or empty"
            + f"{selector}; matching {application_identity} ports: {identities}"
        )
    return _serial_identity(candidates[0])


def _autopts_modules() -> dict[str, ModuleType]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "autopts" or name.startswith("autopts.")
    }


def _require_module_from_root(name: str, module: ModuleType, root: Path) -> None:
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str):
        raise AutoPtsAdapterError(f"AutoPTS module {name!r} has no concrete source origin")
    try:
        resolved_origin = Path(origin).resolve(strict=True)
    except OSError as error:
        raise AutoPtsAdapterError(
            f"unable to resolve AutoPTS module {name!r} origin {origin!r}: {error}"
        ) from error
    if not resolved_origin.is_relative_to(root):
        raise AutoPtsAdapterError(
            f"AutoPTS module {name!r} was loaded from {resolved_origin}, not fixed root {root}"
        )


def load_autopts(autopts_root: Path) -> LoadedAutoPts:
    try:
        root_path = autopts_root.expanduser().resolve(strict=True)
    except OSError as error:
        raise AutoPtsAdapterError(
            f"fixed AutoPTS checkout does not exist at {autopts_root}: {error}"
        ) from error
    if not root_path.is_dir():
        raise AutoPtsAdapterError(f"fixed AutoPTS checkout is not a directory: {root_path}")

    with _AUTOPTS_IMPORT_LOCK:
        before_modules = set(sys.modules)
        previous_path = list(sys.path)
        try:
            for name, module in _autopts_modules().items():
                _require_module_from_root(name, module, root_path)

            sys.path.insert(0, str(root_path))
            iut_module = importlib.import_module("autopts.ptsprojects.iutctl")
            btp_module = importlib.import_module("autopts.pybtp.btp")
            types_module = importlib.import_module("autopts.pybtp.types")
            for name, module in _autopts_modules().items():
                _require_module_from_root(name, module, root_path)

            iut_class = cast(object, vars(iut_module)["IutCtl"])
            btp_error_type = cast(object, vars(types_module)["BTPError"])
            ad_type = cast(object, vars(types_module)["AdType"])
            addr_type = cast(object, vars(types_module)["Addr"])
            uuid_to_le_bytes = cast(object, vars(types_module)["uuid_to_le_bytes"])
        except AutoPtsAdapterError:
            for name in set(sys.modules) - before_modules:
                if name == "autopts" or name.startswith("autopts."):
                    del sys.modules[name]
            raise
        except (ImportError, KeyError, OSError) as error:
            for name in set(sys.modules) - before_modules:
                if name == "autopts" or name.startswith("autopts."):
                    del sys.modules[name]
            raise AutoPtsAdapterError(
                f"unable to import fixed AutoPTS checkout at {root_path}: {error}"
            ) from error
        finally:
            sys.path[:] = previous_path

    return LoadedAutoPts(
        controller_factory=cast(AutoPtsControllerFactory, iut_class),
        btp=cast(AutoPtsBtp, cast(object, btp_module)),
        btp_error_type=cast(type[Exception], btp_error_type),
        ad_type=cast(AutoPtsAdType, ad_type),
        addr_type=cast(AutoPtsAddrType, addr_type),
        uuid_to_le_bytes=cast(Callable[[int | str], bytes], uuid_to_le_bytes),
    )


@final
class AutoPtsSession:
    """Own one fixed AutoPTS transport and serialize its command/response calls."""

    def __init__(
        self,
        loaded: LoadedAutoPts,
        *,
        identity: SerialIdentity,
        log_dir: Path,
        socat_path: Path,
        platform: str = sys.platform,
    ) -> None:
        self._loaded = loaded
        self._identity = identity
        self._log_dir = log_dir
        self._socat_path = socat_path
        self._platform = platform
        self._controller: AutoPtsController | None = None
        self._command_lock = Lock()
        self._registered_services: list[str] = []
        self._cleanup_errors: list[str] = []
        self._previous_path: str | None = None
        self._path_was_present = False
        self._path_modified = False
        self._host_session_lock = InterprocessFileLock()
        self._owns_host_session = False
        self._owns_process_session = False

    @property
    def cleanup_errors(self) -> tuple[str, ...]:
        return tuple(self._cleanup_errors)

    @property
    def cleanup_classification(self) -> str:
        return cleanup_classification(self._cleanup_errors)

    @property
    def stack(self) -> AutoPtsStack:
        if self._controller is None:
            raise AutoPtsAdapterError("AutoPTS session has not been started")
        return self._controller.get_stack()

    def start(
        self,
        *,
        required_services: Sequence[str],
        local_name: str | None = None,
        service_mode: ServiceStartupMode = ServiceStartupMode.AUTO,
    ) -> CapabilitySnapshot:
        if self._controller is not None:
            raise AutoPtsAdapterError("AutoPTS session has already been started")
        if not _PROCESS_SESSION_LOCK.acquire(blocking=False):
            raise AutoPtsAdapterError(
                "another AutoPTS session already owns the process-global client"
            )
        self._owns_process_session = True
        try:
            self._host_session_lock.acquire()
        except InterprocessLockError as error:
            self._owns_process_session = False
            _PROCESS_SESSION_LOCK.release()
            raise AutoPtsAdapterError(str(error)) from error
        self._owns_host_session = True
        self._path_was_present = "PATH" in os.environ
        self._previous_path = os.environ.get("PATH")
        os.environ["PATH"] = str(self._socat_path.parent) + os.pathsep + (self._previous_path or "")
        self._path_modified = True

        try:
            controller = self._loaded.controller_factory(
                iut_arguments(self._identity.port, platform=self._platform)
            )
            self._controller = controller
            stack = controller.get_stack()
            stack.core_init()
            if "GAP" in required_services:
                stack.gap_init(name=local_name)
            if "GATT" in required_services:
                stack.gatt_init()
            self._loaded.btp.init(lambda: controller)
            self._log_dir.mkdir(parents=True, exist_ok=True)
            controller.start(
                SimpleNamespace(name="nrftest-autopts-session", log_dir=str(self._log_dir))
            )

            with self._command_lock:
                self._loaded.btp.read_supp_svcs()
                supported_services = tuple(
                    service
                    for service in ("CORE", "GAP", "GATT")
                    if stack.is_svc_supported(service)
                )
                missing = sorted(set(required_services) - set(supported_services))
                if missing:
                    raise AutoPtsAdapterError(
                        f"Tester supported-services mask 0x{stack.supported_svcs:x} is missing "
                        + f"{missing}"
                    )
                self._loaded.btp.read_supported_commands("CORE")
                service_actions: dict[str, str] = {}
                for service in required_services:
                    if service == "CORE":
                        continue
                    if service not in {"GAP", "GATT"}:
                        raise AutoPtsAdapterError(
                            f"the nrftest thin adapter does not expose service {service!r}"
                        )
                    service_actions[service] = self._start_service(service, service_mode)
                    if service not in stack.supported_cmds:
                        raise AutoPtsAdapterError(
                            f"BTP {service} service did not report supported commands"
                        )

            command_masks = {
                service: stack.supported_cmds[service]
                for service in required_services
                if service in stack.supported_cmds
            }
            return CapabilitySnapshot(
                supported_services_mask=stack.supported_svcs,
                supported_services=supported_services,
                supported_command_masks=command_masks,
                service_actions=service_actions,
            )
        except Exception:
            self.close()
            raise

    def _start_service(self, service: str, mode: ServiceStartupMode) -> str:
        if mode is ServiceStartupMode.REGISTER:
            self._register_service(service)
            return "registered"

        try:
            self._loaded.btp.read_supported_commands(service)
        except self._loaded.btp_error_type as error:
            if mode is ServiceStartupMode.ATTACH:
                raise AutoPtsAdapterError(
                    f"unable to attach to resident BTP {service} service"
                ) from error
            if str(error) != AUTOPTS_STATUS_ERROR:
                raise AutoPtsAdapterError(
                    f"BTP {service} service probe returned an unexpected AutoPTS error: {error}"
                ) from error
            self._register_service(service)
            return "registered"
        except Exception as error:
            raise AutoPtsAdapterError(
                f"BTP {service} service probe failed without a registration fallback"
            ) from error
        return "attached"

    def _register_service(self, service: str) -> None:
        if service == "GAP":
            self._loaded.btp.core_reg_svc_gap()
        else:
            self._loaded.btp.core_reg_svc_gatt()
        self._registered_services.append(service)

    def gap_snapshot(self) -> GapSnapshot:
        gap = self.stack.gap
        settings_value = gap.current_settings.data
        address_value = gap.iut_bd_addr.data
        if not isinstance(settings_value, dict) or not isinstance(address_value, dict):
            raise AutoPtsAdapterError("fixed AutoPTS GAP state has an unexpected shape")
        settings = cast(dict[object, object], settings_value)
        address_fields = cast(dict[object, object], address_value)

        address = address_fields.get("address")
        address_type = address_fields.get("type")
        typed_address = address if isinstance(address, str) else None
        typed_address_type = address_type if isinstance(address_type, int) else None
        connections = tuple(
            ConnectionSnapshot(
                address=str(connection_address),
                address_type=connection.addr_type,
                address_type_name=self._address_type_name(connection.addr_type),
                security_level=connection.sec_level,
            )
            for connection_address, connection in sorted(gap.connections.items())
        )
        return GapSnapshot(
            controller_index=CONTROLLER_INDEX,
            address=typed_address,
            address_type=typed_address_type,
            address_type_name=(
                self._address_type_name(typed_address_type)
                if typed_address_type is not None
                else None
            ),
            current_settings={str(key): bool(value) for key, value in settings.items()},
            connections=connections,
        )

    def read_controller_info(self) -> GapSnapshot:
        with self._command_lock:
            self._loaded.btp.gap_read_controller_info()
        return self.gap_snapshot()

    def set_powered(self, enabled: bool) -> GapSnapshot:
        snapshot = self.gap_snapshot()
        if snapshot.current_settings.get("Powered") is enabled:
            return snapshot
        with self._command_lock:
            if enabled:
                self._loaded.btp.gap_set_powered_on()
            else:
                self._loaded.btp.gap_set_powered_off()
        return self.gap_snapshot()

    def set_connectable(self, enabled: bool) -> GapSnapshot:
        snapshot = self.gap_snapshot()
        if snapshot.current_settings.get("Connectable") is enabled:
            return snapshot
        with self._command_lock:
            if enabled:
                self._loaded.btp.gap_set_connectable()
            else:
                self._loaded.btp.gap_set_non_connectable()
        return self.gap_snapshot()

    def set_discoverable(self, enabled: bool) -> GapSnapshot:
        snapshot = self.gap_snapshot()
        if snapshot.current_settings.get("Discoverable") is enabled:
            return snapshot
        with self._command_lock:
            if enabled:
                self._loaded.btp.gap_set_general_discoverable()
            else:
                self._loaded.btp.gap_set_non_discoverable()
        return self.gap_snapshot()

    def start_advertising(
        self,
        local_name: str,
        *,
        service_uuid16: str | None = None,
        service_uuid128: str | None = None,
    ) -> GapSnapshot:
        encoded_name = local_name.encode("utf-8")
        if not encoded_name or len(encoded_name) > 29:
            raise AutoPtsAdapterError("advertising local name must encode to 1..29 UTF-8 bytes")
        if service_uuid16 is not None and service_uuid128 is not None:
            raise AutoPtsAdapterError("advertising accepts only one service UUID width")

        advertising_data: dict[int, bytes]
        scan_response: dict[int, bytes] = {}
        if service_uuid128 is not None:
            encoded_uuid = self._loaded.uuid_to_le_bytes(service_uuid128)
            if len(encoded_uuid) != 16:
                raise AutoPtsAdapterError("advertising selector must be a 128-bit UUID")
            advertising_data = {UUID128_ALL_AD_TYPE: encoded_uuid}
            scan_response[self._loaded.ad_type.name_full] = encoded_name
        else:
            advertising_data = {self._loaded.ad_type.name_full: encoded_name}
            if service_uuid16 is not None:
                encoded_uuid = self._loaded.uuid_to_le_bytes(service_uuid16)
                if len(encoded_uuid) != 2:
                    raise AutoPtsAdapterError("advertising selector must be a 16-bit UUID")
                advertising_data[self._loaded.ad_type.uuid16_all] = encoded_uuid
        with self._command_lock:
            self._loaded.btp.gap_start_advertising(ad=advertising_data, sd=scan_response)
        return self.gap_snapshot()

    def stop_advertising(self) -> GapSnapshot:
        with self._command_lock:
            self._loaded.btp.gap_stop_advertising()
        return self.gap_snapshot()

    def wait_for_connection(self, timeout: float) -> GapSnapshot:
        with self._command_lock:
            gap = self.stack.gap
            connected = gap.wait_for_connection(timeout)
            if connected and len(gap.connections) == 1:
                address, connection = next(iter(gap.connections.items()))
                self._loaded.btp.set_pts_addr(str(address), connection.addr_type)
        if not connected:
            raise AutoPtsEventTimeout(f"no GAP connected event arrived within {timeout:g} seconds")
        return self.gap_snapshot()

    def wait_for_disconnection(self, timeout: float, *, address: str | None = None) -> GapSnapshot:
        with self._command_lock:
            disconnected = self.stack.gap.wait_for_disconnection(timeout, addr=address)
        if not disconnected:
            raise AutoPtsEventTimeout(
                f"no GAP disconnected event arrived within {timeout:g} seconds"
            )
        return self.gap_snapshot()

    def disconnect(self, connection: ConnectionSnapshot) -> GapSnapshot:
        with self._command_lock:
            self._loaded.btp.gap_disconnect(connection.address, connection.address_type)
        return self.gap_snapshot()

    def gatt_add_primary_service(self, uuid: str) -> None:
        with self._command_lock:
            self._loaded.btp.gatts_add_svc(0, uuid)

    def gatt_add_characteristic(self, uuid: str, *, properties: int, permissions: int) -> None:
        with self._command_lock:
            self._loaded.btp.gatts_add_char(0, properties, permissions, uuid)

    def gatt_set_value(self, handle: int, value: bytes) -> None:
        with self._command_lock:
            self._loaded.btp.gatts_set_val(handle, value.hex())

    def gatt_set_last_value(self, value: bytes) -> None:
        self.gatt_set_value(0, value)

    def gatt_add_ccc(self, *, permissions: int) -> None:
        with self._command_lock:
            self._loaded.btp.gatts_add_desc(0, permissions, "2902")

    def gatt_start_server(self) -> None:
        with self._command_lock:
            self._loaded.btp.gatts_start_server()

    def gatt_get_attributes(
        self,
        *,
        start_handle: int = 0x0001,
        end_handle: int = 0xFFFF,
        type_uuid: str | None = None,
    ) -> tuple[GattAttributeSnapshot, ...]:
        with self._command_lock:
            raw_attributes = self._loaded.btp.gatts_get_attrs(
                start_handle,
                end_handle,
                type_uuid,
            )
        if not isinstance(raw_attributes, list):
            raise AutoPtsAdapterError("AutoPTS GATT attributes response is not a list")
        attributes: list[GattAttributeSnapshot] = []
        for raw_attribute in cast(list[object], raw_attributes):
            if not isinstance(raw_attribute, tuple):
                raise AutoPtsAdapterError("AutoPTS GATT attribute has an unexpected shape")
            fields = cast(tuple[object, ...], raw_attribute)
            if len(fields) != 3:
                raise AutoPtsAdapterError("AutoPTS GATT attribute has an unexpected shape")
            handle, permissions, attribute_uuid = fields
            if (
                not isinstance(handle, int)
                or isinstance(handle, bool)
                or not isinstance(permissions, int)
                or isinstance(permissions, bool)
                or not isinstance(attribute_uuid, str)
            ):
                raise AutoPtsAdapterError("AutoPTS GATT attribute has unexpected field types")
            attributes.append(GattAttributeSnapshot(handle, permissions, attribute_uuid))
        return tuple(attributes)

    def gatt_get_attribute_value(
        self,
        handle: int,
        *,
        peer_address: str = "000000000000",
        peer_address_type: int = 0,
    ) -> GattAttributeValueSnapshot:
        with self._command_lock:
            raw_value = self._loaded.btp.gatts_get_attr_val(
                peer_address_type,
                peer_address,
                handle,
            )
        if not isinstance(raw_value, tuple):
            raise AutoPtsAdapterError(
                "AutoPTS GATT attribute-value response has an unexpected shape"
            )
        fields = cast(tuple[object, ...], raw_value)
        if len(fields) != 3:
            raise AutoPtsAdapterError(
                "AutoPTS GATT attribute-value response has an unexpected shape"
            )
        att_response, value_length, value = fields
        if (
            not isinstance(att_response, int)
            or isinstance(att_response, bool)
            or not isinstance(value_length, int)
            or isinstance(value_length, bool)
            or not isinstance(value, bytes)
        ):
            raise AutoPtsAdapterError(
                "AutoPTS GATT attribute-value response has unexpected field types"
            )
        if value_length != len(value):
            raise AutoPtsAdapterError(
                "AutoPTS GATT attribute-value response length does not match its payload"
            )
        return GattAttributeValueSnapshot(att_response, value)

    def gatt_get_handle_from_uuid(self, uuid: str) -> int:
        with self._command_lock:
            handle = self._loaded.btp.gatts_get_handle_from_uuid(uuid)
        if not isinstance(handle, int) or isinstance(handle, bool):
            raise AutoPtsAdapterError("AutoPTS GATT UUID lookup did not return an integer handle")
        return handle

    def gatt_remove_service_containing_handle(self, handle: int) -> None:
        if not 1 <= handle <= 0xFFFF:
            raise AutoPtsAdapterError("GATT removal handle must be in range 1..=65535")
        with self._command_lock:
            self._loaded.btp.remove_handle_from_db(handle)

    def clear_gatt_value_changed(self, handle: int) -> None:
        with self._command_lock:
            gatt = self.stack.gatt
            _ = gatt.wait_attr_value_changed(handle, timeout=0)
            gatt.attr_value_clr_changed(handle)

    def wait_for_gatt_value_changed(self, handle: int, timeout: float) -> GattValueChangedSnapshot:
        with self._command_lock:
            gatt = self.stack.gatt
            encoded_value = gatt.wait_attr_value_changed(handle, timeout=timeout)
            changed_count = gatt.attr_value_get_changed_cnt(handle)
        if encoded_value is None:
            raise AutoPtsEventTimeout(
                f"no GATT Attribute Value Changed event arrived for handle {handle} "
                + f"within {timeout:g} seconds"
            )
        if not isinstance(encoded_value, bytes):
            raise AutoPtsAdapterError("AutoPTS GATT changed value is not hex-encoded bytes")
        if not isinstance(changed_count, int) or isinstance(changed_count, bool):
            raise AutoPtsAdapterError("AutoPTS GATT changed count is not an integer")
        try:
            value = bytes.fromhex(encoded_value.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise AutoPtsAdapterError(
                "AutoPTS GATT changed value is not valid ASCII hexadecimal data"
            ) from error
        return GattValueChangedSnapshot(handle, value, changed_count)

    def close(self, *, unregister_services: bool = False) -> None:
        controller = self._controller
        self._controller = None
        if controller is not None:
            if unregister_services:
                for service in reversed(self._registered_services):
                    try:
                        with self._command_lock:
                            if service == "GATT":
                                self._loaded.btp.core_unreg_svc_gatt()
                            elif service == "GAP":
                                self._loaded.btp.core_unreg_svc_gap()
                    except Exception as error:
                        self._cleanup_errors.append(
                            f"{service} unregister: {type(error).__name__}: {error}"
                        )
            self._registered_services.clear()
            try:
                controller.stop()
            except Exception as error:
                self._cleanup_errors.append(f"controller stop: {type(error).__name__}: {error}")

        if self._path_modified:
            if self._path_was_present:
                os.environ["PATH"] = self._previous_path or ""
            else:
                _ = os.environ.pop("PATH", None)
            self._previous_path = None
            self._path_was_present = False
            self._path_modified = False
        try:
            if self._owns_host_session:
                self._owns_host_session = False
                self._host_session_lock.release()
        finally:
            if self._owns_process_session:
                self._owns_process_session = False
                _PROCESS_SESSION_LOCK.release()

    def _address_type_name(self, address_type: int) -> str:
        if address_type == self._loaded.addr_type.le_public:
            return "public"
        if address_type == self._loaded.addr_type.le_random:
            return "random"
        return f"unknown-{address_type}"
