from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from intelhex import IntelHex  # pyright: ignore[reportMissingTypeStubs]

from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings
from tools.setup_host_tools import load_dtc_pin, managed_dtc_path, verify_host_tools
from tools.setup_upstream import load_upstream_pins, verify_upstream

BOARD = "nrf52840dongle/nrf52840"
APPLICATION_RELATIVE_PATH = Path("tests/bluetooth/tester")
CONFIG_PATH = PROJECT_ROOT / "firmware" / "app" / "pca10059.conf"
OVERLAY_PATH = PROJECT_ROOT / "firmware" / "app" / "pca10059.overlay"

OUTPUT_DIRECTORY_NAME = "pca10059-tester"
NRF52840_FLASH_END = 0x100000
APPLICATION_START = 0x1000
BOOTLOADER_START = 0xE0000

REQUIRED_CONFIG = {
    "CONFIG_BOARD_HAS_NRF5_BOOTLOADER": "y",
    "CONFIG_BOOTLOADER_MCUBOOT": "n",
    "CONFIG_USE_DT_CODE_PARTITION": "n",
    "CONFIG_FLASH_LOAD_OFFSET": "0x1000",
    "CONFIG_UART_PIPE": "y",
    "CONFIG_UART_CONSOLE": "n",
    "CONFIG_CONSOLE": "n",
    "CONFIG_PRINTK": "n",
    "CONFIG_HWINFO": "y",
    "CONFIG_BOOT_BANNER": "n",
    "CONFIG_TEST_LOGGING_DEFAULTS": "n",
    "CONFIG_LOG": "n",
    "CONFIG_BT": "y",
    "CONFIG_BT_PERIPHERAL": "y",
    "CONFIG_BT_GATT_DYNAMIC_DB": "y",
    "CONFIG_BT_HCI": "y",
    "CONFIG_BT_HCI_HOST": "y",
    "CONFIG_BT_LL_SW_SPLIT": "y",
    "CONFIG_HAS_BT_CTLR": "y",
    "CONFIG_BT_CTLR_HCI": "y",
}

DK_BOARD = "nrf52840dk/nrf52840"
DK_OUTPUT_DIRECTORY_NAME = "pca10056-tester"
# DK 没有原厂 bootloader：应用从 0x0 链接，镜像上限是完整的 1MB flash。
DK_APPLICATION_START = 0x0
DK_FLASH_IMAGE_LIMIT = NRF52840_FLASH_END

DK_REQUIRED_CONFIG = {
    "CONFIG_BOOTLOADER_MCUBOOT": "n",
    "CONFIG_USE_DT_CODE_PARTITION": "n",
    "CONFIG_UART_PIPE": "y",
    "CONFIG_UART_INTERRUPT_DRIVEN": "y",
    "CONFIG_UART_CONSOLE": "n",
    "CONFIG_CONSOLE": "n",
    "CONFIG_PRINTK": "n",
    "CONFIG_HWINFO": "y",
    "CONFIG_BOOT_BANNER": "n",
    "CONFIG_TEST_LOGGING_DEFAULTS": "n",
    "CONFIG_LOG": "n",
    "CONFIG_BT": "y",
    "CONFIG_BT_PERIPHERAL": "y",
    "CONFIG_BT_GATT_DYNAMIC_DB": "y",
    "CONFIG_BT_HCI": "y",
    "CONFIG_BT_HCI_HOST": "y",
    "CONFIG_BT_LL_SW_SPLIT": "y",
    "CONFIG_HAS_BT_CTLR": "y",
    "CONFIG_BT_CTLR_HCI": "y",
}


@dataclass(frozen=True)
class BoardVariant:
    """One explicitly selected hardware target for the pinned Tester firmware."""

    name: str
    board: str
    output_directory_name: str
    config_path: Path
    overlay_path: Path
    required_config: Mapping[str, str]
    application_start: int = APPLICATION_START
    # 段上限：Dongle 是原厂 bootloader 起点，DK 是完整 flash 末尾。
    flash_image_limit: int = BOOTLOADER_START


DONGLE_VARIANT = BoardVariant(
    name="dongle",
    board=BOARD,
    output_directory_name=OUTPUT_DIRECTORY_NAME,
    config_path=CONFIG_PATH,
    overlay_path=OVERLAY_PATH,
    required_config=REQUIRED_CONFIG,
    application_start=APPLICATION_START,
    flash_image_limit=BOOTLOADER_START,
)

