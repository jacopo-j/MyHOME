"""Verify the fork's read-only actuator entities through the v2 setup path."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from OWNd.message import OWNLightingEvent, OWNMessage
from pytest_homeassistant_custom_component.common import MockConfigEntry
from voluptuous import Invalid

from custom_components.myhome.actuator_monitor import _route_targets, _target
from custom_components.myhome.const import CONF_FILE_PATH, DOMAIN
from custom_components.myhome.discovery import Address
from custom_components.myhome.legacy_yaml import CONF_MIGRATED_YAML_PLATFORMS
from custom_components.myhome.validate import binary_sensor_schema

MAC = "00:03:50:81:22:33"
CONFIG = f"""
{MAC}:
  binary_sensor:
    relay:
      {{who: 1, where: "12", class: power, name: Relay, icon: "mdi:power-off", icon_on: "mdi:power"}}
    routed_relay:
      {{who: 1, where: "12#4#1", class: power, name: Remote relay}}
    area_zero_relay:
      {{who: 1, where: "0004", class: power, name: Area zero relay}}
    shared_motion:
      {{who: 1, where: "13", class: motion, name: Hall motion}}
    shared_relay:
      {{who: 1, where: "13", class: power, name: Hall relay, inverted: true}}
    named_actuator:
      {{who: 1, where: "1010", class: power, name: Humidity alarm}}
    named_light_monitor:
      {{who: 1, where: "1011", class: power, name: Ceiling relay}}
    contact:
      {{who: 25, where: "31", class: power, name: Contact}}
    zone:
      {{who: 4, where: "2", class: power, name: Zone}}
    inverted_zone:
      {{who: 4, where: "5", class: power, name: Other zone, inverted: true}}
    valve:
      {{who: 4, where: "2#1", class: power, name: Valve}}
    routed_valve:
      {{who: 4, where: "2#1#4#01", class: power, name: Remote valve}}
    pump:
      {{who: 4, where: "0#2", class: power, name: Pump}}
    legacy_pump:
      {{who: 4, where: "#0", class: power, name: Legacy pump}}
    energy:
      {{who: 18, where: "71", phase: "0", class: power, name: F523}}
    inverted_energy:
      {{who: 18, where: "71#1", class: power, name: Other phase, inverted: true}}
    explicit_energy:
      {{who: 18, where: "73#0", class: power, name: Rewritten meter, inverted: true}}
    legacy_energy:
      {{who: 18, where: "74", phase: "1", class: power, name: Rewritten back, inverted: true}}
    energy_with_disabled_legacy:
      {{who: 18, where: "76#0", class: power, name: Enabled endpoint}}
    energy_disabled:
      {{who: 18, where: "77#0", class: power, name: Disabled endpoint}}
  light:
    ceiling:
      {{where: "1011", name: Ceiling light}}
  sensor:
    meter: {{who: 18, where: "71", class: power, name: F523 meter}}
