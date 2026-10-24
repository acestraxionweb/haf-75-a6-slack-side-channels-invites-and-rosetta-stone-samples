#!/usr/bin/env python3
"""
Slack Incident Management CLI for Hermes Agent.
Allows creating incident channels, inviting responders, posting updates, and archiving channels.
Ensures proper Slack mrkdwn formatting.
"""

import argparse
import json
import os
import re
import sys
import subprocess
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

ENV_PATH = os.path.expanduser("~/.hermes/.env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)

TOKEN = os.getenv("SLACK_BOT_TOKEN")
if not TOKEN:
    print(json.dumps({"ok": False, "error": "SLACK_BOT_TOKEN not found in ~/.hermes/.env"}))
    sys.exit(1)

client = WebClient(token=TOKEN)


def format_slack_mrkdwn(text: str) -> str:
    """Ensure standard markdown links, bolding, headers, code blocks, and lists are converted to Slack mrkdwn."""
    if not text:
        return text
    # Convert literal '\n' sequences from CLI/escaped JSON to real newlines
    text = text.replace('\\n', '\n').replace('\\t', '\t').replace('\r\n', '\n').replace('\r', '\n')
    # Strip language identifier from code blocks (e.g. ```bash\n -> ```\n) because Slack does not support it
    text = re.sub(r'```[a-zA-Z0-9_-]+\n', '```\n', text)
    # Convert [text](http://url) -> <http://url|text>
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<\2|\1>', text)
    # Convert bold-italic ***text*** -> *_text_*
    text = re.sub(r'\*\*\*([^*]+)\*\*\*', r'*_\1_*', text)
    # Convert **bold** -> *bold* (Slack uses single asterisks for bold)
    text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', text)
    # Convert __bold__ -> *bold*
    text = re.sub(r'__([^_]+)__', r'*\1*', text)
    # Convert markdown headers (# Header) -> *HEADER*
    text = re.sub(r'^#{1,6}\s*(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # Convert markdown bullet lists (- item or * item) -> • item
    text = re.sub(r'^(\s*)[-*]\s+', r'\1• ', text, flags=re.MULTILINE)
    return text


def sanitize_channel_name(name: str) -> str:
    """Format string to be a valid Slack channel name (lowercase, letters, numbers, hyphens, max 80 chars)."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower())
    clean = re.sub(r"-+", "-", clean).strip("-_")
    return clean[:80]


def cleanup_heartbeat_state(channel_id: str):
    """Remove channel from heartbeat state tracking when archived/closed."""
    state_file = os.path.expanduser("~/.hermes/cache/slack/incident_heartbeat_state.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                st = json.load(f)
            if channel_id in st:
                del st[channel_id]
                with open(state_file, "w") as f:
                    json.dump(st, f, indent=2)
        except Exception:
            pass


def resolve_channel_id(channel_identifier: str) -> str:
    """If already a channel ID (starts with C), return it. Otherwise search by name."""
    if channel_identifier.startswith("C") and len(channel_identifier) > 8:
        return channel_identifier

    target_name = sanitize_channel_name(channel_identifier.lstrip("#"))
    cursor = None
    while True:
        try:
            resp = client.conversations_list(
                types="public_channel,private_channel",
                limit=200,
                cursor=cursor
            )
        except SlackApiError as e:
            raise RuntimeError(f"Failed to list channels: {e.response.get('error')}")

        for ch in resp.get("channels", []):
            if ch["name"] == target_name:
                return ch["id"]

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    raise ValueError(f"Channel '{channel_identifier}' not found.")


def cmd_create(args):
    """Create incident channel, set topic, and return channel details."""
    raw_name = args.name or (f"incident-{args.ticket_id}" if args.ticket_id else "incident-general")
    channel_name = sanitize_channel_name(raw_name)

    try:
        resp = client.conversations_create(
            name=channel_name,
            is_private=args.private
        )
        channel = resp["channel"]
        channel_id = channel["id"]
        created = True
    except SlackApiError as e:
        error_code = e.response.get("error")
        if error_code == "name_taken":
            channel_id = resolve_channel_id(channel_name)
            created = False
            # Check if archived
            try:
                info = client.conversations_info(channel=channel_id)
                if info.get("channel", {}).get("is_archived"):
                    print(json.dumps({
                        "ok": False,
                        "error": "is_archived",
                        "channel_id": channel_id,
                        "message": "Channel exists but is archived because the incident was already closed. DO NOT UNARCHIVE. Abort workflow."
                    }, indent=2))
                    sys.exit(0)
            except Exception:
                pass
            # Ensure bot is in the channel
            try:
                client.conversations_join(channel=channel_id)
            except Exception:
                pass
        else:
            print(json.dumps({
                "ok": False,
                "error": f"Slack API error creating channel: {error_code}",
                "detail": e.response.get("needed", "")
            }))
            sys.exit(1)

    if args.topic:
        try:
            client.conversations_setTopic(channel=channel_id, topic=format_slack_mrkdwn(args.topic)[:250])
        except Exception:
            pass

    invited = []
    if args.users:
        user_list = [u.strip() for u in args.users.split(",") if u.strip()]
        for u in user_list:
            try:
                client.conversations_invite(channel=channel_id, users=u)
                invited.append(u)
            except SlackApiError as e:
                if e.response.get("error") == "already_in_channel":
                    invited.append(u)

    print(json.dumps({
        "ok": True,
        "created": created,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "invited": invited
    }, indent=2))


def cmd_invite(args):
    """Invite users to a channel."""
    try:
        channel_id = resolve_channel_id(args.channel)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    user_list = [u.strip() for u in args.users.split(",") if u.strip()]
    invited = []
    errors = {}

    for u in user_list:
        try:
            client.conversations_invite(channel=channel_id, users=u)
            invited.append(u)
        except SlackApiError as e:
            err = e.response.get("error")
            if err == "already_in_channel":
                invited.append(u)
            else:
                errors[u] = err

    print(json.dumps({
        "ok": True,
        "channel_id": channel_id,
        "invited": invited,
        "errors": errors
    }, indent=2))


def cmd_post(args):
    """Post message to channel with Slack mrkdwn conversion."""
    try:
        channel_id = resolve_channel_id(args.channel)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    raw_message = ""
    if getattr(args, "message_file", None):
        with open(args.message_file, "r", encoding="utf-8") as f:
            raw_message = f.read()
    elif args.message == "-":
        raw_message = sys.stdin.read()
    elif args.message:
        raw_message = args.message

    formatted_text = format_slack_mrkdwn(raw_message)

    try:
        resp = client.chat_postMessage(
            channel=channel_id,
            text=formatted_text,
            thread_ts=args.thread_ts
        )
        print(json.dumps({
            "ok": True,
            "channel_id": channel_id,
            "ts": resp.get("ts")
        }, indent=2))
    except SlackApiError as e:
        err = e.response.get("error")
        if err == "is_archived":
            print(json.dumps({
                "ok": False,
                "error": "is_archived",
                "message": "CHANNEL_IS_ARCHIVED: This incident has already been resolved and archived. STOP AND ABORT IMMEDIATELY. DO NOT UNARCHIVE."
            }, indent=2))
            sys.exit(0)
        print(json.dumps({
            "ok": False,
            "error": err
        }))
        sys.exit(1)


def cmd_rosetta(args):
    """Orchestrate entire Incident Rosetta Stone workflow: create/join channel, invite users, post formatted Rosetta Stone card."""
    ticket_id = str(args.ticket_id).strip()
    number = str(args.number).strip() if args.number else ticket_id
    raw_name = args.channel or f"incident-{ticket_id}"
    channel_name = sanitize_channel_name(raw_name)

    # 1. Create or resolve channel
    try:
        resp = client.conversations_create(name=channel_name, is_private=False)
        channel_id = resp["channel"]["id"]
    except SlackApiError as e:
        if e.response.get("error") == "name_taken":
            channel_id = resolve_channel_id(channel_name)
            # Check if archived
            try:
                info = client.conversations_info(channel=channel_id)
                if info.get("channel", {}).get("is_archived"):
                    print(json.dumps({
                        "ok": False,
                        "error": "is_archived",
                        "channel_id": channel_id,
                        "message": "Channel is archived. DO NOT UNARCHIVE."
                    }, indent=2))
                    sys.exit(0)
            except Exception:
                pass
            try:
                client.conversations_join(channel=channel_id)
            except Exception:
                pass
        else:
            print(json.dumps({"ok": False, "error": str(e)}))
            sys.exit(1)

    # Set topic
    topic_text = f"Incident {ticket_id}: {args.title}"
    try:
        client.conversations_setTopic(channel=channel_id, topic=format_slack_mrkdwn(topic_text)[:250])
    except Exception:
        pass

    # 2. Invite responders and resolve real names
    users_to_invite = [u.strip() for u in (args.users or "U04A3PR9294").split(",") if u.strip()]
    invited = []
    responder_names = []
    for u in users_to_invite:
        try:
            client.conversations_invite(channel=channel_id, users=u)
            invited.append(u)
        except SlackApiError as e:
            if e.response.get("error") == "already_in_channel":
                invited.append(u)
        try:
            u_info = client.users_info(user=u)
            name = u_info.get("user", {}).get("real_name") or u_info.get("user", {}).get("name") or u
            responder_names.append(f"@{name}")
        except Exception:
            responder_names.append(f"@{u}")

    responder_str = ", ".join(responder_names) if responder_names else "@Responder"

    # 3. Extract event_id, device, trigger, and severity
    event_id = args.event_id or ""
    if not event_id:
        m = re.search(r'\[#(\d+)\]', args.title) or re.search(r'Event #?(\d+)', args.title)
        if m:
            event_id = m.group(1)

    t_lower = args.title.lower()
    if any(k in t_lower for k in ["disaster", "critical"]):
        severity_badge = "🔴 DISASTER"
    elif any(k in t_lower for k in ["high", "major"]):
        severity_badge = "🟠 HIGH"
    elif any(k in t_lower for k in ["warning", "average", "minor"]):
        severity_badge = "🟡 WARNING"
    elif "info" in t_lower:
        severity_badge = "🔵 INFO"
    else:
        severity_badge = "⚠️ INCIDENT"

    device = args.device or ""
    trigger = ""
    clean_title = re.sub(r'\[#\d+\]', '', args.title).strip()
    clean_title = re.sub(r'^(\s*[🚨🔴🟠🟡🔵ℹ️⚠️]*\s*\[[^\]]+\]\s*)+', '', clean_title).strip()

    if ' - ' in clean_title:
        t_part, d_part = clean_title.rsplit(' - ', 1)
        trigger = t_part.strip()
        if not device:
            device = d_part.strip()
    else:
        trigger = clean_title
        if not device:
            device = "Detected Infrastructure"

    group = args.group or "DevOps / Operations"

    # Quick links
    quick_links = [
        f"<http://100.80.103.95:8084/#ticket/zoom/{ticket_id}|Zammad Ticket #{number}>"
    ]
    if event_id:
        quick_links.append(f"<http://100.80.103.95:8083/tr_events.php?eventid={event_id}|Zabbix Event #{event_id}>")

    m_ip = re.search(r'\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.\d+\.\d+\.\d+)\b', device)
    if "alm" in device.lower() or "optical" in group.lower():
        alm_ip = m_ip.group(1) if m_ip else "10.10.10.3"
        quick_links.append(f"<http://{alm_ip}|ALM WebGUI>")
    elif m_ip:
        quick_links.append(f"<http://{m_ip.group(1)}|Device WebGUI>")

    # 4. Format clean, modern Slack NOC dispatch card without code blocks or lines
    message_lines = [
        f"🚨 *NOC INCIDENT DISPATCH* | *SEVERITY: {severity_badge}*",
        "",
        f"• *Zammad Ticket:* <http://100.80.103.95:8084/#ticket/zoom/{ticket_id}|#{number} (DB #{ticket_id})>",
    ]
    if event_id:
        message_lines.append(f"• *Zabbix Event:* <http://100.80.103.95:8083/tr_events.php?eventid={event_id}|Event #{event_id}>")
    else:
        message_lines.append("• *Zabbix Event:* N/A")

    message_lines.extend([
        f"• *Target Group:* {group}",
        f"• *Affected Device:* {device}",
        f"• *Incident Trigger:* {trigger}",
        f"• *Assigned Responder:* {responder_str}",
        "",
        "🔗 *QUICK LINKS*",
        f"• {' • '.join(quick_links)}"
    ])

    message = "\n".join(message_lines)

    try:
        post_resp = client.chat_postMessage(channel=channel_id, text=format_slack_mrkdwn(message))
        dispatch_ts = post_resp.get("ts")

        # Stage 2 (Pattern 2): If Optical / ALM incident, launch background OTDR triangulation
        if "alm" in args.title.lower() or "optical" in group.lower() or "fiber" in args.title.lower():
            m_port = re.search(r'port\s*(\d+)', args.title, re.IGNORECASE)
            port_num = m_port.group(1) if m_port else "1"
            tri_script = os.path.join(os.path.dirname(__file__), "triangulate_alm.py")
            if os.path.exists(tri_script) and dispatch_ts:
                tri_cmd = [
                    sys.executable,
                    tri_script,
                    "--port", str(port_num),
                    "--wait",
                    "--timeout", "75",
                    "--channel", channel_id,
                    "--thread-ts", str(dispatch_ts)
                ]
                try:
                    subprocess.Popen(tri_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                except Exception:
                    pass

        print(json.dumps({
            "ok": True,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "ts": dispatch_ts,
            "invited": invited
        }, indent=2))
    except SlackApiError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)


def cmd_close(args):
    """Post incident resolution card and archive channel."""
    ticket_id = str(args.ticket_id).strip()
    number = str(args.number).strip() if args.number else ticket_id
    raw_name = args.channel or f"incident-{ticket_id}"
    channel_name = sanitize_channel_name(raw_name)

    try:
        # If caller passed an explicit channel ID (starts with C), use it as-is.
        # Otherwise resolve by sanitized name. (FIX: do NOT sanitize an ID before lookup,
        # as sanitize_channel_name lowercases it and breaks the ID short-circuit.)
        if args.channel and args.channel.startswith("C") and len(args.channel) > 8:
            channel_id = args.channel
        else:
            channel_id = resolve_channel_id(channel_name)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    close_links = [
        f"<http://100.80.103.95:8084/#ticket/zoom/{ticket_id}|Zammad Ticket #{number}>"
    ]

    close_lines = [
        "✅ *INCIDENT RESOLVED & ARCHIVED*",
        "",
        f"• *Zammad Ticket:* <http://100.80.103.95:8084/#ticket/zoom/{ticket_id}|#{number} (DB #{ticket_id})>",
        "• *Status:* 🟢 Closed & Recovered",
        "• *Triggered By:* Zabbix Recovery / Engineer Action",
        f"• *Channel Status:* Archiving channel `#{channel_name}`",
        "",
        "🔗 *QUICK LINKS*",
        f"• {' • '.join(close_links)}"
    ]
    summary = "\n".join(close_lines)

    try:
        client.chat_postMessage(channel=channel_id, text=format_slack_mrkdwn(summary))
    except Exception:
        pass

    try:
        client.conversations_archive(channel=channel_id)
        cleanup_heartbeat_state(channel_id)
        print(json.dumps({
            "ok": True,
            "archived": True,
            "channel_id": channel_id
        }, indent=2))
    except SlackApiError as e:
        if e.response.get("error") == "already_archived":
            cleanup_heartbeat_state(channel_id)
            print(json.dumps({"ok": True, "archived": True, "channel_id": channel_id, "note": "already_archived"}))
        else:
            print(json.dumps({"ok": False, "error": e.response.get("error")}))
            sys.exit(1)


def cmd_archive(args):
    """Post optional final summary with Slack mrkdwn conversion and archive channel."""
    try:
        channel_id = resolve_channel_id(args.channel)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    if args.summary:
        try:
            formatted_summary = format_slack_mrkdwn(args.summary)
            client.chat_postMessage(channel=channel_id, text=formatted_summary)
        except Exception:
            pass

    try:
        client.conversations_archive(channel=channel_id)
        cleanup_heartbeat_state(channel_id)
        print(json.dumps({
            "ok": True,
            "archived": True,
            "channel_id": channel_id
        }, indent=2))
    except SlackApiError as e:
        err = e.response.get("error")
        if err == "already_archived":
            cleanup_heartbeat_state(channel_id)
            print(json.dumps({
                "ok": True,
                "archived": True,
                "channel_id": channel_id,
                "note": "already_archived"
            }, indent=2))
        else:
            print(json.dumps({"ok": False, "error": err}))
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Slack Incident Management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_create = subparsers.add_parser("create", help="Create an incident channel")
    p_create.add_argument("--ticket-id", help="Ticket ID or Number")
    p_create.add_argument("--name", help="Channel name (defaults to incident-<ticket_id>)")
    p_create.add_argument("--topic", help="Channel topic / incident title")
    p_create.add_argument("--users", help="Comma-separated user IDs to invite immediately")
    p_create.add_argument("--private", action="store_true", help="Create as private channel")
    p_create.set_defaults(func=cmd_create)

    p_invite = subparsers.add_parser("invite", help="Invite users to channel")
    p_invite.add_argument("--channel", required=True, help="Channel ID or name")
    p_invite.add_argument("--users", required=True, help="Comma-separated user IDs")
    p_invite.set_defaults(func=cmd_invite)

    p_post = subparsers.add_parser("post", help="Post update to channel")
    p_post.add_argument("--channel", required=True, help="Channel ID or name")
    p_post.add_argument("--message", help="Message content (or '-' for stdin)")
    p_post.add_argument("--message-file", help="Path to file containing message content")
    p_post.add_argument("--thread-ts", help="Optional thread timestamp to reply in thread")
    p_post.set_defaults(func=cmd_post)

    p_rosetta = subparsers.add_parser("rosetta", help="Initialize incident channel, invite team, and post Rosetta Stone")
    p_rosetta.add_argument("--ticket-id", required=True, help="Zammad Database Ticket ID")
    p_rosetta.add_argument("--number", help="Zammad Ticket Number")
    p_rosetta.add_argument("--event-id", help="Zabbix Event ID")
    p_rosetta.add_argument("--channel", help="Channel name (defaults to incident-<ticket_id>)")
    p_rosetta.add_argument("--device", help="Device / Host name")
    p_rosetta.add_argument("--title", required=True, help="Incident Alert Title")
    p_rosetta.add_argument("--group", help="Operations group")
    p_rosetta.add_argument("--users", default="U04A3PR9294", help="Comma-separated Slack user IDs")
    p_rosetta.add_argument("--plan", help="Remediation steps")
    p_rosetta.set_defaults(func=cmd_rosetta)

    p_close = subparsers.add_parser("close", help="Post incident resolution summary and archive channel")
    p_close.add_argument("--ticket-id", required=True, help="Zammad Database Ticket ID")
    p_close.add_argument("--number", help="Zammad Ticket Number")
    p_close.add_argument("--channel", help="Channel name (defaults to incident-<ticket_id>)")
    p_close.set_defaults(func=cmd_close)

    p_archive = subparsers.add_parser("archive", help="Archive channel after incident resolution")
    p_archive.add_argument("--channel", required=True, help="Channel ID or name")
    p_archive.add_argument("--summary", help="Final incident closure summary to post before archiving")
    p_archive.set_defaults(func=cmd_archive)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

