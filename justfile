# Run project commands through Pixi, for example:
#   pixi run just check-tools

set dotenv-load := false

# Show the currently available project commands.
default:
    @just --list

# Require the developer or CI to choose machine-local install paths before setup/build/package.
require-local-config:
    python tools/config.py require-local

# Install all pinned upstream sources, SDK, and platform-specific host build tools.
# This is an explicit network and disk operation; it never modifies the existing NCS workspace.
setup: require-local-config setup-upstream setup-host-tools

# Install everything required to build and package firmware without USB driver/udev changes.
setup-firmware-build:
    just setup
    just setup-firmware-tools

# Install everything required to build, package, and invoke the managed flash tooling.
# System driver/udev installation remains a separate explicit provisioning action.
setup-firmware: setup-firmware-build

# Reproduce firmware and its deterministic DFU package without flashing or system USB setup.
firmware-reproduce:
    just setup-firmware-build
    just firmware-build
    just firmware-package

# Show pinned upstream revisions and installation state without downloading anything.
upstream-status:
    python -m tools.setup_upstream status

# Install the pinned Zephyr west workspace and AutoPTS checkout.
setup-upstream-sources: require-local-config
    python -m tools.setup_upstream sources

# Explicitly refresh modules to revisions pinned by the existing Zephyr manifest.
update-upstream-sources: require-local-config
    python -m tools.setup_upstream sources --update

# Install SDK 1.0.1 host tools and only the ARM GNU toolchain after sources exist.
setup-zephyr-sdk: require-local-config setup-upstream-sources
    python -m tools.setup_upstream sdk

# Verify exact Git revisions and the installed ARM SDK without changing anything.
verify-upstream:
    python -m tools.setup_upstream verify

# Install all pinned upstream sources and the matching Zephyr SDK, then verify them.
setup-upstream: setup-zephyr-sdk
    python -m tools.setup_upstream verify

# Show platform-specific host-tool state without downloading anything.
host-tools-status:
    python -m tools.setup_host_tools status

# Install pinned Windows host tools or verify Pixi host tools on macOS/Linux.
setup-host-tools: require-local-config
    python -m tools.setup_host_tools setup

# Strictly verify platform-specific host tools without changing anything.
verify-host-tools:
    python -m tools.setup_host_tools verify

# Show the pinned nRF Util and nrf5sdk-tools state without downloading anything.
firmware-tools-status:
    python -m tools.setup_firmware_tools status

# Explicitly install pinned Nordic firmware packaging tools into the managed host-tools root.
setup-firmware-tools: require-local-config
    python -m tools.setup_firmware_tools setup

# Verify the managed Nordic firmware packaging tools without changing anything.
verify-firmware-tools:
    python -m tools.setup_firmware_tools verify

# Show managed platform driver/udev provisioning state without changing anything.
platform-provisioning-status:
    python -m tools.setup_platform_prerequisites status

# Explicitly install pinned platform driver/udev packages; may request UAC/sudo.
setup-platform-provisioning: require-local-config
    python -m tools.setup_platform_prerequisites setup

# Verify managed platform driver/udev provenance without testing a connected device.
verify-platform-provisioning:
    python -m tools.setup_platform_prerequisites verify

# Compatibility aliases for the earlier prerequisite recipe names.
platform-prereqs-status: platform-provisioning-status
setup-platform-prereqs: setup-platform-provisioning
verify-platform-prereqs: verify-platform-provisioning

# Print resolved project paths, selectors, and their configuration sources.
toolchain-paths:
    python tools/config.py paths

# Print managed tool versions and validate configured external paths.
check-tools:
    python tools/config.py check-tools

# Build the Tester directly from the locked Zephyr source.
firmware-build: require-local-config
    python -m tools.build_firmware

# Build the Tester for the nRF52840 DK (PCA10056); BTP goes to UART0/J-Link VCOM.
firmware-build-dk: require-local-config
    python -m tools.build_firmware --board dk

# Flash the validated DK Tester HEX through the onboard J-Link over SWD after SHA identity checks.
firmware-flash-dk confirm_sha256: require-local-config
    python -m tools.flash_firmware_dk --confirm-sha256 "{{confirm_sha256}}"

# Package the validated Tester HEX for the stock PCA10059 USB bootloader.
firmware-package: require-local-config
    python -m tools.package_firmware

# Flash the package after explicit bootloader-port and SHA identity checks.
firmware-flash port package_sha256: require-local-config
    python -m tools.flash_firmware --port "{{port}}" --confirm-sha256 "{{package_sha256}}"

# Validate a Profile contract and print its identity, semantic signature, and source checksum.
profile-check profile="profiles/blehub-nrf-basic-v1.json":
    python -m tools.profile_check --profile "{{profile}}"

# Run the formal cross-platform Host API doctor and verify the resident Profile.
host-doctor port="" profile="profiles/blehub-nrf-basic-v1.json" transport="dongle":
    python -m host.nrftest.cli doctor --port "{{port}}" --profile "{{profile}}" --transport "{{transport}}"

# Query Core capabilities through the retained Phase 0/1 probe baseline.
btp-doctor port="" transport="dongle":
    python -m tools.btp_core_probe --port "{{port}}" --transport "{{transport}}"

# Build or attach the canonical dynamic GATT Profile and verify nRF-side actual handles/values.
btp-gatt-profile port="" profile="profiles/blehub-nrf-basic-v1.json" transport="dongle":
    python -m tools.btp_gatt_profile_probe --port "{{port}}" --profile "{{profile}}" --transport "{{transport}}"

