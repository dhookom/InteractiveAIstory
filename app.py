"""
Interactive AI Storytelling Web Game — Flask backend with Gemini.
"""
import json
import logging
import os
import re

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

# Logging: show in console; level INFO in prod, DEBUG when Flask debug is on
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Prefer a model with free-tier quota (2.0-flash often has limit 0 on free tier)
GEMINI_MODEL = "gemini-2.5-flash"


def get_client():
    """Return a configured Gemini client (lazy init to avoid missing key at import)."""
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=api_key)


def _call_gemini(client, full_prompt: str) -> str:
    """One Gemini generate_content call. Returns response text or raises."""
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
    )
    if response and getattr(response, "text", None):
        return response.text.strip()
    logger.warning("Gemini returned empty or no text; response=%s", response)
    return ""


def _is_rate_limit(err: Exception) -> bool:
    """True if the exception is a 429 / quota exhausted error."""
    msg = str(err).upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "QUOTA" in msg


def generate(genre: str, user_prompt: str, system_hint: str = "") -> str:
    """Single helper for all Gemini calls. Builds prompt with genre and returns model text."""
    import time
    try:
        client = get_client()
        full_prompt = (
            f"You are a narrative engine for an interactive story. Genre: {genre}. "
            f"{system_hint}\n\n{user_prompt}"
        )
        logger.debug("Calling Gemini model=%s genre=%s prompt_len=%d", GEMINI_MODEL, genre, len(full_prompt))
        try:
            return _call_gemini(client, full_prompt)
        except Exception as first_err:
            if _is_rate_limit(first_err):
                wait_sec = 45
                logger.warning("Rate limit (429) hit; waiting %ds then retrying once.", wait_sec)
                time.sleep(wait_sec)
                return _call_gemini(client, full_prompt)
            raise
    except Exception as e:
        logger.exception("Story engine (Gemini) error: %s", e)
        raise RuntimeError(f"Story engine error: {e}") from e


def parse_character_response(raw: str) -> dict:
    """Parse Gemini's character reply into {name, personality}. Tolerates markdown and minor variations."""
    raw = raw.strip()
    # Try to extract JSON block if present
    match = re.search(r"\{[^{}]*\"name\"[^{}]*\"personality\"[^{}]*\}", raw, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            obj = json.loads(match.group())
            return {
                "name": obj.get("name", "Hero"),
                "personality": obj.get("personality", "Brave and curious."),
            }
        except json.JSONDecodeError:
            pass
    # Fallback: look for "name" and "personality" lines or similar
    name = "Hero"
    personality = raw
    for line in raw.split("\n"):
        if "name" in line.lower() and ":" in line:
            name = line.split(":", 1)[-1].strip().strip('"')
        if "personality" in line.lower() and ":" in line:
            personality = line.split(":", 1)[-1].strip().strip('"')
    return {"name": name, "personality": personality}


def error_response(message: str, status: int, detail: str | None = None):
    """JSON error with optional detail for debugging."""
    body = {"error": message}
    if detail is not None:
        body["detail"] = detail
    return jsonify(body), status


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/suggest-character", methods=["POST"])
def suggest_character():
    """POST body: { theme } -> { name, personality }."""
    try:
        data = request.get_json() or {}
        theme = (data.get("theme") or "adventure").strip() or "adventure"
        logger.info("suggest_character theme=%r", theme)
        user_prompt = (
            f"Given genre: {theme}. Suggest a single protagonist: "
            'full name and one sentence personality. Reply only with valid JSON: {{"name": "...", "personality": "..."}}.'
        )
        text = generate(theme, user_prompt)
        result = parse_character_response(text)
        logger.debug("suggest_character result name=%r", result.get("name"))
        return jsonify(result)
    except ValueError as e:
        logger.warning("suggest_character ValueError: %s", e)
        return error_response(str(e), 400)
    except RuntimeError as e:
        detail = str(e)
        logger.error("suggest_character RuntimeError: %s", detail, exc_info=True)
        return error_response(
            "Story engine is busy; try again.",
            503,
            detail=detail if app.debug else None,
        )


@app.route("/api/start-story", methods=["POST"])
def start_story():
    """POST body: { theme, characterName, characterPersonality } -> { opening }."""
    try:
        data = request.get_json() or {}
        theme = (data.get("theme") or "adventure").strip() or "adventure"
        name = (data.get("characterName") or "Hero").strip()
        personality = (data.get("characterPersonality") or "").strip()
        logger.info("start_story theme=%r character=%r", theme, name)
        user_prompt = (
            f"Character: {name}. Personality: {personality}. "
            "Write exactly 2 short paragraphs: (1) a tranquil setting where the character is. "
            "(2) A sudden disruption (event or danger). No dialogue from the narrator; set the scene only."
        )
        opening = generate(theme, user_prompt)
        return jsonify({"opening": opening or "Something begins..."})
    except RuntimeError as e:
        detail = str(e)
        logger.error("start_story RuntimeError: %s", detail, exc_info=True)
        return error_response(
            "Story engine is busy; try again.",
            503,
            detail=detail if app.debug else None,
        )


@app.route("/api/continue-story", methods=["POST"])
def continue_story():
    """POST body: { theme, characterName, characterPersonality, storySoFar, userAction } -> { segment }."""
    try:
        data = request.get_json() or {}
        theme = (data.get("theme") or "adventure").strip() or "adventure"
        name = (data.get("characterName") or "Hero").strip()
        personality = (data.get("characterPersonality") or "").strip()
        story_so_far = (data.get("storySoFar") or "").strip()
        user_action = (data.get("userAction") or "").strip()
        if not user_action:
            return error_response("No action provided.", 400)
        logger.info("continue_story theme=%r character=%r action=%r", theme, name, user_action[:50])
        user_prompt = (
            f"Story so far:\n{story_so_far}\n\n"
            f"Player action: {user_action}\n\n"
            "Write the next narrative segment (2–4 sentences) that results from this action. "
            "Then briefly describe the new situation so the player can choose another action."
        )
        segment = generate(
            theme,
            user_prompt,
            system_hint=f"Character: {name}. Personality: {personality}. Stay in genre.",
        )
        return jsonify({"segment": segment or "The story continues..."})
    except RuntimeError as e:
        detail = str(e)
        logger.error("continue_story RuntimeError: %s", detail, exc_info=True)
        return error_response(
            "Story engine is busy; try again.",
            503,
            detail=detail if app.debug else None,
        )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
