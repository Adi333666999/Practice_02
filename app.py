import logging
import os
import random
import string
import sys
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, request
import mysql.connector
from mysql.connector import Error

# Configure logging to stdout for container observability
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Fail-fast configuration validation
REQUIRED_ENV_VARS = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    logger.critical(f"Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ["DB_PORT"]),
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "database": os.environ["DB_NAME"],
}


def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)


def init_db(retries=5, delay=3):
    for attempt in range(1, retries + 1):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS urls (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    short_code VARCHAR(10) NOT NULL UNIQUE,
                    long_url TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            cursor.close()
            conn.close()
            logger.info("Database connection verified and 'urls' table ready.")
            return
        except Error as err:
            logger.warning(f"DB connection attempt {attempt}/{retries} failed: {err}")
            if attempt < retries:
                time.sleep(delay)
            else:
                logger.critical("Could not connect to database after maximum retries. Exiting.")
                sys.exit(1)


def is_valid_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def generate_short_code(length=6):
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


# Initialize database on module load (works with Gunicorn workers)
init_db()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


@app.route("/ready", methods=["GET"])
def ready():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        conn.close()
        return jsonify({"status": "ready"}), 200
    except Error as err:
        logger.error(f"Readiness check failed: {err}")
        return jsonify({"status": "not ready", "reason": "database unreachable"}), 503


@app.route("/shorten", methods=["POST"])
def shorten_url():
    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"error": "Missing 'url' in JSON payload"}), 400

    long_url = data["url"].strip()
    if not is_valid_url(long_url):
        logger.warning(f"Rejected invalid URL submission: {long_url}")
        return jsonify({"error": "Invalid URL. Must start with http:// or https://"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Duplicate handling: return existing short code if long_url already exists
        cursor.execute("SELECT short_code FROM urls WHERE long_url = %s LIMIT 1", (long_url,))
        existing = cursor.fetchone()
        if existing:
            cursor.close()
            conn.close()
            short_url = f"{request.host_url}{existing['short_code']}"
            logger.info(f"Duplicate URL found. Returning existing code: {existing['short_code']}")
            return jsonify({"short_code": existing["short_code"], "short_url": short_url}), 200

        # Generate unique short code
        for _ in range(5):
            code = generate_short_code()
            cursor.execute("SELECT id FROM urls WHERE short_code = %s", (code,))
            if not cursor.fetchone():
                break
        else:
            cursor.close()
            conn.close()
            return jsonify({"error": "Failed to generate unique code"}), 500

        cursor.execute("INSERT INTO urls (short_code, long_url) VALUES (%s, %s)", (code, long_url))
        conn.commit()
        cursor.close()
        conn.close()

        short_url = f"{request.host_url}{code}"
        logger.info(f"Shortened {long_url} -> {code}")
        return jsonify({"short_code": code, "short_url": short_url}), 201

    except Error as err:
        logger.error(f"Database error during /shorten: {err}")
        return jsonify({"error": "Internal database error"}), 500


@app.route("/<short_code>", methods=["GET"])
def redirect_to_url(short_code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT long_url FROM urls WHERE short_code = %s LIMIT 1", (short_code,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            logger.info(f"Redirecting {short_code} -> {result['long_url']}")
            return redirect(result["long_url"], code=302)

        logger.warning(f"Short code not found: {short_code}")
        return jsonify({"error": "Short URL not found"}), 404

    except Error as err:
        logger.error(f"Database error during redirect for {short_code}: {err}")
        return jsonify({"error": "Internal database error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
