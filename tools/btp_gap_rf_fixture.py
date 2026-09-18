from __future__ import annotations

import argparse
import json
import string
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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
DEFAULT_SERVICE_UUID16 = "fdf0"


class BtpGapRfFixtureError(RuntimeError):
    """Raised when the independent nRF GAP RF fixture cannot complete safely."""


def normalize_uuid16(value: str) -> str:
    normalized = value.strip().lower().removeprefix("0x")
    if len(normalized) != 4 or any(character not in string.hexdigits for character in normalized):
        raise BtpGapRfFixtureError("service UUID selector must contain exactly four hex digits")
    return normalized


def canonical_uuid16(value: str) -> str:
    return f"0000{normalize_uuid16(value)}-0000-1000-8000-00805f9b34fb"


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGapRfFixtureError(
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
        name: {"expected": value, "actual": snapshot.current_settings.get(name)}
        for name, value in expected.items()
        if snapshot.current_settings.get(name) is not value
    }
    if mismatches:
        raise BtpGapRfFixtureError(f"unexpected GAP settings: {mismatches}")


def _ready_document(
    *,
    run_id: str,
    identity: SerialIdentity,
    local_name: str,
    service_uuid16: str,
    controller: GapSnapshot,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "port": identity.port,
        "local_name": local_name,
        "service_uuid16": service_uuid16,
        "service_uuid": canonical_uuid16(service_uuid16),
        "peripheral_address": controller.address,
        "next_action": "start the independent Central/DUT process now",
    }


def _emit_marker(name: str, document: dict[str, object]) -> None:
    print(f"{name} {json.dumps(document, sort_keys=True)}", flush=True)


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
    service_uuid16: str,
    capabilities: CapabilitySnapshot | None,
    steps: list[dict[str, object]],
    cleanup_errors: list[str],
    cleanup_classification: str,
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
            "nRF Peripheral-side BTP/GAP and BLE connection facts only; "
            "the independent Central/DUT is not invoked or judged"
        ),
        "transport": "fixed AutoPTS IutCtl/BTPSocketSrv/BTPWorker via socat",
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "advertising": {
            "local_name": local_name,
            "service_uuid16": service_uuid16,
            "canonical_service_uuid": canonical_uuid16(service_uuid16),
            "selector_scope": "advertising only; no dynamic GATT service",
        },
        "capabilities": _capability_document(capabilities),
        "steps": steps,
        "fact_boundary": {
            "included": "BTP responses and nRF GAP connection/disconnection events",
            "excluded": "Central/DUT process execution, events, result, and pass/fail judgment",
            "correlation": "use run_id and timestamps in an external HIL runner or report",
        },
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification,
    }
    return write_json_report(reports_root, "btp-gap-rf-fixture", document, now=finished_at)


