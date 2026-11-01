import os
import json
import urllib.parse
import urllib.request
from datetime import datetime
from functools import wraps

import psycopg
from psycopg.rows import dict_row

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-before-deployment")


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is missing in .env file")

    return psycopg.connect(
        database_url,
        sslmode="require",
        row_factory=dict_row
    )


def query_all(sql, params=()):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        conn.close()


def query_one(sql, params=()):
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def ensure_location_columns():
    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            additions = {
                "location_lat": "DECIMAL(10,7)",
                "location_lon": "DECIMAL(10,7)",
                "hospital_name": "VARCHAR(255)",
                "hospital_address": "VARCHAR(255)",
                "hospital_lat": "DECIMAL(10,7)",
                "hospital_lon": "DECIMAL(10,7)"
            }

            for column, data_type in additions.items():
                cur.execute(
                    f"""
                    ALTER TABLE emergency_requests
                    ADD COLUMN IF NOT EXISTS {column} {data_type}
                    """
                )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def find_location_suggestions(search_text, place_type):
    params = {
        "q": search_text,
        "limit": 7,
        "lang": "en"
    }

    if place_type == "hospital":
        params["osm_tag"] = "amenity:hospital"

    url = (
        "https://photon.komoot.io/api/?"
        + urllib.parse.urlencode(params)
    )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PulseRouteSmartAmbulance/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))

    results = []

    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [])

        if len(coords) < 2:
            continue

        name = props.get("name") or ""

        parts = [
            props.get(k)
            for k in (
                "street",
                "housenumber",
                "district",
                "city",
                "state",
                "country"
            )
        ]

        address = ", ".join(
            dict.fromkeys(str(p) for p in parts if p)
        )

        label = name + (", " if name and address else "") + address

        if not label:
            label = props.get("city") or search_text

        results.append({
            "label": label,
            "name": name or label,
            "lat": coords[1],
            "lon": coords[0]
        })

    return results


def analyze_route_options(start_lat, start_lon, end_lat, end_lon):
    coordinates = (
        f"{start_lon},{start_lat};"
        f"{end_lon},{end_lat}"
    )

    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        + coordinates
        + "?alternatives=true&overview=full&geometries=geojson&steps=false"
    )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PulseRouteSmartAmbulance/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))

    routes = payload.get("routes", [])

    if not routes:
        return {
            "recommended": None,
            "routes": [],
            "message": "No drivable route was found."
        }

    distances = [
        max(float(r.get("distance", 0)), 1)
        for r in routes
    ]

    durations = [
        max(float(r.get("duration", 0)), 1)
        for r in routes
    ]

    min_distance = min(distances)
    max_distance = max(distances)

    min_duration = min(durations)
    max_duration = max(durations)

    analyzed = []

    for index, route in enumerate(routes):
        distance = distances[index]
        duration = durations[index]

        distance_score = (
            100 if max_distance == min_distance
            else 100 * (max_distance - distance)
            / (max_distance - min_distance)
        )

        time_score = (
            100 if max_duration == min_duration
            else 100 * (max_duration - duration)
            / (max_duration - min_duration)
        )

        score = round(
            (time_score * 0.65) +
            (distance_score * 0.35),
            1
        )

        analyzed.append({
            "id": index + 1,
            "distance_km": round(distance / 1000, 2),
            "duration_min": round(duration / 60, 1),
            "score": score,
            "geometry": route.get("geometry"),
            "is_shortest": distance == min_distance,
            "is_fastest": duration == min_duration
        })

    recommended = max(
        analyzed,
        key=lambda item: item["score"]
    )

    for item in analyzed:
        item["is_recommended"] = (
            item["id"] == recommended["id"]
        )

    return {
        "recommended": recommended["id"],
        "routes": analyzed,
        "message": "Compared route distance and estimated road travel time.",
        "traffic_note": (
            "Live traffic data is not available from this routing service. "
            "Travel times are estimates."
        )
    }


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))

            if role and session.get("role") != role:
                flash("You do not have permission to access that page.", "danger")
                return redirect(url_for("home"))

            return view(*args, **kwargs)

        return wrapped

    return decorator


