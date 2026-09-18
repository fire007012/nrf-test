from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, cast
from uuid import uuid4

from serial import Serial, SerialException

from host.nrftest.autopts_adapter import (
    AutoPtsAdapterError,
    AutoPtsSession,
    CapabilitySnapshot,
    GapSnapshot,
    SerialIdentity,
    autopts_tty_file,
    identity_document,
    load_autopts,
    select_application_port,
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

DEFAULT_LOCAL_NAME = "NrftestCrashRecovery"
DEFAULT_READY_TIMEOUT_SECONDS = 90
DEFAULT_RELEASE_TIMEOUT_SECONDS = 30
CHILD_HOLD_SECONDS = 300
READY_MARKER = "NRFTEST_CRASH_CHILD_READY"


class BtpGapHostCrashProbeError(RuntimeError):
    """Raised when a forcibly terminated Host cannot be recovered without target reset."""


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise BtpGapHostCrashProbeError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _snapshot_document(snapshot: GapSnapshot) -> dict[str, object]:
    return {
        "controller_index": snapshot.controller_index,
        "address": snapshot.address,
        "address_type": snapshot.address_type,
        "address_type_name": snapshot.address_type_name,
        "current_settings": snapshot.current_settings,
        "connections": [
            {
                "address": connection.address,
                "address_type": connection.address_type,
                "address_type_name": connection.address_type_name,
                "security_level": connection.security_level,
            }
            for connection in snapshot.connections
        ],
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


def _require_setting(snapshot: GapSnapshot, name: str, expected: bool) -> None:
    actual = snapshot.current_settings.get(name)
    if actual is not expected:
        raise BtpGapHostCrashProbeError(
            f"unexpected GAP setting {name}: expected {expected}, found {actual}"
        )


def _emit_marker(document: dict[str, object]) -> None:
    print(f"{READY_MARKER} {json.dumps(document, sort_keys=True)}", flush=True)


def _child_command(
    *,
    port: str,
    local_name: str,
    run_id: str,
    config_path: str | None,
    transport: str = "dongle",
) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "tools.btp_gap_host_crash_probe",
        "--child",
        "--port",
        port,
        "--local-name",
        local_name,
        "--run-id",
        run_id,
        "--hold-seconds",
        str(CHILD_HOLD_SECONDS),
        "--transport",
        transport,
    ]
    if config_path is not None:
        command.extend(("--config", config_path))
    return command


def _collect_output(
    stream: IO[str],
    lines: list[str],
    messages: queue.Queue[str | None],
) -> None:
    try:
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            lines.append(line)
            messages.put(line)
    finally:
        messages.put(None)


def _wait_for_ready_marker(
    process: subprocess.Popen[str],
    messages: queue.Queue[str | None],
    *,
    timeout_seconds: int,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BtpGapHostCrashProbeError(
                f"child did not emit {READY_MARKER} within {timeout_seconds} seconds"
            )
        try:
            line = messages.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            if process.poll() is not None:
                raise BtpGapHostCrashProbeError(
                    f"child exited with code {process.returncode} before its ready marker"
                ) from None
            continue
        if line is None:
            raise BtpGapHostCrashProbeError(
                f"child output closed with code {process.poll()} before its ready marker"
            )
        print(f"[crash-child] {line}")
        prefix = f"{READY_MARKER} "
        if line.startswith(prefix):
            value = cast(object, json.loads(line.removeprefix(prefix)))
            if not isinstance(value, dict):
                raise BtpGapHostCrashProbeError("child ready marker is not a JSON object")
            return cast(dict[str, object], value)


def _wait_for_serial_release(
    identity: SerialIdentity,
    *,
    timeout_seconds: int,
) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + timeout_seconds
    attempts = 0
    last_error: str | None = None
    while time.monotonic() < deadline:
        attempts += 1
        try:
            with Serial(port=identity.port, baudrate=115200, timeout=0.1):
                pass
            return {
                "outcome": "released",
                "attempts": attempts,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "last_error": last_error,
            }
        except (OSError, SerialException) as error:
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(0.2)
    raise BtpGapHostCrashProbeError(
        f"serial port {identity.port} was not released within {timeout_seconds} seconds; "
        + f"last error: {last_error}"
    )


def _write_report(
    reports_root: Path,
    *,
    run_id: str,
    started_at: str,
    outcome: str,
    detail: str | None,
    identity: SerialIdentity,
    autopts_commit: str,
    local_name: str,
    child_command: list[str],
    child_pid: int | None,
    child_returncode: int | None,
    child_ready: dict[str, object] | None,
    child_output: list[str],
    serial_release: dict[str, object] | None,
    recovery_capabilities: CapabilitySnapshot | None,
    recovery_steps: list[dict[str, object]],
    cleanup_errors: list[str],
) -> Path:
    finished_at = datetime.now(UTC)
    document = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "scope": (
            "Force-terminate a Host child while GAP advertising is active, then attach from the "
            "parent and restore GAP operation without resetting or power-cycling PCA10059"
        ),
        "port": identity_document(identity),
        "autopts_commit": autopts_commit,
        "local_name": local_name,
        "child": {
            "command": child_command,
            "pid": child_pid,
            "termination": "subprocess.kill",
            "returncode": child_returncode,
            "ready": child_ready,
            "output": child_output,
        },
        "serial_release": serial_release,
        "recovery_capabilities": (
            _capability_document(recovery_capabilities) if recovery_capabilities else None
        ),
        "recovery_steps": recovery_steps,
        "cleanup_errors": cleanup_errors,
        "reset_boundary": {
            "commanded_target_reset": False,
            "commanded_power_cycle": False,
            "commanded_bluetooth_power_off": False,
            "service_unregistered": False,
            "expected_recovery_action": "attach",
            "proof_limit": (
                "The stock firmware exposes no boot ID. No reset/power command is issued; stable "
                "USB identity and an attached resident GAP handler are used as continuity evidence."
            ),
        },
    }
    return write_json_report(reports_root, "btp-gap-host-crash-probe", document, now=finished_at)


def _run_child(arguments: CrashProbeArguments) -> int:
    settings = resolve_current_settings(config_path=arguments.config)
    upstream = load_upstream_pins()
    verify_upstream(upstream, settings)
    socat_pin = load_socat_pin()
    verify_socat(socat_pin, settings)

    identity = select_application_port(
        port_name=arguments.port,
        serial_number=settings["device_serial"].value,
        transport=arguments.transport,
    )
    loaded = load_autopts(_required_path(settings, "autopts_root"))
    reports_root = _required_path(settings, "reports_dir")
    session = AutoPtsSession(
        loaded,
        identity=identity,
        log_dir=reports_root / "btp-gap-host-crash-probe" / arguments.run_id / "child",
        socat_path=managed_socat_path(socat_pin, settings),
    )
    advertising_started = False
    try:
        capabilities = session.start(required_services=("CORE", "GAP"))
        controller = session.read_controller_info()
        if controller.current_settings.get("Advertising"):
            controller = session.stop_advertising()
        controller = session.set_powered(True)
        controller = session.set_connectable(True)
        controller = session.set_discoverable(True)
        advertising = session.start_advertising(arguments.local_name)
        advertising_started = True
        _require_setting(advertising, "Advertising", True)
        _emit_marker(
            {
                "run_id": arguments.run_id,
                "pid": os.getpid(),
                "port": identity.port,
                "service_actions": capabilities.service_actions,
                "controller_before_advertising": _snapshot_document(controller),
                "advertising": _snapshot_document(advertising),
            }
        )
        time.sleep(arguments.hold_seconds)
        raise BtpGapHostCrashProbeError("crash child was not terminated during its hold window")
    finally:
        if advertising_started:
            with suppress(Exception):
                _ = session.stop_advertising()
        session.close()


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    config_path: str | None = None,
    port_name: str | None = None,
    local_name: str = DEFAULT_LOCAL_NAME,
    ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
    release_timeout_seconds: int = DEFAULT_RELEASE_TIMEOUT_SECONDS,
    transport: str = "dongle",
) -> Path:
    if not 10 <= ready_timeout_seconds <= 300:
        raise BtpGapHostCrashProbeError("ready timeout must be in range 10..=300 seconds")
    if not 5 <= release_timeout_seconds <= 120:
        raise BtpGapHostCrashProbeError("release timeout must be in range 5..=120 seconds")

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
    loaded = load_autopts(_required_path(settings, "autopts_root"))
    reports_root = _required_path(settings, "reports_dir")
    socat = managed_socat_path(socat_pin, settings)

    run_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    command = _child_command(
        port=identity.port,
        local_name=local_name,
        run_id=run_id,
        config_path=config_path,
        transport=transport,
    )
    child_output: list[str] = []
    child_ready: dict[str, object] | None = None
    serial_release: dict[str, object] | None = None
    recovery_capabilities: CapabilitySnapshot | None = None
    recovery_steps: list[dict[str, object]] = []
    cleanup_errors: list[str] = []
    main_error: Exception | None = None
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    recovery: AutoPtsSession | None = None
    child_returncode: int | None = None

    print(f"Run ID: {run_id}")
    print(f"Application port: {identity.port} ({identity.serial_number})")
    print(f"AutoPTS tty transport: {autopts_tty_file(identity.port)}")
    print("The child will be killed while advertising; no target reset will be issued.")

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=PROJECT_ROOT,
        )
        assert process.stdout is not None
        messages: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=_collect_output,
            args=(process.stdout, child_output, messages),
            name="nrftest-crash-child-output",
            daemon=True,
        )
        reader.start()
        child_ready = _wait_for_ready_marker(
            process,
            messages,
            timeout_seconds=ready_timeout_seconds,
        )
        print(f"Crash child ready at pid {process.pid}; force-terminating it now.")
        process.kill()
        child_returncode = process.wait(timeout=10)
        if child_returncode == 0:
            raise BtpGapHostCrashProbeError("crash child exited cleanly instead of being killed")
        reader.join(timeout=5)

        serial_release = _wait_for_serial_release(
            identity,
            timeout_seconds=release_timeout_seconds,
        )
        print(f"Serial transport released after {serial_release['elapsed_seconds']} seconds.")

        rediscovered = select_application_port(
            serial_number=identity.serial_number, transport=transport
        )
        if rediscovered.port != identity.port:
            raise BtpGapHostCrashProbeError(
                f"application port changed from {identity.port} to {rediscovered.port}; "
                + "the no-re-enumeration recovery boundary was not preserved"
            )
        recovery = AutoPtsSession(
            loaded,
            identity=rediscovered,
            log_dir=reports_root / "btp-gap-host-crash-probe" / run_id / "recovery",
            socat_path=socat,
        )
        recovery_capabilities = recovery.start(required_services=("CORE", "GAP"))
        if recovery_capabilities.service_actions.get("GAP") != "attached":
            raise BtpGapHostCrashProbeError(
                "recovery did not attach to the resident GAP service; "
                + "an unobserved reset is possible"
            )

        observed = recovery.read_controller_info()
        _require_setting(observed, "Advertising", True)
        recovery_steps.append(
            {"name": "observed-child-advertising", "gap": _snapshot_document(observed)}
        )

        stopped = recovery.stop_advertising()
        _require_setting(stopped, "Advertising", False)
        recovery_steps.append(
            {"name": "stop-orphaned-advertising", "gap": _snapshot_document(stopped)}
        )

        restarted = recovery.start_advertising(local_name)
        _require_setting(restarted, "Advertising", True)
        recovery_steps.append({"name": "restart-advertising", "gap": _snapshot_document(restarted)})

        restopped = recovery.stop_advertising()
        _require_setting(restopped, "Advertising", False)
        recovery_steps.append(
            {"name": "stop-restarted-advertising", "gap": _snapshot_document(restopped)}
        )
    except Exception as error:
        main_error = error
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                child_returncode = process.wait(timeout=10)
            except Exception as error:
                cleanup_errors.append(f"child termination: {type(error).__name__}: {error}")
        if reader is not None:
            reader.join(timeout=5)
        if recovery is not None:
            try:
                current = recovery.gap_snapshot()
                if current.current_settings.get("Advertising"):
                    _ = recovery.stop_advertising()
            except Exception as error:
                cleanup_errors.append(f"recovery advertising: {type(error).__name__}: {error}")
            recovery.close()
            cleanup_errors.extend(recovery.cleanup_errors)

    if main_error is not None or cleanup_errors:
        detail_parts: list[str] = []
        if main_error is not None:
            detail_parts.append(f"{type(main_error).__name__}: {main_error}")
        detail_parts.extend(cleanup_errors)
        report = _write_report(
            reports_root,
            run_id=run_id,
            started_at=started_at,
            outcome="failure",
            detail="; ".join(detail_parts),
            identity=identity,
            autopts_commit=upstream.autopts.commit,
            local_name=local_name,
            child_command=command,
            child_pid=process.pid if process else None,
            child_returncode=child_returncode,
            child_ready=child_ready,
            child_output=child_output,
            serial_release=serial_release,
            recovery_capabilities=recovery_capabilities,
            recovery_steps=recovery_steps,
            cleanup_errors=cleanup_errors,
        )
        raise BtpGapHostCrashProbeError(
            f"BTP GAP Host crash probe failed; report: {report}"
        ) from main_error

    report = _write_report(
        reports_root,
        run_id=run_id,
        started_at=started_at,
        outcome="pass",
        detail=None,
        identity=identity,
        autopts_commit=upstream.autopts.commit,
        local_name=local_name,
        child_command=command,
        child_pid=process.pid if process else None,
        child_returncode=child_returncode,
        child_ready=child_ready,
        child_output=child_output,
        serial_release=serial_release,
        recovery_capabilities=recovery_capabilities,
        recovery_steps=recovery_steps,
        cleanup_errors=cleanup_errors,
    )
    print("BTP GAP Host crash recovery probe: PASS")
    print(f"Host crash recovery report: {report}")
    return report


