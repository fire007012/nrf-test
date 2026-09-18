import json
import os
from pathlib import Path

import pytest

from tools.build_firmware import (
    DK_REQUIRED_CONFIG,
    DK_VARIANT,
    REQUIRED_CONFIG,
    FirmwareBuildError,
    _build_environment,
    _write_manifest,
    parse_dotconfig,
    validate_flash_segments,
    validate_required_config,
)
from tools.config import ResolvedValue
from tools.setup_host_tools import load_dtc_pin, managed_dtc_path


@pytest.mark.skipif(os.name != "nt", reason="managed portable DTC is Windows-specific")
def test_build_uses_managed_windows_dtc_without_explicit_override(tmp_path: Path) -> None:
    settings = {
        "dtc": ResolvedValue(None, "unset"),
        "host_tools_root": ResolvedValue(str(tmp_path / "host-tools"), "test"),
        "downloads_dir": ResolvedValue(str(tmp_path / "downloads"), "test"),
    }
    dtc = managed_dtc_path(load_dtc_pin(), settings)
    dtc.parent.mkdir(parents=True)
    _ = dtc.write_bytes(b"test executable placeholder")

    environment = _build_environment(settings, tmp_path / "zephyr", tmp_path / "sdk")

    assert environment["PATH"].split(os.pathsep)[0] == str(dtc.parent)


def test_build_manifest_records_locked_source_without_local_patch(tmp_path: Path) -> None:
    output = tmp_path / "zephyr"
    output.mkdir()
    for name in ("zephyr.elf", "zephyr.hex", "zephyr.bin"):
        _ = (output / name).write_bytes(name.encode("ascii"))

    manifest_path = _write_manifest(tmp_path, [(0x1000, 0x2000)], {})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["zephyr"]["repository"].endswith("/zephyr.git")
    assert len(manifest["zephyr"]["commit"]) == 40
    assert "tag" not in manifest["zephyr"]
    assert manifest["inputs"]["tester_patch"] is None
    assert "locked Zephyr repository and commit" in manifest["license"]["notice"]
    assert "local Tester patch" in manifest["license"]["notice"]


def test_generated_config_and_flash_preserve_bootloader_boundaries(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    _ = config.write_text(
        "\n".join(
            (
                "CONFIG_BOARD_HAS_NRF5_BOOTLOADER=y",
                "# CONFIG_BOOTLOADER_MCUBOOT is not set",
                "# CONFIG_USE_DT_CODE_PARTITION is not set",
                "CONFIG_FLASH_LOAD_OFFSET=0x1000",
                "CONFIG_UART_PIPE=y",
                "# CONFIG_UART_CONSOLE is not set",
                "# CONFIG_CONSOLE is not set",
                "# CONFIG_PRINTK is not set",
                "CONFIG_HWINFO=y",
                "# CONFIG_BOOT_BANNER is not set",
                "# CONFIG_TEST_LOGGING_DEFAULTS is not set",
                "# CONFIG_LOG is not set",
                "CONFIG_BT=y",
                "CONFIG_BT_PERIPHERAL=y",
                "CONFIG_BT_GATT_DYNAMIC_DB=y",
                "CONFIG_BT_HCI=y",
                "CONFIG_BT_HCI_HOST=y",
                "CONFIG_BT_LL_SW_SPLIT=y",
                "CONFIG_HAS_BT_CTLR=y",
                "CONFIG_BT_CTLR_HCI=y",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    values = parse_dotconfig(config)
    validate_required_config(values)
    assert validate_flash_segments([(0x1000, 0xDFFFF), (0x10001000, 0x10001010)]) == [
        (0x1000, 0xDFFFF)
    ]


@pytest.mark.parametrize(
    "symbol", ["CONFIG_CONSOLE", "CONFIG_PRINTK", "CONFIG_UART_CONSOLE", "CONFIG_LOG"]
)
def test_btp_build_rejects_text_output(symbol: str) -> None:
    values = dict(REQUIRED_CONFIG)
    values[symbol] = "y"

    with pytest.raises(FirmwareBuildError, match=f"{symbol}: expected n, found y"):
        validate_required_config(values)


@pytest.mark.parametrize("segments", [[(0x0, 0x1001)], [(0x1000, 0xE0001)], []])
def test_flash_validation_rejects_reserved_or_missing_application(
    segments: list[tuple[int, int]],
) -> None:
    with pytest.raises(FirmwareBuildError):
        _ = validate_flash_segments(segments)


def test_dk_variant_links_from_zero_without_bootloader_constraints() -> None:
    assert DK_VARIANT.board == "nrf52840dk/nrf52840"
    assert DK_VARIANT.application_start == 0x0
    assert DK_VARIANT.flash_image_limit == 0x100000
    assert "CONFIG_BOARD_HAS_NRF5_BOOTLOADER" not in DK_REQUIRED_CONFIG
    assert "CONFIG_FLASH_LOAD_OFFSET" not in DK_REQUIRED_CONFIG


def test_dk_flash_validation_accepts_zero_base_and_full_flash() -> None:
    assert validate_flash_segments(
        [(0x0, 0x1000), (0x1000, 0x100000)],
        application_start=DK_VARIANT.application_start,
        flash_image_limit=DK_VARIANT.flash_image_limit,
    ) == [(0x0, 0x1000), (0x1000, 0x100000)]


@pytest.mark.parametrize("segments", [[(0x0, 0x100001)], []])
def test_dk_flash_validation_rejects_overflow_or_missing_application(
    segments: list[tuple[int, int]],
) -> None:
    with pytest.raises(FirmwareBuildError):
        _ = validate_flash_segments(
            segments,
            application_start=DK_VARIANT.application_start,
            flash_image_limit=DK_VARIANT.flash_image_limit,
        )


def test_dk_generated_config_rejects_text_output() -> None:
    values = dict(DK_REQUIRED_CONFIG)
    values["CONFIG_UART_INTERRUPT_DRIVEN"] = "n"

    with pytest.raises(
        FirmwareBuildError, match="CONFIG_UART_INTERRUPT_DRIVEN: expected y, found n"
    ):
        validate_required_config(values, DK_REQUIRED_CONFIG)


def test_build_manifest_records_dk_variant(tmp_path: Path) -> None:
    output = tmp_path / "zephyr"
    output.mkdir()
    for name in ("zephyr.elf", "zephyr.hex", "zephyr.bin"):
        _ = (output / name).write_bytes(name.encode("ascii"))

    manifest_path = _write_manifest(tmp_path, [(0x0, 0x2000)], {}, DK_VARIANT)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["board"] == DK_VARIANT.board
    assert manifest["inputs"]["config"]["path"].endswith("pca10056.conf")
    assert manifest["inputs"]["overlay"]["path"].endswith("pca10056.overlay")