def run_fixture(
    settings: dict[str, ResolvedValue],
    *,
    port_name: str | None = None,
    local_name: str = DEFAULT_LOCAL_NAME,
    service_uuid16: str = DEFAULT_SERVICE_UUID16,
    timeout_seconds: int = 60,
    transport: str = "dongle",
) -> Path:
    if not 5 <= timeout_seconds <= 600:
        raise BtpGapRfFixtureError("timeout must be in range 5..=600 seconds")
    selector = normalize_uuid16(service_uuid16)

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
        raise BtpGapRfFixtureError(str(error)) from error

    reports_root = _required_path(settings, "reports_dir")
    socat = managed_socat_path(socat_pin, settings)
    run_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    print(f"Run ID: {run_id}")
    print(f"Selected application port: {identity.port} ({identity.description}; {identity.hwid})")
    print(f"AutoPTS tty transport: {autopts_tty_file(identity.port)}")

    capabilities: CapabilitySnapshot | None = None
    steps: list[dict[str, object]] = []
    main_error: Exception | None = None
    operation_cleanup_errors: list[str] = []
    advertising_started = False
    session = AutoPtsSession(
        loaded,
        identity=identity,
        log_dir=reports_root / "btp-gap-rf-fixture" / run_id,
        socat_path=socat,
    )
    try:
        capabilities = session.start(
            required_services=("CORE", "GAP"),
            local_name=local_name,
        )
        controller = session.read_controller_info()
        if controller.address is None or controller.address_type is None:
            raise BtpGapRfFixtureError(
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

        advertising = session.start_advertising(local_name, service_uuid16=selector)
        advertising_started = True
        _require_settings(
            advertising,
            Powered=True,
            Connectable=True,
            Discoverable=True,
            Advertising=True,
        )
        _record_step(steps, "start-advertising", advertising)
        _emit_marker(
            "NRFTEST_FIXTURE_READY",
            _ready_document(
                run_id=run_id,
                identity=identity,
                local_name=local_name,
                service_uuid16=selector,
                controller=controller,
            ),
        )

        connected = session.wait_for_connection(timeout_seconds)
        if len(connected.connections) != 1:
            count = len(connected.connections)
            raise BtpGapRfFixtureError(
                f"expected one nRF connection after the BTP event, found {count}"
            )
        peer = connected.connections[0]
        _record_step(steps, "peripheral-observed-connected", connected)
        _emit_marker(
            "NRFTEST_FIXTURE_CONNECTED",
            {"run_id": run_id, "peer_address": peer.address},
        )

        disconnected = session.wait_for_disconnection(
            timeout_seconds,
            address=peer.address,
        )
        if any(connection.address == peer.address for connection in disconnected.connections):
            raise BtpGapRfFixtureError(
                "BTP disconnected event arrived but the peer remains in the GAP connection state"
            )
        _record_step(steps, "peripheral-observed-disconnected", disconnected)
        _emit_marker(
            "NRFTEST_FIXTURE_DISCONNECTED",
            {"run_id": run_id, "peer_address": peer.address},
        )
    except Exception as error:
        main_error = error
    finally:
        if advertising_started:
            try:
                current = session.gap_snapshot()
                if current.current_settings.get("Advertising"):
                    stopped = session.stop_advertising()
                    _record_step(steps, "cleanup-stop-advertising", stopped)
            except Exception as error:
                operation_cleanup_errors.append(
                    f"stop advertising: {type(error).__name__}: {error}"
                )
        session.close()

    all_cleanup_errors = operation_cleanup_errors + list(session.cleanup_errors)
    cleanup_classification = session.cleanup_classification
    if operation_cleanup_errors:
        cleanup_classification = "unexpected"

    if main_error is not None or cleanup_classification == "unexpected":
        details: list[str] = []
        if main_error is not None:
            details.append(f"{type(main_error).__name__}: {main_error}")
        details.extend(all_cleanup_errors)
        report = _write_report(
            reports_root,
            run_id=run_id,
            started_at=started_at,
            outcome="peripheral-rf-failure",
            detail="; ".join(details),
            identity=identity,
            autopts_commit=upstream.autopts.commit,
            local_name=local_name,
            service_uuid16=selector,
            capabilities=capabilities,
            steps=steps,
            cleanup_errors=all_cleanup_errors,
            cleanup_classification=cleanup_classification,
        )
        raise BtpGapRfFixtureError(
            f"nRF Peripheral GAP RF fixture failed; report: {report}"
        ) from main_error

    outcome = (
        "peripheral-pass-with-known-upstream-cleanup-status-defect"
        if cleanup_classification == "known-upstream-unregister-status-defect"
        else "peripheral-pass"
    )
    detail = "; ".join(all_cleanup_errors) if all_cleanup_errors else None
    report = _write_report(
        reports_root,
        run_id=run_id,
        started_at=started_at,
        outcome=outcome,
        detail=detail,
        identity=identity,
        autopts_commit=upstream.autopts.commit,
        local_name=local_name,
        service_uuid16=selector,
        capabilities=capabilities,
        steps=steps,
        cleanup_errors=all_cleanup_errors,
        cleanup_classification=cleanup_classification,
    )
    print("nRF Peripheral GAP RF fixture: PASS")
    print(f"Peripheral-only report: {report}")
    return report


class FixtureArguments(argparse.Namespace):
    config: str | None = None
    port: str | None = None
    local_name: str = DEFAULT_LOCAL_NAME
    service_uuid16: str = DEFAULT_SERVICE_UUID16
    timeout: int = 60
    transport: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Advertise from the Tester and record only nRF-side GAP connection/disconnection facts"
        )
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    port_help = "exact current application serial port; otherwise use configured serial/port "
    port_help += "or the only application USB device"
    _ = parser.add_argument("--port", help=port_help)
    _ = parser.add_argument("--local-name", default=DEFAULT_LOCAL_NAME)
    _ = parser.add_argument("--service-uuid16", default=DEFAULT_SERVICE_UUID16)
    _ = parser.add_argument("--timeout", type=int, default=60)
    _ = parser.add_argument(
        "--transport",
        choices=("dongle", "dk"),
        default="dongle",
        help="application USB identity: dongle (PCA10059 CDC) or dk (J-Link VCOM)",
    )
    return parser


def main() -> int:
    arguments = FixtureArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        _ = run_fixture(
            resolve_current_settings(config_path=arguments.config),
            port_name=arguments.port,
            local_name=arguments.local_name,
            service_uuid16=arguments.service_uuid16,
            timeout_seconds=arguments.timeout,
            transport=arguments.transport,
        )
    except (
        AutoPtsAdapterError,
        BtpGapRfFixtureError,
        ConfigError,
        HostToolError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"nRF Peripheral GAP RF fixture error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
