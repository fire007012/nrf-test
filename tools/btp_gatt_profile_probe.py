from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from host.nrftest.autopts_adapter import (
    AutoPtsAdapterError,
    CapabilitySnapshot,
    SerialIdentity,
    autopts_tty_file,
    identity_document,
)
from host.nrftest.fixture import FixtureEnvironment, FixtureError, PeripheralFixture
from host.nrftest.gatt_profile import GattProfileError, GattProfileMapping
from host.nrftest.profile import (
    PeripheralProfile,
    ProfileError,
    profile_source_sha256,
)
from host.nrftest.telemetry import TelemetryError, write_json_report
from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings
from tools.setup_host_tools import (
    HostToolError,
    load_socat_pin,
    managed_socat_path,
    verify_socat,
)
from tools.setup_upstream import SetupError, load_upstream_pins, verify_upstream

DEFAULT_PROFILE = PROJECT_ROOT / "profiles" / "blehub-nrf-basic-v1.json"


class BtpGattProfileProbeError(RuntimeError):
    """Raised when the nRF-side dynamic GATT Profile probe cannot complete safely."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGattProfileProbeError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _mapping_document(mapping: GattProfileMapping) -> dict[str, object]:
    return {
        "action": mapping.action.value,
        "service_handle": mapping.service_handle,
        "service_end_handle": mapping.service_end_handle,
        "attribute_count": len(mapping.attributes),
        "attributes": [
            {
                "handle": attribute.handle,
                "permissions": f"0x{attribute.permissions:02x}",
                "type_uuid": attribute.type_uuid,
            }
            for attribute in mapping.attributes
        ],
        "characteristics": [
            {
                "role": characteristic.role,
                "uuid": characteristic.uuid,
                "declaration_handle": characteristic.declaration_handle,
                "value_handle": characteristic.value_handle,
                "ccc_handle": characteristic.ccc_handle,
            }
            for characteristic in mapping.characteristics
        ],
    }


def _write_report(
    reports_root: Path,
    *,
    started_at: str,
    outcome: str,
    detail: str | None,
    identity: SerialIdentity | None,
    autopts_commit: str,
    profile_path: Path,
    profile: PeripheralProfile | None,
    capabilities: CapabilitySnapshot | None,
    mapping: GattProfileMapping | None,
    rf: dict[str, object] | None,
    cleanup_errors: list[str],
    cleanup_classification: str,
) -> Path:
    finished_at = datetime.now(UTC)
    document: dict[str, object] = {
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "scope": (
            "nRF-side BTP dynamic GATT database plus BLE connection facts"
            if rf is not None
            else "nRF-side BTP dynamic GATT database build/attach and local verification only"
        ),
        "port": identity_document(identity) if identity is not None else None,
        "autopts_commit": autopts_commit,
        "profile": (
            {
                "path": str(profile_path),
                "source_sha256": profile_source_sha256(profile_path),
                "profile_id": profile.profile_id,
                "root_service_uuid": profile.service.uuid,
                "semantic_signature": profile.semantic_signature(),
            }
            if profile is not None
            else None
        ),
        "capabilities": (
            {
                "supported_services_mask": f"0x{capabilities.supported_services_mask:x}",
                "supported_services": list(capabilities.supported_services),
                "supported_command_masks": {
                    service: f"0x{mask:x}"
                    for service, mask in capabilities.supported_command_masks.items()
                },
                "service_actions": capabilities.service_actions,
            }
            if capabilities is not None
            else None
        ),
        "mapping": _mapping_document(mapping) if mapping is not None else None,
        "rf": rf,
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification,
    }
    return write_json_report(
        reports_root,
        "btp-gatt-profile-probe",
        document,
        now=finished_at,
    )


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    profile_path: Path = DEFAULT_PROFILE,
    port_name: str | None = None,
    rf_timeout_seconds: int | None = None,
    expected_write_role: str | None = None,
    expected_write_value: bytes | None = None,
    transport: str = "dongle",
) -> Path:
    if rf_timeout_seconds is not None and not 5 <= rf_timeout_seconds <= 600:
        raise BtpGattProfileProbeError("RF timeout must be in range 5..=600 seconds")
    if (expected_write_role is None) != (expected_write_value is None):
        raise BtpGattProfileProbeError("expected write role and value must be provided together")
    if expected_write_role is not None and rf_timeout_seconds is None:
        raise BtpGattProfileProbeError("expected write verification requires an RF timeout")
    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    selected_port = port_name or settings["device_port"].value
    profile = PeripheralProfile.load(profile_path)
    reports_root = _required_path(settings, "reports_dir")
    environment = FixtureEnvironment(
        autopts_root=_required_path(settings, "autopts_root"),
        socat_path=managed_socat_path(socat_pin, settings),
        reports_root=reports_root,
        port_name=selected_port,
        device_serial=settings["device_serial"].value,
        transport=transport,
    )
    started_at = datetime.now(UTC).isoformat()

    capabilities: CapabilitySnapshot | None = None
    mapping: GattProfileMapping | None = None
    main_error: Exception | None = None
    rf: dict[str, object] | None = None
    fixture: PeripheralFixture | None = None
    identity: SerialIdentity | None = None
    try:
        fixture = PeripheralFixture.open(environment)
        identity = fixture.identity
        print(f"Selected application port: {identity.port} ({identity.hwid})")
        print(f"AutoPTS tty transport: {autopts_tty_file(identity.port)}")
        print(f"Profile: {profile.profile_id} ({profile_source_sha256(profile_path)})")

        doctor = fixture.doctor(local_name=profile.local_name)
        capabilities = doctor.capabilities
        controller = doctor.controller
        mapping = fixture.load_profile(profile)
        if rf_timeout_seconds is not None:
            expected_write_handle: int | None = None
            if expected_write_role is not None and expected_write_value is not None:
                specification = profile.characteristic_for_role(expected_write_role)
                if not (specification.writable or specification.writable_without_response):
                    raise BtpGattProfileProbeError(f"role {expected_write_role} is not writable")
                expected_length = len(specification.initial_value)
                if len(expected_write_value) != expected_length:
                    raise BtpGattProfileProbeError(
                        f"role {expected_write_role} requires {expected_length}-byte writes, "
                        + f"got {len(expected_write_value)} bytes"
                    )
                fixture_mapping = fixture.mapping
                if fixture_mapping is None:
                    raise BtpGattProfileProbeError(
                        "fixture did not retain the loaded Profile mapping"
                    )
                expected_write_handle = fixture_mapping.characteristic_for_role(
                    expected_write_role
                ).value_handle
                fixture.clear_write_events(expected_write_role)

            run_id = str(uuid4())
            advertising = fixture.start_advertising()
            rf_steps: list[dict[str, object]] = [
                {"name": "start-advertising", "gap": asdict(advertising)}
            ]
            rf = {
                "run_id": run_id,
                "advertising": {
                    "local_name": profile.local_name,
                    "service_uuid": profile.service.uuid,
                    "service_uuid_scope": "advertising selector and registered GATT root service",
                },
                "steps": rf_steps,
                "expected_write": (
                    {
                        "role": expected_write_role,
                        "handle": expected_write_handle,
                        "value_hex": expected_write_value.hex(),
                    }
                    if expected_write_role is not None and expected_write_value is not None
                    else None
                ),
            }
            ready = {
                "run_id": run_id,
                "profile_id": profile.profile_id,
                "local_name": profile.local_name,
                "service_uuid": profile.service.uuid,
                "peripheral_address": controller.address,
                "mapping": _mapping_document(mapping),
                "expected_write": rf["expected_write"],
            }
            print("NRFTEST_GATT_FIXTURE_READY " + json.dumps(ready, sort_keys=True), flush=True)

            _ = fixture.wait_for_connection(rf_timeout_seconds)
            connected = fixture.snapshot()
            if len(connected.connections) != 1:
                raise BtpGattProfileProbeError(
                    f"expected one nRF connection, found {len(connected.connections)}"
                )
            peer = connected.connections[0]
            rf_steps.append({"name": "peripheral-observed-connected", "gap": asdict(connected)})
            print(
                "NRFTEST_GATT_FIXTURE_CONNECTED "
                + json.dumps(
                    {"run_id": run_id, "peer_address": peer.address},
                    sort_keys=True,
                ),
                flush=True,
            )

            if expected_write_handle is not None and expected_write_value is not None:
                assert expected_write_role is not None
                changed = fixture.wait_for_write(
                    expected_write_role,
                    timeout=rf_timeout_seconds,
                    expected=expected_write_value,
                )
                if changed.value != expected_write_value or changed.changed_count != 1:
                    raise BtpGattProfileProbeError(
                        f"unexpected Attribute Value Changed for handle {expected_write_handle}: "
                        + f"value={changed.value.hex()} count={changed.changed_count}"
                    )
                rf_steps.append(
                    {
                        "name": "peripheral-observed-write",
                        "gatt": {
                            "handle": changed.handle,
                            "value_hex": changed.value.hex(),
                            "changed_count": changed.changed_count,
                        },
                    }
                )
                print(
                    "NRFTEST_GATT_FIXTURE_WRITE "
                    + json.dumps(
                        {
                            "run_id": run_id,
                            "role": expected_write_role,
                            "handle": changed.handle,
                            "value_hex": changed.value.hex(),
                            "changed_count": changed.changed_count,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

            disconnected = fixture.wait_for_disconnection(rf_timeout_seconds)
            rf_steps.append(
                {"name": "peripheral-observed-disconnected", "gap": asdict(disconnected)}
            )
            print(
                "NRFTEST_GATT_FIXTURE_DISCONNECTED "
                + json.dumps(
                    {"run_id": run_id, "peer_address": peer.address},
                    sort_keys=True,
                ),
                flush=True,
            )
    except Exception as error:
        main_error = error
    finally:
        if fixture is not None:
            fixture.close()

    cleanup_errors = list(fixture.cleanup_errors) if fixture is not None else []
    cleanup_classification = (
        fixture.cleanup_classification if fixture is not None else "not-started"
    )
    success_outcome = "profile-rf-pass" if rf_timeout_seconds is not None else "profile-pass"
    outcome = success_outcome
    detail = None
    if main_error is not None or cleanup_classification == "unexpected":
        outcome = "profile-failure"
        details: list[str] = []
        if main_error is not None:
            details.append(f"{type(main_error).__name__}: {main_error}")
        details.extend(cleanup_errors)
        detail = "; ".join(details)

    report = _write_report(
        reports_root,
        started_at=started_at,
        outcome=outcome,
        detail=detail,
        identity=identity,
        autopts_commit=upstream.autopts.commit,
        profile_path=profile_path,
        profile=profile,
        capabilities=capabilities,
        mapping=mapping,
        rf=rf,
        cleanup_errors=cleanup_errors,
        cleanup_classification=cleanup_classification,
    )
    if outcome != success_outcome:
        raise BtpGattProfileProbeError(detail or "dynamic GATT Profile probe failed")

    assert mapping is not None
    label = (
        "nRF dynamic GATT RF fixture"
        if rf_timeout_seconds is not None
        else "nRF dynamic GATT Profile"
    )
    print(
        f"{label}: PASS "
        + f"({mapping.action.value}; service={mapping.service_handle}; "
        + f"attributes={len(mapping.attributes)})"
    )
    print(f"Peripheral-only report: {report}")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe the nRF dynamic GATT Profile over BTP")
    _ = parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    _ = parser.add_argument("--port")
    _ = parser.add_argument("--rf-timeout", type=int)
    _ = parser.add_argument("--expected-write-role")
    _ = parser.add_argument("--expected-write-hex")
    _ = parser.add_argument(
        "--transport",
        choices=("dongle", "dk"),
        default="dongle",
        help="application USB identity: dongle (PCA10059 CDC) or dk (J-Link VCOM)",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    profile_path = cast(Path, arguments.profile)
    port = cast(str | None, arguments.port) or None
    rf_timeout = cast(int | None, arguments.rf_timeout)
    expected_write_role = cast(str | None, arguments.expected_write_role)
    expected_write_hex = cast(str | None, arguments.expected_write_hex)
    transport = cast(str, arguments.transport)
    try:
        expected_write_value = (
            bytes.fromhex(expected_write_hex) if expected_write_hex is not None else None
        )
        _ = run_probe(
            resolve_current_settings(),
            profile_path=profile_path,
            port_name=port,
            rf_timeout_seconds=rf_timeout,
            expected_write_role=expected_write_role,
            expected_write_value=expected_write_value,
            transport=transport,
        )
    except (
        AutoPtsAdapterError,
        BtpGattProfileProbeError,
        ConfigError,
        FixtureError,
        GattProfileError,
        HostToolError,
        ProfileError,
        SetupError,
        TelemetryError,
        ValueError,
    ) as error:
        print(f"Dynamic GATT Profile probe failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
