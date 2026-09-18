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

DEFAULT_LOCAL_NAME = "NrftestLifecycle"
DEFAULT_CYCLES = 10
DEFAULT_SETTLE_SECONDS = 0.5


class BtpGapLifecycleProbeError(RuntimeError):
    """Raised when repeated GAP sessions cannot complete without a device reset."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGapLifecycleProbeError(
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
        raise BtpGapLifecycleProbeError(
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
            "Repeated BTP GAP sessions on one attached PCA10059 without an intentional "
            "device reset or power cycle"
        ),
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "local_name": local_name,
        "requested_cycles": requested_cycles,
        "completed_cycles": sum(cycle.get("outcome") == "pass" for cycle in cycles),
        "settle_seconds": settle_seconds,
        "diagnostic_sequence": [
            "register GAP",
            "start and stop advertising",
            "BTP GAP SET_POWERED(false)",
            "BTP GAP SET_POWERED(true) in the same session",
            "start and stop advertising after re-enable",
            "BTP GAP SET_POWERED(false)",
            "BTP Core unregister GAP",
            "close and rebuild the AutoPTS transport",
        ],
        "shutdown_sequence": [
            "stop advertising",
            "BTP GAP SET_POWERED(false)",
            "BTP Core unregister GAP",
            "close AutoPTS transport",
        ],
        "reset_boundary": {
            "commanded_reset": False,
            "commanded_power_cycle": False,
            "proof_limit": (
                "The firmware exposes no boot ID. Same-boot continuity is inferred from no reset "
                "action and stable USB identity/availability between cycles."
            ),
        },
        "cycles": cycles,
    }
    return write_json_report(reports_root, "btp-gap-lifecycle-probe", document, now=finished_at)


def _close_cycle(
    session: AutoPtsSession,
    cycle: dict[str, object],
    *,
    attempt_power_off: bool,
) -> None:
    recovery_errors: list[str] = []
    if attempt_power_off:
        try:
            current = session.gap_snapshot()
            if current.current_settings.get("Advertising"):
                _ = session.stop_advertising()
            if current.current_settings.get("Powered"):
                powered_off = session.set_powered(False)
                cycle["recovery_powered_off"] = _snapshot_document(powered_off)
        except Exception as error:
            recovery_errors.append(f"power off recovery: {type(error).__name__}: {error}")
    session.close(unregister_services=True)
    cycle["cleanup_errors"] = list(session.cleanup_errors)
    cycle["cleanup_classification"] = session.cleanup_classification
    cycle["recovery_errors"] = recovery_errors


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    port_name: str | None = None,
    local_name: str = DEFAULT_LOCAL_NAME,
    requested_cycles: int = DEFAULT_CYCLES,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    transport: str = "dongle",
) -> Path:
    if not 2 <= requested_cycles <= 100:
        raise BtpGapLifecycleProbeError("cycles must be in range 2..=100")
    if not 0 <= settle_seconds <= 10:
        raise BtpGapLifecycleProbeError("settle must be in range 0..=10 seconds")

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
        raise BtpGapLifecycleProbeError(str(error)) from error

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
    print("No reset or power-cycle command will be issued by this probe.")

    main_error: Exception | None = None
    for cycle_number in range(1, requested_cycles + 1):
        cycle: dict[str, object] = {
            "cycle": cycle_number,
            "started_at_utc": datetime.now(UTC).isoformat(),
            "outcome": "running",
        }
        cycles.append(cycle)
        session: AutoPtsSession | None = None
        session_started = False
        try:
            cycle["stage"] = "select-application-port"
            identity = select_application_port(
                serial_number=initial_identity.serial_number, transport=transport
            )
            cycle["port_before"] = identity_document(identity)
            if identity.serial_number != initial_identity.serial_number:
                raise BtpGapLifecycleProbeError(
                    "application hardware serial changed between cycles"
                )

            session = AutoPtsSession(
                loaded,
                identity=identity,
                log_dir=reports_root / "btp-gap-lifecycle-probe" / run_id / f"cycle-{cycle_number}",
                socat_path=socat,
            )
            cycle["stage"] = "register-gap"
            capabilities = session.start(
                required_services=("CORE", "GAP"),
                local_name=local_name,
                service_mode=ServiceStartupMode.REGISTER,
            )
            session_started = True
            cycle["capabilities"] = _capability_document(capabilities)

            cycle["stage"] = "read-controller-info"
            controller = session.read_controller_info()
            _require_setting(controller, "Powered", True)
            cycle["controller"] = _snapshot_document(controller)

            cycle["stage"] = "start-advertising-before-power-cycle"
            advertising = session.start_advertising(local_name)
            _require_setting(advertising, "Advertising", True)
            cycle["advertising"] = _snapshot_document(advertising)

            cycle["stage"] = "stop-advertising-before-power-cycle"
            stopped = session.stop_advertising()
            _require_setting(stopped, "Advertising", False)
            cycle["advertising_stopped"] = _snapshot_document(stopped)

            cycle["stage"] = "power-off-before-same-session-reenable"
            powered_off = session.set_powered(False)
            _require_setting(powered_off, "Powered", False)
            cycle["powered_off"] = _snapshot_document(powered_off)

            cycle["stage"] = "same-session-reenable"
            reenabled = session.set_powered(True)
            _require_setting(reenabled, "Powered", True)
            cycle["reenabled"] = _snapshot_document(reenabled)

            cycle["stage"] = "start-advertising-after-reenable"
            readvertising = session.start_advertising(local_name)
            _require_setting(readvertising, "Advertising", True)
            cycle["readvertising"] = _snapshot_document(readvertising)

            cycle["stage"] = "stop-advertising-after-reenable"
            restopped = session.stop_advertising()
            _require_setting(restopped, "Advertising", False)
            cycle["readvertising_stopped"] = _snapshot_document(restopped)

            cycle["stage"] = "final-power-off"
            final_powered_off = session.set_powered(False)
            _require_setting(final_powered_off, "Powered", False)
            cycle["final_powered_off"] = _snapshot_document(final_powered_off)
            cycle["stage"] = "close-session"
            cycle["outcome"] = "pass"
        except Exception as error:
            cycle["outcome"] = "failure"
            cycle["error"] = f"{type(error).__name__}: {error}"
            main_error = error
        finally:
            if session is not None:
                _close_cycle(
                    session,
                    cycle,
                    attempt_power_off=(
                        session_started
                        and cycle["outcome"] != "pass"
                        and cycle.get("stage") != "same-session-reenable"
                    ),
                )
            cycle["finished_at_utc"] = datetime.now(UTC).isoformat()

        cleanup_classification = cycle.get("cleanup_classification")
        recovery_errors = cycle.get("recovery_errors")
        if cleanup_classification not in {
            "clean",
            "known-upstream-unregister-status-defect",
        }:
            main_error = BtpGapLifecycleProbeError(
                f"cycle {cycle_number} had unexpected cleanup: {cleanup_classification}"
            )
        if recovery_errors:
            main_error = BtpGapLifecycleProbeError(
                f"cycle {cycle_number} recovery failed: {recovery_errors}"
            )
        if main_error is not None:
            break

        print(
            f"Lifecycle cycle {cycle_number}/{requested_cycles}: PASS "
            + f"({cleanup_classification})"
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
            cycles=cycles,
        )
        raise BtpGapLifecycleProbeError(
            f"BTP GAP lifecycle probe failed; report: {report}"
        ) from main_error

    cleanup_classes = {str(cycle["cleanup_classification"]) for cycle in cycles}
    outcome = (
        "pass-with-known-upstream-unregister-status-defect"
        if cleanup_classes == {"known-upstream-unregister-status-defect"}
        else "pass"
    )
    report = _write_report(
        reports_root,
        run_id=run_id,
        started_at=started_at,
        outcome=outcome,
        detail=None,
        identity=initial_identity,
        autopts_commit=upstream.autopts.commit,
        local_name=local_name,
        requested_cycles=requested_cycles,
        settle_seconds=settle_seconds,
        cycles=cycles,
    )
    print(f"BTP GAP lifecycle probe: PASS ({requested_cycles}/{requested_cycles} cycles)")
    print(f"Lifecycle report: {report}")
    return report


class ProbeArguments(argparse.Namespace):
    config: str | None = None
    port: str | None = None
    local_name: str = DEFAULT_LOCAL_NAME
    cycles: int = DEFAULT_CYCLES
    settle: float = DEFAULT_SETTLE_SECONDS
    transport: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repeat GAP init/advertise/power-off/unregister sessions without resetting the target"
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
            transport=arguments.transport,
        )
    except (
        AutoPtsAdapterError,
        BtpGapLifecycleProbeError,
        ConfigError,
        HostToolError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"BTP GAP lifecycle probe error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
