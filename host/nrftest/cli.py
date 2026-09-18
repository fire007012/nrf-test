from __future__ import annotations

import argparse
import json
import platform
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from host.nrftest.autopts_adapter import (
    AutoPtsAdapterError,
    CapabilitySnapshot,
    identity_document,
)
from host.nrftest.fixture import (
    DoctorSnapshot,
    FixtureEnvironment,
    FixtureError,
    PeripheralFixture,
)
from host.nrftest.gatt_profile import GattProfileError, GattProfileMapping
from host.nrftest.profile import PeripheralProfile, ProfileError, profile_source_sha256
from host.nrftest.telemetry import TelemetryError, write_json_report
from tools.config import ConfigError, ResolvedValue, resolve_current_settings
from tools.setup_host_tools import (
    HostToolError,
    load_socat_pin,
    managed_socat_path,
    verify_socat,
)
from tools.setup_upstream import SetupError, load_upstream_pins, verify_upstream

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = PROJECT_ROOT / "profiles" / "blehub-nrf-basic-v1.json"


class HostCliError(RuntimeError):
    """Raised when a formal nrftest Host command cannot complete safely."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise HostCliError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _capability_document(capabilities: CapabilitySnapshot) -> dict[str, object]:
    return {
        "supported_services_mask": f"0x{capabilities.supported_services_mask:x}",
        "supported_services": list(capabilities.supported_services),
        "supported_command_masks": {
            service: f"0x{mask:x}" for service, mask in capabilities.supported_command_masks.items()
        },
        "service_actions": capabilities.service_actions,
    }


def _mapping_document(mapping: GattProfileMapping) -> dict[str, object]:
    return {
        "action": mapping.action.value,
        "service_handle": mapping.service_handle,
        "service_end_handle": mapping.service_end_handle,
        "attribute_count": len(mapping.attributes),
        "characteristics": [asdict(characteristic) for characteristic in mapping.characteristics],
    }


def _doctor_document(snapshot: DoctorSnapshot) -> dict[str, object]:
    return {
        "identity": identity_document(snapshot.identity),
        "capabilities": _capability_document(snapshot.capabilities),
        "controller": asdict(snapshot.controller),
    }


def run_doctor(
    settings: dict[str, ResolvedValue],
    *,
    profile_path: Path = DEFAULT_PROFILE,
    port_name: str | None = None,
    transport: str = "dongle",
) -> Path:
    started_at = datetime.now(UTC)
    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    reports_root = _required_path(settings, "reports_dir")
    profile = PeripheralProfile.load(profile_path)
    environment = FixtureEnvironment(
        autopts_root=_required_path(settings, "autopts_root"),
        socat_path=managed_socat_path(socat_pin, settings),
        reports_root=reports_root,
        port_name=port_name or settings["device_port"].value,
        device_serial=settings["device_serial"].value,
        transport=transport,
    )

    fixture: PeripheralFixture | None = None
    doctor: DoctorSnapshot | None = None
    mapping: GattProfileMapping | None = None
    main_error: Exception | None = None
    try:
        fixture = PeripheralFixture.open(environment)
        doctor = fixture.doctor(local_name=profile.local_name)
        mapping = fixture.load_profile(profile)
    except Exception as error:
        main_error = error
    finally:
        if fixture is not None:
            fixture.close()

    cleanup_errors = list(fixture.cleanup_errors) if fixture is not None else []
    cleanup_classification = (
        fixture.cleanup_classification if fixture is not None else "not-started"
    )
    outcome = "pass"
    detail = None
    if main_error is not None or cleanup_classification == "unexpected":
        outcome = "failure"
        details: list[str] = []
        if main_error is not None:
            details.append(f"{type(main_error).__name__}: {main_error}")
        details.extend(cleanup_errors)
        detail = "; ".join(details)

    document: dict[str, object] = {
        "schema_version": 1,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "outcome": outcome,
        "detail": detail,
        "host": {
            "platform": sys.platform,
            "platform_detail": platform.platform(),
            "python": platform.python_version(),
        },
        "upstream": {
            "zephyr_repository": upstream.zephyr.repository,
            "zephyr_revision": upstream.zephyr.commit,
            "zephyr_commit": upstream.zephyr.commit,
            "zephyr_sdk_version": upstream.zephyr.sdk_version,
            "autopts_commit": upstream.autopts.commit,
        },
        "profile": {
            "path": str(profile_path),
            "profile_id": profile.profile_id,
            "source_sha256": profile_source_sha256(profile_path),
            "semantic_signature": profile.semantic_signature(),
        },
        "doctor": _doctor_document(doctor) if doctor is not None else None,
        "mapping": _mapping_document(mapping) if mapping is not None else None,
        "cleanup_errors": cleanup_errors,
        "cleanup_classification": cleanup_classification,
    }
    report = write_json_report(reports_root, "host-doctor", document)
    if outcome != "pass":
        raise HostCliError(
            f"nrftest Host doctor failed: {detail}; report: {report}"
        ) from main_error

    assert doctor is not None
    assert mapping is not None
    print(
        "NRFTEST_HOST_DOCTOR "
        + json.dumps(
            {
                "outcome": outcome,
                "platform": sys.platform,
                "port": doctor.identity.port,
                "device_serial": doctor.identity.serial_number,
                "profile_id": profile.profile_id,
                "profile_action": mapping.action.value,
                "service_handle": mapping.service_handle,
                "attribute_count": len(mapping.attributes),
                "cleanup_classification": cleanup_classification,
                "report": str(report),
            },
            sort_keys=True,
        )
    )
    return report


class HostArguments(argparse.Namespace):
    command: str = ""
    config: str | None = None
    port: str | None = None
    profile: Path = DEFAULT_PROFILE
    transport: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control the nrftest Peripheral fixture")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser(
        "doctor",
        help="verify Host environment, BTP capabilities, and the resident Profile",
    )
    _ = doctor.add_argument("--config", help="path to the machine-local TOML configuration")
    _ = doctor.add_argument("--port", help="exact application serial port override")
    _ = doctor.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    _ = doctor.add_argument(
        "--transport",
        choices=("dongle", "dk"),
        default="dongle",
        help="application USB identity: dongle (PCA10059 CDC) or dk (J-Link VCOM)",
    )
    return parser


def main() -> int:
    arguments = HostArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        if arguments.command == "doctor":
            _ = run_doctor(
                resolve_current_settings(config_path=arguments.config),
                profile_path=arguments.profile,
                port_name=arguments.port,
                transport=arguments.transport,
            )
            return 0
        raise HostCliError(f"unsupported command: {arguments.command}")
    except (
        AutoPtsAdapterError,
        ConfigError,
        FixtureError,
        GattProfileError,
        HostCliError,
        HostToolError,
        ProfileError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"nrftest Host error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
