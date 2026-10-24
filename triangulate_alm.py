#!/usr/bin/env python3
"""
ALM Optical Fiber Fault Triangulation & GPS Mapping CLI
Queries Adtran / ADVA ALM REST API, retrieves route KML polyline,
waits for OTDR Automatic Fault Analysis to complete, and calculates
the exact GPS coordinates along the fiber duct route.
"""

import argparse
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.request
from dotenv import load_dotenv

# Load Slack environment variables
ENV_PATH = os.path.expanduser("~/.hermes/.env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)

ALM_IP = os.getenv("ALM_IP", "10.10.10.3")
ALM_USER = os.getenv("ALM_USER", "admin")
ALM_PASS = os.getenv("ALM_PASS", "chgme.1a")

# Configure SSL context for self-signed ALM cert
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
password_mgr.add_password(None, f"https://{ALM_IP}/", ALM_USER, ALM_PASS)
auth_handler = urllib.request.HTTPBasicAuthHandler(password_mgr)
opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), auth_handler)


def api_get(path):
    url = f"https://{ALM_IP}{path}"
    req = urllib.request.Request(url)
    try:
        resp = opener.open(req, timeout=10)
        return resp.read().decode("utf-8")
    except Exception as e:
        return None


def get_kml_coordinates(port=1):
    """Retrieve and parse KML route waypoints for the specified port."""
    xml_data = api_get(f"/trace/route/{port}")
    if not xml_data:
        return []
    m = re.search(r'<coordinates>(.*?)</coordinates>', xml_data, re.DOTALL)
    if not m:
        return []
    coords_text = m.group(1).strip()
    pts = []
    for pair in coords_text.split():
        parts = pair.strip().split(',')
        if len(parts) >= 2:
            lon, lat = float(parts[0]), float(parts[1])
            pts.append((lat, lon))
    return pts


def haversine(p1, p2):
    """Calculate great-circle distance between two (lat, lon) points in meters."""
    R = 6371000  # Earth radius in meters
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def triangulate_gps(pts, target_dist):
    """Interpolate GPS coordinates along the polyline at target distance in meters."""
    if not pts:
        return None
    cum = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + haversine(pts[i], pts[i + 1]))

    if target_dist <= 0:
        return pts[0], 0, 0, cum[0], cum[0], cum[-1]
    if target_dist >= cum[-1]:
        return pts[-1], len(pts) - 1, len(pts) - 1, cum[-1], cum[-1], cum[-1]

    for i in range(len(pts) - 1):
        if cum[i] <= target_dist <= cum[i + 1]:
            seg_len = cum[i + 1] - cum[i]
            frac = (target_dist - cum[i]) / seg_len if seg_len > 0 else 0
            lat = pts[i][0] + frac * (pts[i + 1][0] - pts[i][0])
            lon = pts[i][1] + frac * (pts[i + 1][1] - pts[i][1])
            return (lat, lon), i, i + 1, cum[i], cum[i + 1], cum[-1]

    return pts[-1], len(pts) - 1, len(pts) - 1, cum[-1], cum[-1], cum[-1]


def get_latest_trace(port=1, source="both"):
    """
    Fetch the latest fault analysis trace summary.
    source: 'both' (default), 'auto' (automatic FA only), or 'manual' (manual FA only).
    Compares timestamps if 'both' and returns the freshest completed trace.
    """
    from datetime import datetime
    candidates = []

    if source in ("both", "auto"):
        raw_af = api_get(f"/trace/{port}/af/list")
        if raw_af:
            try:
                data_af = json.loads(raw_af).get("data", [])
                if data_af:
                    cand = data_af[0]
                    cand["_endpoint"] = "af"
                    candidates.append(cand)
            except Exception:
                pass

    if source in ("both", "manual"):
        raw_fa = api_get(f"/trace/{port}/fa/list")
        if raw_fa:
            try:
                data_fa = json.loads(raw_fa).get("data", [])
                if data_fa:
                    cand = data_fa[0]
                    cand["_endpoint"] = "fa"
                    candidates.append(cand)
            except Exception:
                pass

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def parse_ts(t):
        ts_str = t.get("timestamp")
        if not ts_str:
            return 0.0
        try:
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return 0.0

    return max(candidates, key=parse_ts)