DK_VARIANT = BoardVariant(
    name="dk",
    board=DK_BOARD,
    output_directory_name=DK_OUTPUT_DIRECTORY_NAME,
    config_path=PROJECT_ROOT / "firmware" / "app" / "pca10056.conf",
    overlay_path=PROJECT_ROOT / "firmware" / "app" / "pca10056.overlay",
    required_config=DK_REQUIRED_CONFIG,
    application_start=DK_APPLICATION_START,
    flash_image_limit=DK_FLASH_IMAGE_LIMIT,
)

BOARD_VARIANTS: Mapping[str, BoardVariant] = {
    variant.name: variant for variant in (DONGLE_VARIANT, DK_VARIANT)
}


class FirmwareBuildError(RuntimeError):
    """Raised when the pinned Tester firmware cannot be built or validated safely."""


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise FirmwareBuildError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise FirmwareBuildError(f"unable to hash {path}: {error}") from error
    return digest.hexdigest()


def parse_dotconfig(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise FirmwareBuildError(f"unable to read generated Kconfig at {path}: {error}") from error
    for line in lines:
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", maxsplit=1)
            values[name] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line.removeprefix("# ").removesuffix(" is not set")] = "n"
    return values


def validate_required_config(
    values: Mapping[str, str],
    required_config: Mapping[str, str] = REQUIRED_CONFIG,
) -> None:
    mismatches = [
        f"{name}: expected {expected}, found {values.get(name, '<missing>')}"
        for name, expected in required_config.items()
        if values.get(name, "n") != expected
    ]
    if mismatches:
        raise FirmwareBuildError(
            "generated Kconfig violates firmware invariants:\n- " + "\n- ".join(mismatches)
        )


def flash_segments_from_hex(path: Path) -> list[tuple[int, int]]:
    try:
        image = IntelHex(str(path))
    except (OSError, ValueError) as error:
        raise FirmwareBuildError(
            f"unable to parse generated Intel HEX at {path}: {error}"
        ) from error
    segments = cast(list[tuple[int, int]], image.segments())
    return [(int(start), int(end)) for start, end in segments]


def validate_flash_segments(
    segments: Sequence[tuple[int, int]],
    *,
    application_start: int = APPLICATION_START,
    flash_image_limit: int = BOOTLOADER_START,
) -> list[tuple[int, int]]:
    flash_segments = [
        (start, end) for start, end in segments if start < NRF52840_FLASH_END and end > 0
    ]
    if not flash_segments:
        raise FirmwareBuildError("generated HEX contains no nRF52840 internal flash data")
    for start, end in flash_segments:
        if start < application_start:
            raise FirmwareBuildError(
                f"generated HEX overlaps the reserved range below 0x{application_start:x}: "
                + f"0x{start:x}-0x{end:x}"
            )
        if end > flash_image_limit:
            raise FirmwareBuildError(
                f"generated HEX exceeds the flash image limit 0x{flash_image_limit:x}: "
                + f"0x{start:x}-0x{end:x}"
            )
    return flash_segments


def _display_command(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command)


