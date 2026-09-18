from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from host.nrftest.autopts_adapter import (
    AutoPtsAdapterError,
    AutoPtsSession,
    CapabilitySnapshot,
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


class BtpCoreProbeError(RuntimeError):
    """Raised when the fixed AutoPTS Core probe cannot complete safely."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpCoreProbeError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _write_report(
    reports_root: Path,
    *,
    started_at: str,
    outcome: str,
    detail: str | None,
    identity: SerialIdentity,
    autopts_commit: str,
    capabilities: CapabilitySnapshot | None,
    cleanup_errors: list[str],
    cleanup_classification_value: str,
) -> Path:
    finished_at = datetime.now(UTC)
    document = {
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "transport": "fixed AutoPTS IutCtl/BTPSocketSrv/BTPWorker via socat",
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "supported_services_mask": (
            f"0x{capabilities.supported_services_mask:x}" if capabilities else None
        ),
        "supported_services": list(capabilities.supported_services) if capabilities else [],
        "supported_command_masks": (
            {
                service: f"0x{mask:x}"
                for service, mask in capabilities.supported_command_masks.items()
            }
            if capabilities
            else {}
        ),
        "service_actions": capabilities.service_actions if capabilities else {},
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification_value,
    }
    return write_json_report(reports_root, "btp-core-probe", document, now=finished_at)


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    port_name: str | None = None,
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
        raise BtpCoreProbeError(str(error)) from error

    reports_root = _required_path(settings, "reports_dir")
    socat = managed_socat_path(socat_pin, settings)
    print(f"Selected application port: {identity.port} ({identity.description}; {identity.hwid})")
    print(f"Fixed AutoPTS checkout: {_required_path(settings, 'autopts_root')}")
    print(f"Managed socat: {socat}")
    print(f"AutoPTS tty transport: {autopts_tty_file(identity.port)}")

    started_at = datetime.now(UTC).isoformat()
    session = AutoPtsSession(
        loaded,
        identity=identity,
        log_dir=reports_root / "btp-core-probe",
        socat_path=socat,
    )
    capabilities: CapabilitySnapshot | None = None
    protocol_error: Exception | None = None
    try:
        capabilities = session.start(required_services=("CORE", "GAP", "GATT"))
    except Exception as error:
        protocol_error = error
    finally:
        session.close()

    cleanup_errors = list(session.cleanup_errors)
    cleanup_classification_value = session.cleanup_classification
    if protocol_error is not None or cleanup_classification_value == "unexpected":
        detail_parts: list[str] = []
        if protocol_error is not None:
            detail_parts.append(f"{type(protocol_error).__name__}: {protocol_error}")
        detail_parts.extend(cleanup_errors)
        report = _write_report(
            reports_root,
            started_at=started_at,
            outcome="protocol-failure",
            detail="; ".join(detail_parts),
            identity=identity,
            autopts_commit=upstream.autopts.commit,
            capabilities=capabilities,
            cleanup_errors=cleanup_errors,
            cleanup_classification_value=cleanup_classification_value,
        )
        raise BtpCoreProbeError(f"BTP Core probe failed; report: {report}") from protocol_error

    assert capabilities is not None
    outcome = (
        "pass-with-known-upstream-cleanup-status-defect"
        if cleanup_classification_value == "known-upstream-unregister-status-defect"
        else "pass"
    )
    detail = "; ".join(cleanup_errors) if cleanup_errors else None
    report = _write_report(
        reports_root,
        started_at=started_at,
        outcome=outcome,
        detail=detail,
        identity=identity,
        autopts_commit=upstream.autopts.commit,
        capabilities=capabilities,
        cleanup_errors=cleanup_errors,
        cleanup_classification_value=cleanup_classification_value,
    )
    success_message = f"mask=0x{capabilities.supported_services_mask:x}; "
    success_message += f"services={list(capabilities.supported_services)}; "
    success_message += f"commands={capabilities.supported_command_masks}; "
    success_message += f"actions={capabilities.service_actions}"
    print(f"BTP Core probe passed: {success_message}")
    print(f"BTP Core probe report: {report}")
    return report


class ProbeArguments(argparse.Namespace):
    config: str | None = None
    port: str | None = None
    transport: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe the Tester with the fixed AutoPTS Core client"
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    port_help = "exact current application serial port; otherwise use configured serial/port "
    port_help += "or the only application USB device"
    _ = parser.add_argument("--port", help=port_help)
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
            transport=arguments.transport,
        )
    except (
        AutoPtsAdapterError,
        BtpCoreProbeError,
        ConfigError,
        HostToolError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"BTP Core probe error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