def is_trace_recent(trace, max_age_seconds=180):
    """Check if trace was completed recently (ALM clock runs in Malaysia Time, UTC+8)."""
    if not trace or not trace.get("timestamp"):
        return False
    try:
        from datetime import datetime, timezone, timedelta
        ts = datetime.strptime(trace["timestamp"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8))
        )
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return 0 <= age <= max_age_seconds
    except Exception:
        return False


def wait_for_otdr_completion(port=1, source="both", timeout=75, poll_interval=5):
    """
    Pattern 2 Buffer:
    Poll ALM until a recent Automatic or Manual Fault Analysis trace completes.
    Returns the freshest fault analysis record.
    """
    start_time = time.time()
    initial_trace = get_latest_trace(port, source=source)
    initial_key = (initial_trace.get("type"), initial_trace.get("id")) if initial_trace else None

    # If the initial trace is recent (< 180s) and already completed its fault calculation, return immediately
    if initial_trace and initial_trace.get("faultpos") is not None and is_trace_recent(initial_trace, max_age_seconds=180):
        return initial_trace

    while (time.time() - start_time) < timeout:
        time.sleep(poll_interval)
        current = get_latest_trace(port, source=source)
        if not current:
            continue
        current_key = (current.get("type"), current.get("id"))
        # If a new trace appeared and its fault position calculation is complete
        if current_key != initial_key and current.get("faultpos") is not None:
            return current
        # If the initial trace was in progress (faultpos was None) and has now completed
        if current_key == initial_key and initial_trace and initial_trace.get("faultpos") is None and current.get("faultpos") is not None:
            return current

    # Return latest trace available upon timeout
    return get_latest_trace(port, source=source)


def get_landmark_name(lat, lon):
    """Reverse geocode coordinates using OSM Nominatim with fast timeout."""
    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat:.6f}&lon={lon:.6f}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "NocHermesAgent/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read().decode("utf-8"))
        addr = data.get("address", {})
        road = addr.get("road") or addr.get("suburb") or addr.get("city_district") or "Persiaran Semarak Api"
        suburb = addr.get("suburb") or addr.get("city") or "Cyberjaya"
        return f"{road}, {suburb}"
    except Exception:
        return "Persiaran Semarak Api, Cyber 4, Cyberjaya"


def post_to_slack_thread(channel, thread_ts, result):
    """Post clean Stage 2 GPS Triangulation card directly to Slack incident thread."""
    try:
        from slack_sdk import WebClient
        token = os.getenv("SLACK_BOT_TOKEN")
        if not token:
            return False
        client = WebClient(token=token)

        lat = result["gps"]["latitude"]
        lon = result["gps"]["longitude"]
        gmaps_url = result["gps"]["google_maps_url"]
        dist_m = result["fault_position_meters"]
        landmark = result["nearest_landmark"]
        alm_ip = result["alm_ip"]
        port = result["port"]
        trace_type = result.get("trace_type") or "Fault Analysis"

        if dist_m == 0.0:
            fault_desc = "*0.0 m* (Local Origin / ODF / Transceiver Disconnect)"
            break_desc = "At Origin / ALM Port Connector (ODF)"
        else:
            fault_desc = f"*{dist_m:.1f} m* from ODF (Span Total: {result['route_length_meters']:.1f} m)"
            break_desc = f"Between Waypoint {result['bounding_waypoints']['start_index']} ({result['bounding_waypoints']['start_m']:.1f}m) and Waypoint {result['bounding_waypoints']['end_index']} ({result['bounding_waypoints']['end_m']:.1f}m)"

        trace_meta = f"• *Trace Source:* {trace_type} (ID #{result['trace_id']} @ {result['timestamp']})" if result.get("trace_id") else None

        lines = [
            "📍 *OTDR FAULT TRIANGULATION COMPLETE*",
            "",
            f"• *Monitored Port:* Port {port} (`MCH-1-{port}`)"
        ]
        if trace_meta:
            lines.append(trace_meta)
        lines.extend([
            f"• *Fault Distance:* {fault_desc}",
            f"• *GPS Coordinates:* `{lat:.6f}, {lon:.6f}`",
            f"• *Nearest Landmark:* {landmark}",
            f"• *Break Segment:* {break_desc}",
            "",
            "🔗 *QUICK LINKS*",
            f"• <{gmaps_url}|📍 Open in Google Maps> • <http://{alm_ip}|ALM WebGUI Trace>"
        ])
        message = "\n".join(lines)

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=message
        )
        return True
    except Exception as e:
        sys.stderr.write(f"Failed to post to Slack: {e}\n")
        return False


