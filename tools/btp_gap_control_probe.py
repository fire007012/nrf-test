from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from host.nrftest.autopts_adapter import (
    AutoPtsAdapterError,
    AutoPtsSession,
    CapabilitySnapshot,
    GapSnapshot,
    SerialIdentity,
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

DEFAULT_LOCAL_NAME = "NrftestP1"


class BtpGapControlProbeError(RuntimeError):
    """Raised when the fixed AutoPTS GAP control probe fails."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGapControlProbeError(
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


def _capability_document(capabilities: CapabilitySnapshot | None) -> dict[str, object] | None:
    if capabilities is None:
        return None
    return {
        "supported_services_mask": f"0x{capabilities.supported_services_mask:x}",
        "supported_services": list(capabilities.supported_services),
        "supported_command_masks": {
            service: f"0x{mask:x}" for service, mask in capabilities.supported_command_masks.items()
        },
        "service_actions": capabilities.service_actions,
    }


def _record_step(steps: list[dict[str, object]], name: str, snapshot: GapSnapshot) -> None:
    steps.append(
        {
            "name": name,
            "observed_at_utc": datetime.now(UTC).isoformat(),
            "gap": _snapshot_document(snapshot),
        }
    )


def _require_settings(snapshot: GapSnapshot, **expected: bool) -> None:
    mismatches = {
        name: {
            "expected": value,
            "actual": snapshot.current_settings.get(name),
        }
        for name, value in expected.items()
        if snapshot.current_settings.get(name) is not value
    }
    if mismatches:
        raise BtpGapControlProbeError(f"unexpected GAP settings: {mismatches}")


def _write_report(
    reports_root: Path,
    *,
    started_at: str,
    outcome: str,
    detail: str | None,
    identity: SerialIdentity,
    autopts_commit: str,
    local_name: str,
    capabilities: CapabilitySnapshot | None,
    steps: list[dict[str, object]],
    cleanup_errors: list[str],
    cleanup_classification: str,
) -> Path:
    finished_at = datetime.now(UTC)
    document = {
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "scope": "BTP Core/GAP control only; no independent BLE RF observation",
        "transport": "fixed AutoPTS IutCtl/BTPSocketSrv/BTPWorker via socat",
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "advertising_local_name": local_name,
        "controller_selection": {
            "index": 0,
            "source": "fixed AutoPTS CONTROLLER_INDEX",
            "controller_index_list": (
                "not executed: fixed AutoPTS revision defines the opcode but exposes no wrapper"
            ),
        },
        "capabilities": _capability_document(capabilities),
        "steps": steps,
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification,
    }
    return write_json_report(reports_root, "btp-gap-control-probe", document, now=finished_at)


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    port_name: str | None = None,
    local_name: str = DEFAULT_LOCAL_NAME,
    transport: str = "dongle",
) -> Path:
    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    selected_port = port_name or settings["device_port"].value
    configured_serial = settings["device_serial"].value
    try:
        identity = select_application_port(
            port_name=selected_port,
            serial_number=configured_serial,
            transport=transport,
        )
        loaded = load_autopts(_required_path(settings, "autopts_root"))
    except AutoPtsAdapterError as error:
        raise BtpGapControlProbeError(str(error)) from error

    reports_root = _required_path(settings, "reports_dir")
    socat = managed_socat_path(socat_pin, settings)
    print(f"Selected application port: {identity.port} ({identity.description}; {identity.hwid})")
    print(f"Fixed AutoPTS checkout: {_required_path(settings, 'autopts_root')}")
    print(f"Managed socat: {socat}")
    print(f"AutoPTS tty transport: {autopts_tty_file(identity.port)}")

    started_at = datetime.now(UTC).isoformat()
    steps: list[dict[str, object]] = []
    capabilities: CapabilitySnapshot | None = None
    main_error: Exception | None = None
    operation_cleanup_errors: list[str] = []
    advertising_started = False
    session = AutoPtsSession(
        loaded,
        identity=identity,
        log_dir=reports_root / "btp-gap-control-probe",
        socat_path=socat,
    )
    try:
        capabilities = session.start(
            required_services=("CORE", "GAP"),
            local_name=local_name,
        )

        controller = session.read_controller_info()
        if controller.address is None or controller.address_type is None:
            raise BtpGapControlProbeError(
                "AutoPTS read-controller-info completed without a controller address/type snapshot"
            )
        _record_step(steps, "read-controller-info", controller)

        powered = session.set_powered(True)
        _require_settings(powered, Powered=True)
        _record_step(steps, "ensure-powered-on", powered)

        connectable = session.set_connectable(True)
        _require_settings(connectable, Powered=True, Connectable=True)
        _record_step(steps, "ensure-connectable", connectable)

        discoverable = session.set_discoverable(True)
        _require_settings(
            discoverable,
            Powered=True,
            Connectable=True,
            Discoverable=True,
        )
        _record_step(steps, "set-general-discoverable", discoverable)

        advertising = session.start_advertising(local_name)
        advertising_started = True
        _require_settings(
            advertising,
            Powered=True,
            Connectable=True,
            Discoverable=True,
            Advertising=True,
        )
        _record_step(steps, "start-advertising", advertising)

        stopped = session.stop_advertising()
        advertising_started = False
        _require_settings(stopped, Advertising=False)
        _record_step(steps, "stop-advertising", stopped)
    except Exception as error:
        main_error = error
    finally:
        if advertising_started:
            try:
                stopped = session.stop_advertising()
                _record_step(steps, "cleanup-stop-advertising", stopped)
            except Exception as error:
                operation_cleanup_errors.append(
                    f"stop advertising: {type(error).__name__}: {error}"
                )
        session.close()

    session_cleanup_errors = list(session.cleanup_errors)
    all_cleanup_errors = operation_cleanup_errors + session_cleanup_errors
    cleanup_classification = session.cleanup_classification
    if operation_cleanup_errors:
        cleanup_classification = "unexpected"

    if main_error is not None or cleanup_classification == "unexpected":
        detail_parts: list[str] = []
        if main_error is not None:
            detail_parts.append(f"{type(main_error).__name__}: {main_error}")
        detail_parts.extend(all_cleanup_errors)
        report = _write_report(
            reports_root,
            started_at=started_at,
            outcome="control-failure",
            detail="; ".join(detail_parts),
            identity=identity,
            autopts_commit=upstream.autopts.commit,
            local_name=local_name,
            capabilities=capabilities,
            steps=steps,
            cleanup_errors=all_cleanup_errors,
            cleanup_classification=cleanup_classification,
        )
        raise BtpGapControlProbeError(
            f"BTP GAP control probe failed; report: {report}"
        ) from main_error

    outcome = (
        "pass-with-known-upstream-cleanup-status-defect"
        if cleanup_classification == "known-upstream-unregister-status-defect"
        else "pass"
    )
    detail = "; ".join(all_cleanup_errors) if all_cleanup_errors else None
    report = _write_report(
        reports_root,
        started_at=started_at,
        outcome=outcome,
        detail=detail,
        identity=identity,
        autopts_commit=upstream.autopts.commit,
        local_name=local_name,
        capabilities=capabilities,
        steps=steps,
        cleanup_errors=all_cleanup_errors,
        cleanup_classification=cleanup_classification,
    )
    print(f"BTP GAP control probe passed: local_name={local_name!r}; steps={len(steps)}")
    print(f"BTP GAP control probe report: {report}")
    return report


class ProbeArguments(argparse.Namespace):
    config: str | None = None
    port: str | None = None
    local_name: str = DEFAULT_LOCAL_NAME
    transport: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exercise Tester Core/GAP controls through the fixed AutoPTS client"
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    port_help = "exact current application serial port; otherwise use configured serial/port "
    port_help += "or the only application USB device"
    _ = parser.add_argument("--port", help=port_help)
    _ = parser.add_argument("--local-name", default=DEFAULT_LOCAL_NAME)
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
            transport=arguments.transport,
        )
    except (
        AutoPtsAdapterError,
        BtpGapControlProbeError,
        ConfigError,
        HostToolError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"BTP GAP control probe error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
