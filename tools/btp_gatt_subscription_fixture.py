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
from host.nrftest.profile import PeripheralProfile, ProfileError, profile_source_sha256
from host.nrftest.subscription import (
    CccObservation,
    SubscriptionError,
    SubscriptionMode,
    ValueUpdateObservation,
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


class BtpGattSubscriptionFixtureError(RuntimeError):
    """Raised when the nRF-side subscription fixture cannot complete safely."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGattSubscriptionFixtureError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _mapping_document(mapping: GattProfileMapping) -> dict[str, object]:
    return {
        "action": mapping.action.value,
        "service_handle": mapping.service_handle,
        "service_end_handle": mapping.service_end_handle,
        "attribute_count": len(mapping.attributes),
        "characteristics": [asdict(characteristic) for characteristic in mapping.characteristics],
    }


def _ccc_document(observation: CccObservation) -> dict[str, object]:
    return {
        "handle": observation.handle,
        "value_hex": observation.value.hex(),
        "att_response": f"0x{observation.att_response:02x}",
        "attempts": observation.attempts,
    }


def _update_document(observation: ValueUpdateObservation) -> dict[str, object]:
    return {
        "handle": observation.handle,
        "value_hex": observation.value.hex(),
        "att_response": f"0x{observation.att_response:02x}",
        "btp_set_value_semantics": (
            "BTP command returned success; exact meaning depends on the active firmware; "
            "local readback is verified here, while RF delivery requires Central evidence"
        ),
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
    scenario: dict[str, object] | None,
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
        "scope": "nRF-side CCC state, BTP Set Value/readback, and BLE connection facts",
        "port": identity_document(identity) if identity is not None else None,
        "autopts_commit": autopts_commit,
        "profile": (
            {
                "path": str(profile_path),
                "source_sha256": profile_source_sha256(profile_path),
                "profile_id": profile.profile_id,
                "root_service_uuid": profile.service.uuid,
            }
            if profile is not None
            else None
        ),
        "capabilities": (
            {
                "supported_services_mask": f"0x{capabilities.supported_services_mask:x}",
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
        "scenario": scenario,
        "confirmation_observability": {
            "notification": "no ATT confirmation exists; Central receipt is the delivery fact",
            "indication": (
                "Zephyr callback exists but fixed Tester emits no BTP confirmation event; "
                + "Peripheral confirmation is not machine-readable through fixed AutoPTS"
            ),
        },
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification,
    }
    return write_json_report(
        reports_root,
        "btp-gatt-subscription-fixture",
        document,
        now=finished_at,
    )


def _validate_values(
    profile: PeripheralProfile,
    mode: SubscriptionMode,
    value: bytes,
    after_disable_value: bytes,
) -> None:
    specification = profile.characteristic_for_role("updates")
    if mode is SubscriptionMode.NOTIFICATION and not specification.notifiable:
        raise BtpGattSubscriptionFixtureError("updates role does not support notifications")
    if mode is SubscriptionMode.INDICATION and not specification.indicatable:
        raise BtpGattSubscriptionFixtureError("updates role does not support indications")
    expected_length = len(specification.initial_value)
    if len(value) != expected_length or len(after_disable_value) != expected_length:
        raise BtpGattSubscriptionFixtureError(
            f"updates role requires {expected_length}-byte values for the fixed Tester"
        )
    if value == specification.initial_value or after_disable_value in {
        specification.initial_value,
        value,
    }:
        raise BtpGattSubscriptionFixtureError(
            "enabled and after-disable values must be distinct from each other "
            + "and the initial value"
        )


def run_fixture(
    settings: dict[str, ResolvedValue],
    *,
    mode: SubscriptionMode,
    value: bytes,
    after_disable_value: bytes,
    profile_path: Path = DEFAULT_PROFILE,
    port_name: str | None = None,
    timeout_seconds: int = 60,
    transport: str = "dongle",
) -> Path:
    if not 5 <= timeout_seconds <= 600:
        raise BtpGattSubscriptionFixtureError("timeout must be in range 5..=600 seconds")
    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    selected_port = port_name or settings["device_port"].value
    profile = PeripheralProfile.load(profile_path)
    _validate_values(profile, mode, value, after_disable_value)
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
    scenario: dict[str, object] | None = None
    main_error: Exception | None = None
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
        updates = mapping.characteristic_for_role("updates")
        if updates.ccc_handle is None:
            raise BtpGattSubscriptionFixtureError("updates role has no mapped CCC handle")

        run_id = str(uuid4())
        advertising = fixture.start_advertising()
        steps: list[dict[str, object]] = [{"name": "start-advertising", "gap": asdict(advertising)}]
        scenario = {
            "run_id": run_id,
            "mode": mode.value,
            "expected_ccc_hex": mode.ccc_value.hex(),
            "enabled_value_hex": value.hex(),
            "after_disable_value_hex": after_disable_value.hex(),
            "steps": steps,
        }
        print(
            "NRFTEST_GATT_SUBSCRIPTION_READY "
            + json.dumps(
                {
                    "run_id": run_id,
                    "mode": mode.value,
                    "profile_id": profile.profile_id,
                    "service_uuid": profile.service.uuid,
                    "peripheral_address": controller.address,
                    "mapping": _mapping_document(mapping),
                },
                sort_keys=True,
            ),
            flush=True,
        )

        _ = fixture.wait_for_connection(timeout_seconds)
        connected = fixture.snapshot()
        if len(connected.connections) != 1:
            raise BtpGattSubscriptionFixtureError(
                f"expected one nRF connection, found {len(connected.connections)}"
            )
        peer = connected.connections[0]
        steps.append({"name": "peripheral-observed-connected", "gap": asdict(connected)})
        print(
            "NRFTEST_GATT_SUBSCRIPTION_CONNECTED "
            + json.dumps({"run_id": run_id, "peer_address": peer.address}, sort_keys=True),
            flush=True,
        )

        enabled_ccc = fixture.wait_for_subscription_state(
            mode,
            enabled=True,
            timeout=timeout_seconds,
        )
        steps.append({"name": "peripheral-observed-enabled", "ccc": _ccc_document(enabled_ccc)})
        print(
            "NRFTEST_GATT_SUBSCRIPTION_ENABLED "
            + json.dumps(
                {"run_id": run_id, "mode": mode.value, **_ccc_document(enabled_ccc)},
                sort_keys=True,
            ),
            flush=True,
        )

        enabled_update = fixture.set_value("updates", value)
        steps.append({"name": "set-value-while-enabled", "gatt": _update_document(enabled_update)})
        print(
            "NRFTEST_GATT_SUBSCRIPTION_UPDATE "
            + json.dumps(
                {"run_id": run_id, "phase": "enabled", **_update_document(enabled_update)},
                sort_keys=True,
            ),
            flush=True,
        )

        disabled_ccc = fixture.wait_for_subscription_state(
            mode,
            enabled=False,
            timeout=timeout_seconds,
        )
        steps.append({"name": "peripheral-observed-disabled", "ccc": _ccc_document(disabled_ccc)})
        print(
            "NRFTEST_GATT_SUBSCRIPTION_DISABLED "
            + json.dumps({"run_id": run_id, **_ccc_document(disabled_ccc)}, sort_keys=True),
            flush=True,
        )

        disabled_update = fixture.set_value("updates", after_disable_value)
        steps.append({"name": "set-value-after-disable", "gatt": _update_document(disabled_update)})
        print(
            "NRFTEST_GATT_SUBSCRIPTION_UPDATE "
            + json.dumps(
                {"run_id": run_id, "phase": "after-disable", **_update_document(disabled_update)},
                sort_keys=True,
            ),
            flush=True,
        )

        disconnected = fixture.wait_for_disconnection(timeout_seconds)
        steps.append({"name": "peripheral-observed-disconnected", "gap": asdict(disconnected)})
        print(
            "NRFTEST_GATT_SUBSCRIPTION_DISCONNECTED "
            + json.dumps({"run_id": run_id, "peer_address": peer.address}, sort_keys=True),
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
    outcome = "subscription-rf-pass"
    detail = None
    if main_error is not None or cleanup_classification == "unexpected":
        outcome = "subscription-failure"
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
        scenario=scenario,
        cleanup_errors=cleanup_errors,
        cleanup_classification=cleanup_classification,
    )
    if outcome != "subscription-rf-pass":
        raise BtpGattSubscriptionFixtureError(
            f"subscription fixture failed: {detail}; report: {report}"
        )

    assert mapping is not None
    print(
        f"nRF {mode.value} subscription fixture: PASS "
        + f"({mapping.action.value}; service={mapping.service_handle})"
    )
    print(f"Peripheral-only report: {report}")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one nRF Notification/Indication and post-disable update over BTP"
    )
    _ = parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    _ = parser.add_argument("--port")
    _ = parser.add_argument(
        "--mode",
        type=SubscriptionMode,
        choices=tuple(SubscriptionMode),
        required=True,
    )
    _ = parser.add_argument("--value-hex", required=True)
    _ = parser.add_argument("--after-disable-value-hex", required=True)
    _ = parser.add_argument("--timeout", type=int, default=60)
    _ = parser.add_argument(
        "--transport",
        choices=("dongle", "dk"),
        default="dongle",
        help="application USB identity: dongle (PCA10059 CDC) or dk (J-Link VCOM)",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        _ = run_fixture(
            resolve_current_settings(),
            mode=cast(SubscriptionMode, arguments.mode),
            value=bytes.fromhex(cast(str, arguments.value_hex)),
            after_disable_value=bytes.fromhex(cast(str, arguments.after_disable_value_hex)),
            profile_path=cast(Path, arguments.profile),
            port_name=cast(str | None, arguments.port) or None,
            timeout_seconds=cast(int, arguments.timeout),
            transport=cast(str, arguments.transport),
        )
    except (
        AutoPtsAdapterError,
        BtpGattSubscriptionFixtureError,
        ConfigError,
        GattProfileError,
        HostToolError,
        ProfileError,
        SetupError,
        FixtureError,
        SubscriptionError,
        TelemetryError,
        ValueError,
    ) as error:
        print(f"nRF GATT subscription fixture failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
