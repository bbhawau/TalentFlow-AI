import os
import json
from functools import wraps
from pathlib import Path

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)

app.config["SECRET_KEY"] = get_app_config()["app_secret"]
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
)

AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")
DB_SECRET_ARN = os.getenv("DB_SECRET_ARN")
APP_SECRET_ARN = os.getenv("APP_SECRET_ARN")

secrets_client = boto3.client(
    "secretsmanager",
    region_name=AWS_REGION
)


def get_secret(secret_arn):
    response = secrets_client.get_secret_value(
        SecretId=secret_arn
    )
    return json.loads(response["SecretString"])


def get_app_config():
    if not APP_SECRET_ARN:
        raise RuntimeError("APP_SECRET_ARN is not configured")

    return get_secret(APP_SECRET_ARN)

def get_db_connection():
    if not DB_SECRET_ARN:
        raise RuntimeError("DB_SECRET_ARN is not configured")

    credentials = get_secret(DB_SECRET_ARN)

    return psycopg2.connect(
        host=credentials["host"],
        database=os.getenv(
            "DB_NAME",
            credentials.get("dbname", "talentflow")
        ),
        user=credentials["username"],
        password=credentials["password"],
        port=credentials.get("port", 5432),
        connect_timeout=5,
        sslmode="require"
    )


def query_db(query, values=None, fetch=False):
    connection = get_db_connection()

    try:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            cursor.execute(query, values or ())

            if fetch:
                result = cursor.fetchall()
                return [dict(row) for row in result]

            if cursor.description:
                result = cursor.fetchone()
                return dict(result) if result else None

            connection.commit()
            return None

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def initialize_database():
    schema_path = Path(__file__).parent / "schema.sql"

    schema = schema_path.read_text(encoding="utf-8")

    connection = get_db_connection()

    try:
        with connection.cursor() as cursor:
            cursor.execute(schema)

        config = get_app_config()

        email = config.get("bootstrap_email")
        password = config.get("bootstrap_password")

        if email and password:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users (email, password_hash, role)
                    VALUES (%s, %s, 'recruiter')
                    ON CONFLICT (email) DO NOTHING
                    """,
                    (
                        email.lower(),
                        generate_password_hash(password)
                    )
                )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def recruiter_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Authentication required"}), 401

        if session.get("role") != "recruiter":
            return jsonify({"error": "Access denied"}), 403

        return function(*args, **kwargs)

    return wrapper


@app.route("/health", methods=["GET"])
def health():
    try:
        query_db("SELECT 1")
        return jsonify({
            "status": "healthy",
            "database": "connected"
        }), 200

    except Exception as error:
        app.logger.exception("Health check failed")
        return jsonify({
            "status": "unhealthy",
            "database": "disconnected"
        }), 503


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    try:
        user = query_db(
            "SELECT * FROM users WHERE email = %s",
            (email,)
        )

        if not user or not check_password_hash(
            user["password_hash"], password
        ):
            return jsonify({"error": "Invalid login details"}), 401

        session.clear()
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        session["email"] = user["email"]

        return jsonify({
            "message": "Login successful",
            "user": {
                "id": user["id"],
                "email": user["email"],
                "role": user["role"]
            }
        })

    except Exception:
        app.logger.exception("Login failed")
        return jsonify({"error": "Unable to log in"}), 500


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully"})


@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    try:
        jobs = query_db(
            "SELECT * FROM jobs ORDER BY created_at DESC",
            fetch=True
        )
        return jsonify(jobs)

    except Exception:
        app.logger.exception("Unable to retrieve jobs")
        return jsonify({"error": "Unable to retrieve jobs"}), 500


@app.route("/api/jobs", methods=["POST"])
@recruiter_required
def create_job():
    data = request.get_json(silent=True) or {}

    title = data.get("title", "").strip()

    if not title:
        return jsonify({"error": "Job title is required"}), 400

    try:
        job = query_db(
            """
            INSERT INTO jobs
            (title, department, location, employment_type, description)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                title,
                data.get("department"),
                data.get("location"),
                data.get("employment_type"),
                data.get("description")
            )
        )

        return jsonify(job), 201

    except Exception:
        app.logger.exception("Unable to create job")
        return jsonify({"error": "Unable to create job"}), 500


