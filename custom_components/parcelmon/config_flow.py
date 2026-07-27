"""Config flow for Parcelmon."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_FOLDER,
    CONF_MARK_SEEN,
    CONF_POLL_INTERVAL,
    CONF_RETIRE_DAYS,
    DEFAULT_FOLDER,
    DEFAULT_HOST,
    DEFAULT_MARK_SEEN,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_RETIRE_DAYS,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .imap_client import (
    ImapSettings,
    ParcelmonAuthError,
    ParcelmonConnectionError,
    ParcelmonFolderError,
    verify,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
        vol.Required(CONF_FOLDER, default=DEFAULT_FOLDER): str,
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
    }
)


def _settings_from(user_input: Mapping[str, Any]) -> ImapSettings:
    return ImapSettings(
        host=user_input[CONF_HOST],
        port=int(user_input[CONF_PORT]),
        username=user_input[CONF_USERNAME],
        # Gmail shows App Passwords in groups of four; people paste the spaces.
        password=str(user_input[CONF_PASSWORD]).replace(" ", ""),
        folder=user_input[CONF_FOLDER],
    )


class ParcelmonConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            settings = _settings_from(user_input)
            await self.async_set_unique_id(f"{settings.username}:{settings.folder}".lower())
            self._abort_if_unique_id_configured()

            try:
                unread = await self.hass.async_add_executor_job(verify, settings)
            except ParcelmonAuthError:
                errors["base"] = "invalid_auth"
            except ParcelmonFolderError as err:
                errors[CONF_FOLDER] = "unknown_folder"
                description_placeholders["folders"] = ", ".join(err.available[:25]) or "(none)"
            except (ParcelmonConnectionError, OSError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Parcelmon setup")
                errors["base"] = "unknown"
            else:
                _LOGGER.debug("Validated mailbox, %d unread in folder", unread)
                data = dict(user_input)
                data[CONF_PASSWORD] = settings.password
                return self.async_create_entry(
                    title=f"{settings.username} ({settings.folder})", data=data
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """App Passwords are revoked when the account password changes."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None

        if user_input is not None:
            merged = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            settings = _settings_from(merged)
            try:
                await self.hass.async_add_executor_job(verify, settings)
            except ParcelmonAuthError:
                errors["base"] = "invalid_auth"
            except (ParcelmonConnectionError, ParcelmonFolderError, OSError):
                errors["base"] = "cannot_connect"
            else:
                merged[CONF_PASSWORD] = settings.password
                return self.async_update_reload_and_abort(entry, data=merged)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ParcelmonOptionsFlow()


class ParcelmonOptionsFlow(OptionsFlow):
    """Polling and housekeeping options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=1,
                        unit_of_measurement="minutes",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_RETIRE_DAYS,
                    default=options.get(CONF_RETIRE_DAYS, DEFAULT_RETIRE_DAYS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=90, step=1,
                        unit_of_measurement="days",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MARK_SEEN,
                    default=options.get(CONF_MARK_SEEN, DEFAULT_MARK_SEEN),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
