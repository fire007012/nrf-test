import hashlib
import json
from pathlib import Path

import pytest

from tools.build_firmware import DK_BOARD
from tools.config import ResolvedValue
from tools.flash_firmware_dk import (
    FirmwareFlashDkError,
    _debugger_serial,
    flash_firmware_dk,
    load_hex_identity,
)


def _write_manifest(
    build_root: Path,
    *,
    board: str = DK_BOARD,
    hex_sha256: str | None = None,
    hex_content: bytes = b"hex",
) -> str:
    hex_path = build_root / "zephyr" / "zephyr.hex"
    hex_path.parent.mkdir(parents=True, exist_ok=True)
    _ = hex_path.write_bytes(hex_content)
    sha256 = hex_sha256 if hex_sha256 is not None else hashlib.sha256(hex_content).hexdigest()
    document = {
        "schema_version": 1,
        "board": board,
        "artifacts": [
            {"path": "zephyr/zephyr.hex", "sha256": sha256},
            {"path": "zephyr/zephyr.elf", "sha256": "0" * 64},
        ],
    }
    _ = (build_root / "nrftest-build-manifest.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return sha256


class RecordingDriver:
    def __init__(self, library_path: Path, debugger_serial: int) -> None:
        self.library_path = library_path
        self.debugger_serial = debugger_serial
        self.calls = []

    def open(self) -> None:
        self.calls.append("open")

    def flash_and_reset(self, path: Path) -> None:
        self.calls.append("flash")

    def close(self) -> None:
        self.calls.append("close")


class FailingDriver(RecordingDriver):
    def flash_and_reset(self, path: Path) -> None:
        raise RuntimeError("swd error")


def _settings(tmp_path: Path, build_root: Path) -> dict[str, ResolvedValue]:
    return {
        "jlink_library": ResolvedValue(str(tmp_path / "JLinkARM.dll"), "test"),
        "build_dir": ResolvedValue(str(build_root.parent), "test"),
        "reports_dir": ResolvedValue(str(tmp_path / "reports"), "test"),
        "debugger_serial": ResolvedValue("1050252028", "test"),
    }


def _library_placeholder(settings: dict[str, ResolvedValue]) -> None:
    library = Path(settings["jlink_library"].value or "")
    library.parent.mkdir(parents=True, exist_ok=True)
    _ = library.write_bytes(b"library placeholder")


def test_load_hex_identity_verifies_manifest_and_file(tmp_path: Path) -> None:
    build_root = tmp_path / "pca10056-tester"
    sha256 = _write_manifest(build_root)

    identity = load_hex_identity(build_root)

    assert identity.path == build_root / "zephyr" / "zephyr.hex"
    assert identity.sha256 == sha256


def test_load_hex_identity_rejects_dongle_manifest(tmp_path: Path) -> None:
    build_root = tmp_path / "pca10056-tester"
    _ = _write_manifest(build_root, board="nrf52840dongle/nrf52840")

    with pytest.raises(FirmwareFlashDkError, match="board must be nrf52840dk/nrf52840"):
        _ = load_hex_identity(build_root)


def test_load_hex_identity_rejects_hex_changed_after_validation(tmp_path: Path) -> None:
    build_root = tmp_path / "pca10056-tester"
    _ = _write_manifest(build_root, hex_sha256="0" * 64)

    with pytest.raises(FirmwareFlashDkError, match="HEX changed after validation"):
        _ = load_hex_identity(build_root)


def test_load_hex_identity_rejects_manifest_without_hex_artifact(tmp_path: Path) -> None:
    build_root = tmp_path / "pca10056-tester"
    build_root.mkdir(parents=True)
    _ = (build_root / "nrftest-build-manifest.json").write_text(
        json.dumps({"schema_version": 1, "board": DK_BOARD, "artifacts": []}),
        encoding="utf-8",
    )

    with pytest.raises(FirmwareFlashDkError, match="exactly one zephyr/zephyr.hex"):
        _ = load_hex_identity(build_root)


def test_flash_requires_exact_sha_confirmation(tmp_path: Path) -> None:
    build_root = tmp_path / "pca10056-tester"
    sha256 = _write_manifest(build_root)
    settings = _settings(tmp_path, build_root)

    def _unexpected_factory(library_path: Path, debugger_serial: int) -> RecordingDriver:
        raise AssertionError("confirmation mismatch must fail before the J-Link is opened")

    with pytest.raises(FirmwareFlashDkError, match="image confirmation mismatch"):
        flash_firmware_dk(
            settings,
            confirm_sha256="0" * 64,
            driver_factory=_unexpected_factory,
        )
    assert sha256 != "0" * 64


def test_flash_flashes_validated_hex_and_writes_report(tmp_path: Path) -> None:
    build_root = tmp_path / "pca10056-tester"
    sha256 = _write_manifest(build_root)
    settings = _settings(tmp_path, build_root)
    _library_placeholder(settings)

    driver = RecordingDriver(Path(settings["jlink_library"].value or ""), 1050252028)
    flash_firmware_dk(settings, confirm_sha256=sha256, driver_factory=lambda *_: driver)

    assert driver.calls == ["open", "flash", "close"]
    reports = list((tmp_path / "reports" / "firmware-flash-dk").glob("*.json"))
    assert len(reports) == 1
    document = json.loads(reports[0].read_text(encoding="utf-8"))
    assert document["outcome"] == "command-succeeded"
    assert document["hex"]["sha256"] == sha256
    assert document["jlink"]["debugger_serial"] == "1050252028"


def test_flash_reports_command_failure_without_stopping_cleanup(tmp_path: Path) -> None:
    build_root = tmp_path / "pca10056-tester"
    sha256 = _write_manifest(build_root)
    settings = _settings(tmp_path, build_root)
    _library_placeholder(settings)

    driver = FailingDriver(Path(settings["jlink_library"].value or ""), 1050252028)
    with pytest.raises(FirmwareFlashDkError, match="swd error"):
        flash_firmware_dk(settings, confirm_sha256=sha256, driver_factory=lambda *_: driver)

    assert driver.calls == ["open", "close"]
    reports = list((tmp_path / "reports" / "firmware-flash-dk").glob("*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["outcome"] == "command-failure"


def test_debugger_serial_must_be_positive_decimal_integer() -> None:
    valid = {"debugger_serial": ResolvedValue("1050252028", "test")}
    assert _debugger_serial(valid) == 1050252028
    for invalid in (None, "", "abc", "-1", "0"):
        settings = {"debugger_serial": ResolvedValue(invalid, "test")}
        with pytest.raises(FirmwareFlashDkError):
            _ = _debugger_serial(settings)
