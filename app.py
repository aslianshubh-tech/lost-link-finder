from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import (
    Blueprint,
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


APP_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = APP_DIR / "instance"
UPLOAD_DIR = APP_DIR / "uploads"
DATABASE_PATH = INSTANCE_DIR / "lostlink.sqlite3"
if os.environ.get("LOSTLINK_DATABASE"):
    DATABASE_PATH = Path(os.environ["LOSTLINK_DATABASE"]).expanduser().resolve()
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/") or ""

ALLOWED_CATEGORIES = [
    "ID Card",
    "Electronics",
    "Books",
    "Stationery",
    "Bags",
    "Clothing",
    "Accessories",
    "Keys",
    "Other",
]
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MATCH_THRESHOLDS = {"high": 80, "possible": 60}


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(APP_DIR / "templates"),
        static_folder=str(APP_DIR / "static"),
        static_url_path=f"{BASE_PATH}/static",
    )
    app.config.update(
        SECRET_KEY=os.environ.get("SESSION_SECRET", "lostlink-development-secret"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    INSTANCE_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    init_db()

    main = Blueprint("main", __name__, url_prefix=BASE_PATH or None)

    @app.before_request
    def load_logged_in_user() -> None:
        user_id = session.get("user_id")
        if user_id is None:
            g.user = None
        else:
            g.user = query_db("SELECT id, full_name, email, phone FROM users WHERE id = ?", (user_id,), one=True)

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {"categories": ALLOWED_CATEGORIES, "match_thresholds": MATCH_THRESHOLDS}

    @main.route("/")
    def home():
        return render_template("index.html")

    @main.route("/register", methods=("GET", "POST"))
    def register():
        if g.user:
            return redirect(url_for("main.dashboard"))
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            phone = request.form.get("phone", "").strip()
            error = None
            if len(full_name) < 2:
                error = "Please enter your full name."
            elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                error = "Please enter a valid college email address."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif query_db("SELECT id FROM users WHERE email = ?", (email,), one=True):
                error = "An account with this email already exists."
            if error is None:
                execute_db(
                    "INSERT INTO users (full_name, email, password_hash, phone) VALUES (?, ?, ?, ?)",
                    (full_name, email, generate_password_hash(password), phone or None),
                )
                flash("Account created. You can now sign in.", "success")
                return redirect(url_for("main.login"))
            flash(error, "error")
        return render_template("auth.html", mode="register")

    @main.route("/login", methods=("GET", "POST"))
    def login():
        if g.user:
            return redirect(url_for("main.dashboard"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = query_db("SELECT * FROM users WHERE email = ?", (email,), one=True)
            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Email or password is incorrect.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                flash(f"Welcome back, {user['full_name'].split()[0]}.", "success")
                return redirect(url_for("main.dashboard"))
        return render_template("auth.html", mode="login")

    @main.route("/logout", methods=("POST",))
    def logout():
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("main.home"))

    @main.route("/dashboard")
    @login_required
    def dashboard():
        user_id = g.user["id"]
        stats = {
            "lost": scalar_db("SELECT COUNT(*) FROM lost_items WHERE user_id = ?", (user_id,)),
            "found": scalar_db("SELECT COUNT(*) FROM found_items WHERE user_id = ?", (user_id,)),
            "matches": scalar_db(
                """SELECT COUNT(*) FROM matches m
                   JOIN lost_items l ON l.id = m.lost_item_id
                   JOIN found_items f ON f.id = m.found_item_id
                   WHERE m.score >= ? AND (l.user_id = ? OR f.user_id = ?)""",
                (MATCH_THRESHOLDS["possible"], user_id, user_id),
            ),
            "resolved": scalar_db(
                """SELECT COUNT(*) FROM (
                   SELECT id FROM lost_items WHERE user_id = ? AND status = 'resolved'
                   UNION ALL
                   SELECT id FROM found_items WHERE user_id = ? AND status = 'resolved'
                )""",
                (user_id, user_id),
            ),
        }
        recent = query_db(
            """SELECT id, item_name, category, location, status, created_at, 'lost' AS kind
               FROM lost_items WHERE user_id = ?
               UNION ALL
               SELECT id, item_name, category, location, status, created_at, 'found' AS kind
               FROM found_items WHERE user_id = ?
               ORDER BY created_at DESC LIMIT 5""",
            (user_id, user_id),
        )
        return render_template("dashboard.html", stats=stats, recent=recent)

    @main.route("/report/<kind>", methods=("GET", "POST"))
    @login_required
    def report(kind: str):
        if kind not in {"lost", "found"}:
            abort(404)
        if request.method == "POST":
            form_data, error = collect_report_form()
            image_filename, upload_error = save_uploaded_image(request.files.get("image"))
            error = error or upload_error
            if error is None:
                table = f"{kind}_items"
                execute_db(
                    f"""INSERT INTO {table}
                    (user_id, item_name, category, color, description, location, item_date,
                     approx_time, image_filename, additional_info)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        g.user["id"],
                        form_data["item_name"],
                        form_data["category"],
                        form_data["color"],
                        form_data["description"],
                        form_data["location"],
                        form_data["item_date"],
                        form_data["approx_time"],
                        image_filename,
                        form_data["additional_info"],
                    ),
                )
                recompute_matches()
                flash(f"{kind.title()} item report submitted.", "success")
                return redirect(url_for("main.my_reports"))
            flash(error, "error")
        return render_template("report_form.html", kind=kind)

    @main.route("/lost")
    def lost_items():
        items = browse_items("lost")
        return render_template("browse.html", kind="lost", items=items)

    @main.route("/found")
    def found_items():
        items = browse_items("found")
        return render_template("browse.html", kind="found", items=items)

    @main.route("/matches")
    @login_required
    def matches():
        user_id = g.user["id"]
        rows = query_db(
            """SELECT m.*, l.item_name AS lost_name, l.category AS lost_category,
                      l.color AS lost_color, l.location AS lost_location, l.item_date AS lost_date,
                      l.description AS lost_description, l.image_filename AS lost_image,
                      f.item_name AS found_name, f.category AS found_category,
                      f.color AS found_color, f.location AS found_location, f.item_date AS found_date,
                      f.description AS found_description, f.image_filename AS found_image,
                      l.user_id AS lost_owner, f.user_id AS found_owner
               FROM matches m
               JOIN lost_items l ON l.id = m.lost_item_id
               JOIN found_items f ON f.id = m.found_item_id
               WHERE m.score >= ? AND (l.user_id = ? OR f.user_id = ?)
               ORDER BY m.score DESC, m.created_at DESC""",
            (MATCH_THRESHOLDS["possible"], user_id, user_id),
        )
        rows = [dict(row) for row in rows]
        for row in rows:
            row["reasons"] = json.loads(row["reasons"])
        return render_template("matches.html", matches=rows)

    @main.route("/my-reports")
    @login_required
    def my_reports():
        user_id = g.user["id"]
        reports = query_db(
            """SELECT id, item_name, category, color, location, item_date, status, created_at,
                      'lost' AS kind FROM lost_items WHERE user_id = ?
               UNION ALL
               SELECT id, item_name, category, color, location, item_date, status, created_at,
                      'found' AS kind FROM found_items WHERE user_id = ?
               ORDER BY created_at DESC""",
            (user_id, user_id),
        )
        return render_template("my_reports.html", reports=reports)

    @main.route("/item/<kind>/<int:item_id>")
    def item_detail(kind: str, item_id: int):
        if kind not in {"lost", "found"}:
            abort(404)
        item = query_db(
            f"""SELECT i.*, u.full_name AS reporter_name, u.email AS reporter_email,
                       u.phone AS reporter_phone
                FROM {kind}_items i JOIN users u ON u.id = i.user_id WHERE i.id = ?""",
            (item_id,),
            one=True,
        )
        if item is None:
            abort(404)
        related = []
        if g.user:
            if kind == "lost":
                related = query_db(
                    """SELECT m.score, m.reasons, f.id, f.item_name, f.location, f.item_date
                       FROM matches m JOIN found_items f ON f.id = m.found_item_id
                       WHERE m.lost_item_id = ? AND m.score >= ?
                       ORDER BY m.score DESC""",
                    (item_id, MATCH_THRESHOLDS["possible"]),
                )
            else:
                related = query_db(
                    """SELECT m.score, m.reasons, l.id, l.item_name, l.location, l.item_date
                       FROM matches m JOIN lost_items l ON l.id = m.lost_item_id
                       WHERE m.found_item_id = ? AND m.score >= ?
                       ORDER BY m.score DESC""",
                    (item_id, MATCH_THRESHOLDS["possible"]),
                )
            related = [dict(match) for match in related]
            for match in related:
                match["reasons"] = json.loads(match["reasons"])
        is_owner = bool(g.user and g.user["id"] == item["user_id"])
        return render_template("item_detail.html", item=item, kind=kind, is_owner=is_owner, related=related)

    @main.route("/resolve/<kind>/<int:item_id>", methods=("POST",))
    @login_required
    def resolve(kind: str, item_id: int):
        if kind not in {"lost", "found"}:
            abort(404)
        item = query_db(f"SELECT user_id FROM {kind}_items WHERE id = ?", (item_id,), one=True)
        if item is None:
            abort(404)
        if item["user_id"] != g.user["id"]:
            abort(403)
        execute_db(f"UPDATE {kind}_items SET status = 'resolved' WHERE id = ?", (item_id,))
        flash("Report marked as resolved.", "success")
        return redirect(request.referrer or url_for("main.my_reports"))

    @main.route("/about")
    def about():
        return render_template("about.html")

    @main.route("/uploads/<path:filename>")
    def uploaded_file(filename: str):
        return send_from_directory(UPLOAD_DIR, filename)

    app.register_blueprint(main)

    @app.route(f"{BASE_PATH}/healthz" if BASE_PATH else "/healthz")
    def healthz():
        return {"status": "ok", "app": "LostLink"}

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", title="Page not found", message="We couldn't find that LostLink page."), 404

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", title="Access not allowed", message="You can only manage your own reports."), 403

    @app.errorhandler(413)
    def too_large(_error):
        flash("That image is too large. Please choose a file under 4 MB.", "error")
        return redirect(request.referrer or url_for("main.home"))

    @app.errorhandler(Exception)
    def handle_error(error):
        if isinstance(error, HTTPException):
            return error
        app.logger.exception("Unexpected application error", exc_info=error)
        return render_template(
            "error.html",
            title="Something went wrong",
            message="LostLink could not complete that request. Please try again.",
        ), 500

    return app


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped_view(**kwargs: Any):
        if g.user is None:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("main.login", next=request.path))
        return view(**kwargs)

    return wrapped_view


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    INSTANCE_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            phone TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS lost_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_name TEXT NOT NULL,
            category TEXT NOT NULL,
            color TEXT NOT NULL,
            description TEXT NOT NULL,
            location TEXT NOT NULL,
            item_date TEXT NOT NULL,
            approx_time TEXT NOT NULL,
            image_filename TEXT,
            additional_info TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'possible_match', 'resolved')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS found_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_name TEXT NOT NULL,
            category TEXT NOT NULL,
            color TEXT NOT NULL,
            description TEXT NOT NULL,
            location TEXT NOT NULL,
            item_date TEXT NOT NULL,
            approx_time TEXT NOT NULL,
            image_filename TEXT,
            additional_info TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'possible_match', 'resolved')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lost_item_id INTEGER NOT NULL REFERENCES lost_items(id) ON DELETE CASCADE,
            found_item_id INTEGER NOT NULL REFERENCES found_items(id) ON DELETE CASCADE,
            score INTEGER NOT NULL,
            reasons TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lost_item_id, found_item_id)
        );
        """
    )
    connection.commit()
    connection.close()


def query_db(sql: str, params: tuple[Any, ...] = (), one: bool = False) -> Any:
    cursor = get_db().execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    return (rows[0] if rows else None) if one else rows


def scalar_db(sql: str, params: tuple[Any, ...] = ()) -> int:
    row = query_db(sql, params, one=True)
    return int(row[0]) if row else 0


def execute_db(sql: str, params: tuple[Any, ...] = ()) -> int:
    db = get_db()
    cursor = db.execute(sql, params)
    db.commit()
    lastrowid = cursor.lastrowid
    cursor.close()
    return int(lastrowid or 0)


def collect_report_form() -> tuple[dict[str, str], str | None]:
    values = {
        "item_name": request.form.get("item_name", "").strip(),
        "category": request.form.get("category", "").strip(),
        "color": request.form.get("color", "").strip(),
        "description": request.form.get("description", "").strip(),
        "location": request.form.get("location", "").strip(),
        "item_date": request.form.get("item_date", "").strip(),
        "approx_time": request.form.get("approx_time", "").strip(),
        "additional_info": request.form.get("additional_info", "").strip(),
    }
    required = ("item_name", "category", "color", "description", "location", "item_date", "approx_time")
    if any(not values[field] for field in required):
        return values, "Please complete all required report fields."
    if values["category"] not in ALLOWED_CATEGORIES:
        return values, "Please choose a category from the list."
    try:
        date.fromisoformat(values["item_date"])
    except ValueError:
        return values, "Please choose a valid date."
    return values, None


def save_uploaded_image(upload: Any) -> tuple[str | None, str | None]:
    if upload is None or not upload.filename:
        return None, None
    original = secure_filename(upload.filename)
    extension = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if extension not in ALLOWED_EXTENSIONS:
        return None, "Please upload a PNG, JPG, GIF, or WEBP image."
    filename = f"{uuid.uuid4().hex}_{original}"
    upload.save(UPLOAD_DIR / filename)
    return filename, None


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").lower().strip())


def tokens(value: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalized(value)))


def text_similarity(first: str | None, second: str | None) -> float:
    a, b = normalized(first), normalized(second)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    left, right = tokens(a), tokens(b)
    overlap = len(left & right) / max(len(left | right), 1)
    return max(sequence, overlap)


def date_score(first: str, second: str) -> tuple[int, str | None]:
    try:
        days = abs((date.fromisoformat(first) - date.fromisoformat(second)).days)
    except ValueError:
        return 0, None
    if days == 0:
        return 10, "Same date"
    if days <= 1:
        return 8, "Similar date"
    if days <= 3:
        return 5, "Dates are close"
    return 0, None


def time_score(first: str, second: str) -> tuple[int, str | None]:
    try:
        a = datetime.strptime(first, "%H:%M")
        b = datetime.strptime(second, "%H:%M")
    except ValueError:
        return 0, None
    minutes = abs(int((a - b).total_seconds() / 60))
    if minutes <= 60:
        return 10, "Similar time"
    if minutes <= 120:
        return 8, "Approximate times are close"
    if minutes <= 240:
        return 5, "Times are in the same part of the day"
    return 0, None


def score_pair(lost: sqlite3.Row, found: sqlite3.Row) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if normalized(lost["category"]) == normalized(found["category"]):
        score += 20
        reasons.append("Same category")

    item_similarity = text_similarity(lost["item_name"], found["item_name"])
    if item_similarity >= 0.75:
        score += 20
        reasons.append("Very similar item name")
    elif item_similarity >= 0.45:
        score += 12
        reasons.append("Related item name")

    if normalized(lost["color"]) == normalized(found["color"]):
        score += 15
        reasons.append("Same color")
    elif text_similarity(lost["color"], found["color"]) >= 0.6:
        score += 8
        reasons.append("Similar color")

    location_similarity = text_similarity(lost["location"], found["location"])
    if location_similarity >= 0.8:
        score += 15
        reasons.append("Same location")
    elif location_similarity >= 0.45:
        score += 9
        reasons.append("Nearby or similar location")

    points, reason = date_score(lost["item_date"], found["item_date"])
    score += points
    if reason:
        reasons.append(reason)
    points, reason = time_score(lost["approx_time"], found["approx_time"])
    score += points
    if reason:
        reasons.append(reason)

    description_similarity = text_similarity(lost["description"], found["description"])
    if description_similarity >= 0.55:
        score += 10
        reasons.append("Similar description")
    elif description_similarity >= 0.3:
        score += 5
        reasons.append("Some description details overlap")
    return min(score, 100), reasons


def recompute_matches() -> None:
    lost_items = query_db("SELECT * FROM lost_items WHERE status != 'resolved'")
    found_items = query_db("SELECT * FROM found_items WHERE status != 'resolved'")
    db = get_db()
    for lost in lost_items:
        for found in found_items:
            score, reasons = score_pair(lost, found)
            db.execute(
                """INSERT INTO matches (lost_item_id, found_item_id, score, reasons)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(lost_item_id, found_item_id)
                   DO UPDATE SET score = excluded.score, reasons = excluded.reasons""",
                (lost["id"], found["id"], score, json.dumps(reasons)),
            )
            if score >= MATCH_THRESHOLDS["possible"]:
                db.execute(
                    "UPDATE lost_items SET status = 'possible_match' WHERE id = ? AND status = 'active'",
                    (lost["id"],),
                )
                db.execute(
                    "UPDATE found_items SET status = 'possible_match' WHERE id = ? AND status = 'active'",
                    (found["id"],),
                )
    db.commit()


def browse_items(kind: str) -> list[sqlite3.Row]:
    table = f"{kind}_items"
    filters = []
    params: list[str] = []
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    color = request.args.get("color", "").strip()
    location = request.args.get("location", "").strip()
    item_date = request.args.get("item_date", "").strip()
    if query:
        filters.append("(item_name LIKE ? OR category LIKE ? OR color LIKE ? OR location LIKE ? OR item_date LIKE ?)")
        params.extend([f"%{query}%"] * 5)
    if category:
        filters.append("category = ?")
        params.append(category)
    if color:
        filters.append("color LIKE ?")
        params.append(f"%{color}%")
    if location:
        filters.append("location LIKE ?")
        params.append(f"%{location}%")
    if item_date:
        filters.append("item_date = ?")
        params.append(item_date)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    return query_db(
        f"""SELECT i.*, u.full_name AS reporter_name
            FROM {table} i JOIN users u ON u.id = i.user_id
            {where} ORDER BY i.created_at DESC""",
        tuple(params),
    )


app = create_app()
app.teardown_appcontext(close_db)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)