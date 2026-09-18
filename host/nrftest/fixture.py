from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, final

from host.nrftest.autopts_adapter import (
    AutoPtsSession,
    CapabilitySnapshot,
    ConnectionSnapshot,
    GapSnapshot,
    GattValueChangedSnapshot,
    SerialIdentity,
    load_autopts,
    select_application_port,
)
from host.nrftest.gatt_profile import (
    GattProfileMapping,
    GattProfileSession,
    ensure_gatt_profile,
)
from host.nrftest.passive_disconnect import (
    PassiveDisconnectSession,
    request_peripheral_disconnect,
    require_peer_disconnected,
)
from host.nrftest.profile import PeripheralProfile
from host.nrftest.subscription import (
    CccObservation,
    SubscriptionMode,
    SubscriptionSession,
    ValueUpdateObservation,
    set_and_verify_value,
    wait_for_ccc,
)


class FixtureError(RuntimeError):
    """Raised when the high-level Peripheral fixture lifecycle is used incorrectly."""


class DisconnectTrigger(StrEnum):
    GAP_DISCONNECT = "gap-disconnect"
    RADIO_STACK_RESTART = "radio-stack-restart"


class FixtureSession(
    GattProfileSession,
    SubscriptionSession,
    PassiveDisconnectSession,
    Protocol,
):
    @property
    def cleanup_errors(self) -> tuple[str, ...]: ...

    @property
    def cleanup_classification(self) -> str: ...

    def start(
        self,
        *,
        required_services: Sequence[str],
        local_name: str | None = None,
    ) -> CapabilitySnapshot: ...

    def gap_snapshot(self) -> GapSnapshot: ...

    def read_controller_info(self) -> GapSnapshot: ...

    def set_powered(self, enabled: bool) -> GapSnapshot: ...

    def set_connectable(self, enabled: bool) -> GapSnapshot: ...

    def set_discoverable(self, enabled: bool) -> GapSnapshot: ...

    def start_advertising(
        self,
        local_name: str,
        *,
        service_uuid16: str | None = None,
        service_uuid128: str | None = None,
    ) -> GapSnapshot: ...

    def stop_advertising(self) -> GapSnapshot: ...

    def wait_for_connection(self, timeout: float) -> GapSnapshot: ...

    def clear_gatt_value_changed(self, handle: int) -> None: ...

    def wait_for_gatt_value_changed(
        self,
        handle: int,
        timeout: float,
    ) -> GattValueChangedSnapshot: ...

    def close(self, *, unregister_services: bool = False) -> None: ...


@dataclass(frozen=True)
class FixtureEnvironment:
    autopts_root: Path
    socat_path: Path
    reports_root: Path
    port_name: str | None = None
    device_serial: str | None = None
    transport: str = "dongle"


@dataclass(frozen=True)
class DoctorSnapshot:
    identity: SerialIdentity
    capabilities: CapabilitySnapshot
    controller: GapSnapshot


@dataclass(frozen=True)
class DisconnectObservation:
    trigger: DisconnectTrigger
    command_snapshot: GapSnapshot
    event_snapshot: GapSnapshot
    recovery_snapshot: GapSnapshot | None


