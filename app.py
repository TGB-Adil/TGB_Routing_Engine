import json
import time

import requests
import streamlit as st
import folium
from streamlit_folium import st_folium


# ============================================================
# ORS Directions API Explorer
# GET:  /v2/directions/{profile}?start=lon,lat&end=lon,lat
# POST: /v2/directions/{profile} with ORS JSON body
#
# Install:
#   pip install streamlit requests folium streamlit-folium
# Run:
#   streamlit run ors_directions_explorer.py
# ============================================================

st.set_page_config(page_title="ORS Directions Explorer", page_icon="🗺️", layout="wide")

DEFAULT_BASE_URL = "http://localhost/ors"
PROFILES = ["driving-car", "driving-hgv"]


def parse_lon_lat(text: str, label: str) -> list[float]:
    try:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 2:
            raise ValueError
        lon, lat = float(parts[0]), float(parts[1])
    except (ValueError, AttributeError):
        raise ValueError(f"{label}: enter longitude,latitude, e.g. 8.681495,49.41461")
    if not -180 <= lon <= 180:
        raise ValueError(f"{label}: longitude must be between -180 and 180.")
    if not -90 <= lat <= 90:
        raise ValueError(f"{label}: latitude must be between -90 and 90.")
    return [lon, lat]


def parse_coordinate_lines(text: str) -> list[list[float]]:
    coords = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        coords.append(parse_lon_lat(line, f"Coordinate row {line_number}"))
    if len(coords) < 2:
        raise ValueError("Enter at least two coordinates: a start and a destination.")
    return coords


def parse_csv_list(text: str, cast=str):
    if not text.strip():
        return []
    values = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(cast(item))
        except ValueError:
            raise ValueError(f"Could not parse '{item}' as {cast.__name__}.")
    return values


def parse_json_object(text: str, label: str):
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}")
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def decode_polyline(encoded: str, precision: int = 5) -> list[list[float]]:
    if not isinstance(encoded, str):
        return []
    index, lat, lon = 0, 0, 0
    coordinates = []
    factor = 10 ** precision
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result, shift = 0, 0
            while True:
                if index >= len(encoded):
                    return coordinates
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else (result >> 1))
        lat += deltas[0]
        lon += deltas[1]
        coordinates.append([lon / factor, lat / factor])
    return coordinates


def extract_route(data):
    if not isinstance(data, dict):
        return [], {}, [], "Unknown"

    features = data.get("features")
    if isinstance(features, list) and features:
        feature = features[0] if isinstance(features[0], dict) else {}
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        return (
            geometry.get("coordinates", []),
            properties.get("summary") or {},
            properties.get("segments") or [],
            "GeoJSON",
        )

    routes = data.get("routes")
    if isinstance(routes, list) and routes:
        route = routes[0] if isinstance(routes[0], dict) else {}
        geometry = route.get("geometry", [])
        if isinstance(geometry, str):
            coordinates = decode_polyline(geometry)
        elif isinstance(geometry, dict):
            coordinates = geometry.get("coordinates", [])
        elif isinstance(geometry, list):
            coordinates = geometry
        else:
            coordinates = []
        return coordinates, route.get("summary") or {}, route.get("segments") or [], "JSON"

    return [], {}, [], "No route geometry detected"


def build_map(coordinates, start, end):
    route_latlon = [
        [float(p[1]), float(p[0])]
        for p in coordinates
        if isinstance(p, (list, tuple)) and len(p) >= 2
    ]
    if route_latlon:
        center = [
            sum(p[0] for p in route_latlon) / len(route_latlon),
            sum(p[1] for p in route_latlon) / len(route_latlon),
        ]
        map_obj = folium.Map(location=center, zoom_start=7, control_scale=True)
        folium.PolyLine(route_latlon, weight=5, opacity=0.9, tooltip="ORS route").add_to(map_obj)
        map_obj.fit_bounds([
            [min(p[0] for p in route_latlon), min(p[1] for p in route_latlon)],
            [max(p[0] for p in route_latlon), max(p[1] for p in route_latlon)],
        ])
    else:
        map_obj = folium.Map(location=[start[1], start[0]], zoom_start=7, control_scale=True)

    folium.Marker([start[1], start[0]], tooltip="Start", icon=folium.Icon(color="green", icon="play")).add_to(map_obj)
    folium.Marker([end[1], end[0]], tooltip="Destination", icon=folium.Icon(color="red", icon="flag")).add_to(map_obj)
    return map_obj


