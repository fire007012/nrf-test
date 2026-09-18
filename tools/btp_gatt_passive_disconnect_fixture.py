from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import sleep
from typing import cast
from uuid import uuid4

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
from host.nrftest.gatt_profile import GattProfileError, GattProfileMapping, ensure_gatt_profile
from host.nrftest.passive_disconnect import (
    PassiveDisconnectError,
    request_peripheral_disconnect,
    require_peer_disconnected,
)
from host.nrftest.profile import PeripheralProfile, ProfileError, profile_source_sha256
from host.nrftest.subscription import (
    CccObservation,
    SubscriptionError,
    SubscriptionMode,
    wait_for_ccc,
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


class PassiveDisconnectAnchor(StrEnum):
    CONNECT_ONLY = "connect-only"
    NOTIFICATION = "notification"
    INDICATION = "indication"

    @property
    def subscription_mode(self) -> SubscriptionMode | None:
        if self is PassiveDisconnectAnchor.NOTIFICATION:
            return SubscriptionMode.NOTIFICATION
        if self is PassiveDisconnectAnchor.INDICATION:
            return SubscriptionMode.INDICATION
        return None


class PassiveDisconnectTrigger(StrEnum):
    GAP_DISCONNECT = "gap-disconnect"
    POWER_OFF = "power-off"


class BtpGattPassiveDisconnectFixtureError(RuntimeError):
    """Raised when the nRF-side active disconnect fixture cannot complete safely."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGattPassiveDisconnectFixtureError(
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


def _capability_document(capabilities: CapabilitySnapshot | None) -> dict[str, object] | None:
    if capabilities is None:
        return None
    return {
        "supported_services_mask": f"0x{capabilities.supported_services_mask:x}",
        "supported_command_masks": {
            service: f"0x{mask:x}" for service, mask in capabilities.supported_command_masks.items()
        },
        "service_actions": capabilities.service_actions,
    }


def _write_report(
    reports_root: Path,
    *,
    started_at: str,
    outcome: str,
    detail: str | None,
    identity: SerialIdentity,
    autopts_commit: str,
    profile_path: Path,
    profile: PeripheralProfile,
    capabilities: CapabilitySnapshot | None,
    mapping: GattProfileMapping | None,
    scenario: dict[str, object] | None,
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
        "scope": (
            "nRF-side BTP GAP disconnect command/response, disconnect event, "
            "optional CCC state, and cleanup facts"
        ),
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "profile": {
            "path": str(profile_path),
            "source_sha256": profile_source_sha256(profile_path),
            "profile_id": profile.profile_id,
            "root_service_uuid": profile.service.uuid,
        },
        "capabilities": _capability_document(capabilities),
        "mapping": _mapping_document(mapping) if mapping is not None else None,
        "scenario": scenario,
        "fact_boundary": {
            "included": "nRF BTP responses plus Peripheral-side connection/CCC/disconnection facts",
            "excluded": (
                "Central/DUT process execution, passive event, result, and resource accounting"
            ),
            "correlation": "use run_id and timestamps in an external HIL runner or report",
        },
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification,
    }
    return write_json_report(
        reports_root, "btp-gatt-passive-disconnect-fixture", document, now=finished_at
    )


def _validate_anchor(profile: PeripheralProfile, anchor: PassiveDisconnectAnchor) -> None:
    mode = anchor.subscription_mode
    if mode is None:
        return
    updates = profile.characteristic_for_role("updates")
    if mode is SubscriptionMode.NOTIFICATION and not updates.notifiable:
        raise BtpGattPassiveDisconnectFixtureError("updates role does not support notifications")
    if mode is SubscriptionMode.INDICATION and not updates.indicatable:
        raise BtpGattPassiveDisconnectFixtureError("updates role does not support indications")


def run_fixture(
    settings: dict[str, ResolvedValue],
    *,
    anchor: PassiveDisconnectAnchor,
    trigger: PassiveDisconnectTrigger = PassiveDisconnectTrigger.POWER_OFF,
    profile_path: Path = DEFAULT_PROFILE,
    port_name: str | None = None,
    timeout_seconds: int = 60,
    disconnect_delay_seconds: float = 1.0,
    transport: str = "dongle",
) -> Path:
    if not 5 <= timeout_seconds <= 600:
        raise BtpGattPassiveDisconnectFixtureError("timeout must be in range 5..=600 seconds")
    if not 0 <= disconnect_delay_seconds <= 10:
        raise BtpGattPassiveDisconnectFixtureError(
            "disconnect delay must be in range 0..=10 seconds"
        )

    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    selected_port = port_name or settings["device_port"].value
    identity = select_application_port(
        port_name=selected_port,
        serial_number=settings["device_serial"].value,
        transport=transport,
    )
    profile = PeripheralProfile.load(profile_path)
    _validate_anchor(profile, anchor)
    loaded = load_autopts(_required_path(settings, "autopts_root"))
    reports_root = _required_path(settings, "reports_dir")
    socat = managed_socat_path(socat_pin, settings)
    started_at = datetime.now(UTC).isoformat()

    print(f"Selected application port: {identity.port} ({identity.hwid})")
    print(f"AutoPTS tty transport: {autopts_tty_file(identity.port)}")
    print(f"Profile: {profile.profile_id} ({profile_source_sha256(profile_path)})")

    capabilities: CapabilitySnapshot | None = None
    mapping: GattProfileMapping | None = None
    scenario: dict[str, object] | None = None
    main_error: Exception | None = None
    operation_cleanup_errors: list[str] = []
    advertising_started = False
    radio_powered_off = False
    session = AutoPtsSession(
        loaded,
        identity=identity,
        log_dir=reports_root / "btp-gatt-passive-disconnect-fixture" / "autopts",
        socat_path=socat,
    )
    try:
        capabilities = session.start(
            required_services=("CORE", "GAP", "GATT"),
            local_name=profile.local_name,
        )
        controller = session.read_controller_info()
        _ = session.set_powered(True)
        mapping = ensure_gatt_profile(
            session,
            profile,
            command_mask=capabilities.supported_command_masks["GATT"],
        )
        updates = mapping.characteristic_for_role("updates")
        mode = anchor.subscription_mode
        if mode is not None and updates.ccc_handle is None:
            raise BtpGattPassiveDisconnectFixtureError("updates role has no mapped CCC handle")

        run_id = str(uuid4())
        _ = session.set_connectable(True)
        _ = session.set_discoverable(True)
        advertising = session.start_advertising(
            profile.local_name,
            service_uuid128=profile.service.uuid,
        )
        advertising_started = True
        steps: list[dict[str, object]] = [{"name": "start-advertising", "gap": asdict(advertising)}]
        scenario = {
            "run_id": run_id,
            "anchor": anchor.value,
            "trigger": trigger.value,
            "expected_ccc_hex": mode.ccc_value.hex() if mode is not None else None,
            "disconnect_delay_seconds": disconnect_delay_seconds,
            "steps": steps,
        }
        print(
            "NRFTEST_GATT_PASSIVE_DISCONNECT_READY "
            + json.dumps(
                {
                    "run_id": run_id,
                    "anchor": anchor.value,
                    "trigger": trigger.value,
                    "profile_id": profile.profile_id,
                    "service_uuid": profile.service.uuid,
                    "peripheral_address": controller.address,
                    "mapping": _mapping_document(mapping),
                    "next_action": "start the independent passive-disconnect Central consumer now",
                },
                sort_keys=True,
            ),
            flush=True,
        )

        connected = session.wait_for_connection(timeout_seconds)
        if len(connected.connections) != 1:
            raise BtpGattPassiveDisconnectFixtureError(
                f"expected one nRF connection, found {len(connected.connections)}"
            )
        peer = connected.connections[0]
        steps.append({"name": "peripheral-observed-connected", "gap": asdict(connected)})
        print(
            "NRFTEST_GATT_PASSIVE_DISCONNECT_CONNECTED "
            + json.dumps({"run_id": run_id, "peer_address": peer.address}, sort_keys=True),
            flush=True,
        )

        if mode is not None:
            assert updates.ccc_handle is not None
            ccc = wait_for_ccc(
                session,
                updates.ccc_handle,
                peer_address=peer.address,
                peer_address_type=peer.address_type,
                expected=mode.ccc_value,
                allowed_pending=(b"\x00\x00",),
                timeout=timeout_seconds,
            )
            steps.append({"name": "peripheral-observed-subscribed", "ccc": _ccc_document(ccc)})
            print(
                "NRFTEST_GATT_PASSIVE_DISCONNECT_SUBSCRIBED "
                + json.dumps(
                    {"run_id": run_id, "mode": mode.value, **_ccc_document(ccc)}, sort_keys=True
                ),
                flush=True,
            )

        print(
            "NRFTEST_GATT_PASSIVE_DISCONNECT_TRIGGER_PENDING "
            + json.dumps(
                {"run_id": run_id, "delay_seconds": disconnect_delay_seconds}, sort_keys=True
            ),
            flush=True,
        )
        sleep(disconnect_delay_seconds)
        if trigger is PassiveDisconnectTrigger.GAP_DISCONNECT:
            observation = request_peripheral_disconnect(session, peer, timeout=timeout_seconds)
            command_snapshot = observation.command_snapshot
            event_snapshot = observation.event_snapshot
            command_step = "peripheral-disconnect-command-accepted"
        else:
            command_snapshot = session.set_powered(False)
            radio_powered_off = True
            if command_snapshot.current_settings.get("Powered") is not False:
                raise BtpGattPassiveDisconnectFixtureError(
                    "BTP SET_POWERED(false) completed without Powered=false"
                )
            event_snapshot = session.wait_for_disconnection(
                timeout_seconds,
                address=peer.address,
            )
            require_peer_disconnected(event_snapshot, peer)
            command_step = "peripheral-radio-power-off-accepted"

        steps.append({"name": command_step, "gap": asdict(command_snapshot)})
        steps.append(
            {
                "name": "peripheral-observed-disconnected",
                "gap": asdict(event_snapshot),
            }
        )
        if radio_powered_off:
            restored = session.set_powered(True)
            if restored.current_settings.get("Powered") is not True:
                raise BtpGattPassiveDisconnectFixtureError(
                    "BTP SET_POWERED(true) completed without Powered=true"
                )
            radio_powered_off = False
            steps.append({"name": "peripheral-radio-powered-on", "gap": asdict(restored)})
        print(
            "NRFTEST_GATT_PASSIVE_DISCONNECT_DISCONNECTED "
            + json.dumps({"run_id": run_id, "peer_address": peer.address}, sort_keys=True),
            flush=True,
        )
    except Exception as error:
        main_error = error
    finally:
        if radio_powered_off:
            try:
                _ = session.set_powered(True)
                radio_powered_off = False
            except Exception as error:
                operation_cleanup_errors.append(
                    f"restore radio power: {type(error).__name__}: {error}"
                )
        if advertising_started:
            try:
                current = session.gap_snapshot()
                if current.current_settings.get("Advertising"):
                    _ = session.stop_advertising()
            except Exception as error:
                operation_cleanup_errors.append(
                    f"stop advertising: {type(error).__name__}: {error}"
                )
        session.close()

    cleanup_errors = operation_cleanup_errors + list(session.cleanup_errors)
    cleanup_classification = session.cleanup_classification
    if operation_cleanup_errors:
        cleanup_classification = "unexpected"
    outcome = "peripheral-initiated-disconnect-pass"
    detail = None
    if main_error is not None or cleanup_classification == "unexpected":
        outcome = "peripheral-initiated-disconnect-failure"
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
    if outcome != "peripheral-initiated-disconnect-pass":
        raise BtpGattPassiveDisconnectFixtureError(
            f"Peripheral disconnect fixture failed: {detail}; report: {report}"
        )

    assert mapping is not None
    print(
        "nRF Peripheral-initiated disconnect fixture: PASS "
        + f"({mapping.action.value}; service={mapping.service_handle})"
    )
    print(f"Peripheral-only report: {report}")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Request one nRF Peripheral GAP disconnect through fixed BTP"
    )
    _ = parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    _ = parser.add_argument("--port")
    _ = parser.add_argument(
        "--anchor",
        type=PassiveDisconnectAnchor,
        choices=tuple(PassiveDisconnectAnchor),
        default=PassiveDisconnectAnchor.NOTIFICATION,
    )
    _ = parser.add_argument(
        "--trigger",
        type=PassiveDisconnectTrigger,
        choices=tuple(PassiveDisconnectTrigger),
        default=PassiveDisconnectTrigger.POWER_OFF,
    )
    _ = parser.add_argument("--timeout", type=int, default=60)
    _ = parser.add_argument("--disconnect-delay", type=float, default=1.0)
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
            anchor=cast(PassiveDisconnectAnchor, arguments.anchor),
            trigger=cast(PassiveDisconnectTrigger, arguments.trigger),
            profile_path=cast(Path, arguments.profile),
            port_name=cast(str | None, arguments.port) or None,
            timeout_seconds=cast(int, arguments.timeout),
            disconnect_delay_seconds=cast(float, arguments.disconnect_delay),
            transport=cast(str, arguments.transport),
        )
    except (
        AutoPtsAdapterError,
        BtpGattPassiveDisconnectFixtureError,
        ConfigError,
        GattProfileError,
        HostToolError,
        PassiveDisconnectError,
        ProfileError,
        SetupError,
        SubscriptionError,
        TelemetryError,
    ) as error:
        print(f"nRF GATT passive disconnect fixture failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
