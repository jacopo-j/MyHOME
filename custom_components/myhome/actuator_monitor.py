"""Read-only binary sensors for explicitly configured OpenWebNet actuators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (  # type: ignore[attr-defined, unused-ignore]
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from OWNd.message import (
    OWNEnergyCommand,
    OWNEnergyEvent,
    OWNHeatingEvent,
    OWNLightingCommand,
    OWNLightingEvent,
)
from voluptuous import Invalid

from .const import (
    BUS_ROUTING,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_MODEL,
    CONF_ENTITY_NAME,
    CONF_ICON,
    CONF_ICON_ON,
    CONF_INVERTED,
    CONF_MANUFACTURER,
    CONF_PHASE,
    CONF_WHERE,
    CONF_WHO,
    DOMAIN,
    LOGGER,
)
from .data import MyHOMEConfigEntry
from .discovery import Address, DeviceContext, PlatformDiscovery, config_for, parse_unique_id
from .myhome_device import MyHOMEEntity
from .typing_compat import device_info_to_dict
from .validate import is_who1_power_monitor, validate_actuator_monitor_where
from .where_grammar import (
    canonical_point_spelling,
    is_pump,
    thermo_dim20_is_active,
    thermo_event_zone,
)

# The frame router is shared with lights, climate and power meters. Its public
# address keys can lose the actuator or phase, so monitors subscribe separately.
_ROUTE_SUFFIX = "-actuator-monitor"
_EVENTS = {"1": OWNLightingEvent, "4": OWNHeatingEvent, "18": OWNEnergyEvent}
_PLATFORM = "binary_sensor"
_DIGITS = tuple("0123456789")
_F523_ADDRESS = "f523_address"


def actuator_monitor_registry_address(
    unique_id: str, gateway_mac: str, entry_mac: str
) -> tuple[str, str] | None:
    """Recognize the fork's explicit WHO and power class in the unique ID."""
    who, device_id = parse_unique_id(unique_id, gateway_mac, entry_mac)
    where = device_id.removesuffix("-power")
    if who is not None and who in _EVENTS and where and where != device_id:
        return who, where
    return None


def _is_power_monitor(cfg: Mapping[str, Any]) -> bool:
    return (cfg.get(CONF_DEVICE_CLASS) or cfg.get("device_class")) == BinarySensorDeviceClass.POWER


def configured_relay_monitor(
    configured: Mapping[str, dict[str, Any]], address: Address
) -> dict[str, Any]:
    """Look up the named WHO 1 observer without borrowing from another bus."""
    interface = address.interface.zfill(2) if address.interface else None
    address_key = Address(canonical_point_spelling(address.where), interface).key
    key = f"1-{address_key}"
    for candidate in (f"{key}-power", key):
        cfg = configured.get(candidate, {})
        if is_who1_power_monitor(cfg):
            return cfg
    return {}


def _target(who: str, address: Address, cfg: Mapping[str, Any]) -> str | None:
    """Canonical wire address, independent of the legacy registry identity."""
    try:
        where, interface = validate_actuator_monitor_where(who, address.where, address.interface)
    except Invalid:
        return None
    if who == "18" and "#" not in where:
        if not cfg:
            # A pre-existing entity ID omitted the phase, so it cannot be used
            # to infer which endpoint a restored automation should observe.
            return None
        suffix = str(cfg.get(CONF_WHERE, where)).partition("#")[2]
        phase = suffix or cfg.get(CONF_PHASE, "0")
        if phase not in _DIGITS:
            return None
        where = f"{where}#{phase}"
    if who == "4":
        where = _thermo_spelling(where)
    return Address(where, interface).key


def _legacy_f523_address(
    reg_entry: er.RegistryEntry, where: str, configured: Mapping[str, Any]
) -> str | None:
    options: Mapping[str, Any] = reg_entry.options.get(DOMAIN) or {}
    stored = options.get(_F523_ADDRESS)
    if isinstance(stored, str):
        base, _, phase = stored.partition("#")
        if base == where and phase in _DIGITS:
            return stored
    bare = configured.get(f"18-{where}", {})
    if _is_power_monitor(bare):
        return _target("18", Address(where), bare)
    default = f"{where}#0"
    # An old ID does not encode the phase. Only the documented default
    # can be inferred from an explicit spelling without prior configuration.
    return default if _is_power_monitor(configured.get(f"18-{default}", {})) else None


