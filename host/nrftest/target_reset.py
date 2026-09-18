from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from serial.tools import list_ports

from host.nrftest.autopts_adapter import (
    APPLICATION_USB_PID,
    APPLICATION_USB_VID,
    SerialIdentity,
    SerialPort,
    select_application_port,
)

JLINK_TARGET_DEVICE = "NRF52840_XXAA"


class TargetResetError(RuntimeError):
    """Raised when an external reset cannot restore a uniquely identified target."""


class TargetResetDriver(Protocol):
    def open(self) -> None: ...

    def reset_and_halt(self) -> None: ...

    def run(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class TargetResetResult:
    before: SerialIdentity
    after: SerialIdentity
    disappearance_seconds: float
    reappearance_seconds: float


@dataclass(frozen=True)
class DkTargetResetResult:
    """One DK reset outcome: the VCOM port must survive with an identical identity."""

    before: SerialIdentity
    after: SerialIdentity
    reset_seconds: float


class PyLinkTargetResetDriver:
    """Reset one nRF52840 through an explicitly selected SEGGER J-Link."""

    def __init__(self, library_path: Path, debugger_serial: int) -> None:
        if not library_path.is_file():
            raise TargetResetError(f"J-Link native library does not exist: {library_path}")
        if debugger_serial <= 0:
            raise TargetResetError("J-Link debugger serial must be a positive integer")
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
                raise TargetResetError("J-Link rejected the SWD target interface")
            link.connect(JLINK_TARGET_DEVICE)
        except TargetResetError:
            raise
        except Exception as error:
            raise TargetResetError(
                f"unable to connect J-Link {self._debugger_serial} to {JLINK_TARGET_DEVICE}: "
                + f"{type(error).__name__}: {error}"
            ) from error

    def reset_and_halt(self) -> None:
        link = self._required_link()
        try:
            _ = link.reset(halt=True)
            if not link.halted():
                raise TargetResetError("J-Link reset returned without halting the target")
        except TargetResetError:
            raise
        except Exception as error:
            raise TargetResetError(
                f"J-Link reset-and-halt failed: {type(error).__name__}: {error}"
            ) from error

    def run(self) -> None:
        link = self._required_link()
        try:
            if not link.restart():
                raise TargetResetError("J-Link could not release the halted target")
        except TargetResetError:
            raise
        except Exception as error:
            raise TargetResetError(
                f"J-Link target release failed: {type(error).__name__}: {error}"
            ) from error

    def close(self) -> None:
        link = self._link
        if link is None:
            return
        try:
            link.close()
        except Exception as error:
            raise TargetResetError(
                f"J-Link close failed: {type(error).__name__}: {error}"
            ) from error
        self._link = None

    def _required_link(self):
        if self._link is None:
            raise TargetResetError("J-Link target is not open")
        return self._link


def _matching_application_ports(
    serial_number: str,
    ports: Iterable[SerialPort],
) -> list[SerialPort]:
    return [
        port
        for port in ports
        if port.vid == APPLICATION_USB_VID
        and port.pid == APPLICATION_USB_PID
        and port.serial_number == serial_number
    ]


def _identity_facts(identity: SerialIdentity) -> tuple[str, int | None, int | None, str | None]:
    return (identity.port, identity.vid, identity.pid, identity.serial_number)


def _current_identity(
    port_name: str,
    ports: Iterable[SerialPort],
) -> SerialIdentity:
    named = [port for port in ports if port.device.casefold() == port_name.casefold()]
    if len(named) != 1:
        names = ", ".join(port.device for port in ports) or "<none>"
        raise TargetResetError(
            f"explicit port {port_name!r} is not uniquely present after reset; "
            + f"available ports: {names}"
        )
    return SerialIdentity(
        port=named[0].device,
        description=named[0].description,
        hwid=named[0].hwid,
        vid=named[0].vid,
        pid=named[0].pid,
        serial_number=named[0].serial_number,
    )


def _wait_for_disappearance(
    serial_number: str,
    *,
    ports: Callable[[], Iterable[SerialPort]],
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> float:
    started = monotonic()
    deadline = started + timeout_seconds
    while monotonic() <= deadline:
        if not _matching_application_ports(serial_number, ports()):
            return monotonic() - started
        sleep(poll_interval_seconds)
    raise TargetResetError(
        f"application USB identity {APPLICATION_USB_VID:04X}:{APPLICATION_USB_PID:04X} "
        + f"serial={serial_number} did not disappear within {timeout_seconds:g} seconds"
    )


def _wait_for_reappearance(
    serial_number: str,
    *,
    ports: Callable[[], Iterable[SerialPort]],
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[SerialIdentity, float]:
    started = monotonic()
    deadline = started + timeout_seconds
    while monotonic() <= deadline:
        available = list(ports())
        matches = _matching_application_ports(serial_number, available)
        if len(matches) > 1:
            names = ", ".join(port.device for port in matches)
            raise TargetResetError(
                f"application hardware serial {serial_number!r} reappeared ambiguously: {names}"
            )
        if len(matches) == 1:
            return (
                select_application_port(serial_number=serial_number, ports=available),
                monotonic() - started,
            )
        sleep(poll_interval_seconds)
    raise TargetResetError(
        f"application USB identity {APPLICATION_USB_VID:04X}:{APPLICATION_USB_PID:04X} "
        + f"serial={serial_number} did not reappear within {timeout_seconds:g} seconds"
    )


def reset_target_and_rediscover(
    driver: TargetResetDriver,
    before: SerialIdentity,
    *,
    disappearance_timeout_seconds: float = 10.0,
    reappearance_timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.05,
    ports: Callable[[], Iterable[SerialPort]] = list_ports.comports,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TargetResetResult:
    if before.serial_number is None:
        raise TargetResetError("target reset requires a stable application hardware serial")
    if disappearance_timeout_seconds <= 0 or reappearance_timeout_seconds <= 0:
        raise TargetResetError("target reset timeouts must be positive")
    if poll_interval_seconds <= 0:
        raise TargetResetError("target reset poll interval must be positive")

    main_error: Exception | None = None
    cleanup_errors: list[str] = []
    halted = False
    disappearance_seconds = 0.0
    try:
        driver.open()
        driver.reset_and_halt()
        halted = True
        disappearance_seconds = _wait_for_disappearance(
            before.serial_number,
            ports=ports,
            timeout_seconds=disappearance_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
    except Exception as error:
        main_error = error
    finally:
        if halted:
            try:
                driver.run()
            except Exception as error:
                cleanup_errors.append(f"release: {type(error).__name__}: {error}")
        try:
            driver.close()
        except Exception as error:
            cleanup_errors.append(f"close: {type(error).__name__}: {error}")

    if main_error is not None or cleanup_errors:
        details: list[str] = []
        if main_error is not None:
            details.append(f"{type(main_error).__name__}: {main_error}")
        details.extend(cleanup_errors)
        raise TargetResetError("; ".join(details)) from main_error

    after, reappearance_seconds = _wait_for_reappearance(
        before.serial_number,
        ports=ports,
        timeout_seconds=reappearance_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        monotonic=monotonic,
        sleep=sleep,
    )
    return TargetResetResult(
        before=before,
        after=after,
        disappearance_seconds=disappearance_seconds,
        reappearance_seconds=reappearance_seconds,
    )


def reset_dk_target(
    driver: TargetResetDriver,
    before: SerialIdentity,
    *,
    ports: Callable[[], Iterable[SerialPort]] = list_ports.comports,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    halt_settle_seconds: float = 0.1,
) -> DkTargetResetResult:
    """Reset one DK through J-Link and require the VCOM identity to stay stable.

    Unlike the Dongle, the DK's J-Link VCOM does not disappear while the target is
    halted, so the recovery boundary here is: reset-and-halt, release, and confirm
    the same port is present with an identical identity. BTP re-handshake is a
    separate gate owned by the caller.
    """
    if halt_settle_seconds < 0:
        raise TargetResetError("halt settle seconds must not be negative")

    started = monotonic()
    main_error: Exception | None = None
    cleanup_errors: list[str] = []
    halted = False
    try:
        driver.open()
        driver.reset_and_halt()
        halted = True
        sleep(halt_settle_seconds)
    except Exception as error:
        main_error = error
    finally:
        if halted:
            try:
                driver.run()
            except Exception as error:
                cleanup_errors.append(f"release: {type(error).__name__}: {error}")
        try:
            driver.close()
        except Exception as error:
            cleanup_errors.append(f"close: {type(error).__name__}: {error}")

    if main_error is not None or cleanup_errors:
        details: list[str] = []
        if main_error is not None:
            details.append(f"{type(main_error).__name__}: {main_error}")
        details.extend(cleanup_errors)
        raise TargetResetError("; ".join(details)) from main_error

    after = _current_identity(before.port, ports())
    if _identity_facts(after) != _identity_facts(before):
        raise TargetResetError(
            "J-Link VCOM identity changed across reset: "
            + f"expected {_identity_facts(before)!r}, found {_identity_facts(after)!r}"
        )
    return DkTargetResetResult(
        before=before,
        after=after,
        reset_seconds=monotonic() - started,
    )