def traffic_prediction(density, blockage, peak_hour):
    score = (
        float(density)
        + (3 if blockage else 0)
        + (2 if peak_hour else 0)
    )

    if score >= 9:
        level = "High"
    elif score >= 5:
        level = "Moderate"
    else:
        level = "Low"

    return {
        "level": level,
        "score": round(score, 1),
        "note": (
            "Prediction based on entered conditions; "
            "not a live traffic feed."
        )
    }


@app.route("/")
def home():
    try:
        stats = query_one("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (
                       WHERE status='Available'
                   ) AS available,
                   COUNT(*) FILTER (
                       WHERE status='Assigned'
                   ) AS assigned,
                   COUNT(*) FILTER (
                       WHERE status='On Emergency'
                   ) AS emergency
            FROM ambulances
        """) or {}

        latest = query_all("""
            SELECT id, request_code, patient_name,
                   emergency_type, location, priority,
                   status, created_at
            FROM emergency_requests
            ORDER BY created_at DESC
            LIMIT 5
        """)

        ambulances = query_all("""
            SELECT id, ambulance_number, driver_name, status
            FROM ambulances
            ORDER BY id
        """)

        db_error = None

    except psycopg.Error as exc:
        app.logger.exception("Database error on home page")
        stats = {}
        latest = []
        ambulances = []
        db_error = str(exc)

    return render_template(
        "index.html",
        stats=stats,
        latest=latest,
        ambulances=ambulances,
        db_error=db_error
    )


@app.route("/api/location-suggestions")
def location_suggestions():
    q = request.args.get("q", "").strip()
    place_type = request.args.get("type", "pickup")

    if len(q) < 3:
        return jsonify([])

    if place_type not in ("pickup", "hospital"):
        return jsonify({
            "error": "Invalid location type"
        }), 400

    try:
        return jsonify(find_location_suggestions(q, place_type))

    except Exception:
        app.logger.exception("Location suggestion lookup failed")
        return jsonify({
            "error": "Location suggestions are temporarily unavailable."
        }), 502


@app.route("/emergency", methods=["GET", "POST"])
@app.route("/emergency-request", methods=["GET", "POST"])
def emergency():
    try:
        ensure_location_columns()

    except psycopg.Error as exc:
        app.logger.exception("Could not prepare location columns")

        if request.method == "GET":
            flash(
                "Database location fields could not be prepared: " + str(exc),
                "danger"
            )

    if request.method == "POST":
        name = request.form.get("patient_name", "").strip()
        phone = request.form.get("phone", "").strip()
        location = request.form.get("location", "").strip()
        hospital_name = request.form.get("hospital_name", "").strip()
        hospital_address = request.form.get("hospital_address", "").strip()
        kind = request.form.get("emergency_type", "Medical")
        details = request.form.get("details", "").strip()

        try:
            location_lat = float(request.form.get("location_lat", ""))
            location_lon = float(request.form.get("location_lon", ""))
            hospital_lat = float(request.form.get("hospital_lat", ""))
            hospital_lon = float(request.form.get("hospital_lon", ""))

        except (TypeError, ValueError):
            flash(
                "Please select a pickup location and destination hospital from the suggestions.",
                "danger"
            )
            return render_template("emergency.html")

        if not all([
            name,
            phone,
            location,
            hospital_name,
            hospital_address
        ]):
            flash(
                "Please complete patient details, pickup location and destination hospital.",
                "danger"
            )
            return render_template("emergency.html")

        code = "EMS-" + datetime.now().strftime("%y%m%d%H%M%S")

        try:
            execute("""
                INSERT INTO emergency_requests
                (
                    request_code,
                    patient_name,
                    phone,
                    location,
                    location_lat,
                    location_lon,
                    hospital_name,
                    hospital_address,
                    hospital_lat,
                    hospital_lon,
                    emergency_type,
                    details,
                    priority,
                    status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    'High', 'Pending'
                )
            """, (
                code,
                name,
                phone,
                location,
                location_lat,
                location_lon,
                hospital_name,
                hospital_address,
                hospital_lat,
                hospital_lon,
                kind,
                details
            ))

            flash(
                f"Emergency request submitted. Reference: {code}",
                "success"
            )

            return redirect(url_for("track_request", code=code))

        except psycopg.Error as exc:
            app.logger.exception("Emergency request database insert failed")
            flash(
                "Could not save request. Check Neon database and schema. " + str(exc),
                "danger"
            )

    return render_template("emergency.html")


@app.route("/track/<code>")
def track_request(code):
    item = query_one("""
        SELECT request_code, patient_name,
               location, location_lat, location_lon,
               hospital_name, hospital_address,
               hospital_lat, hospital_lon,
               emergency_type, priority, status, created_at
        FROM emergency_requests
        WHERE request_code=%s
    """, (code,))

    if not item:
        flash("Request not found.", "warning")
        return redirect(url_for("emergency"))

    route_analysis = None

    if all(
        item.get(key) is not None
        for key in (
            "location_lat",
            "location_lon",
            "hospital_lat",
            "hospital_lon"
        )
    ):
        try:
            route_analysis = analyze_route_options(
                float(item["location_lat"]),
                float(item["location_lon"]),
                float(item["hospital_lat"]),
                float(item["hospital_lon"])
            )

        except Exception:
            app.logger.exception("Route analysis failed")
            route_analysis = {
                "recommended": None,
                "routes": [],
                "message": "Route analysis is temporarily unavailable.",
                "traffic_note": (
                    "Check your internet connection and try again."
                )
            }

    return render_template(
        "track.html",
        item=item,
        route_analysis=route_analysis
    )


@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    requests_list = query_all("""
        SELECT er.*, a.ambulance_number
        FROM emergency_requests er
        LEFT JOIN ambulances a
        ON a.id=er.ambulance_id
        ORDER BY
            CASE er.status
                WHEN 'Pending' THEN 1
                WHEN 'Assigned' THEN 2
                WHEN 'Accepted' THEN 3
                WHEN 'En Route' THEN 4
                WHEN 'Arrived' THEN 5
                WHEN 'Completed' THEN 6
                WHEN 'Cancelled' THEN 7
                ELSE 8
            END,
            er.created_at DESC
    """)

    ambulances = query_all("""
        SELECT *
        FROM ambulances
        ORDER BY id
    """)

    stats = query_one("""
        SELECT COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE status='Pending'
            ) AS pending,
            COUNT(*) FILTER (
                WHERE status IN (
                    'Assigned',
                    'Accepted',
                    'En Route'
                )
            ) AS active,
            COUNT(*) FILTER (
                WHERE status='Completed'
            ) AS completed
        FROM emergency_requests
    """) or {}

    return render_template(
        "admin.html",
        requests_list=requests_list,
        ambulances=ambulances,
        stats=stats
    )


@app.route("/admin/assign/<int:request_id>", methods=["POST"])
@login_required("admin")
def assign_ambulance(request_id):
    ambulance_id = request.form.get("ambulance_id", type=int)

    req = query_one(
        "SELECT * FROM emergency_requests WHERE id=%s",
        (request_id,)
    )

    amb = query_one(
        "SELECT * FROM ambulances WHERE id=%s",
        (ambulance_id,)
    )

    if not req or not amb:
        flash("Request or ambulance was not found.", "danger")

    elif amb["status"] != "Available":
        flash("Choose an ambulance with Available status.", "warning")

    elif req["status"] != "Pending":
        flash("Only pending requests can be assigned.", "warning")

    else:
        conn = get_db_connection()

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE emergency_requests
                    SET ambulance_id=%s,
                        status='Assigned',
                        assigned_at=NOW()
                    WHERE id=%s
                """, (ambulance_id, request_id))

                cur.execute("""
                    UPDATE ambulances
                    SET status='Assigned'
                    WHERE id=%s
                """, (ambulance_id,))

            conn.commit()

            flash(
                f"Ambulance {amb['ambulance_number']} assigned to {req['request_code']}.",
                "success"
            )

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/status/<int:request_id>", methods=["POST"])
@login_required("admin")
def admin_update_status(request_id):
    status = request.form.get("status", "")

    allowed = {
        "Assigned",
        "Accepted",
        "En Route",
        "Arrived",
        "Completed",
        "Cancelled"
    }

    req = query_one(
        "SELECT * FROM emergency_requests WHERE id=%s",
        (request_id,)
    )

    if not req or status not in allowed:
        flash("Invalid request/status.", "danger")

    else:
        execute(
            "UPDATE emergency_requests SET status=%s WHERE id=%s",
            (status, request_id)
        )

        if req.get("ambulance_id"):
            if status in ("Completed", "Cancelled"):
                execute(
                    "UPDATE ambulances SET status='Available' WHERE id=%s",
                    (req["ambulance_id"],)
                )

            elif status in ("Assigned", "Accepted", "En Route", "Arrived"):
                execute(
                    "UPDATE ambulances SET status='On Emergency' WHERE id=%s",
                    (req["ambulance_id"],)
                )

        flash("Request status updated.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/driver")
