from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from host.nrftest.autopts_adapter import (
    AutoPtsAdapterError,
    AutoPtsSession,
    CapabilitySnapshot,
    GapSnapshot,
    SerialIdentity,
    ServiceStartupMode,
    autopts_tty_file,
    identity_document,
    load_autopts,
    select_application_port,
)
from host.nrftest.telemetry import TelemetryError, write_json_report
from tools.config import ConfigError, ResolvedValue, resolve_current_settings
from tools.setup_host_tools import (
    HostToolError,
    load_socat_pin,
    managed_socat_path,
    verify_socat,
)
from tools.setup_upstream import SetupError, load_upstream_pins, verify_upstream

DEFAULT_LOCAL_NAME = "NrftestResidentGap"
DEFAULT_CYCLES = 10
DEFAULT_SETTLE_SECONDS = 0.5


class BtpGapTransportReuseProbeError(RuntimeError):
    """Raised when a new Host transport cannot reuse the resident GAP service."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGapTransportReuseProbeError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _snapshot_document(snapshot: GapSnapshot) -> dict[str, object]:
    return {
        "controller_index": snapshot.controller_index,
        "address": snapshot.address,
        "address_type": snapshot.address_type,
        "address_type_name": snapshot.address_type_name,
        "current_settings": snapshot.current_settings,
        "connections": [
            {
                "address": connection.address,
                "address_type": connection.address_type,
                "address_type_name": connection.address_type_name,
                "security_level": connection.security_level,
            }
            for connection in snapshot.connections
        ],
    }


def _capability_document(capabilities: CapabilitySnapshot) -> dict[str, object]:
    return {
        "supported_services_mask": f"0x{capabilities.supported_services_mask:x}",
        "supported_services": list(capabilities.supported_services),
        "supported_command_masks": {
            service: f"0x{mask:x}" for service, mask in capabilities.supported_command_masks.items()
        },
        "service_actions": capabilities.service_actions,
    }


def _require_setting(snapshot: GapSnapshot, name: str, expected: bool) -> None:
    actual = snapshot.current_settings.get(name)
    if actual is not expected:
        raise BtpGapTransportReuseProbeError(
            f"unexpected GAP setting {name}: expected {expected}, found {actual}"
        )


def _write_report(
    reports_root: Path,
    *,
    run_id: str,
    started_at: str,
    outcome: str,
    detail: str | None,
    identity: SerialIdentity,
    autopts_commit: str,
    local_name: str,
    requested_cycles: int,
    settle_seconds: float,
    initial_mode: ServiceStartupMode,
    cycles: list[dict[str, object]],
) -> Path:
    finished_at = datetime.now(UTC)
    document = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "scope": (
            "Start with the requested GAP register/attach mode, then rebuild only the fixed "
            + "AutoPTS Host transport and attach to the resident GAP service on the same "
            + "PCA10059 boot"
        ),
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "local_name": local_name,
        "requested_cycles": requested_cycles,
        "completed_cycles": sum(cycle.get("outcome") == "pass" for cycle in cycles),
        "settle_seconds": settle_seconds,
        "initial_service_startup_mode": initial_mode.value,
        "diagnostic_sequence": [
            (
                "session 1: register GAP"
                if initial_mode is ServiceStartupMode.REGISTER
                else "session 1: attach to an already-resident GAP service"
            ),
            "session 1: read controller info and start/stop advertising",
            "session 1: close AutoPTS transport without unregistering GAP",
            "sessions 2..N: send GAP READ_SUPPORTED_COMMANDS without registering GAP",
            "sessions 2..N: read controller info and start/stop advertising",
            "sessions 2..N: close AutoPTS transport without unregistering GAP",
        ],
        "lifecycle_boundary": {
            "host_transport_rebuilt_each_cycle": True,
            "gap_service_registered_by_this_run": initial_mode is ServiceStartupMode.REGISTER,
            "gap_service_expected_resident_at_start": initial_mode is ServiceStartupMode.ATTACH,
            "gap_service_unregistered": False,
            "bluetooth_power_disabled": False,
            "commanded_reset": False,
            "commanded_power_cycle": False,
            "normal_exit_leaves_gap_service_resident": True,
            "proof_limit": (
                "The stock firmware exposes no boot ID. Same-boot continuity is inferred from no "
                "reset/power command and stable USB port plus hardware serial between cycles."
            ),
        },
        "cycles": cycles,
    }
    return write_json_report(
        reports_root, "btp-gap-transport-reuse-probe", document, now=finished_at
    )


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    port_name: str | None = None,
    local_name: str = DEFAULT_LOCAL_NAME,
    requested_cycles: int = DEFAULT_CYCLES,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    initial_mode: ServiceStartupMode = ServiceStartupMode.REGISTER,
    transport: str = "dongle",
) -> Path:
    if not 2 <= requested_cycles <= 100:
        raise BtpGapTransportReuseProbeError("cycles must be in range 2..=100")
    if not 0 <= settle_seconds <= 10:
        raise BtpGapTransportReuseProbeError("settle must be in range 0..=10 seconds")

    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    selected_port = port_name or settings["device_port"].value
    configured_serial = settings["device_serial"].value
    try:
        initial_identity = select_application_port(
            port_name=selected_port,
            serial_number=configured_serial,
            transport=transport,
        )
        loaded = load_autopts(_required_path(settings, "autopts_root"))
    except AutoPtsAdapterError as error:
        raise BtpGapTransportReuseProbeError(str(error)) from error

    reports_root = _required_path(settings, "reports_dir")
    socat = managed_socat_path(socat_pin, settings)
    run_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    cycles: list[dict[str, object]] = []
    print(f"Run ID: {run_id}")
    print(
        f"Initial application port: {initial_identity.port} "
        + f"({initial_identity.description}; {initial_identity.hwid})"
    )
    print(f"AutoPTS tty transport: {autopts_tty_file(initial_identity.port)}")
    if initial_mode is ServiceStartupMode.REGISTER:
        print(
            "Session 1 will register GAP; later sessions will attach without registering it again."
        )
    else:
        print("Every session will attach to the GAP service left resident by an earlier process.")
    print(
        "No service unregister, Bluetooth power-off, reset, or power-cycle command will be issued."
    )

    main_error: Exception | None = None
    for cycle_number in range(1, requested_cycles + 1):
        startup_mode = initial_mode if cycle_number == 1 else ServiceStartupMode.ATTACH
        cycle: dict[str, object] = {
            "cycle": cycle_number,
            "started_at_utc": datetime.now(UTC).isoformat(),
            "service_startup_mode": startup_mode.value,
            "service_close_mode": "retain-resident-service",
            "outcome": "running",
        }
        cycles.append(cycle)
        session: AutoPtsSession | None = None
        advertising_started = False
        recovery_errors: list[str] = []
        try:
            cycle["stage"] = "select-application-port"
            identity = select_application_port(
                serial_number=initial_identity.serial_number, transport=transport
            )
            cycle["port_before"] = identity_document(identity)
            if identity.serial_number != initial_identity.serial_number:
                raise BtpGapTransportReuseProbeError(
                    "application hardware serial changed between transport sessions"
                )
            if identity.port != initial_identity.port:
                raise BtpGapTransportReuseProbeError(
                    f"application port changed from {initial_identity.port} to {identity.port}; "
                    + "same-enumeration continuity was not preserved"
                )

            session = AutoPtsSession(
                loaded,
                identity=identity,
                log_dir=(
                    reports_root
                    / "btp-gap-transport-reuse-probe"
                    / run_id
                    / f"cycle-{cycle_number}"
                ),
                socat_path=socat,
            )
            cycle["stage"] = (
                "register-gap" if startup_mode is ServiceStartupMode.REGISTER else "attach-gap"
            )
            capabilities = session.start(
                required_services=("CORE", "GAP"),
                local_name=local_name,
                service_mode=startup_mode,
            )
            cycle["capabilities"] = _capability_document(capabilities)

            cycle["stage"] = "read-controller-info"
            controller = session.read_controller_info()
            _require_setting(controller, "Powered", True)
            cycle["controller"] = _snapshot_document(controller)

            cycle["stage"] = "start-advertising"
            advertising = session.start_advertising(local_name)
            advertising_started = True
            _require_setting(advertising, "Advertising", True)
            cycle["advertising"] = _snapshot_document(advertising)

            cycle["stage"] = "stop-advertising"
            stopped = session.stop_advertising()
            advertising_started = False
            _require_setting(stopped, "Advertising", False)
            cycle["advertising_stopped"] = _snapshot_document(stopped)
            cycle["stage"] = "close-transport-retain-gap"
            cycle["outcome"] = "pass"
        except Exception as error:
            cycle["outcome"] = "failure"
            cycle["error"] = f"{type(error).__name__}: {error}"
            main_error = error
        finally:
            if session is not None:
                if advertising_started:
                    try:
                        stopped = session.stop_advertising()
                        cycle["recovery_advertising_stopped"] = _snapshot_document(stopped)
                    except Exception as error:
                        recovery_errors.append(f"stop advertising: {type(error).__name__}: {error}")
                session.close(unregister_services=False)
                cycle["cleanup_errors"] = list(session.cleanup_errors)
                cycle["cleanup_classification"] = session.cleanup_classification
            cycle["recovery_errors"] = recovery_errors
            cycle["finished_at_utc"] = datetime.now(UTC).isoformat()

        if cycle.get("cleanup_classification") != "clean":
            main_error = BtpGapTransportReuseProbeError(
                f"cycle {cycle_number} had unexpected transport cleanup: "
                + f"{cycle.get('cleanup_errors')}"
            )
        if recovery_errors:
            main_error = BtpGapTransportReuseProbeError(
                f"cycle {cycle_number} operation recovery failed: {recovery_errors}"
            )
        if main_error is not None:
            break

        print(
            f"Transport reuse cycle {cycle_number}/{requested_cycles}: PASS "
            + f"({startup_mode.value})"
        )
        if cycle_number < requested_cycles:
            time.sleep(settle_seconds)

    if main_error is not None:
        detail = f"{type(main_error).__name__}: {main_error}"
        report = _write_report(
            reports_root,
            run_id=run_id,
            started_at=started_at,
            outcome="failure",
            detail=detail,
            identity=initial_identity,
            autopts_commit=upstream.autopts.commit,
            local_name=local_name,
            requested_cycles=requested_cycles,
            settle_seconds=settle_seconds,
            initial_mode=initial_mode,
            cycles=cycles,
        )
        raise BtpGapTransportReuseProbeError(
            f"BTP GAP transport reuse probe failed; report: {report}"
        ) from main_error

    report = _write_report(
        reports_root,
        run_id=run_id,
        started_at=started_at,
        outcome="pass",
        detail=None,
        identity=initial_identity,
        autopts_commit=upstream.autopts.commit,
        local_name=local_name,
        requested_cycles=requested_cycles,
        settle_seconds=settle_seconds,
        initial_mode=initial_mode,
        cycles=cycles,
    )
    print(f"BTP GAP transport reuse probe: PASS ({requested_cycles}/{requested_cycles} cycles)")
    print(f"Transport reuse report: {report}")
    return report


class ProbeArguments(argparse.Namespace):
    config: str | None = None
    port: str | None = None
    local_name: str = DEFAULT_LOCAL_NAME
    cycles: int = DEFAULT_CYCLES
    settle: float = DEFAULT_SETTLE_SECONDS
    initial_mode: ServiceStartupMode = ServiceStartupMode.REGISTER
    transport: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Register GAP once and verify new AutoPTS transports can attach on the same boot"
        )
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    port_help = "exact current application serial port; otherwise use configured serial/port "
    port_help += "or the only application USB device"
    _ = parser.add_argument("--port", help=port_help)
    _ = parser.add_argument("--local-name", default=DEFAULT_LOCAL_NAME)
    _ = parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    _ = parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SECONDS)
    _ = parser.add_argument(
        "--initial-mode",
        type=ServiceStartupMode,
        choices=tuple(ServiceStartupMode),
        default=ServiceStartupMode.REGISTER,
        help="register on a clean boot, or attach to a service left resident by an earlier process",
    )
    _ = parser.add_argument(
        "--transport",
        choices=("dongle", "dk"),
        default="dongle",
        help="application USB identity: dongle (PCA10059 CDC) or dk (J-Link VCOM)",
    )
    return parser


def main() -> int:
    arguments = ProbeArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        _ = run_probe(
            resolve_current_settings(config_path=arguments.config),
            port_name=arguments.port,
            local_name=arguments.local_name,
            requested_cycles=arguments.cycles,
            settle_seconds=arguments.settle,
            initial_mode=arguments.initial_mode,
            transport=arguments.transport,
        )
    except (
        AutoPtsAdapterError,
        BtpGapTransportReuseProbeError,
        ConfigError,
        HostToolError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"BTP GAP transport reuse probe error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