def _remember_f523_address(
    registry: er.EntityRegistry, reg_entry: er.RegistryEntry, target: str
) -> None:
    options: Mapping[str, Any] = reg_entry.options.get(DOMAIN) or {}
    if options.get(_F523_ADDRESS) != target:
        registry.async_update_entity_options(
            reg_entry.entity_id, DOMAIN, {**options, _F523_ADDRESS: target}
        )


def preserve_f523_addresses(
    hass: HomeAssistant, entry: MyHOMEConfigEntry, previous: Mapping[str, Any]
) -> None:
    """Save legacy F523 bindings before the old YAML snapshot is replaced."""
    configured = previous.get(_PLATFORM, {})
    if not any(key.startswith("18-") for key in configured):
        return
    registry = er.async_get(hass)
    mac = entry.data[CONF_MAC]
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        match = actuator_monitor_registry_address(reg_entry.unique_id, dr.format_mac(mac), mac)
        if reg_entry.domain == _PLATFORM and match and match[0] == "18" and "#" not in match[1]:
            if target := _legacy_f523_address(reg_entry, match[1], configured):
                _remember_f523_address(registry, reg_entry, target)


def _f523_config(
    reg_entry: er.RegistryEntry, device_id: str, configured: Mapping[str, Any]
) -> Mapping[str, Any]:
    where, sep, _phase = device_id.partition("#")
    target = device_id if sep else _legacy_f523_address(reg_entry, where, configured)
    for key in (device_id, where if sep else target):
        cfg = configured.get(f"18-{key}", {})
        if (
            target
            and isinstance(cfg, Mapping)
            and _is_power_monitor(cfg)
            and _target("18", Address(where), cfg) == target
        ):
            return cfg
    return {}


def _thermo_spelling(where: str) -> str:
    """Normalize leading zeros in the zone while preserving central addressing."""
    zone, sep, actuator = where.removeprefix("#").partition("#")
    prefix = "#" if where.startswith("#") else ""
    return f"{prefix}{int(zone)}{sep}{actuator}"


def _route_targets(message: OWNLightingEvent | OWNHeatingEvent | OWNEnergyEvent) -> list[str]:
    if isinstance(message, OWNEnergyEvent):
        return [message.target_address] if message.dimension == 71 else []
    address = Address.from_message(message)
    if address is None:
        return []
    where = address.where
    interface = address.interface.zfill(2) if address.interface else None
    if isinstance(message, OWNLightingEvent):
        if message.is_sensor or message.is_on is None:
            return []
        return [Address(canonical_point_spelling(where), interface).key]
    if message.dimension != 20 or not where.removeprefix("#").isdigit():
        return []
    # OWNd exposes the WHO 4 address head and hash parameters separately.
    # F422 routing lives at the end of the parameters.
    params = message.where_param[:-2] if address.interface else message.where_param
    head = _thermo_spelling(where)
    if params:
        if len(params) != 1 or params[0] not in _DIGITS:
            return []
        targets = [f"{head}#{params[0]}"]
        if is_pump(message):
            targets.extend(("0#0", "#0"))
        elif not head.startswith("#"):
            targets.extend((head, f"{head}#0"))
    elif head == "0":
        return []
    else:
        targets = [head]
        if not head.startswith("#"):
            targets.extend((f"{head}#1", f"{head}#0"))
    # The old fork delivered central-addressed reports to their logical zone.
    # Only the zone-wide monitor gets that alias. Explicit actuators and pumps
    # still use their own wire addresses.
    if head.startswith("#") and (zone := thermo_event_zone(message)) is not None:
        targets.append(zone)
    return [Address(target, interface).key for target in targets]


def actuator_monitor_discoveries(
    hass: HomeAssistant, entry: MyHOMEConfigEntry, async_add_entities: AddEntitiesCallback
) -> list[PlatformDiscovery]:
    """Create independent lifecycle handlers for the three supported WHOs."""
    return [_discovery(hass, entry, async_add_entities, who) for who in _EVENTS]


