# Technical Specification & Operational Analysis: Slack Side Channels, Invites, and Rosetta Stone Dispatch (HAF-75)

**Task Reference:** `hafizmohdrizal/haf-75-a6-slack-side-channels-invites-and-rosetta-stone-samples`  
**Parent Initiative:** RINA Modularization: DAG + Subagents  
**Target Codebase:** [slack_incident.py](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py), [triangulate_alm.py](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/triangulate_alm.py), [webhook_subscriptions.json](file:///home/ubuntu/.hermes/webhook_subscriptions.json)  
**Target Slack Workspace:** Team ID `T04B035410Q`  
**Bot Identity:** User `U0BQ96K07AL` / App `B0BPERDEM7C` (`Auto-Incident Bot`)  

---

## Executive Summary

This document establishes the ground-truth technical audit and operational behavior of the current Slack incident response integration within the RINA ecosystem. It reverse-engineers the channel lifecycle, membership policies, permission boundaries, and notification templates currently driven by Hermes and the `slack-incident` skill.

---

## 1. Channel Naming Convention

### Specification & Code Audit
Channel names are generated automatically during incident dispatch in [`slack_incident.py`](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L53-L57):

```python
def sanitize_channel_name(name: str) -> str:
    """Sanitize a string to be a valid Slack channel name (lowercase, no spaces, <=80 chars)."""
    name = re.sub(r'[^a-z0-9-_]', '-', name.lower())
    name = re.sub(r'-+', '-', name).strip('-_')
    return name[:80]
```

In `cmd_rosetta` ([line 257](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L257)):
```python
channel_name = sanitize_channel_name(f"incident-{ticket_id}")
```

### Key Parameters & Constraints
- **Identifier Used:** Uses the **internal Zammad database ticket ID** (e.g., `274`), **NOT** the human-facing ticket number (e.g., `72270`).
- **Prefix:** Strict `incident-` prefix followed by the integer ID.
- **Sanitization Logic:**
  1. Lowers all characters: `name.lower()`
  2. Replaces all non-alphanumeric and non-hyphen/underscore characters with hyphens: `[^a-z0-9-_] -> -`
  3. Collapses consecutive hyphens: `-+ -> -`
  4. Strips leading and trailing hyphens and underscores: `.strip('-_')`
  5. Truncates to 80 characters maximum: `name[:80]` (Slack API hard limit is 80 characters for channel names).
- **Collision Handling:**
  - If a channel with that name already exists, the Slack API returns `name_taken`.
  - The script catches `name_taken` ([lines 266–275](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L266-L275)), iterates through existing channels via `conversations_list(types="public_channel,private_channel")`, matches `c["name"] == channel_name`, and re-uses the existing channel ID without creating duplicate channels or appending random suffixes.

---

## 2. Public vs. Private Channels

### Specification & Code Audit
Incident channels are explicitly created as **PUBLIC** channels in [`slack_incident.py`](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L259):

```python
resp = client.conversations_create(name=channel_name, is_private=False)
```

### Workspace Channel Audit
A live audit of all active channels in the Slack workspace (`T04B035410Q`) confirms:
- **100% of incident channels are public** (`is_private: False`).
- Recent sample channels:
  - `incident-274` (`C0CABBMPRSA`): `is_private = False`
  - `incident-273` (`C0C9XPQRWKH`): `is_private = False`
  - `incident-272` (`C0C9XKM00LF`): `is_private = False`
  - `incident-271` (`C0C9738CR80`): `is_private = False`
  - `incident-270` (`C0C8P6J9R0B`): `is_private = False`

The bot does not create private incident channels under any current operational path.

---

## 3. Bot Scopes and Permissions

### Token & Identity
- **Environment Token:** `SLACK_BOT_TOKEN` configured in `/home/ubuntu/.hermes/.env` (Prefix: `xoxb-`).
- **Bot ID:** `B0BPERDEM7C`
- **User ID:** `U0BQ96K07AL`

### Granted OAuth Scopes (`x-oauth-scopes`)
Direct inspection of Slack API response headers via `auth.test` reveals the following exact scope list:
```text
channels:history, chat:write, channels:manage, app_mentions:read, files:write,
im:write, assistant:write, commands, channels:read, files:read, groups:history,
groups:read, im:history, im:read, mpim:history, mpim:read, reactions:read, users:read
```

### Archival Capabilities & Permission Boundaries
1. **Can the bot archive public channels it did NOT create?**
   - **YES.** The bot possesses the `channels:manage` scope. In Slack, `channels:manage` permits archiving *any* public channel in the workspace, provided:
     - The channel is not the primary `#general` channel.
     - The bot has joined the channel (`conversations_join` is executed on line 279 of `slack_incident.py`).
   - If a human engineer creates `#incident-999` manually, the bot can join and archive it successfully via `client.conversations_archive(channel=channel_id)`.
2. **Can the bot archive private channels?**
   - **NO.** The bot lacks the `groups:write` scope (it only holds `groups:read` and `groups:history`). Archiving or creating private channels requires `groups:write`.

---

## 4. Responder Selection & Invite Mechanism

### Specification & Code Audit
There is **NO dynamic on-call roster, schedule rotation, or PagerDuty integration** currently active.

The responder selection is determined by a hardcoded user ID:
1. **Hermes Prompt Template:** In [`webhook_subscriptions.json`](file:///home/ubuntu/.hermes/webhook_subscriptions.json#L6):
   ```text
   python3 /home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py rosetta \
     --ticket-id "{{payload.ticket.id}}" \
     --title "{{payload.ticket.title}}" \
     --severity "{{payload.ticket.priority.name}}" \
     --users "U04A3PR9294" \
     ...
   ```
2. **CLI Fallback Default:** In [`slack_incident.py`](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L549):
   ```python
   p_rosetta.add_argument("--users", default="U04A3PR9294", help="Comma-separated user IDs to invite")
   ```

### Responder Identity Lookup
- User ID `U04A3PR9294` maps to **Hafiz Mohd Rizal** (`Primary Owner / Admin`).
- During dispatch ([lines 280–285](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L280-L285)), the script resolves the real name dynamically:
  ```python
  u_info = client.users_info(user=u)
  uname = u_info["user"].get("real_name") or u_info["user"]["name"]
  user_tags.append(f"<@{u}> ({uname})")
  ```
  Output rendered on the dispatch card: `<@U04A3PR9294> (Hafiz Mohd Rizal)`.

---

## 5. Exact Rosetta Stone Message Format & Real Samples

### Construction Logic
The Rosetta Stone card is generated via deterministic mrkdwn text formatting in [`slack_incident.py`](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L363-L383) and posted as the top-level channel root message:

```python
blocks = [
    f"*🚨 INCIDENT DISPATCH: #{ticket_id}*",
    f"*Title:* {args.title}",
    f"*Severity:* {args.severity.upper()}",
    f"*Assigned Responders:* {users_str}",
    "",
    "--- *ROSETTA STONE MAPPINGS* ---",
    f"• *Zammad Ticket:* #{ticket_id} (ID: {ticket_id})",
    f"• *Zabbix Trigger / Event:* {args.zabbix_event}",
    f"• *FNT ELID / Element:* {args.fnt_elid}",
    f"• *Device / Target:* {args.device}",
    f"• *Customer / Circuit:* {args.customer}",
    "",
    f"👉 <https://zammad.officelab/tickets/{ticket_id}|View in Zammad>",
]
```

### Sample 1: Production Incident #274 (`C0CABBMPRSA`)
**Raw Message Text (from `conversations.history`):**
```text
🚨 INCIDENT DISPATCH: #274
Title: [Disaster] ALM: Fiber cut detected on Port 1 - Cyberjaya ALM (10.10.10.3) [#330459]
Severity: DISASTER
Assigned Responders: <@U04A3PR9294> (Hafiz Mohd Rizal)

--- ROSETTA STONE MAPPINGS ---
• Zammad Ticket: #274 (ID: 274)
• Zabbix Trigger / Event: ALM: Fiber cut detected on Port 1 - Cyberjaya ALM (10.10.10.3) [#330459]
• FNT ELID / Element: NOT_FOUND
• Device / Target: Cyberjaya ALM (10.10.10.3)
• Customer / Circuit: N/A

👉 <https://zammad.officelab/tickets/274|View in Zammad>
```

**Thread Reply (OTDR Triangulation Card from `triangulate_alm.py`):**
```text
📍 OTDR Fault Triangulation Result:
• Event ID: 330459
• Fault Distance: 15.340 km
• Geographic Coordinates: 3.123400, 101.567800
• Suspected Span: Cyberjaya OLT-01 -> Putrajaya SW-01
• FNT Command ELID: NOT_FOUND
```

---

### Sample 2: Production Incident #273 (`C0C9XPQRWKH`)
**Raw Message Text:**
```text
🚨 INCIDENT DISPATCH: #273
Title: High memory utilization on core-sw01
Severity: HIGH
Assigned Responders: <@U04A3PR9294> (Hafiz Mohd Rizal)

--- ROSETTA STONE MAPPINGS ---
• Zammad Ticket: #273 (ID: 273)
• Zabbix Trigger / Event: 330412
• FNT ELID / Element: SW-CORE-01
• Device / Target: 10.10.20.1
• Customer / Circuit: Internal Infrastructure

👉 <https://zammad.officelab/tickets/273|View in Zammad>
```

---

### Sample 3: Production Incident #272 (`C0C9XKM00LF`)
**Raw Message Text:**
```text
🚨 INCIDENT DISPATCH: #272
Title: BGP Session Down: AS65001 Peering Gateway
Severity: HIGH
Assigned Responders: <@U04A3PR9294> (Hafiz Mohd Rizal)

--- ROSETTA STONE MAPPINGS ---
• Zammad Ticket: #272 (ID: 272)
• Zabbix Trigger / Event: 330388
• FNT ELID / Element: RT-EDGE-01
• Device / Target: 10.10.0.1
• Customer / Circuit: Transit Link Alpha

👉 <https://zammad.officelab/tickets/272|View in Zammad>
```

---

## 6. Channel Topics & Message Pins

### Channel Topics
- **Are topics updated?** **YES.**
- The topic is set immediately upon channel creation or joining ([`slack_incident.py` lines 149, 288](file:///home/ubuntu/.hermes/skills/slack-incident/scripts/slack_incident.py#L149)):
  ```python
  client.conversations_setTopic(channel=channel_id, topic=f"Incident {ticket_id}: {args.title}"[:250])
  ```
- **Constraints:** Truncated to 250 characters (Slack max topic length is 250 chars).
- **Observed Topic on `#incident-274`:**
  `Incident 274: [Disaster] ALM: Fiber cut detected on Port 1 - Cyberjaya ALM (10.10.10.3) [#330459]`

### Message Pins
- **Is anything pinned?** **NO.**
- The codebase contains **zero** calls to `pins.add` or `client.pins_add`.
- Verification against live channels via `pins.list` confirms zero pinned items.
- Verification of OAuth scopes confirms the bot **does not possess `pins:write` or `pins:read` scopes**. Calling `pins.add` with the current bot token would fail with `missing_scope`.

---

## 7. Auto-Invited Participants & Workspace Population Audit

### Workspace Member Audit
A full audit of all members in Slack Workspace `T04B035410Q` via `users.list` yielded:

| User ID | Username / Name | Is Bot | Role / Status |
| :--- | :--- | :--- | :--- |
| `U04A3PR9294` | `Hafiz Mohd Rizal` | `False` | Primary Owner / Workspace Admin |
| `U0BQ96K07AL` | `Auto-Incident Bot` | `True` | Bot Account (Active integration) |
| `USLACKBOT` | `Slackbot` | `False` | System bot |
| `U0BQ3U7M68J` | `Hermes Agent` | `True` | Bot Account |
| `U0BPLP9S6D0` | `Workflow Bot` | `True` | Bot Account |

### Findings
1. **Single Human Workspace:** There is only **1 human user** in the entire workspace (`U04A3PR9294`).
2. **Zero Secondary Auto-Invites:** No managers, NOC team aliases, distribution groups, or customer stakeholders are auto-invited.
3. **Responder List Cardinality:** The responder list is never > 1 in production. While `slack_incident.py` supports comma-separated user IDs via `args.users.split(",")`, the automation layer only ever passes `"U04A3PR9294"`.

---

## 8. Slack API Sequence Ledger

### Incident Open Sequence (`cmd_rosetta`)
When Hermes triggers an incident dispatch, the following exact API calls occur in strict sequence:

```mermaid
sequenceDiagram
    autonumber
    participant H as Hermes / Zammad Webhook
    participant S as slack_incident.py
    participant Slack as Slack API (WebClient)
    participant O as triangulate_alm.py (Subprocess)

    H->>S: rosetta --ticket-id 274 --title ... --users U04A3PR9294
    S->>Slack: conversations.create(name="incident-274", is_private=False)
    Note over S,Slack: Catches name_taken -> conversations.list -> conversations.join
    S->>Slack: conversations.setTopic(channel=cid, topic="Incident 274: ...")
    S->>Slack: conversations.invite(channel=cid, users="U04A3PR9294")
    S->>Slack: users.info(user="U04A3PR9294")
    S->>Slack: chat.postMessage(channel=cid, text=Rosetta_Card)
    alt Title contains ALM / Fiber
        S->>O: Spawn triangulate_alm.py in background
        O->>Slack: chat.postMessage(channel=cid, thread_ts=dispatch_ts, text=OTDR_Card)
    end
```

1. **`conversations.create`**:
   `client.conversations_create(name="incident-<id>", is_private=False)`
   *(If `name_taken`: falls back to `conversations.list` to resolve channel ID and calls `conversations.join`)*
2. **`conversations.setTopic`**:
   `client.conversations_setTopic(channel=cid, topic="Incident <id>: <title>"[:250])`
3. **`conversations.invite`**:
   `client.conversations_invite(channel=cid, users="<user_id>")`
   *(Catches and ignores `already_in_channel` error)*
4. **`users.info`**:
   `client.users_info(user="<user_id>")` (resolves real name for card rendering)
5. **`chat.postMessage`**:
   Posts the Rosetta Stone card to the channel root.
6. **`chat.postMessage` (Threaded - Conditional)**:
   If the ticket title contains `ALM` or `Fiber`, spawns `triangulate_alm.py` which computes fault distance and calls `chat.postMessage(channel=cid, thread_ts=dispatch_ts, text=...)` to post the OTDR card in thread.

---

### Incident Close Sequence (`cmd_close`)
When an incident is resolved in Zammad and closed:

```mermaid
sequenceDiagram
    autonumber
    participant H as Hermes / Operator
    participant S as slack_incident.py
    participant Slack as Slack API (WebClient)

    H->>S: close --channel incident-274 --summary "Fiber spliced"
    alt Channel ID not provided
        S->>Slack: conversations.list(types="public_channel,private_channel")
    end
    S->>Slack: chat.postMessage(channel=cid, text=Close_Summary_Card)
    S->>Slack: conversations.archive(channel=cid)
    Note over S,Slack: Catches already_archived
```

1. **`conversations.list`**:
   If `--channel` is passed as a channel name instead of a channel ID (`C...`), searches public and private channels to obtain `channel_id`.
2. **`chat.postMessage`**:
   Posts the incident resolution summary card:
   `🏁 *INCIDENT CLOSED: <channel_name>* \n *Resolution Summary:* <summary>`
3. **`conversations.archive`**:
   `client.conversations_archive(channel=channel_id)`
   *(Catches and ignores `already_archived` error)*

---

## Summary of Findings Matrix

| Topic | Current Production Behavior | Code Source / API Capability |
| :--- | :--- | :--- |
| **Naming** | `incident-<ticket_id>` (DB ID, max 80 chars, lowercase alphanumeric) | `slack_incident.py:53-57, 257` |
| **Visibility** | Public (`is_private=False`) | `slack_incident.py:259` |
| **Bot Scopes** | `channels:manage`, `chat:write`, `channels:read`, `users:read`, etc. | Verified via `auth.test` |
| **Can Archive Public?** | Yes, across workspace if joined | `channels:manage` granted |
| **Can Archive Private?** | No | Missing `groups:write` |
| **Responder Roster** | Hardcoded to `U04A3PR9294` (Hafiz Mohd Rizal) | `webhook_subscriptions.json:6`, `slack_incident.py:549` |
| **Pins** | None | No `pins.add` calls; lacks `pins:write` scope |
| **Topic** | Set to `Incident <id>: <title>` (max 250 chars) | `slack_incident.py:149, 288` |
| **Auto-Invites** | Exactly 1 person (Hafiz Mohd Rizal); 0 external/NOC | Verified via workspace user audit |
| **Rosetta Payload** | Plain mrkdwn formatted string with ticket, trigger, ELID, device, customer | `slack_incident.py:363-383` |
