"""Constants for the Parcelmon integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "parcelmon"

CONF_FOLDER: Final = "folder"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_RETIRE_DAYS: Final = "retire_days"
CONF_MARK_SEEN: Final = "mark_seen"

DEFAULT_HOST: Final = "imap.gmail.com"
DEFAULT_PORT: Final = 993
DEFAULT_FOLDER: Final = "Parcels"
DEFAULT_POLL_INTERVAL: Final = 5  # minutes
DEFAULT_RETIRE_DAYS: Final = 3
DEFAULT_MARK_SEEN: Final = True

MIN_POLL_INTERVAL: Final = 1
MAX_POLL_INTERVAL: Final = 120

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