# Remove one verified Profile A and build distinct Profile B; target reset is required afterward.
btp-gatt-profile-rebuild port="" profile_a="profiles/blehub-nrf-basic-v1.json" profile_b="profiles/blehub-nrf-rebuild-v1.json" transport="dongle": require-local-config
    python -m tools.btp_gatt_profile_rebuild_probe --port "{{port}}" --profile-a "{{profile_a}}" --profile-b "{{profile_b}}" --transport "{{transport}}"

# Reset through the external J-Link: dongle verifies USB re-enumeration, dk verifies a stable
# VCOM identity plus a fresh BTP Core/GAP handshake.
btp-target-reset port="" disappearance_timeout="10" reappearance_timeout="30" transport="dongle": require-local-config
    python -m tools.target_reset --port "{{port}}" --disappearance-timeout "{{disappearance_timeout}}" --reappearance-timeout "{{reappearance_timeout}}" --transport "{{transport}}"

# Reset/re-enumerate (dongle) or reset plus BTP re-handshake (dk), then rebuild the Profile.
btp-target-recover port="" profile="profiles/blehub-nrf-basic-v1.json" disappearance_timeout="10" reappearance_timeout="30" transport="dongle":
    just btp-target-reset "{{port}}" "{{disappearance_timeout}}" "{{reappearance_timeout}}" "{{transport}}"
    just btp-doctor "{{port}}" "{{transport}}"
    just btp-gatt-profile "{{port}}" "{{profile}}" "{{transport}}"

# Advertise the canonical dynamic GATT Profile and record only nRF-side RF facts.
btp-gatt-rf-fixture port="" profile="profiles/blehub-nrf-basic-v1.json" timeout="60" transport="dongle":
    python -m tools.btp_gatt_profile_probe --port "{{port}}" --profile "{{profile}}" --rf-timeout "{{timeout}}" --transport "{{transport}}"

# Wait for one exact Central write and record the nRF Attribute Value Changed fact.
btp-gatt-write-fixture port="" profile="profiles/blehub-nrf-basic-v1.json" role="read-write" value_hex="10" timeout="60" transport="dongle":
    python -m tools.btp_gatt_profile_probe --port "{{port}}" --profile "{{profile}}" --rf-timeout "{{timeout}}" --expected-write-role "{{role}}" --expected-write-hex "{{value_hex}}" --transport "{{transport}}"

# Observe one real peer CCC mode, emit one update, observe disable, then update without delivery.
btp-gatt-subscription-fixture mode value_hex after_disable_value_hex port="" profile="profiles/blehub-nrf-basic-v1.json" timeout="60" transport="dongle":
    python -m tools.btp_gatt_subscription_fixture --port "{{port}}" --profile "{{profile}}" --mode "{{mode}}" --value-hex "{{value_hex}}" --after-disable-value-hex "{{after_disable_value_hex}}" --timeout "{{timeout}}" --transport "{{transport}}"

# Connect or wait for a subscription, then trigger a Peripheral-side disconnect fault.
btp-gatt-passive-disconnect-fixture anchor="notification" trigger="power-off" port="" profile="profiles/blehub-nrf-basic-v1.json" timeout="60" disconnect_delay="1" transport="dongle":
    python -m tools.btp_gatt_passive_disconnect_fixture --port "{{port}}" --profile "{{profile}}" --anchor "{{anchor}}" --trigger "{{trigger}}" --timeout "{{timeout}}" --disconnect-delay "{{disconnect_delay}}" --transport "{{transport}}"

# Exercise Core/GAP power and advertising controls without claiming an independent RF result.
btp-gap-control port="" local_name="NrftestP1" transport="dongle":
    python -m tools.btp_gap_control_probe --port "{{port}}" --local-name "{{local_name}}" --transport "{{transport}}"

# Diagnose stock Tester unregister/register behavior across repeated Host sessions.
btp-gap-lifecycle port="" local_name="NrftestLifecycle" cycles="10" settle="0.5" transport="dongle":
    python -m tools.btp_gap_lifecycle_probe --port "{{port}}" --local-name "{{local_name}}" --cycles "{{cycles}}" --settle "{{settle}}" --transport "{{transport}}"

# Register GAP once, or attach to an earlier process, then rebuild only the Host transport.
btp-gap-transport-reuse port="" local_name="NrftestResidentGap" cycles="10" settle="0.5" initial_mode="register" transport="dongle":
    python -m tools.btp_gap_transport_reuse_probe --port "{{port}}" --local-name "{{local_name}}" --cycles "{{cycles}}" --settle "{{settle}}" --initial-mode "{{initial_mode}}" --transport "{{transport}}"

# Force-kill an advertising Host child and attach without resetting the nRF target.
btp-gap-host-crash port="" local_name="NrftestCrashRecovery" ready_timeout="90" release_timeout="30" transport="dongle":
    python -m tools.btp_gap_host_crash_probe --port "{{port}}" --local-name "{{local_name}}" --ready-timeout "{{ready_timeout}}" --release-timeout "{{release_timeout}}" --transport "{{transport}}"

# Advertise independently and record only nRF-side connection/disconnection RF facts.
btp-gap-rf-fixture port="" local_name="NrftestP1" service_uuid16="fdf0" timeout="60" transport="dongle":
    python -m tools.btp_gap_rf_fixture --port "{{port}}" --local-name "{{local_name}}" --service-uuid16 "{{service_uuid16}}" --timeout "{{timeout}}" --transport "{{transport}}"

# Format active Python sources. Archived implementations are intentionally excluded.
fmt:
    ruff format host tools tests

# Run static checks against active Python sources.
lint:
    ruff check host tools tests
    ruff format --check host tools tests

# Run non-hardware unit tests.
test:
    pytest -q tests/unit
