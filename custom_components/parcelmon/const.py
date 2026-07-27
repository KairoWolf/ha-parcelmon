"""Constants for the Parcelmon integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "parcelmon"

CONF_FOLDER: Final = "folder"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_RETIRE_DAYS: Final = "retire_days"
CONF_MARK_SEEN: Final = "mark_seen"
CONF_PUSH: Final = "push"

DEFAULT_HOST: Final = "imap.gmail.com"
DEFAULT_PORT: Final = 993
DEFAULT_FOLDER: Final = "Parcels"
DEFAULT_POLL_INTERVAL: Final = 60  # minutes
DEFAULT_RETIRE_DAYS: Final = 3
DEFAULT_MARK_SEEN: Final = True
DEFAULT_PUSH: Final = False

MIN_POLL_INTERVAL: Final = 10
MAX_POLL_INTERVAL: Final = 1440

#: With push on, IDLE delivers new mail within seconds and the timed poll drops
#: back to a safety net in case the connection dies quietly.
PUSH_FALLBACK_INTERVAL: Final = 60  # minutes

#: Gmail drops an IDLE that runs past ~30 minutes, so renew well inside that.
IDLE_RENEW_SECONDS: Final = 9 * 60
IDLE_RECONNECT_SECONDS: Final = 30

SERVICE_RESCAN: Final = "rescan"
SERVICE_ADD_PARCEL: Final = "add_parcel"
SERVICE_REMOVE_PARCEL: Final = "remove_parcel"
SERVICE_SET_STATUS: Final = "set_status"
SERVICE_CLEAR_DELIVERED: Final = "clear_delivered"
SERVICE_REFRESH: Final = "refresh"
SERVICE_GET_PARCELS: Final = "get_parcels"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_DAYS: Final = "days"
ATTR_LIMIT: Final = "limit"
ATTR_TRACKING: Final = "tracking_number"
ATTR_CARRIER: Final = "carrier"
ATTR_SENDER: Final = "sender"
ATTR_ETA: Final = "eta"
ATTR_STATUS: Final = "status"

#: Fired on the bus when a parcel is new or changes status. Device triggers in
#: device_trigger.py are built on top of it; the payload is the public contract.
EVENT_PARCEL_UPDATE: Final = "parcelmon_parcel_update"

#: Normal polling only ever sees UNSEEN mail, so parcels that were already read
#: are invisible. A rescan reads the folder read-only to backfill them.
DEFAULT_RESCAN_DAYS: Final = 30
DEFAULT_RESCAN_LIMIT: Final = 200
MAX_RESCAN_DAYS: Final = 3650
MAX_RESCAN_LIMIT: Final = 2000

STATUS_ICONS: Final[dict[str, str]] = {
    "in_transit": "mdi:truck-fast",
    "out_for_delivery": "mdi:truck-delivery",
    "delivered": "mdi:package-variant-closed-check",
    "attempted": "mdi:package-variant-remove",
    "awaiting_collection": "mdi:store-marker",
    "returned": "mdi:keyboard-return",
    "unknown": "mdi:package-variant",
}

CARRIER_LABELS: Final[dict[str, str]] = {
    "auspost": "Australia Post",
    "tge": "Team Global Express",
}

#: Only for parcels added by hand. Mail-derived parcels use the link in the
#: email itself, which is authoritative. Team Global Express is absent on
#: purpose: their emails carry a per-shipment link with no public URL pattern
#: to reconstruct, so guessing one would produce a dead link.
TRACKING_URLS: Final[dict[str, str]] = {
    "auspost": "https://auspost.com.au/mypost/track/details/{}",
}
