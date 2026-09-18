from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
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
from host.nrftest.gatt_profile import (
    GattProfileAction,
    GattProfileError,
    GattProfileMapping,
    GattProfileRemoval,
    ensure_gatt_profile,
    gatt_profile_service_range,
    remove_gatt_profile,
)
from host.nrftest.profile import PeripheralProfile, ProfileError, profile_source_sha256
from host.nrftest.telemetry import TelemetryError, write_json_report
from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings
from tools.setup_host_tools import (
    HostToolError,
    load_socat_pin,
    managed_socat_path,
    verify_socat,
)
from tools.setup_upstream import SetupError, load_upstream_pins, verify_upstream

DEFAULT_PROFILE_A = PROJECT_ROOT / "profiles" / "blehub-nrf-basic-v1.json"
DEFAULT_PROFILE_B = PROJECT_ROOT / "profiles" / "blehub-nrf-rebuild-v1.json"


class BtpGattProfileRebuildProbeError(RuntimeError):
    """Raised when one bounded GATT Profile replacement cannot be verified safely."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGattProfileRebuildProbeError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _profile_uuids(profile: PeripheralProfile) -> set[str]:
    return {
        profile.service.uuid,
        *(characteristic.uuid for characteristic in profile.service.characteristics),
    }


def _require_distinct_profiles(
    profile_a: PeripheralProfile,
    profile_b: PeripheralProfile,
) -> None:
    if profile_a.profile_id == profile_b.profile_id:
        raise BtpGattProfileRebuildProbeError(
            "Profile A and Profile B must use distinct profile IDs"
        )
    overlap = sorted(_profile_uuids(profile_a).intersection(_profile_uuids(profile_b)))
    if overlap:
        raise BtpGattProfileRebuildProbeError(
            "Profile A and Profile B UUIDs must be disjoint: " + ", ".join(overlap)
        )


def _mapping_document(mapping: GattProfileMapping) -> dict[str, object]:
    return {
        "action": mapping.action.value,
        "service_handle": mapping.service_handle,
        "service_end_handle": mapping.service_end_handle,
        "attribute_count": len(mapping.attributes),
        "attribute_handles": [attribute.handle for attribute in mapping.attributes],
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


def _profile_document(path: Path, profile: PeripheralProfile) -> dict[str, object]:
    return {
        "path": str(path),
        "source_sha256": profile_source_sha256(path),
        "profile_id": profile.profile_id,
        "root_service_uuid": profile.service.uuid,
        "semantic_signature": profile.semantic_signature(),
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


def _write_report(
    reports_root: Path,
    *,
    started_at: str,
    outcome: str,
    detail: str | None,
    identity: SerialIdentity,
    autopts_commit: str,
    profile_a_path: Path,
    profile_a: PeripheralProfile,
    profile_b_path: Path,
    profile_b: PeripheralProfile,
    capabilities: CapabilitySnapshot | None,
    mapping_a: GattProfileMapping | None,
    removal: GattProfileRemoval | None,
    mapping_b: GattProfileMapping | None,
    mutation_started: bool,
    cleanup_errors: list[str],
    cleanup_classification: str,
) -> Path:
    finished_at = datetime.now(UTC)
    reused_handles = (
        sorted(
            {attribute.handle for attribute in mapping_a.attributes}.intersection(
                attribute.handle for attribute in mapping_b.attributes
            )
        )
        if mapping_a is not None and mapping_b is not None
        else []
    )
    document = {
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "scope": "one nRF-side BTP GATT Profile A removal and distinct Profile B rebuild",
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "profile_a": _profile_document(profile_a_path, profile_a),
        "profile_b": _profile_document(profile_b_path, profile_b),
        "capabilities": _capability_document(capabilities) if capabilities is not None else None,
        "mapping_a": _mapping_document(mapping_a) if mapping_a is not None else None,
        "removal": asdict(removal) if removal is not None else None,
        "mapping_b": _mapping_document(mapping_b) if mapping_b is not None else None,
        "numeric_handles_reused_after_rediscovery": reused_handles,
        "lifecycle_boundary": {
            "mutation_started": mutation_started,
            "core_service_unregistered": False,
            "core_service_registered_again": False,
            "target_reset_commanded": False,
            "target_reset_required_after_probe": mutation_started,
            "target_state": (
                "profile-b-resident-reset-required"
                if outcome == "profile-rebuild-pass"
                else "indeterminate-reset-required"
                if mutation_started
                else "unchanged"
            ),
            "proof_limit": (
                "BTP opcode 0x23 removes the effective native service but fixed Tester allocation "
                "counters, backing arrays, value storage, and CCC bookkeeping are append-only and "
                "not reclaimed. This one replacement is not a reusable database-reset boundary."
            ),
        },
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification,
    }
    return write_json_report(
        reports_root, "btp-gatt-profile-rebuild-probe", document, now=finished_at
    )


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    profile_a_path: Path = DEFAULT_PROFILE_A,
    profile_b_path: Path = DEFAULT_PROFILE_B,
    port_name: str | None = None,
    transport: str = "dongle",
) -> Path:
    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    profile_a = PeripheralProfile.load(profile_a_path)
    profile_b = PeripheralProfile.load(profile_b_path)
    _require_distinct_profiles(profile_a, profile_b)
    identity = select_application_port(
        port_name=port_name or settings["device_port"].value,
        serial_number=settings["device_serial"].value,
        transport=transport,
    )
    loaded = load_autopts(_required_path(settings, "autopts_root"))
    reports_root = _required_path(settings, "reports_dir")
    started_at = datetime.now(UTC).isoformat()

    print(f"Selected application port: {identity.port} ({identity.hwid})")
    print(f"AutoPTS tty transport: {autopts_tty_file(identity.port)}")
    print(f"Profile A: {profile_a.profile_id} ({profile_source_sha256(profile_a_path)})")
    print(f"Profile B: {profile_b.profile_id} ({profile_source_sha256(profile_b_path)})")
    print("This bounded probe mutates the dynamic database and requires target reset afterward.")

    capabilities: CapabilitySnapshot | None = None
    mapping_a: GattProfileMapping | None = None
    removal: GattProfileRemoval | None = None
    mapping_b: GattProfileMapping | None = None
    mutation_started = False
    main_error: Exception | None = None
    session = AutoPtsSession(
        loaded,
        identity=identity,
        log_dir=reports_root / "btp-gatt-profile-rebuild-probe" / "autopts",
        socat_path=managed_socat_path(socat_pin, settings),
    )
    try:
        capabilities = session.start(
            required_services=("CORE", "GAP", "GATT"),
            local_name=profile_a.local_name,
        )
        controller = session.read_controller_info()
        if controller.connections:
            raise BtpGattProfileRebuildProbeError(
                f"database mutation requires zero connections; found {len(controller.connections)}"
            )
        if controller.current_settings.get("Advertising"):
            raise BtpGattProfileRebuildProbeError(
                "database mutation requires advertising to be stopped before the probe"
            )
        _ = session.set_powered(True)
        command_mask = capabilities.supported_command_masks["GATT"]
        if gatt_profile_service_range(session, profile_b) is not None:
            raise BtpGattProfileRebuildProbeError(
                "Profile B is already resident; refusing an ambiguous repeated rebuild"
            )

        mapping_a = ensure_gatt_profile(session, profile_a, command_mask=command_mask)
        mutation_started = True
        removal = remove_gatt_profile(
            session,
            profile_a,
            mapping_a,
            command_mask=command_mask,
        )
        mapping_b = ensure_gatt_profile(session, profile_b, command_mask=command_mask)
        if mapping_b.action is not GattProfileAction.BUILT:
            raise BtpGattProfileRebuildProbeError(
                "Profile B was not newly built after Profile A removal"
            )
        if gatt_profile_service_range(session, profile_a) is not None:
            raise BtpGattProfileRebuildProbeError(
                "Profile A reappeared in the effective database after Profile B build"
            )
    except Exception as error:
        main_error = error
    finally:
        session.close(unregister_services=False)

    cleanup_errors = list(session.cleanup_errors)
    outcome = "profile-rebuild-pass"
    detail = None
    if main_error is not None or session.cleanup_classification == "unexpected":
        outcome = "profile-rebuild-failure"
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
        profile_a_path=profile_a_path,
        profile_a=profile_a,
        profile_b_path=profile_b_path,
        profile_b=profile_b,
        capabilities=capabilities,
        mapping_a=mapping_a,
        removal=removal,
        mapping_b=mapping_b,
        mutation_started=mutation_started,
        cleanup_errors=cleanup_errors,
        cleanup_classification=session.cleanup_classification,
    )
    print(f"Report: {report}")
    if outcome != "profile-rebuild-pass":
        raise BtpGattProfileRebuildProbeError(detail or "profile rebuild failed")
    print("nRF one-shot GATT Profile rebuild probe: PASS")
    print("Target reset is required before another canonical Profile lifecycle.")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove one verified dynamic GATT Profile and build one distinct replacement"
    )
    _ = parser.add_argument("--profile-a", default=str(DEFAULT_PROFILE_A))
    _ = parser.add_argument("--profile-b", default=str(DEFAULT_PROFILE_B))
    _ = parser.add_argument("--port", help="explicit application COM/tty port")
    _ = parser.add_argument("--config", help="path to machine-local TOML configuration")
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
        settings = resolve_current_settings(config_path=arguments.config)
        _ = run_probe(
            settings,
            profile_a_path=Path(arguments.profile_a),
            profile_b_path=Path(arguments.profile_b),
            port_name=arguments.port,
            transport=arguments.transport,
        )
    except (
        AutoPtsAdapterError,
        BtpGattProfileRebuildProbeError,
        ConfigError,
        GattProfileError,
        HostToolError,
        ProfileError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"BTP GATT Profile rebuild probe failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