class CrashProbeArguments(argparse.Namespace):
    config: str | None = None
    port: str | None = None
    local_name: str = DEFAULT_LOCAL_NAME
    ready_timeout: int = DEFAULT_READY_TIMEOUT_SECONDS
    release_timeout: int = DEFAULT_RELEASE_TIMEOUT_SECONDS
    child: bool = False
    run_id: str = ""
    hold_seconds: int = CHILD_HOLD_SECONDS
    transport: str = "dongle"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Force-kill a GAP advertising Host and attach without resetting the target"
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    _ = parser.add_argument("--port", help="exact current application serial port")
    _ = parser.add_argument("--local-name", default=DEFAULT_LOCAL_NAME)
    _ = parser.add_argument("--ready-timeout", type=int, default=DEFAULT_READY_TIMEOUT_SECONDS)
    _ = parser.add_argument("--release-timeout", type=int, default=DEFAULT_RELEASE_TIMEOUT_SECONDS)
    _ = parser.add_argument(
        "--transport",
        choices=("dongle", "dk"),
        default="dongle",
        help="application USB identity: dongle (PCA10059 CDC) or dk (J-Link VCOM)",
    )
    _ = parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument(
        "--hold-seconds", type=int, default=CHILD_HOLD_SECONDS, help=argparse.SUPPRESS
    )
    return parser


def main() -> int:
    arguments = CrashProbeArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        if arguments.child:
            if not arguments.run_id or arguments.port is None:
                raise BtpGapHostCrashProbeError("internal crash child requires run ID and port")
            return _run_child(arguments)
        _ = run_probe(
            resolve_current_settings(config_path=arguments.config),
            config_path=arguments.config,
            port_name=arguments.port,
            local_name=arguments.local_name,
            ready_timeout_seconds=arguments.ready_timeout,
            release_timeout_seconds=arguments.release_timeout,
            transport=arguments.transport,
        )
    except (
        AutoPtsAdapterError,
        BtpGapHostCrashProbeError,
        ConfigError,
        HostToolError,
        SetupError,
        TelemetryError,
    ) as error:
        print(f"BTP GAP Host crash probe error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