@app.route("/api/candidates", methods=["GET"])
@recruiter_required
def get_candidates():
    try:
        candidates = query_db(
            "SELECT * FROM candidates ORDER BY created_at DESC",
            fetch=True
        )
        return jsonify(candidates)

    except Exception:
        app.logger.exception("Unable to retrieve candidates")
        return jsonify({"error": "Unable to retrieve candidates"}), 500


@app.route("/api/candidates", methods=["POST"])
@recruiter_required
def create_candidate():
    data = request.get_json(silent=True) or {}

    full_name = data.get("full_name", "").strip()

    if not full_name:
        return jsonify({"error": "Candidate name is required"}), 400

    try:
        candidate = query_db(
            """
            INSERT INTO candidates (full_name, email, phone, skills)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (
                full_name,
                data.get("email"),
                data.get("phone"),
                data.get("skills")
            )
        )

        return jsonify(candidate), 201

    except Exception:
        app.logger.exception("Unable to create candidate")
        return jsonify({"error": "Unable to create candidate"}), 500


@app.route("/api/applications", methods=["POST"])
def submit_application():
    data = request.get_json(silent=True) or {}

    full_name = data.get("full_name", "").strip()
    job_id = data.get("job_id")

    if not full_name or not job_id:
        return jsonify({
            "error": "Candidate name and job ID are required"
        }), 400

    connection = get_db_connection()

    try:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            cursor.execute(
                """
                INSERT INTO candidates (full_name, email, phone, skills)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (
                    full_name,
                    data.get("email"),
                    data.get("phone"),
                    data.get("skills")
                )
            )

            candidate_id = cursor.fetchone()["id"]

            cursor.execute(
                """
                INSERT INTO applications
                (candidate_id, job_id, cover_letter)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (
                    candidate_id,
                    job_id,
                    data.get("cover_letter")
                )
            )

            application = dict(cursor.fetchone())

        connection.commit()

        return jsonify(application), 201

    except Exception:
        connection.rollback()
        app.logger.exception("Application submission failed")
        return jsonify({"error": "Unable to submit application"}), 400

    finally:
        connection.close()


@app.route("/api/applications", methods=["GET"])
@recruiter_required
def get_applications():
    try:
        applications = query_db(
            """
            SELECT
                applications.*,
                candidates.full_name,
                candidates.email,
                jobs.title AS job_title
            FROM applications
            JOIN candidates
                ON applications.candidate_id = candidates.id
            JOIN jobs
                ON applications.job_id = jobs.id
            ORDER BY applications.created_at DESC
            """,
            fetch=True
        )

        return jsonify(applications)

    except Exception:
        app.logger.exception("Unable to retrieve applications")
        return jsonify({"error": "Unable to retrieve applications"}), 500


@app.route("/api/interviews", methods=["GET"])
@recruiter_required
def get_interviews():
    try:
        interviews = query_db(
            """
            SELECT
                interviews.*,
                candidates.full_name,
                jobs.title AS job_title
            FROM interviews
            JOIN candidates
                ON interviews.candidate_id = candidates.id
            JOIN jobs
                ON interviews.job_id = jobs.id
            ORDER BY interviews.interview_date
            """,
            fetch=True
        )

        return jsonify(interviews)

    except Exception:
        app.logger.exception("Unable to retrieve interviews")
        return jsonify({"error": "Unable to retrieve interviews"}), 500


@app.route("/api/interviews", methods=["POST"])
@recruiter_required
def create_interview():
    data = request.get_json(silent=True) or {}

    required_fields = [
        "candidate_id",
        "job_id",
        "interview_date"
    ]

    if any(not data.get(field) for field in required_fields):
        return jsonify({"error": "Missing interview information"}), 400

    try:
        interview = query_db(
            """
            INSERT INTO interviews
            (candidate_id, job_id, interview_date, interview_type, notes)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                data["candidate_id"],
                data["job_id"],
                data["interview_date"],
                data.get("interview_type"),
                data.get("notes")
            )
        )

        return jsonify(interview), 201

    except Exception:
        app.logger.exception("Unable to create interview")
        return jsonify({"error": "Unable to create interview"}), 400


# Create the tables and initial recruiter account when the app starts.
initialize_database()