def route_steps(segments):
    rows = []
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        for step in segment.get("steps", []):
            if not isinstance(step, dict):
                continue
            distance, duration = step.get("distance"), step.get("duration")
            rows.append({
                "Instruction": step.get("instruction") or step.get("name") or "Continue",
                "Distance (km)": round(distance / 1000, 3) if isinstance(distance, (int, float)) else None,
                "Duration (min)": round(duration / 60, 2) if isinstance(duration, (int, float)) else None,
                "Road": step.get("name") or "",
                "Type": step.get("type"),
            })
    return rows


def pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False)


def distance_text(value):
    return f"{value / 1000:,.2f} km" if isinstance(value, (int, float)) else "—"


def duration_text(value):
    if not isinstance(value, (int, float)):
        return "—"
    minutes = round(value / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


# ---------------- Sidebar ----------------
st.sidebar.title("ORS connection")
base_url = st.sidebar.text_input(
    "Base URL",
    value=st.session_state.get("ors_base_url", DEFAULT_BASE_URL),
    help="Example: http://localhost/ors (do not include /v2/directions).",
).strip().rstrip("/")
st.session_state["ors_base_url"] = base_url
st.sidebar.caption("No API key is added. Requests run from the computer hosting Streamlit.")

# ---------------- Page / request builder ----------------
st.title("🗺️ ORS Directions API Explorer")
st.caption("GET and POST for the Directions endpoint, with form-based options and route visualization.")

request_tab, response_tab = st.tabs(["Request builder", "Response"])

with request_tab:
    top1, top2 = st.columns([1, 2])
    with top1:
        method = st.radio("HTTP method", ["GET", "POST"], horizontal=True)
    with top2:
        profile = st.selectbox(
            "Routing profile",
            PROFILES,
            format_func=lambda p: "Driving Car (driving-car)" if p == "driving-car" else "Driving HGV (driving-hgv)",
        )

    st.subheader("Route coordinates")
    st.caption("Enter one coordinate per line as longitude,latitude. For POST, add intermediate waypoints by adding more lines.")
    default_coordinates = "8.681495,49.41461\n8.686507,49.41943\n8.687872,49.420318"
    coords_text = st.text_area(
        "Coordinates (longitude,latitude)",
        value=st.session_state.get("coordinates_text", default_coordinates),
        height=110,
        help="Example: 8.681495,49.41461. The first row is the start; the last row is the destination.",
        key="coordinates_input",
    )
    st.session_state["coordinates_text"] = coords_text

    st.subheader("POST options")
    st.caption("These controls map directly to ORS request fields. Defaults are preselected; fields marked optional are omitted when disabled or left empty.")
    with st.container(border=True):
        # Everyday options in simple controls
        c1, c2, c3 = st.columns(3)
        with c1:
            preference = st.selectbox("preference", ["recommended", "fastest", "shortest"], index=0,
                                      help="Default: recommended.")
            units = st.selectbox("units", ["m", "km", "mi"], index=0, help="Default: m.")
            language = st.text_input("language", value="en", help="Instruction language code. Default: en.")
        with c2:
            instructions = st.checkbox("instructions", value=True, help="Default: true.")
            geometry = st.checkbox("geometry", value=True, help="Default: true.")
            elevation = st.checkbox("elevation", value=False, help="Default: false.")
        with c3:
            continue_straight = st.selectbox("continue_straight", ["Use ORS default", "false", "true"], index=0,
                                             help="Optional override. Default in ORS: false.")
            instructions_format = st.selectbox("instructions_format", ["text", "html"], index=0,
                                               help="Default: text.")
            geometry_simplify = st.checkbox("geometry_simplify", value=False, help="Default: false.")

        c4, c5, c6 = st.columns(3)
        with c4:
            maneuvers = st.checkbox("maneuvers", value=False, help="Default: false.")
            roundabout_exits = st.checkbox("roundabout_exits", value=False, help="Default: false.")
            suppress_warnings = st.checkbox("suppress_warnings", value=False, help="Default: false.")
        with c5:
            request_id = st.text_input("id (optional)", value="", help="Optional request identifier, e.g. my_request.")
            maximum_speed = st.number_input("maximum_speed (optional)", min_value=0.0, value=0.0, step=5.0,
                                            help="Leave at 0 to omit. Must be supported by your ORS version/profile.")
        with c6:
            radiuses_text = st.text_input("radiuses (optional)", value="", placeholder="200, -1, 30",
                                          help="Comma-separated metres; use -1 for no limit at a waypoint.")
            skip_segments_text = st.text_input("skip_segments (optional)", value="", placeholder="2, 4",
                                               help="Comma-separated segment indices to skip.")

    with st.expander("Extra route data", expanded=False):
        st.caption("Array options: enter comma-separated values. Leave blank to omit.")
        ex1, ex2 = st.columns(2)
        with ex1:
            attributes_text = st.text_input("attributes", value="", placeholder="avgspeed, percentage")
            extra_info_text = st.text_input("extra_info", value="", placeholder="waytype, surface")
        with ex2:
            # ORS expects alternative_routes as an object. Expose fields instead of requiring JSON.
            alt_enabled = st.checkbox("Request alternative routes", value=False)
            alt1, alt2 = st.columns(2)
            with alt1:
                alt_target_count = st.number_input("alternative_routes.target_count", min_value=1, max_value=3, value=2)
            with alt2:
                alt_weight_factor = st.number_input("alternative_routes.weight_factor", min_value=1.0, max_value=3.0, value=1.6, step=0.1)

    with st.expander("Avoidance and custom routing", expanded=False):
        st.caption("Use form controls for common options. Custom model rules are advanced and may be profile/version dependent.")
        av1, av2 = st.columns(2)
        with av1:
            avoid_borders = st.selectbox("options.avoid_borders", ["Not set", "all", "controlled", "none"], index=0)
            avoid_features_text = st.text_input("options.avoid_features (optional)", value="", placeholder="ferries, tollways")
            avoid_countries_text = st.text_input("options.avoid_countries (optional)", value="", placeholder="11, 32")
        with av2:
            avoid_borders_help = "Avoid border crossings: all, controlled, or none."
            st.caption(avoid_borders_help)
            custom_model_enabled = st.checkbox("Add custom_model rules", value=False)
            custom_speed_condition = st.text_input("Speed rule condition", value="true",
                                                   disabled=not custom_model_enabled,
                                                   help="Example: true or road_class == MOTORWAY.")
            custom_speed_limit = st.number_input("Speed limit (optional)", min_value=0.0, value=0.0, step=5.0,
                                                 disabled=not custom_model_enabled,
                                                 help="Set 0 to omit speed rule.")
            custom_priority_condition = st.text_input("Priority rule condition (optional)", value="",
                                                      disabled=not custom_model_enabled,
                                                      help="Example: road_class == MOTORWAY.")
            custom_priority_multiplier = st.number_input("Priority multiplier", min_value=0.0, max_value=10.0,
                                                          value=0.0, step=0.1, disabled=not custom_model_enabled,
                                                          help="0 means omit priority rule; values above 0 add a multiply_by rule.")

    with st.expander("Raw JSON override (advanced)", expanded=False):
        st.caption("You do not need this for normal use. Use it only for ORS fields not represented by controls above.")
        extra_json_text = st.text_area("Additional top-level POST fields", value="{}", height=100,
                                       help="Must be a JSON object. Values here override generated fields.")
    try:
        extra_json = parse_json_object(extra_json_text, "Additional fields")
        extra_json_error = None
    except ValueError as exc:
        extra_json, extra_json_error = {}, str(exc)

    # Build request payload internally from the UI controls.
    try:
        coordinates = parse_coordinate_lines(coords_text)
        coordinate_error = None
    except ValueError as exc:
        coordinates, coordinate_error = [], str(exc)

    body = {
        "coordinates": coordinates,
        "preference": preference,
        "units": units,
        "instructions": instructions,
        "geometry": geometry,
        "elevation": elevation,
        "geometry_simplify": geometry_simplify,
        "instructions_format": instructions_format,
        "language": language.strip() or "en",
        "maneuvers": maneuvers,
        "roundabout_exits": roundabout_exits,
        "suppress_warnings": suppress_warnings,
    }
    if continue_straight != "Use ORS default":
        body["continue_straight"] = continue_straight == "true"
    if request_id.strip():
        body["id"] = request_id.strip()
    if maximum_speed > 0:
        body["maximum_speed"] = maximum_speed

    try:
        radiuses = parse_csv_list(radiuses_text, float)
        if radiuses:
            body["radiuses"] = radiuses
        skip_segments = parse_csv_list(skip_segments_text, int)
        if skip_segments:
            body["skip_segments"] = skip_segments
        attributes = parse_csv_list(attributes_text, str)
        if attributes:
            body["attributes"] = attributes
        extra_info = parse_csv_list(extra_info_text, str)
        if extra_info:
            body["extra_info"] = extra_info

        if alt_enabled:
            body["alternative_routes"] = {
                "target_count": int(alt_target_count),
                "weight_factor": float(alt_weight_factor),
            }

        options = {}
        if avoid_borders != "Not set":
            options["avoid_borders"] = avoid_borders
        avoid_features = parse_csv_list(avoid_features_text, str)
        if avoid_features:
            options["avoid_features"] = avoid_features
        avoid_countries = parse_csv_list(avoid_countries_text, int)
        if avoid_countries:
            options["avoid_countries"] = avoid_countries
        if options:
            body["options"] = options

        if custom_model_enabled:
            custom_model = {}
            speed_rule = {"if": custom_speed_condition.strip() or "true"}
            if custom_speed_limit > 0:
                speed_rule["limit_to"] = float(custom_speed_limit)
            custom_model["speed"] = [speed_rule]
            if custom_priority_condition.strip() and custom_priority_multiplier > 0:
                custom_model["priority"] = [{
                    "if": custom_priority_condition.strip(),
                    "multiply_by": float(custom_priority_multiplier),
                }]
            body["custom_model"] = custom_model

        # Advanced extra fields are merged last.
        body.update(extra_json)
        build_error = None
    except ValueError as exc:
        build_error = str(exc)

    endpoint_url = f"{base_url}/v2/directions/{profile}" if base_url else ""
    if method == "POST":
        preview_body = body
        st.markdown("**Generated POST body preview**")
        st.code(pretty(preview_body), language="json")
    else:
        st.markdown("**Generated GET request**")
        if coordinates:
            get_params = {
                "start": f"{coordinates[0][0]},{coordinates[0][1]}",
                "end": f"{coordinates[-1][0]},{coordinates[-1][1]}",
            }
        else:
            get_params = {}
        st.code(f"GET {endpoint_url}\nQuery parameters:\n{pretty(get_params)}", language="http")
        st.caption("GET uses the deployment's start/end query convention. POST uses the ORS coordinates array.")

    send_col, info_col = st.columns([1, 5])
    with send_col:
        send = st.button("▶ Send request", type="primary", use_container_width=True)
    with info_col:
        st.caption("POST options are assembled internally from the form. No manual JSON editing is required.")

    if send:
        try:
            if coordinate_error:
                raise ValueError(coordinate_error)
            if build_error:
                raise ValueError(build_error)
            if extra_json_error:
                raise ValueError(extra_json_error)
            if not base_url.startswith(("http://", "https://")):
                raise ValueError("Base URL must begin with http:// or https://.")

            started = time.perf_counter()
            if method == "GET":
                params = {
                    "start": f"{coordinates[0][0]},{coordinates[0][1]}",
                    "end": f"{coordinates[-1][0]},{coordinates[-1][1]}",
                }
                response = requests.get(endpoint_url, params=params, timeout=120)
                sent_body = None
            else:
                params = {}
                response = requests.post(
                    endpoint_url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=120,
                )
                sent_body = body
            elapsed = time.perf_counter() - started
            try:
                response_data = response.json()
            except ValueError:
                response_data = None

            st.session_state["ors_last_request"] = {
                "method": method,
                "url": response.url,
                "profile": profile,
                "params": params,
                "body": sent_body,
            }
            st.session_state["ors_last_response"] = {
                "status": response.status_code,
                "reason": response.reason,
                "elapsed": elapsed,
                "headers": dict(response.headers),
                "text": response.text,
                "json": response_data,
            }
            st.success(f"HTTP {response.status_code} {response.reason} — {elapsed:.2f} seconds")
            st.info("Open the Response tab for the map, raw response, and turn-by-turn directions.")
        except (requests.RequestException, ValueError, TypeError) as exc:
            st.error(f"Request could not be completed: {exc}")
            st.caption("Confirm the ORS URL is reachable and the selected profile/options are supported by your server.")

# ---------------- Response tab ----------------
with response_tab:
    req = st.session_state.get("ors_last_request")
    res = st.session_state.get("ors_last_response")
    if not res:
        st.info("Send a GET or POST request from the Request builder tab to see its response here.")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("HTTP status", f'{res["status"]} {res["reason"]}')
        m2.metric("Response time", f'{res["elapsed"]:.2f} s')
        m3.metric("Response size", f'{len(res["text"].encode("utf-8")):,} bytes')

        with st.expander("Request sent", expanded=False):
            st.code(f'{req["method"]} {req["url"]}', language="http")
            if req["params"]:
                st.markdown("**Query parameters**")
                st.code(pretty(req["params"]), language="json")
            if req["body"] is not None:
                st.markdown("**POST JSON body**")
                st.code(pretty(req["body"]), language="json")

        if res["status"] >= 400:
            st.error("The API returned an error. Inspect the response body for the server's error message.")

        data = res["json"]
        coordinates_out, summary, segments, response_format = extract_route(data)

        st.subheader("Route map")
        if coordinates_out:
            if req["method"] == "POST":
                sent_coords = (req.get("body") or {}).get("coordinates", [])
                try:
                    start = sent_coords[0]
                    end = sent_coords[-1]
                except (IndexError, TypeError):
                    start, end = coordinates_out[0], coordinates_out[-1]
            else:
                params = req.get("params") or {}
                try:
                    start = parse_lon_lat(params.get("start", ""), "Start")
                    end = parse_lon_lat(params.get("end", ""), "Destination")
                except ValueError:
                    start, end = coordinates_out[0], coordinates_out[-1]
            st_folium(build_map(coordinates_out, start, end), use_container_width=True, height=520, returned_objects=[])
            st.caption(f"Geometry parsed from {response_format} response.")
        else:
            st.warning("No recognizable route geometry was found in the response, so a route line cannot be drawn.")

        if summary:
            st.subheader("Route summary")
            a, b, c = st.columns(3)
            a.metric("Distance", distance_text(summary.get("distance")))
            b.metric("Duration", duration_text(summary.get("duration")))
            c.metric("Geometry points", f"{len(coordinates_out):,}" if coordinates_out else "—")

        st.subheader("API response")
        view = st.radio("Response display", ["Pretty JSON", "Raw text"], horizontal=True, key="response_display_mode")
        if view == "Pretty JSON" and data is not None:
            st.code(pretty(data), language="json")
        else:
            st.code(res["text"] or "(empty response)", language="text")

        st.subheader("Turn-by-turn directions")
        steps = route_steps(segments)
        if steps:
            st.dataframe(steps, use_container_width=True, hide_index=True)
        else:
            st.info("No steps were found. Ensure instructions=true and check the response schema returned by your ORS deployment.")

        with st.expander("Response headers", expanded=False):
            st.code(pretty(res["headers"]), language="json")
