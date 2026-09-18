from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import cast

import pytest

from host.nrftest.profile import (
    BASIC_GATT_ROLES,
    PeripheralProfile,
    ProfileError,
    profile_source_sha256,
    require_basic_gatt_roles,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_PROFILE = PROJECT_ROOT / "profiles" / "blehub-nrf-basic-v1.json"
# SHA-256 of the committed profiles/blehub-nrf-basic-v1.json (the reviewed artifact).
ACTIVE_PROFILE_SHA256 = "2c315af0092cb67ebcdd257913ec96a877edae648a0993f2be707dd725e26924"

PROFILE: dict[str, object] = {
    "schema_version": 1,
    "profile_id": "blehub-nrf52840-basic-gatt-v1",
    "advertising": {
        "local_name": "BleHubNrf52840",
        "require_local_name": False,
        "service_uuids": ["fd000000-0000-4000-8000-000000000000"],
    },
    "services": [
        {
            "uuid": "fd000000-0000-4000-8000-000000000000",
            "primary": True,
            "advertise": True,
            "characteristics": [
                {
                    "role": "read-write",
                    "uuid": "fff10000-0000-4000-8000-000000000000",
                    "properties": ["read", "write", "write_without_response"],
                    "initial_value_hex": "00",
                },
                {
                    "role": "updates",
                    "uuid": "fff20000-0000-4000-8000-000000000000",
                    "properties": ["read", "notify", "indicate"],
                    "initial_value_hex": "00",
                },
            ],
        }
    ],
}


def test_active_profile_preserves_the_reviewed_basic_gatt_contract() -> None:
    profile = PeripheralProfile.load(ACTIVE_PROFILE)

    assert profile.profile_id == "blehub-nrf52840-basic-gatt-v1"
    assert profile.local_name == "BleHubNrf52840"
    assert profile.require_local_name is False
    assert profile.advertised_service_uuids == ("fd000000-0000-4000-8000-000000000000",)
    assert profile.service.uuid == "fd000000-0000-4000-8000-000000000000"
    assert profile.service.primary is True
    assert profile.service.advertise is True
    assert {item.role for item in profile.service.characteristics} == BASIC_GATT_ROLES

    expected = {
        "read-write": (
            "fff10000-0000-4000-8000-000000000000",
            frozenset({"read", "write", "write_without_response"}),
            b"\x00",
        ),
        "updates": (
            "fff20000-0000-4000-8000-000000000000",
            frozenset({"read", "notify", "indicate"}),
            b"\x00",
        ),
        "read-only": (
            "fff30000-0000-4000-8000-000000000000",
            frozenset({"read"}),
            b"BleHub",
        ),
        "write-only": (
            "fff40000-0000-4000-8000-000000000000",
            frozenset({"write", "write_without_response"}),
            b"\x00",
        ),
    }
    for role, contract in expected.items():
        characteristic = profile.characteristic_for_role(role)
        assert (
            characteristic.uuid,
            characteristic.properties,
            characteristic.initial_value,
        ) == contract

    require_basic_gatt_roles(profile)
    assert profile_source_sha256(ACTIVE_PROFILE) == ACTIVE_PROFILE_SHA256


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _characteristic(document: dict[str, object], index: int) -> dict[str, object]:
    service = _object(_array(document["services"])[0])
    return _object(_array(service["characteristics"])[index])


def test_semantic_signature_is_provider_independent_and_property_order_stable() -> None:
    reordered = copy.deepcopy(PROFILE)
    reordered["profile_id"] = "different-provider"
    _object(reordered["advertising"])["local_name"] = "DifferentName"
    _array(_characteristic(reordered, 0)["properties"]).reverse()

    original_signature = PeripheralProfile.from_document(PROFILE).semantic_signature()
    reordered_signature = PeripheralProfile.from_document(reordered).semantic_signature()

    assert reordered_signature == original_signature
    assert "profile_id" not in original_signature
    assert "local_name" not in original_signature


def test_schema_allows_a_valid_role_subset_for_noncanonical_profiles(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    _ = path.write_text(json.dumps(PROFILE), encoding="utf-8")

    profile = PeripheralProfile.load(path)

    assert tuple(item.role for item in profile.service.characteristics) == (
        "read-write",
        "updates",
    )
    with pytest.raises(ProfileError, match="missing required roles: read-only, write-only"):
        require_basic_gatt_roles(profile)


def test_rejects_advertising_root_service_mismatch() -> None:
    document = copy.deepcopy(PROFILE)
    _object(document["advertising"])["service_uuids"] = ["fd000001-0000-4000-8000-000000000000"]

    with pytest.raises(ProfileError, match="advertised service"):
        _ = PeripheralProfile.from_document(document)


def test_rejects_duplicate_characteristic_roles_and_uuids() -> None:
    duplicate_role = copy.deepcopy(PROFILE)
    service = _object(_array(duplicate_role["services"])[0])
    _array(service["characteristics"]).append(
        {
            "role": "read-write",
            "uuid": "fff50000-0000-4000-8000-000000000000",
            "properties": ["read", "write", "write_without_response"],
        }
    )
    with pytest.raises(ProfileError, match="roles must be unique"):
        _ = PeripheralProfile.from_document(duplicate_role)

    duplicate_uuid = copy.deepcopy(PROFILE)
    _characteristic(duplicate_uuid, 1)["uuid"] = "fff10000-0000-4000-8000-000000000000"
    with pytest.raises(ProfileError, match="UUIDs must be unique"):
        _ = PeripheralProfile.from_document(duplicate_uuid)


def test_rejects_properties_that_do_not_match_the_role_contract() -> None:
    document = copy.deepcopy(PROFILE)
    _characteristic(document, 0)["properties"] = ["read", "write"]

    with pytest.raises(ProfileError, match="properties for role read-write"):
        _ = PeripheralProfile.from_document(document)


def test_rejects_invalid_initial_value_hex() -> None:
    document = copy.deepcopy(PROFILE)
    _characteristic(document, 0)["initial_value_hex"] = "0"

    with pytest.raises(ProfileError, match="even number of hex digits"):
        _ = PeripheralProfile.from_document(document)