def _discovery(
    hass: HomeAssistant, entry: MyHOMEConfigEntry, async_add_entities: AddEntitiesCallback, who: str
) -> PlatformDiscovery:
    gateway = entry.runtime_data.gateway
    configured = entry.runtime_data.platforms.get(_PLATFORM, {})
    legacy_f523_targets: set[str] = set()
    enabled_explicit_f523_targets: set[str] = set()

    def registry_address(reg_entry: er.RegistryEntry) -> Address | None:
        match = actuator_monitor_registry_address(
            reg_entry.unique_id, gateway.mac, entry.data[CONF_MAC]
        )
        if match is None or match[0] != who:
            return None
        return Address.from_device_id(match[1], _ROUTE_SUFFIX)

    def config(ctx: DeviceContext) -> Mapping[str, Any]:
        if ctx.source == "yaml":
            return ctx.cfg
        # The unqualified YAML aliases may belong to another WHO.
        device_id = ctx.address.key.removesuffix(_ROUTE_SUFFIX)
        key = f"{who}-{device_id}"
        if who == "18" and ctx.registry_entry is not None:
            return _f523_config(ctx.registry_entry, device_id, configured)
        if who == "1":
            return configured_relay_monitor(configured, ctx.address)
        cfg = configured.get(key, {})
        return cfg if _is_power_monitor(cfg) else {}

    def known_keys(ctx: DeviceContext) -> list[str]:
        target = _target(who, ctx.address, config(ctx))
        return [ctx.key, f"{target}{_ROUTE_SUFFIX}"] if target else [ctx.key]

    def accept(ctx: DeviceContext) -> bool:
        if ctx.source == "registry":
            if who != "18":
                return True
            if ctx.address.where in legacy_f523_targets:
                return False
            if ctx.registry_entry is not None and ctx.registry_entry.disabled_by is not None:
                # PlatformDiscovery reserves the keys before Home Assistant
                # skips the disabled entity. Yield to an enabled sibling.
                return _target(who, ctx.address, config(ctx)) not in enabled_explicit_f523_targets
            return True
        if ctx.source != "yaml" or ctx.who != who or not _is_power_monitor(ctx.cfg):
            return False
        # A legacy energy ID (71) and an explicit endpoint ID (71#0) can
        # describe the same monitor. Preserve the one already restored.
        return not any(key in discovery.known for key in known_keys(ctx))

    def build(ctx: DeviceContext) -> ActuatorMonitor:
        cfg = config(ctx)
        address = ctx.address
        device_id = address.key.removesuffix(_ROUTE_SUFFIX)
        target = _target(who, address, cfg)
        if who == "1":
            # The monitor and the controllable entity share a device entry.
            # Use the light or switch's name when it has its own YAML declaration.
            point = Address.from_device_id(target or device_id)
            for platform in ("light", "switch"):
                actuator = config_for(
                    entry.runtime_data.platforms.get(platform, {}), point, f"1-{point.key}"
                )
                if str(actuator.get(CONF_WHO, "1")) == "1" and (name := actuator.get(CONF_NAME)):
                    cfg = {**cfg, CONF_NAME: name}
                    break
        entity = ActuatorMonitor(
            hass,
            entry,
            who,
            device_id,
            address.where,
            target,
            cfg,
        )
        if ctx.registry_entry is not None:
            entity._attr_unique_id = ctx.registry_entry.unique_id
            if registered_device_id := ctx.registry_entry.device_id:
                device = dr.async_get(hass).async_get(registered_device_id)
                if isinstance(device, dr.DeviceEntry):
                    info = device_info_to_dict(entity.device_info)
                    info.update(identifiers=device.identifiers, connections=device.connections)
                    # DeviceInfo is a TypedDict through Core 2026.10 and a
                    # dataclass on 2026.11. Rebuild it with the same fields.
                    entity._attr_device_info = dr.DeviceInfo(**info)  # type: ignore[typeddict-item, unused-ignore]
        if target is None:
            LOGGER.warning(
                "Cannot determine an actuator address for %s. Check myhome.yaml.", entity.unique_id
            )
        return entity

    def route_keys(message: Any, _address: Address | None) -> list[str]:
        return [f"{target}{_ROUTE_SUFFIX}" for target in _route_targets(message)]

    discovery = PlatformDiscovery(
        hass,
        entry,
        async_add_entities,
        platform=_PLATFORM,
        who=who,
        event_type=_EVENTS[who],
        build=build,
        accept=accept,
        registry_address=registry_address,
        known_keys=known_keys,
        route_keys=route_keys,
        key_suffix=_ROUTE_SUFFIX,
        general_is_device=who == "4",
    )
    if who == "18":
        _, entries = discovery.registry_entries()
        for reg_entry in entries:
            if reg_entry.domain != _PLATFORM or reg_entry.disabled_by is not None:
                continue
            address = registry_address(reg_entry)
            if address is None:
                continue
            if "#" in address.where:
                enabled_explicit_f523_targets.add(address.where)
            else:
                cfg = _f523_config(reg_entry, address.where, configured)
                if target := _target(who, address, cfg):
                    legacy_f523_targets.add(target)
    return discovery


