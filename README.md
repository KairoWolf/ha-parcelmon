# Parcelmon

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Validate](https://github.com/KairoWolf/ha-parcelmon/actions/workflows/validate.yml/badge.svg)](https://github.com/KairoWolf/ha-parcelmon/actions/workflows/validate.yml)
[![Tests](https://github.com/KairoWolf/ha-parcelmon/actions/workflows/tests.yml/badge.svg)](https://github.com/KairoWolf/ha-parcelmon/actions/workflows/tests.yml)

Australian parcel tracking for Home Assistant, built from the emails the carriers
already send you. No API keys, no scraping, no third-party tracking account.

Supports **Australia Post** and **Team Global Express** (ex-Toll), including the
delivery photo where the carrier provides one.

---

## What it creates

Each parcel becomes its own device, added and removed automatically.

| Entity | Example state |
| --- | --- |
| `sensor.parcel_auspost_36ypj5053229_status` | `in_transit` |
| `sensor.parcel_tge_go2s501988_status` | `delivered` |
| `image.parcel_tge_go2s501988_delivery_photo` | the driver's photo |
| `sensor.parcelmon_parcels_in_transit` | `3` |

Status sensor attributes: `carrier`, `tracking_number`, `status`, `status_text`,
`sender`, `eta`, `destination`, `delivered_on`, `tracking_url`, `has_photo`,
`photo_url`, `subject`, `email_date`, `last_seen`.

Statuses are normalised across carriers: `in_transit`, `out_for_delivery`,
`delivered`, `attempted`, `awaiting_collection`, `returned`, `unknown`.

Delivered parcels are removed after a configurable number of days (default 3),
so your entity list doesn't fill up with history.

---

## Install

### 1. Gmail filter

Create one filter so the integration has a single label to read.

Gmail → Settings → **Filters and Blocked Addresses** → *Create a new filter* →
put this in **Has the words**:

```
from:(auspost.com.au OR teamglobalexp.com)
```

Then: **Skip the Inbox**, **Apply the label** → `Parcels`, and tick
**Also apply filter to matching conversations** to backfill.

Domains rather than exact addresses on purpose — Australia Post send from
several (`notifications.`, `mypost.`) and pinning one address misses the rest.

### 2. App Password

<https://myaccount.google.com/apppasswords> → name it `Home Assistant` → Create.

16 characters, shown once. Requires 2-Step Verification on the account. Your
normal Gmail password will not work; Google blocks it for IMAP.

### 3. HACS

HACS → ⋮ → **Custom repositories** → add
`https://github.com/KairoWolf/ha-parcelmon`, category **Integration**.

Then find **Parcelmon** in HACS, download, and restart Home Assistant.

### 4. Configure

Settings → Devices & Services → **Add Integration** → **Parcelmon**.

Enter your address, the App Password, and the label name (`Parcels`). Setup
fails immediately with a clear error if the credentials or the label are wrong,
rather than silently doing nothing.

<details>
<summary>Manual install without HACS</summary>

Copy `custom_components/parcelmon/` into your HA `config/custom_components/`
directory and restart.
</details>

---

## Privacy and scope

The integration only ever `SELECT`s the one folder you configure. It never opens
INBOX and never runs a server-side search across the account, so the Gmail label
is an actual boundary rather than a promise. Nothing is sent anywhere: parsing
happens locally and photos are held in memory, never written to disk.

Diagnostics output redacts tracking numbers, addresses, subjects, and photo URLs,
so it is safe to paste into an issue.

---

## Carrier notes

Worth knowing before you wonder why a field is empty.

**Australia Post** — single-part HTML from Salesforce Marketing Cloud. Deep
nested tables, but the label text is stable, so parsing works off rendered text
rather than CSS selectors.

> **No delivery photo.** Their footer says a photo "may be available in the app".
> It is behind MyPost authentication and never embedded in the email. AusPost
> parcels therefore never get an image entity. This is not a missing feature —
> the bytes are not in the message.

> ⚠️ Every AusPost email, *including in-transit ones*, carries the boilerplate
> "Safe Drop is only available for locations not in public view". Naive
> whole-body keyword matching reports those parcels as **delivered**. Parcelmon
> classifies on the `<h1>` and subject first and only falls back to the body,
> with a named regression test pinning the behaviour.

**Team Global Express** — multipart/mixed via Amazon SES with a
quoted-printable HTML part. Much better structured: the "View Details" button
href carries `?shipmentID=GO2S501988&status=delivered`, so status comes from a
query parameter rather than headline guessing.

> The proof-of-delivery photo arrives as a base64 data URI in the image
> `srcset`, with a `p.mytge.co` shortlink in `src`. Parcelmon keeps the decoded
> bytes, because TGE state the photo is only available for 7 days — the link
> expires, the bytes don't.

Their mail also arrives with attributes rewritten by a defanging gateway
(`defang_contenteditable=`), which the parser normalises before parsing.

---

## Automations

Notify on any parcel being delivered, with the photo attached when there is one:

```yaml
alias: Parcel delivered
triggers:
  - trigger: state
    to: delivered
conditions:
  - condition: template
    value_template: >
      {{ trigger.entity_id.startswith('sensor.parcel_')
         and trigger.entity_id.endswith('_status') }}
actions:
  - variables:
      photo: "{{ trigger.entity_id | replace('sensor.', 'image.') | replace('_status', '_delivery_photo') }}"
  - action: notify.mobile_app_your_phone
    data:
      title: "📦 {{ state_attr(trigger.entity_id, 'sender') or 'Parcel' }} delivered"
      message: >
        {{ state_attr(trigger.entity_id, 'tracking_number') }}
        {%- if state_attr(trigger.entity_id, 'delivered_on') %}
        · {{ state_attr(trigger.entity_id, 'delivered_on') }}
        {%- endif %}
      data:
        image: "{{ '/api/image_proxy/' ~ photo if states(photo) not in ['unknown','unavailable'] else '' }}"
```

Announce when something is out for delivery:

```yaml
alias: Parcel arriving today
triggers:
  - trigger: state
    to: out_for_delivery
conditions:
  - "{{ trigger.entity_id.startswith('sensor.parcel_') }}"
actions:
  - action: tts.speak
    target:
      entity_id: tts.piper
    data:
      media_player_entity_id: media_player.kitchen
      message: >
        A parcel from {{ state_attr(trigger.entity_id, 'sender') or 'a sender' }}
        is out for delivery today.
```

Dashboard card listing everything inbound:

```yaml
type: markdown
title: Parcels
content: >
  {% for p in state_attr('sensor.parcelmon_parcels_in_transit', 'parcels') %}
  - **{{ p.sender or p.carrier }}** — {{ p.status | replace('_', ' ') }}
    {%- if p.eta %} (ETA {{ p.eta }}){% endif %}
  {% else %}
  Nothing on its way.
  {% endfor %}
```

---

## Troubleshooting

**"The mail server rejected these credentials"** — you are using your account
password rather than a 16-character App Password, or IMAP is disabled in Gmail
(Settings → Forwarding and POP/IMAP → Enable IMAP).

**"That label was not found"** — the error lists every available folder. Nested
Gmail labels appear as `Parent/Child`.

**A parcel didn't appear** — turn on debug logging and check for a
`No parcel found in ...` warning, which means the carrier changed their template.
That email is deliberately left unread so it isn't lost.

```yaml
logger:
  logs:
    custom_components.parcelmon: debug
```

**Reporting a template change** — save the email (Gmail → ⋮ → **Show original** →
Download), redact your tracking number and address, and attach it to an issue.
Every parser is driven by fixtures, so a real sample is the whole fix.

---

## Development

```bash
pip install -r requirements-test.txt
python -m pytest tests/ -v      # 38 tests
ruff check custom_components
```

Adding a carrier:

1. New module in `custom_components/parcelmon/parsers/` subclassing
   `CarrierParser`, setting `carrier` and `from_domains`, returning a `Parcel`.
2. Register it in `parsers/__init__.py`.
3. Add a fixture `.eml` and tests.
4. Widen the Gmail filter to include the new domain.

The model, coordinator, and entity platforms need no changes.

## Licence

MIT.

Not affiliated with, endorsed by, or supported by Australia Post or Team Global
Express.
