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
| `sensor.parcelmon_delivered_today` | `2` |
| `sensor.parcelmon_last_checked` | timestamp, diagnostic |
| `binary_sensor.parcelmon_mailbox` | `on` while the mailbox is readable |
| `button.parcelmon_check_for_mail_now` | reads the mailbox immediately |
| `button.parcelmon_rescan_mailbox` | scans mail that was already read |

`binary_sensor.parcelmon_mailbox` is worth putting on a dashboard. A revoked App
Password or a renamed label leaves Parcelmon quietly reporting the parcels it
already knew about, which looks exactly like "nothing has arrived" — this is the
entity that tells them apart.

Status sensor attributes: `carrier`, `tracking_number`, `status`, `status_text`,
`sender`, `eta`, `destination`, `delivered_on`, `tracking_url`, `has_photo`,
`photo_url`, `subject`, `email_date`, `last_seen`.

Statuses are normalised across carriers: `in_transit`, `out_for_delivery`,
`delivered`, `attempted`, `awaiting_collection`, `returned`, `unknown`.

Delivered parcels are removed after a configurable number of days (default 3),
so your entity list doesn't fill up with history.

Tracked parcels are saved to disk and restored on restart. This matters because
mail is marked read once parsed: a parcel held only in memory would vanish at
shutdown and could never be recovered, since the message no longer shows up in
an unread search. Parcels that finished while Home Assistant was down are
retired on the way back in rather than reappearing. Removing the mailbox deletes
the stored file.

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

## How often it checks

| Setting | Default | Range |
| --- | --- | --- |
| Check for new mail every | 60 minutes | 10 – 1440 |
| Update as soon as email arrives | off | — |

Turn on **Update as soon as email arrives** to hold an IMAP IDLE connection open,
so carrier mail is picked up within seconds instead of on the timer. The timed
check drops back to an hourly safety net for the case where the connection dies
without saying so.

If your server doesn't advertise IDLE, Parcelmon logs it once and keeps using
the timer — push is an optimisation, never a requirement.

---

## Notifications

Every parcel device offers device triggers, so a notification is a few clicks in
the automation editor rather than a template. Pick the parcel device, then a
trigger like **Parcel was delivered** or **Parcel status changed**.

Underneath, they filter the `parcelmon_parcel_update` event, which you can also
use directly:

```yaml
triggers:
  - trigger: event
    event_type: parcelmon_parcel_update
    event_data:
      status: delivered
actions:
  - action: notify.mobile_app
    data:
      message: "{{ trigger.event.data.sender }} parcel delivered"
```

Payload: `uid`, `carrier`, `tracking_number`, `status`, `previous_status`,
`status_text`, `sender`, `eta`, `tracking_url`, `has_photo`.

It fires when a parcel is new or its status actually changes — not on every
poll, and never during a rescan, so importing history won't set off a burst of
notifications for parcels that arrived weeks ago.

---

## Actions

| Action | What it does |
| --- | --- |
| `parcelmon.refresh` | Check for new mail now |
| `parcelmon.rescan` | Sweep the folder, including mail already read |
| `parcelmon.add_parcel` | Track a parcel by hand |
| `parcelmon.remove_parcel` | Stop tracking a parcel |
| `parcelmon.set_status` | Correct a parcel's status |
| `parcelmon.clear_delivered` | Drop finished parcels now |
| `parcelmon.get_parcels` | Return every parcel, changes nothing |

All of them take an optional `config_entry_id` if you have more than one
mailbox, and all return a response.

`set_status` is the escape hatch for a parcel the classifier read wrongly, or
one that turned up on the doorstep without a delivery email:

```yaml
action: parcelmon.set_status
data:
  tracking_number: 36YPJ5053229
  status: delivered
```

It fires the same status-change event a real update does, so your automations
still run.

`get_parcels` is the one to build dashboards on:

```yaml
action: parcelmon.get_parcels
response_variable: result
```

Each entry carries the full attribute set plus `uid` and `manual`.

---

## Adding a parcel by hand

For a parcel whose email never arrives, or arrives in a shape the parser can't
read:

```yaml
action: parcelmon.add_parcel
data:
  tracking_number: 36YPJ5053229
  carrier: auspost
  status: in_transit
  sender: UCL Co. Ltd
```

If the carrier's email turns up later it takes over the same parcel rather than
creating a duplicate — the uid is derived from carrier plus tracking number, and
real mail always supersedes a hand-typed placeholder.

```yaml
action: parcelmon.remove_parcel
data:
  tracking_number: 36YPJ5053229    # or the uid, auspost_36ypj5053229
```

---

## Rescanning old email

Routine checking only ever looks at **unread** mail, and marks it read once
parsed. So on a fresh install, a label already full of read carrier email looks
empty — nothing appears until your next parcel is posted.

Press **Rescan mailbox** on the Parcelmon device to backfill from mail that has
already been read. The folder is opened read-only, so nothing is marked read and
you can press it as often as you like.

For a wider window, call the action directly:

```yaml
action: parcelmon.rescan
data:
  days: 90     # 0 scans the whole folder
  limit: 500   # newest N messages
```

It returns what it found, so you can use it in a script:

```yaml
action: parcelmon.rescan
response_variable: scan
```

```json
{ "scanned": 412, "matched": 37, "new_parcels": 6, "tracked": 9 }
```

Two things worth knowing:

- Parcels are dated by their **email**, not by the scan, so `retire_days` still
  measures from when the parcel arrived. Scanning a year of history won't flood
  you with long-delivered parcels — they're retired on the way in.
- A rescan never overwrites something newer that polling already knows.

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