class ActuatorMonitor(MyHOMEEntity, BinarySensorEntity):
    """Observe an actuator and provide status reads."""

    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_is_on = None
    _name_from_device_class = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MyHOMEConfigEntry,
        who: str,
        device_id: str,
        where: str,
        target: str | None,
        cfg: Mapping[str, Any],
    ) -> None:
        super().__init__(
            hass=hass,
            name=cfg.get(CONF_NAME, f"Actuator {device_id}"),
            platform=_PLATFORM,
            device_id=device_id,
            who=who,
            where=where,
            entity_name=cfg.get(CONF_ENTITY_NAME),
            manufacturer=cfg.get(CONF_MANUFACTURER),
            model=cfg.get(CONF_DEVICE_MODEL),
            gateway=entry.runtime_data.gateway,
        )
        self._attr_unique_id = f"{self._attr_unique_id}-power"
        self._target = target
        self._full_where = target or device_id
        self._poll_on_add = who != "4" and target is not None
        self._inverted = bool(cfg.get(CONF_INVERTED, False))
        self._off_icon = cfg.get(CONF_ICON)
        self._on_icon = cfg.get(CONF_ICON_ON)
        self._attr_icon = self._off_icon
        if who == "1":
            self._attr_extra_state_attributes = {
                "A": where[: len(where) // 2],
                "PL": where[len(where) // 2 :],
            }
            if target and BUS_ROUTING in target:
                self._attr_extra_state_attributes["Int"] = target.rsplit(BUS_ROUTING, 1)[1]
        elif who == "4":
            self._attr_extra_state_attributes = {"Z": where}
        elif where.split("#")[0].isdigit():
            self._attr_extra_state_attributes = {"P": str(int(where.split("#")[0]) - 70)}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._who == "18" and "#" not in self._where and self._target and self.registry_entry:
            _remember_f523_address(er.async_get(self.hass), self.registry_entry, self._target)
        if self._who == "4" and self._target is not None:
            self.async_on_remove(self._gateway_handler.watch_zone_actuators(self._target))

    async def async_update(self) -> None:
        if self._target is None:
            return
        if self._who == "4":
            self._gateway_handler.request_zone_dimension(self._target, 20)
        elif self._who == "1":
            await self._gateway_handler.send_status_request(OWNLightingCommand.status(self._target))
        else:
            await self._gateway_handler.send_status_request(
                OWNEnergyCommand(f"*#18*{self._target}*71##")
            )

    async def async_restore_last_state(self, last_state: State) -> None:
        """Wait for a new report as the original fork did."""

    @callback
    def handle_event(self, message: OWNLightingEvent | OWNHeatingEvent | OWNEnergyEvent) -> None:
        if isinstance(message, OWNLightingEvent):
            state = message.is_on
        elif isinstance(message, OWNHeatingEvent):
            state = thermo_dim20_is_active(message)
        else:
            fields = message.dimension_value
            # Dimension 71 first reports the disabled flag. Preserve the
            # legacy polarity, including abbreviated gateway reports.
            state = fields[0] == "1" if fields and fields[0] in ("0", "1") else None
        if state is None:
            return
        self._bus_state_complete = True
        self._attr_is_on = state != self._inverted
        if self._off_icon is not None and self._on_icon is not None:
            self._attr_icon = self._on_icon if self._attr_is_on else self._off_icon
        self._publish_state()
