from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from host.nrftest.telemetry import TelemetryError, write_json_report
from tools.build_firmware import DK_BOARD, DK_VARIANT, FirmwareBuildError, sha256_file
from tools.config import ConfigError, ResolvedValue, resolve_current_settings
from tools.package_firmware import BUILD_MANIFEST_NAME

BUILD_MANIFEST_HEX_PATH = "zephyr/zephyr.hex"
DK_JLINK_TARGET_DEVICE = "NRF52840_XXAA"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class FirmwareFlashDkError(RuntimeError):
    """Raised when an explicitly selected DK firmware flash cannot proceed safely."""


@dataclass(frozen=True)
class HexIdentity:
    path: Path
    sha256: str


class FlashDriver(Protocol):
    def open(self) -> None: ...

    def flash_and_reset(self, path: Path) -> None: ...

    def close(self) -> None: ...


class PyLinkFlashDriver:
    """Flash one nRF52840 DK through an explicitly selected SEGGER J-Link over SWD."""

    def __init__(self, library_path: Path, debugger_serial: int) -> None:
        if not library_path.is_file():
            raise FirmwareFlashDkError(f"J-Link native library does not exist: {library_path}")
        if debugger_serial <= 0:
            raise FirmwareFlashDkError("J-Link debugger serial must be a positive integer")
        self._library_path = library_path
        self._debugger_serial = debugger_serial
        self._link: object | None = None

    def open(self) -> None:
        try:
            import pylink
            from pylink import library

            native_library = library.Library(dllpath=str(self._library_path))
            link = pylink.JLink(lib=native_library)
            self._link = link
            link.open(serial_no=self._debugger_serial)
            if not link.set_tif(pylink.enums.JLinkInterfaces.SWD):
                raise FirmwareFlashDkError("J-Link rejected the SWD target interface")
            link.connect(DK_JLINK_TARGET_DEVICE)
        except FirmwareFlashDkError:
            raise
        except Exception as error:
            raise FirmwareFlashDkError(
                f"unable to connect J-Link {self._debugger_serial} to {DK_JLINK_TARGET_DEVICE}: "
                + f"{type(error).__name__}: {error}"
            ) from error

    def flash_and_reset(self, path: Path) -> None:
        link = self._required_link()
        try:
            # flash_file halts the target, programs, and lets the DLL verify the image.
            _ = link.flash_file(str(path), 0)
            _ = link.reset()
            if not link.restart():
                raise FirmwareFlashDkError("J-Link could not release the target after reset")
        except FirmwareFlashDkError:
            raise
        except Exception as error:
            raise FirmwareFlashDkError(
                f"J-Link flash failed: {type(error).__name__}: {error}"
            ) from error

    def close(self) -> None:
        link = self._link
        if link is None:
            return
        try:
            link.close()
        except Exception as error:
            raise FirmwareFlashDkError(
                f"J-Link close failed: {type(error).__name__}: {error}"
            ) from error
        self._link = None

    def _required_link(self):
        if self._link is None:
            raise FirmwareFlashDkError("J-Link target is not open")
        return self._link


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise FirmwareFlashDkError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def load_hex_identity(build_root: Path) -> HexIdentity:
    """Verify the DK build manifest and return the validated HEX identity."""

    manifest_path = build_root / BUILD_MANIFEST_NAME
    try:
        document = cast(object, json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FirmwareFlashDkError(
            f"unable to read build manifest {manifest_path}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise FirmwareFlashDkError("build manifest root must be an object")
    manifest = cast(dict[str, object], document)
    schema_version = manifest.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise FirmwareFlashDkError("build manifest schema_version must be 1")
    if manifest.get("board") != DK_BOARD:
        raise FirmwareFlashDkError(f"build manifest board must be {DK_BOARD}")

    artifacts_value = manifest.get("artifacts")
    if not isinstance(artifacts_value, list):
        raise FirmwareFlashDkError("build manifest artifacts must be a list")
    hex_entries = [
        entry
        for entry in artifacts_value
        if isinstance(entry, dict) and entry.get("path") == BUILD_MANIFEST_HEX_PATH
    ]
    if len(hex_entries) != 1:
        raise FirmwareFlashDkError(
            f"build manifest must contain exactly one {BUILD_MANIFEST_HEX_PATH} artifact"
        )
    entry = cast(dict[str, object], hex_entries[0])
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
        raise FirmwareFlashDkError(
            "HEX sha256 in build manifest must be 64 canonical lowercase hex characters"
        )
    path = (build_root / Path(*PurePosixPath(BUILD_MANIFEST_HEX_PATH).parts)).resolve()
    if not path.is_relative_to(build_root.resolve()):
        raise FirmwareFlashDkError("HEX path resolves outside the firmware build root")
    if not path.is_file():
        raise FirmwareFlashDkError(f"validated HEX is missing: {path}")
    actual = sha256_file(path)
    if actual != sha256:
        raise FirmwareFlashDkError(
            f"HEX changed after validation: expected {sha256}, found {actual}"
        )
    return HexIdentity(path=path, sha256=sha256)


def _debugger_serial(settings: Mapping[str, ResolvedValue]) -> int:
    resolved = settings.get("debugger_serial")
    value = resolved.value if resolved is not None else None
    if value is None or not value.strip():
        raise FirmwareFlashDkError(
            "debugger_serial is not configured; set device.debugger_serial in "
            + "nrftest.local.toml, NRFTEST_DEBUGGER_SERIAL, or --debugger-serial"
        )
    try:
        serial = int(value, 10)
    except ValueError as error:
        raise FirmwareFlashDkError("debugger_serial must be a decimal integer") from error
    if serial <= 0:
        raise FirmwareFlashDkError("debugger_serial must be a positive decimal integer")
    return serial


def _write_report(
    reports_root: Path,
    hex_identity: HexIdentity,
    *,
    started_at: str,
    outcome: str,
    detail: str | None,
    jlink_library: Path,
    debugger_serial: int,
) -> Path:
    finished_at = datetime.now(UTC)
    report = {
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "method": "onboard SEGGER J-Link OB over SWD",
        "hex": {"path": str(hex_identity.path), "sha256": hex_identity.sha256},
        "jlink": {
            "library": str(jlink_library),
            "debugger_serial": str(debugger_serial),
            "target_device": DK_JLINK_TARGET_DEVICE,
        },
    }
    return write_json_report(reports_root, "firmware-flash-dk", report, now=finished_at)


def flash_firmware_dk(
    settings: dict[str, ResolvedValue],
    *,
    confirm_sha256: str,
    driver_factory=None,
) -> None:
    jlink_library = _required_path(settings, "jlink_library")
    build_root = _required_path(settings, "build_dir") / DK_VARIANT.output_directory_name
    reports_root = _required_path(settings, "reports_dir")
    debugger_serial = _debugger_serial(settings)

    hex_identity = load_hex_identity(build_root)
    if confirm_sha256.lower() != hex_identity.sha256:
        raise FirmwareFlashDkError(
            f"image confirmation mismatch: expected exact SHA-256 {hex_identity.sha256}"
        )

    print(f"Selected HEX: {hex_identity.path}")
    print(f"HEX SHA-256: {hex_identity.sha256}")
    print(f"Selected J-Link debugger serial: {debugger_serial}")
    print(f"Selected J-Link native library: {jlink_library}")

    factory = driver_factory if driver_factory is not None else PyLinkFlashDriver
    driver = factory(jlink_library, debugger_serial)
    started_at = datetime.now(UTC).isoformat()
    try:
        driver.open()
    except Exception as error:
        report = _write_report(
            reports_root,
            hex_identity,
            started_at=started_at,
            outcome="environment-failure",
            detail=str(error),
            jlink_library=jlink_library,
            debugger_serial=debugger_serial,
        )
        raise FirmwareFlashDkError(f"firmware flash failed: {error}; report: {report}") from error
    try:
        driver.flash_and_reset(hex_identity.path)
    except Exception as error:
        report = _write_report(
            reports_root,
            hex_identity,
            started_at=started_at,
            outcome="command-failure",
            detail=str(error),
            jlink_library=jlink_library,
            debugger_serial=debugger_serial,
        )
        raise FirmwareFlashDkError(f"firmware flash failed: {error}; report: {report}") from error
    finally:
        try:
            driver.close()
        except Exception as error:
            print(f"J-Link close failed after flash: {type(error).__name__}: {error}")
    report = _write_report(
        reports_root,
        hex_identity,
        started_at=started_at,
        outcome="command-succeeded",
        detail=None,
        jlink_library=jlink_library,
        debugger_serial=debugger_serial,
    )
    print(f"Firmware flash command succeeded; report: {report}")


class FlashDkArguments(argparse.Namespace):
    config: str | None = None
    build_dir: str | None = None
    confirm_sha256: str = ""
    debugger_serial: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flash the validated DK Tester HEX through the onboard J-Link over SWD"
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    _ = parser.add_argument("--build-dir", help="firmware build root override")
    _ = parser.add_argument(
        "--confirm-sha256",
        required=True,
        help="exact SHA-256 printed by firmware-build-dk (zephyr.hex)",
    )
    _ = parser.add_argument(
        "--debugger-serial",
        help=(
            "exact J-Link debugger serial; overrides device.debugger_serial and "
            "NRFTEST_DEBUGGER_SERIAL"
        ),
    )
    return parser


def main() -> int:
    arguments = FlashDkArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        flash_firmware_dk(
            resolve_current_settings(
                {
                    "build_dir": arguments.build_dir,
                    "debugger_serial": arguments.debugger_serial,
                },
                config_path=arguments.config,
            ),
            confirm_sha256=arguments.confirm_sha256,
        )
    except (
        ConfigError,
        FirmwareBuildError,
        FirmwareFlashDkError,
        TelemetryError,
    ) as error:
        print(f"firmware flash error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