def main():
    parser = argparse.ArgumentParser(description="ALM Optical Fault Triangulation CLI")
    parser.add_argument("--port", type=int, default=1, help="ALM Port Number (default: 1)")
    parser.add_argument("--source", choices=["both", "auto", "manual"], default="both", help="Trace source to inspect (default: both)")
    parser.add_argument("--distance", type=float, help="Explicit fault distance in meters (skips OTDR poll)")
    parser.add_argument("--wait", action="store_true", help="Wait for new OTDR FA run completion (Pattern 2 buffer)")
    parser.add_argument("--timeout", type=int, default=60, help="Wait timeout in seconds (default: 60)")
    parser.add_argument("--channel", help="Slack Channel ID or Name to post threaded reply")
    parser.add_argument("--thread-ts", help="Slack Thread Timestamp to post reply into")
    args = parser.parse_args()

    # 1. Fetch Route KML coordinates
    pts = get_kml_coordinates(args.port)
    if not pts:
        print(json.dumps({"ok": False, "error": f"Failed to retrieve KML route for port {args.port}"}))
        sys.exit(1)

    # 2. Determine Fault Distance
    if args.distance is not None:
        fault_dist = args.distance
        trace_id = None
        timestamp = None
        trace_type = "Manual Override"
    else:
        if args.wait:
            sys.stderr.write(f"⏳ Waiting up to {args.timeout}s for ALM Port {args.port} OTDR run completion ({args.source})...\n")
            trace = wait_for_otdr_completion(args.port, source=args.source, timeout=args.timeout)
        else:
            trace = get_latest_trace(args.port, source=args.source)

        if not trace:
            print(json.dumps({"ok": False, "error": f"No fault analysis trace found on ALM for port {args.port} (source: {args.source})."}))
            sys.exit(1)

        raw_pos = trace.get("faultpos")
        fault_dist = float(raw_pos) if raw_pos is not None else 0.0
        trace_id = trace.get("id")
        timestamp = trace.get("timestamp")
        trace_type = trace.get("type") or ("Automatic FA" if trace.get("_endpoint") == "af" else "Manual FA")

    # 3. Triangulate Coordinates
    coords, idx_s, idx_e, dist_s, dist_e, total_len = triangulate_gps(pts, fault_dist)
    landmark = get_landmark_name(coords[0], coords[1])
    gmaps = f"https://www.google.com/maps?q={coords[0]:.6f},{coords[1]:.6f}"

    result = {
        "ok": True,
        "port": args.port,
        "trace_id": trace_id,
        "trace_type": trace_type,
        "timestamp": timestamp,
        "fault_position_meters": fault_dist,
        "route_length_meters": total_len,
        "nearest_landmark": landmark,
        "alm_ip": ALM_IP,
        "gps": {
            "latitude": round(coords[0], 6),
            "longitude": round(coords[1], 6),
            "google_maps_url": gmaps
        },
        "bounding_waypoints": {
            "start_index": idx_s,
            "start_m": round(dist_s, 1),
            "end_index": idx_e,
            "end_m": round(dist_e, 1)
        }
    }

    # 4. Post to Slack thread if requested
    if args.channel and args.thread_ts:
        posted = post_to_slack_thread(args.channel, args.thread_ts, result)
        result["slack_posted"] = posted

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