@final
class PeripheralFixture:
    """High-level synchronous API for one resident nRF Peripheral fixture session."""

    def __init__(self, session: FixtureSession, identity: SerialIdentity) -> None:
        self._session = session
        self._identity = identity
        self._capabilities: CapabilitySnapshot | None = None
        self._profile: PeripheralProfile | None = None
        self._mapping: GattProfileMapping | None = None
        self._connection: ConnectionSnapshot | None = None
        self._advertising = False
        self._closed = False
        self._cleanup_errors: list[str] = []

    @classmethod
    def open(cls, environment: FixtureEnvironment) -> PeripheralFixture:
        identity = select_application_port(
            port_name=environment.port_name,
            serial_number=environment.device_serial,
            transport=environment.transport,
        )
        loaded = load_autopts(environment.autopts_root)
        session = AutoPtsSession(
            loaded,
            identity=identity,
            log_dir=environment.reports_root / "host-session" / "autopts",
            socat_path=environment.socat_path,
        )
        return cls(session, identity)

    @property
    def identity(self) -> SerialIdentity:
        return self._identity

    @property
    def capabilities(self) -> CapabilitySnapshot | None:
        return self._capabilities

    @property
    def profile(self) -> PeripheralProfile | None:
        return self._profile

    @property
    def mapping(self) -> GattProfileMapping | None:
        return self._mapping

    @property
    def connection(self) -> ConnectionSnapshot | None:
        return self._connection

    @property
    def cleanup_errors(self) -> tuple[str, ...]:
        return (*self._cleanup_errors, *self._session.cleanup_errors)

    @property
    def cleanup_classification(self) -> str:
        if self._cleanup_errors:
            return "unexpected"
        return self._session.cleanup_classification

    def __enter__(self) -> PeripheralFixture:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def doctor(self, *, local_name: str | None = None) -> DoctorSnapshot:
        capabilities = self._ensure_started(local_name=local_name)
        controller = self._session.read_controller_info()
        if controller.address is None or controller.address_type is None:
            raise FixtureError("BTP controller information did not include an address and type")
        return DoctorSnapshot(self._identity, capabilities, controller)

    def load_profile(self, profile: PeripheralProfile) -> GattProfileMapping:
        self._require_open()
        if self._profile is not None and (
            self._profile.semantic_signature() != profile.semantic_signature()
        ):
            raise FixtureError(
                "cannot switch Profile topology in a resident fixture session; use another "
                + "provisioned target or an explicit suite-boundary reset"
            )

        capabilities = self._ensure_started(local_name=profile.local_name)
        command_mask = capabilities.supported_command_masks.get("GATT")
        if command_mask is None:
            raise FixtureError("BTP GATT service did not report a supported-command mask")
        mapping = ensure_gatt_profile(self._session, profile, command_mask=command_mask)
        self._profile = profile
        self._mapping = mapping
        return mapping

    def start_advertising(self) -> GapSnapshot:
        profile, _ = self._require_profile()
        current = self._session.gap_snapshot()
        if current.connections:
            raise FixtureError("cannot start advertising while a peer remains connected")
        if current.current_settings.get("Advertising"):
            current = self._session.stop_advertising()
        if current.current_settings.get("Powered") is not True:
            current = self._session.set_powered(True)
        if current.current_settings.get("Connectable") is not True:
            current = self._session.set_connectable(True)
        if current.current_settings.get("Discoverable") is not True:
            _ = self._session.set_discoverable(True)
        self._advertising = True
        advertising = self._session.start_advertising(
            profile.local_name,
            service_uuid128=profile.service.uuid,
        )
        if advertising.current_settings.get("Advertising") is not True:
            raise FixtureError("BTP advertising command completed without Advertising=true")
        return advertising

    def stop_advertising(self) -> GapSnapshot:
        self._require_open()
        current = self._session.gap_snapshot()
        if current.current_settings.get("Advertising"):
            current = self._session.stop_advertising()
        self._advertising = False
        return current

    def snapshot(self) -> GapSnapshot:
        self._require_open()
        current = self._session.gap_snapshot()
        if len(current.connections) > 1:
            raise FixtureError(
                f"expected at most one peer connection, found {len(current.connections)}"
            )
        self._connection = current.connections[0] if current.connections else None
        self._advertising = current.current_settings.get("Advertising") is True
        return current

    def wait_for_connection(self, timeout: float) -> ConnectionSnapshot:
        _ = self._require_profile()
        if timeout <= 0:
            raise FixtureError("connection timeout must be greater than zero")
        snapshot = self._session.wait_for_connection(timeout)
        if len(snapshot.connections) != 1:
            raise FixtureError(f"expected one peer connection, found {len(snapshot.connections)}")
        self._connection = snapshot.connections[0]
        self._advertising = False
        return self._connection

    def wait_for_disconnection(self, timeout: float) -> GapSnapshot:
        self._require_open()
        connection = self._require_connection("wait for disconnection")
        if timeout <= 0:
            raise FixtureError("disconnect timeout must be greater than zero")
        snapshot = self._session.wait_for_disconnection(timeout, address=connection.address)
        require_peer_disconnected(snapshot, connection)
        self._connection = None
        self._advertising = False
        return snapshot

    def wait_for_subscription_state(
        self,
        mode: SubscriptionMode,
        *,
        enabled: bool,
        timeout: float,
    ) -> CccObservation:
        profile, mapping = self._require_profile()
        connection = self._require_connection("wait for subscription state")

        specification = profile.characteristic_for_role("updates")
        if mode is SubscriptionMode.NOTIFICATION and not specification.notifiable:
            raise FixtureError("updates role does not support notifications")
        if mode is SubscriptionMode.INDICATION and not specification.indicatable:
            raise FixtureError("updates role does not support indications")

        ccc_handle = mapping.characteristic_for_role("updates").ccc_handle
        if ccc_handle is None:
            raise FixtureError("updates role has no mapped CCC handle")
        expected = mode.ccc_value if enabled else b"\x00\x00"
        allowed_pending = (b"\x00\x00",) if enabled else (mode.ccc_value,)
        return wait_for_ccc(
            self._session,
            ccc_handle,
            peer_address=connection.address,
            peer_address_type=connection.address_type,
            expected=expected,
            allowed_pending=allowed_pending,
            timeout=timeout,
        )

    def set_value(self, role: str, payload: bytes) -> ValueUpdateObservation:
        _, mapping = self._require_profile()
        handles = mapping.characteristic_for_role(role)
        peer_address = self._connection.address if self._connection is not None else "000000000000"
        peer_address_type = self._connection.address_type if self._connection is not None else 0
        return set_and_verify_value(
            self._session,
            handles.value_handle,
            payload,
            peer_address=peer_address,
            peer_address_type=peer_address_type,
        )

    def clear_write_events(self, role: str) -> None:
        _, mapping = self._require_profile()
        self._session.clear_gatt_value_changed(mapping.characteristic_for_role(role).value_handle)

    def wait_for_write(
        self,
        role: str,
        *,
        timeout: float,
        expected: bytes | None = None,
    ) -> GattValueChangedSnapshot:
        _, mapping = self._require_profile()
        _ = self._require_connection("wait for a peer write")
        if timeout <= 0:
            raise FixtureError("write wait timeout must be greater than zero")
        observation = self._session.wait_for_gatt_value_changed(
            mapping.characteristic_for_role(role).value_handle,
            timeout,
        )
        if expected is not None and observation.value != expected:
            raise FixtureError(
                f"role {role} changed to {observation.value.hex()}; expected {expected.hex()}"
            )
        return observation

    def disconnect_peer(
        self,
        *,
        trigger: DisconnectTrigger = DisconnectTrigger.RADIO_STACK_RESTART,
        timeout: float,
    ) -> DisconnectObservation:
        self._require_open()
        connection = self._require_connection("disconnect")
        if timeout <= 0:
            raise FixtureError("disconnect timeout must be greater than zero")

        if trigger is DisconnectTrigger.GAP_DISCONNECT:
            observation = request_peripheral_disconnect(
                self._session,
                connection,
                timeout=timeout,
            )
            result = DisconnectObservation(
                trigger,
                observation.command_snapshot,
                observation.event_snapshot,
                None,
            )
        else:
            command_snapshot = self._session.set_powered(False)
            if command_snapshot.current_settings.get("Powered") is not False:
                raise FixtureError("radio shutdown completed without Powered=false")
            try:
                event_snapshot = self._session.wait_for_disconnection(
                    timeout,
                    address=connection.address,
                )
                require_peer_disconnected(event_snapshot, connection)
            finally:
                recovery_snapshot = self._session.set_powered(True)
            if recovery_snapshot.current_settings.get("Powered") is not True:
                raise FixtureError("radio restart completed without Powered=true")
            result = DisconnectObservation(
                trigger,
                command_snapshot,
                event_snapshot,
                recovery_snapshot,
            )

        self._connection = None
        self._advertising = False
        return result

    def close(self) -> None:
        if self._closed:
            return
        if self._advertising:
            try:
                _ = self.stop_advertising()
            except Exception as error:
                self._cleanup_errors.append(f"stop advertising: {type(error).__name__}: {error}")
        self._session.close()
        self._closed = True

    def _ensure_started(self, *, local_name: str | None) -> CapabilitySnapshot:
        self._require_open()
        if self._capabilities is None:
            self._capabilities = self._session.start(
                required_services=("CORE", "GAP", "GATT"),
                local_name=local_name,
            )
        return self._capabilities

    def _require_profile(self) -> tuple[PeripheralProfile, GattProfileMapping]:
        self._require_open()
        if self._profile is None or self._mapping is None:
            raise FixtureError("a verified Profile must be loaded before this operation")
        return self._profile, self._mapping

    def _require_connection(self, operation: str) -> ConnectionSnapshot:
        if self._connection is None:
            raise FixtureError(f"cannot {operation} without an observed peer connection")
        return self._connection

    def _require_open(self) -> None:
        if self._closed:
            raise FixtureError("fixture session is closed")
