"""Device triggers, so a parcel notification is a few clicks rather than a template.

Each parcel device offers one trigger per status plus a catch-all. They are thin
wrappers over the parcelmon_parcel_update event: the device's identifier carries
the parcel uid, which is exactly what the event payload is keyed on.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_PARCEL_UPDATE
from .models import STATUSES

#: A trigger per status, plus one that fires on any change at all.
ANY_CHANGE = "status_changed"
TRIGGER_TYPES = {ANY_CHANGE, *STATUSES}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


def _uid_for_device(hass: HomeAssistant, device_id: str) -> str | None:
    """The parcel uid behind a device, or None if it isn't one of ours."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            return identifier
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Offer the full status vocabulary for any Parcelmon parcel device."""
    if _uid_for_device(hass, device_id) is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in sorted(TRIGGER_TYPES)
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach by filtering the parcel event down to this device's uid."""
    uid = _uid_for_device(hass, config[CONF_DEVICE_ID])
    if uid is None:
        raise InvalidDeviceAutomationConfig(
            f"Device {config[CONF_DEVICE_ID]} is not a Parcelmon parcel"
        )

    event_data: dict[str, Any] = {"uid": uid}
    if config[CONF_TYPE] != ANY_CHANGE:
        event_data["status"] = config[CONF_TYPE]

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_PARCEL_UPDATE,
            event_trigger.CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