@login_required("driver")
def driver_dashboard():
    driver = query_one(
        "SELECT * FROM ambulances WHERE id=%s",
        (session.get("ambulance_id"),)
    )

    assignments = query_all("""
        SELECT *
        FROM emergency_requests
        WHERE ambulance_id=%s
        AND status NOT IN ('Completed', 'Cancelled')
        ORDER BY assigned_at DESC
    """, (session.get("ambulance_id"),))

    return render_template(
        "driver.html",
        driver=driver,
        assignments=assignments
    )


@app.route("/driver/status/<int:request_id>", methods=["POST"])
@login_required("driver")
def driver_update_status(request_id):
    status = request.form.get("status", "")

    allowed = {
        "Accepted",
        "En Route",
        "Arrived",
        "Completed"
    }

    req = query_one("""
        SELECT *
        FROM emergency_requests
        WHERE id=%s
        AND ambulance_id=%s
    """, (
        request_id,
        session.get("ambulance_id")
    ))

    if not req or status not in allowed:
        flash("Invalid assignment/status.", "danger")

    else:
        execute(
            "UPDATE emergency_requests SET status=%s WHERE id=%s",
            (status, request_id)
        )

        if status == "Completed":
            execute(
                "UPDATE ambulances SET status='Available' WHERE id=%s",
                (session["ambulance_id"],)
            )

        else:
            execute(
                "UPDATE ambulances SET status='On Emergency' WHERE id=%s",
                (session["ambulance_id"],)
            )

        flash("Assignment status updated.", "success")

    return redirect(url_for("driver_dashboard"))


@app.route("/traffic", methods=["GET", "POST"])
def traffic():
    result = None

    if request.method == "POST":
        result = traffic_prediction(
            request.form.get("density", type=int) or 1,
            request.form.get("blockage") == "yes",
            request.form.get("peak_hour") == "yes"
        )

    return render_template(
        "traffic.html",
        result=result
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = query_one("""
            SELECT *
            FROM users
            WHERE username=%s
            AND is_active=TRUE
        """, (username,))

        if user and check_password_hash(
            user["password_hash"],
            password
        ):
            session.clear()

            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["ambulance_id"] = user.get("ambulance_id")

            if user["role"] == "admin":
                return redirect(url_for("admin_dashboard"))

            if user["role"] == "driver":
                return redirect(url_for("driver_dashboard"))

            return redirect(url_for("home"))

        flash("Incorrect username or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/api/stats")
def api_stats():
    stats = query_one("""
        SELECT COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE status='Available'
            ) AS available,
            COUNT(*) FILTER (
                WHERE status='Assigned'
            ) AS assigned,
            COUNT(*) FILTER (
                WHERE status='On Emergency'
            ) AS emergency
        FROM ambulances
    """)

    return jsonify(stats or {})


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true"
    )