"""


@pytest.mark.parametrize(
    ("who", "where", "extras"),
    [
        (1, "12#4#16", {}),
        (1, "12#4#01", {"interface": "02"}),
        (1, "#5", {}),
        (1, "00", {}),
        (1, "12", {"phase": "0"}),
        (4, "2#1#", {}),
        (18, "71#1", {"phase": "0"}),
        (18, "51", {"phase": "0"}),
        (18, "71", {"phase": "0\n"}),
        (25, "31#0", {}),
    ],
)
def test_invalid_actuator_addresses(who, where, extras):
    with pytest.raises(Invalid):
        binary_sensor_schema(
            {"monitor": {"who": who, "where": where, "name": "Monitor", "class": "power", **extras}}
        )


def test_invalid_restored_address_stays_unmapped():
    assert _target("4", Address("2#1#"), {}) is None
    assert _target("18", Address("71"), {"phase": "typo"}) is None


def test_lighting_report_without_address_is_ignored():
    assert not _route_targets(MagicMock(spec=OWNLightingEvent, where=None))


@pytest.mark.parametrize("classes", [("motion", "power"), ("power", "motion")])
def test_shared_who1_address_keeps_both_yaml_entries(classes):
    devices = binary_sensor_schema(
        {kind: {"who": 1, "where": "13", "name": kind, "class": kind} for kind in classes}
    )
    assert devices["1-13"]["class"] == "motion"
    assert devices["1-13-power"]["class"] == "power"


@pytest.mark.parametrize("duplicate_first", [False, True])
async def test_actuator_monitor_upgrade_and_reload(hass, tmp_path, duplicate_first):
    """Restore old registry IDs, route status reports, and reload the YAML snapshot."""
    path = tmp_path / "myhome.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.0.2.10",
            "port": 20000,
            "password": "12345",
            "mac": MAC,
            "name": "F461",
            "firmware": "2.0.0",
        },
        options={CONF_FILE_PATH: str(path)},
        unique_id=MAC,
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{MAC}-18-71")}, name="F523"
    )
    duplicate_uid = f"{MAC}-18-71#0-power"
    if duplicate_first:
        registry.async_get_or_create("binary_sensor", DOMAIN, duplicate_uid, config_entry=entry)
    old_sensor = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{MAC}-18-71-power",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="shed_load_status",
    )
    registry.async_update_entity(old_sensor.entity_id, name="Original Load Status")
    if not duplicate_first:
        registry.async_get_or_create("binary_sensor", DOMAIN, duplicate_uid, config_entry=entry)
    disabled_legacy, enabled_explicit = "18-76-power", "18-76#0-power"
    second_pair = (
        (enabled_explicit, disabled_legacy)
        if duplicate_first
        else (disabled_legacy, enabled_explicit)
    )
    for tail in (*second_pair, "18-77-power"):
        registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{MAC}-{tail}",
            config_entry=entry,
            disabled_by=None if tail == enabled_explicit else er.RegistryEntryDisabler.USER,
        )
    routed_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{MAC}-1-12#4#1")},
        name="Legacy routed relay",
    )
    area = ar.async_get(hass).async_get_or_create("Hall")
    dr.async_get(hass).async_update_device(routed_device.id, area_id=area.id)
    old_routed_relay = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{MAC}-1-12#4#1-power",
        config_entry=entry,
        device_id=routed_device.id,
    )
    registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{MAC}-9-5-opening",
        config_entry=entry,
        original_device_class="opening",
    )
    old_relay = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{MAC}-1-0004-power",
        config_entry=entry,
        suggested_object_id="area_zero_relay",
    )
    unmapped_load = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{MAC}-18-72-power",
        config_entry=entry,
        suggested_object_id="unmapped_load",
    )
    for unique_id in ("1-13-motion", "4-3-power", "18-73-power", "18-74#1-power", "18-75#1-power"):
        registry.async_get_or_create(
            "binary_sensor", DOMAIN, f"{MAC}-{unique_id}", config_entry=entry
        )
    # The monitor should coexist with a registry-restored light without an
    # explicit entry under light: in the YAML file.
    old_light = registry.async_get_or_create(
        "light",
        DOMAIN,
        f"{MAC}-1-12",
        config_entry=entry,
        suggested_object_id="original_light",
    )

    def entity_id(domain, tail):
        return registry.async_get_entity_id(domain, DOMAIN, f"{MAC}-{tail}")

    def state(domain, tail):
        return hass.states.get(entity_id(domain, tail))

    async def deliver(frame):
        message = OWNMessage.parse(frame)
        assert message is not None
        async_dispatcher_send(hass, f"myhome_message_{MAC}", message)
        await hass.async_block_till_done()

    with (
        patch(
            "custom_components.myhome.gateway.OWNSession.test_connection",
            return_value={"Success": True},
        ),
        patch("custom_components.myhome.gateway.MyHOMEGatewayHandler.listening_loop"),
        patch("custom_components.myhome.gateway.MyHOMEGatewayHandler.sending_loop"),
        patch(
            "custom_components.myhome.gateway.MyHOMEGatewayHandler.is_who_available",
            return_value=True,
        ),
        patch("custom_components.myhome.gateway.MyHOMEGatewayHandler.send_status_request") as read,
        patch("custom_components.myhome.gateway.MyHOMEGatewayHandler.send_paced") as paced,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.gateway.wait_for_zone_requests()
        await hass.async_block_till_done()

        monitors = {
            reg.unique_id
            for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
            if reg.domain == "binary_sensor"
        }
        assert monitors == {f"{MAC}-1-13-motion", f"{MAC}-9-5-opening"} | {
            f"{MAC}-{tail}-power"
            for tail in (
                "1-12",
                "1-12#4#1",
                "1-13",
                "1-04",
                "1-1010",
                "1-1011",
                "25-31",
                "4-2",
                "4-2#1",
                "4-2#1#4#01",
                "4-3",
                "4-5",
                "4-0#2",
                "4-#0",
                "18-71",
                "18-71#0",
                "18-71#1",
                "18-72",
                "18-73",
                "18-74#1",
                "18-75#1",
                "18-76",
                "18-76#0",
                "18-77",
            )
        }
        assert entity_id("light", "1-12") == old_light.entity_id
        assert entity_id("binary_sensor", "18-71-power") == old_sensor.entity_id
        assert entity_id("binary_sensor", "1-04-power") == old_relay.entity_id
        restored = registry.async_get(old_sensor.entity_id)
        assert restored.name == "Original Load Status" and restored.device_id == device.id
        assert registry.async_get(old_routed_relay.entity_id).device_id == routed_device.id
        assert dr.async_get(hass).async_get(routed_device.id).area_id == area.id
        assert {
            reg.unique_id
            for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
            if reg.domain == "switch" and reg.unique_id.endswith("-invert")
        } == {f"{MAC}-25-31-invert", f"{MAC}-9-5-invert"}

        # Trigger an explicit read through the ordinary HA service.
        read.reset_mock()
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": old_sensor.entity_id}, blocking=True
        )
        assert "*#18*71#0*71##" in {str(call.args[0]) for call in read.await_args_list}
        read.reset_mock()
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": unmapped_load.entity_id}, blocking=True
        )
        read.assert_not_awaited()
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": entity_id("binary_sensor", "4-0#2-power")},
            blocking=True,
        )
        await entry.runtime_data.gateway.wait_for_zone_requests()
        zone_requests = {frame for call in paced.await_args_list for frame in call.args[0]}
        assert "*#4*0#2*20##" in zone_requests
        assert "*#4*2#1#4#01*20##" not in zone_requests

        await deliver("*1*1*12##")
        assert state("light", "1-12").state == "on"
        assert state("binary_sensor", "1-12-power").state == "on"
        assert state("binary_sensor", "1-12-power").attributes["icon"] == "mdi:power"
        await deliver("*1*0*12#4#01##")
        assert hass.states.get(old_routed_relay.entity_id).state == "off"
        assert hass.states.get(old_routed_relay.entity_id).attributes["Int"] == "01"
        await deliver("*1*34*12##")
        await deliver("*#1*12*6*10##")
        assert state("binary_sensor", "1-12-power").state == "on"
        await deliver("*1*34*13##")
        await deliver("*1*1*13##")
        assert state("binary_sensor", "1-13-motion").state == "on"
        assert state("binary_sensor", "1-13-power").state == "off"

        # Discovery adds a light and its buttons to the existing monitor's
        # device. The configured actuator name must survive that addition.
        monitor = registry.async_get(entity_id("binary_sensor", "1-1010-power"))
        await deliver("*1*1*1010##")
        light = registry.async_get(entity_id("light", "1-1010"))
        assert light.device_id == monitor.device_id
        assert dr.async_get(hass).async_get(light.device_id).name == "Humidity alarm"
        assert state("light", "1-1010").name == "Humidity alarm"
        ceiling = registry.async_get(entity_id("light", "1-1011"))
        assert dr.async_get(hass).async_get(ceiling.device_id).name == "Ceiling light"

        await deliver("*1*1*04##")
        assert hass.states.get(old_relay.entity_id).state == "on"

        await deliver("*#4*2#1*20*1##")
        assert state("binary_sensor", "4-2#1-power").state == "on"
        for central_report in ("#0#2", "#2#1"):
            await deliver("*#4*2#2*20*0##")
            assert state("binary_sensor", "4-2-power").state == "off"
            await deliver(f"*#4*{central_report}*20*1##")
            assert state("binary_sensor", "4-2-power").state == "on"
        assert state("binary_sensor", "4-2-power").state == "on"
        await deliver("*#4*2#2*20*0##")
        assert state("binary_sensor", "4-2-power").state == "off"
        assert state("binary_sensor", "4-2#1-power").state == "on"
        await deliver("*#4*2#1#4#1*20*0##")
        assert state("binary_sensor", "4-2#1#4#01-power").state == "off"
        assert state("binary_sensor", "4-2#1-power").state == "on"
        await deliver("*#4*0#2*20*1##")
        assert state("binary_sensor", "4-0#2-power").state == "on"
        assert state("binary_sensor", "4-#0-power").state == "on"
        await deliver("*#4*#0#2*20*0##")
        assert state("binary_sensor", "4-0#2-power").state == "on"
        assert state("binary_sensor", "4-2-power").state == "off"
        await deliver("*#4*2#19*20*1##")
        await deliver("*#4*2#1#3*20*1##")
        await deliver("*#4*2*19*1##")
        assert state("binary_sensor", "4-2-power").state == "off"
        await deliver("*#4*#2*20*1##")
        await deliver("*#4*#0*20*0##")
        await deliver("*#4*0*20*1##")
        assert state("binary_sensor", "4-2-power").state == "on"
        assert state("binary_sensor", "4-#0-power").state == "off"
        await deliver("*#4*2*20*0##")
        assert state("binary_sensor", "4-2#1-power").state == "off"
        await deliver("*#4*3#1*20*1##")
        assert state("binary_sensor", "4-3-power").state == "on"
        assert state("binary_sensor", "4-2-power").state == "off"
        await deliver("*9*1*5##")
        aux = state("binary_sensor", "9-5-opening")
        assert (aux.state, aux.attributes["device_class"]) == ("on", "opening")

        await deliver("*#18*71#0*71*1*0*0*0*1*1##")
        assert state("binary_sensor", "18-71-power").state == "on"
        duplicate_state = state("binary_sensor", "18-71#0-power")
        assert duplicate_state is None or duplicate_state.state == "unavailable"
        assert state("binary_sensor", "18-71#1-power").state == "unknown"
        await deliver("*#18*71#1*71*0*0*0*0*1*1##")
        assert state("binary_sensor", "18-71#1-power").state == "on"
        await deliver("*#18*71#0*71*2*0*0*0*1*1##")
        await deliver("*#18*71#0*71##")
        await deliver("*#*1##")
        await deliver("*#*0##")
        await deliver("*#18*71#0*113*123##")
        assert state("sensor", "18-71-power").state == "123"
        assert state("binary_sensor", "18-71-power").state == "on"
        await deliver("*#18*71#0*71*0*0*0*0*1*1##")
        assert state("binary_sensor", "18-71-power").state == "off"
        await deliver("*#18*72#0*71*1*0*0*0*1*1##")
        assert hass.states.get(unmapped_load.entity_id).state == "unknown"
        for old_id, where in (("18-73-power", "73#0"), ("18-74#1-power", "74#1")):
            await deliver(f"*#18*{where}*71*1*0*0*0*1*1##")
            assert state("binary_sensor", old_id).state == "off"
        assert entity_id("binary_sensor", "18-73#0-power") is None
        assert entity_id("binary_sensor", "18-74-power") is None
        await deliver("*#18*75#0*71*1*0*0*0*1*1##")
        assert state("binary_sensor", "18-75#1-power").state == "unknown"
        await deliver("*#18*75#1*71*1*0*0*0*1*1##")
        assert state("binary_sensor", "18-75#1-power").state == "on"
        await deliver("*#18*76#0*71*1*0*0*0*1*1##")
        assert state("binary_sensor", enabled_explicit).state == "on"
        assert (
            registry.async_get(entity_id("binary_sensor", disabled_legacy)).disabled_by
            is er.RegistryEntryDisabler.USER
        )
        assert entity_id("binary_sensor", "18-77#0-power") is None

        # Simulate an earlier build that imported the YAML before saving an
        # entity-level binding. Starting with only the new phase must not
        # repoint the entity that used to observe 73#0.
        original_load_id = entity_id("binary_sensor", "18-73-power")
        registry.async_update_entity_options(original_load_id, DOMAIN, None)
        changed_config = CONFIG.replace('where: "73#0"', 'where: "73#1"')
        path.write_text(changed_config, encoding="utf-8")
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await deliver("*#18*73#1*71*0*0*0*0*1*1##")
        assert hass.states.get(original_load_id).state == "unknown"
        assert state("binary_sensor", "18-73#1-power").state == "on"

        # Bringing the original phase back alongside the new phase reuses the
        # legacy identity and keeps the two endpoints independent across reloads.
        path.write_text(
            changed_config.replace(
                "  light:",
                '    original_phase: {who: 18, where: "73#0", class: power, name: First phase}\n  light:',
            ),
            encoding="utf-8",
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await deliver("*#18*73#0*71*1*0*0*0*1*1##")
        await deliver("*#18*73#1*71*1*0*0*0*1*1##")
        assert hass.states.get(original_load_id).state == "on"
        assert state("binary_sensor", "18-73#1-power").state == "off"
        assert entity_id("binary_sensor", "18-73#0-power") is None

        assert CONF_MIGRATED_YAML_PLATFORMS in entry.options
        dr.async_get(hass).async_update_device(light.device_id, name="Light 1010")
        path.unlink()
        assert await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        assert registry.async_get(old_sensor.entity_id) is not None
        assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
        await hass.async_block_till_done()
        assert dr.async_get(hass).async_get(light.device_id).name == "Humidity alarm"
        assert entry.runtime_data.platforms["binary_sensor"]["18-71"]["phase"] == "0"
        assert entity_id("binary_sensor", "18-71-power") == old_sensor.entity_id
        assert state("binary_sensor", "18-71-power").state == "unknown"
        await deliver("*#18*71#0*71*1*0*0*0*1*1##")
        assert state("binary_sensor", "18-71-power").state == "on"
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