def _run_build(command: list[str], *, cwd: Path, environment: Mapping[str, str]) -> None:
    print(f"+ {_display_command(command)}")
    try:
        _ = subprocess.run(command, cwd=cwd, env=environment, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise FirmwareBuildError(f"firmware build failed: {error}") from error


def _build_environment(
    settings: Mapping[str, ResolvedValue], zephyr_root: Path, sdk_root: Path
) -> dict[str, str]:
    environment = dict(os.environ)
    environment["ZEPHYR_BASE"] = str(zephyr_root)
    environment["ZEPHYR_TOOLCHAIN_VARIANT"] = "zephyr"
    environment["ZEPHYR_SDK_INSTALL_DIR"] = str(sdk_root)
    dtc = managed_dtc_path(load_dtc_pin(), settings)
    if not dtc.is_file():
        raise FirmwareBuildError("dtc is unavailable; run pixi run just setup-host-tools")
    environment["PATH"] = str(dtc.parent) + os.pathsep + environment.get("PATH", "")
    return environment


def _artifact_entry(path: Path, build_root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(build_root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _write_manifest(
    build_root: Path,
    flash_segments: Sequence[tuple[int, int]],
    generated_config: Mapping[str, str],
    variant: BoardVariant = DONGLE_VARIANT,
) -> Path:
    pins = load_upstream_pins()
    zephyr_output = build_root / "zephyr"
    artifacts: list[dict[str, object]] = []
    for name in ("zephyr.elf", "zephyr.hex", "zephyr.bin"):
        path = zephyr_output / name
        if not path.is_file():
            raise FirmwareBuildError(f"expected build artifact is missing: {path}")
        artifacts.append(_artifact_entry(path, build_root))
    manifest = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "board": variant.board,
        "application": APPLICATION_RELATIVE_PATH.as_posix(),
        "zephyr": {
            "repository": pins.zephyr.repository,
            "commit": pins.zephyr.commit,
            "sdk_version": pins.zephyr.sdk_version,
            "gnu_toolchains": list(pins.zephyr.sdk_gnu_toolchains),
        },
        "inputs": {
            "config": {
                "path": variant.config_path.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": sha256_file(variant.config_path),
            },
            "overlay": {
                "path": variant.overlay_path.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": sha256_file(variant.overlay_path),
            },
            # Retained for schema compatibility: local patches are no longer supported.
            "tester_patch": None,
        },
        "validated_config": {
            name: generated_config.get(name, "n") for name in variant.required_config
        },
        "nrf52840_flash_segments": [
            {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
            for start, end in flash_segments
        ],
        "artifacts": artifacts,
        "license": {
            "upstream_tester": "Apache-2.0",
            "notice": (
                "Built directly from the locked Zephyr repository and commit; "
                + "no local Tester patch is applied and no Tester source is vendored."
            ),
        },
    }
    path = build_root / "nrftest-build-manifest.json"
    _ = path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_firmware(settings: dict[str, ResolvedValue], variant: BoardVariant) -> None:
    pins = load_upstream_pins()
    verify_upstream(pins, settings)
    verify_host_tools(load_dtc_pin(), settings)

    zephyr_root = _required_path(settings, "zephyr_root")
    sdk_root = _required_path(settings, "zephyr_sdk_root")
    build_root = _required_path(settings, "build_dir") / variant.output_directory_name
    upstream_application = zephyr_root / APPLICATION_RELATIVE_PATH
    for required in (upstream_application, variant.config_path, variant.overlay_path):
        if not required.exists():
            raise FirmwareBuildError(f"required firmware input does not exist: {required}")
    build_root.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "west",
        "build",
        "--pristine=always",
        "--board",
        variant.board,
        "--build-dir",
        str(build_root),
        str(upstream_application),
        "--",
        f"-DEXTRA_CONF_FILE={variant.config_path.as_posix()}",
        f"-DDTC_OVERLAY_FILE={variant.overlay_path.as_posix()}",
    ]
    _run_build(
        command,
        cwd=zephyr_root.parent,
        environment=_build_environment(settings, zephyr_root, sdk_root),
    )

    generated_config = parse_dotconfig(build_root / "zephyr" / ".config")
    validate_required_config(generated_config, variant.required_config)
    all_segments = flash_segments_from_hex(build_root / "zephyr" / "zephyr.hex")
    flash_segments = validate_flash_segments(
        all_segments,
        application_start=variant.application_start,
        flash_image_limit=variant.flash_image_limit,
    )
    manifest = _write_manifest(
        build_root,
        flash_segments,
        generated_config,
        variant,
    )
    print(f"Firmware verified: {build_root / 'zephyr' / 'zephyr.hex'}")
    print(f"Build manifest: {manifest}")


class FirmwareArguments(argparse.Namespace):
    config: str | None = None
    build_dir: str | None = None
    board: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the pinned Zephyr Tester firmware")
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    _ = parser.add_argument("--build-dir", help="firmware build root override")
    _ = parser.add_argument(
        "--board",
        choices=tuple(BOARD_VARIANTS),
        default="dongle",
        help="explicit hardware target: dongle (PCA10059) or dk (PCA10056, nRF52840 DK)",
    )
    return parser


def main() -> int:
    arguments = FirmwareArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        build_firmware(
            resolve_current_settings(
                {"build_dir": arguments.build_dir},
                config_path=arguments.config,
            ),
            BOARD_VARIANTS[arguments.board],
        )
    except (ConfigError, FirmwareBuildError) as error:
        print(f"firmware build error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
