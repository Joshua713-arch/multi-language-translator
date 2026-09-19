"""Multi-Language Translator: a real-time multilingual chat server.

Users create or join a room, pick a language, and every message (and every
shared PDF) is delivered to each member translated into their own language.
"""

import logging
import os
import random
import re
import secrets
import shutil
import threading
from functools import lru_cache
from string import ascii_uppercase, digits

from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from flask import (Flask, abort, redirect, render_template, request,
                   send_from_directory, session, url_for)
from flask_socketio import SocketIO, emit, join_room, leave_room
from pypdf import PdfReader
from io import BytesIO

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mlt")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

MAX_PDF_BYTES = 10 * 1024 * 1024       # largest PDF accepted
MAX_MESSAGE_CHARS = 2000               # longest chat message accepted
CHUNK_CHARS = 4500                     # Google Translate rejects requests over 5000 chars
ROOM_GRACE_SECONDS = int(os.environ.get("ROOM_GRACE_SECONDS", 120))  # how long an empty room survives

socketio = SocketIO(app, async_mode="threading", max_http_buffer_size=MAX_PDF_BYTES + 1024 * 1024)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(BASE_DIR, "translated_files")
os.makedirs(FILES_DIR, exist_ok=True)

# name -> code, e.g. {"english": "en", "twi": "ak", ...}
LANGUAGES = GoogleTranslator().get_supported_languages(as_dict=True)
LANGUAGE_CODES = set(LANGUAGES.values())

# rooms[code] = {"members": {sid: {"name", "language", "user_id"}}, "messages": [{"name", "message"}]}
rooms = {}
rooms_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def generate_unique_code(length=4):
    while True:
        code = "".join(random.choice(ascii_uppercase) for _ in range(length))
        if code not in rooms:
            return code


def generate_unique_id(length=8):
    return "".join(random.choice(ascii_uppercase + digits) for _ in range(length))


def split_text(text, limit=CHUNK_CHARS):
    """Split long text into chunks under the API limit, preferring line breaks."""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:                      # a single very long line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


@lru_cache(maxsize=4096)
def translate_chunk(text, dest):
    return GoogleTranslator(source="auto", target=dest).translate(text) or text


def translate(text, dest):
    """Translate text into `dest`, auto-detecting the source. Falls back to the original on failure."""
    if not text or not text.strip():
        return text
    try:
        return "".join(translate_chunk(chunk, dest) for chunk in split_text(text))
    except Exception as exc:                          # network error, rate limit, etc.
        log.warning("Translation to %s failed: %s", dest, exc)
        return text


def translate_for_members(text, members):
    """Translate once per distinct language in the room; return {language: translation}."""
    return {lang: translate(text, lang) for lang in {m["language"] for m in members.values()}}


def room_dir(room):
    return os.path.join(FILES_DIR, room)


def safe_filename(name):
    name = os.path.basename(name or "document.pdf")
    stem = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "document"
    return stem[:80]


def delete_room(room):
    rooms.pop(room, None)
    shutil.rmtree(room_dir(room), ignore_errors=True)
    log.info("Room %s deleted", room)


def delete_room_if_still_empty(room):
    """Wait, then delete the room only if nobody has (re)joined it.

    A short grace period means a page refresh, a dropped connection, or the
    moment between creating a room and the room page connecting does not
    destroy the room.
    """
    socketio.sleep(ROOM_GRACE_SECONDS)
    with rooms_lock:
        if room in rooms and not rooms[room]["members"]:
            delete_room(room)


# ----------------------------------------------------------------- routes

@app.route("/", methods=["POST", "GET"])
def home():
    session.clear()
    languages = sorted(LANGUAGES.items())
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:40]
        code = (request.form.get("code") or "").strip().upper()
        language = request.form.get("language") or "en"
        join = request.form.get("join", False)
        create = request.form.get("create", False)
        ctx = dict(code=code, name=name, language=language, languages=languages)

        if not name:
            return render_template("home.html", error="Please enter a name!", **ctx)
        if language not in LANGUAGE_CODES:
            return render_template("home.html", error="Please choose a language!", **ctx)
        if join is not False and not code:
            return render_template("home.html", error="Please enter a room code!", **ctx)

        with rooms_lock:
            room = code
            if create is not False:
                room = generate_unique_code(4)
                rooms[room] = {"members": {}, "messages": []}
                log.info("Room %s created by %s", room, name)
            elif code not in rooms:
                log.info("Join failed: %s tried code %r; open rooms: %s", name, code, sorted(rooms) or "none")
                return render_template("home.html", error="Room does not exist.", **ctx)

        session["room"] = room
        session["name"] = name
        session["language"] = language
        session["user_id"] = generate_unique_id()
        return redirect(url_for("room"))

    return render_template("home.html", languages=languages, language="en")


@app.route("/room")
def room():
    room = session.get("room")
    if room is None or session.get("name") is None or room not in rooms:
        return redirect(url_for("home"))
    return render_template("room.html", code=room)


@app.route("/files/<room>/<path:filename>")
def download_file(room, filename):
    # Only members of the room can download its files.
    if session.get("room") != room or room not in rooms:
        abort(404)
    return send_from_directory(room_dir(room), filename, as_attachment=True)


# ---------------------------------------------------------- socket events

@socketio.on("connect")
def connect(auth=None):
    room = session.get("room")
    name = session.get("name")
    language = session.get("language")
    if not room or not name:
        return False
    with rooms_lock:
        if room not in rooms:
            return False
        join_room(room)
        rooms[room]["members"][request.sid] = {
            "name": name, "language": language, "user_id": session.get("user_id")}
        history = list(rooms[room]["messages"])
        members = dict(rooms[room]["members"])

    # Earlier chat history, translated for the newcomer.
    for message in history:
        emit("message", {"name": message["name"],
                         "message": translate(message["message"], language)}, to=request.sid)

    notices = translate_for_members("has entered the room", members)
    for sid, member in members.items():
        emit("message", {"name": name, "message": notices[member["language"]], "notice": True}, to=sid)
    log.info("%s joined room %s (%s)", name, room, language)


@socketio.on("message")
def handle_message(data):
    room = session.get("room")
    text = str((data or {}).get("data", "")).strip()[:MAX_MESSAGE_CHARS]
    if not text:
        return
    with rooms_lock:
        if room not in rooms:
            return
        rooms[room]["messages"].append({"name": session.get("name"), "message": text})
        members = dict(rooms[room]["members"])

    translations = translate_for_members(text, members)
    for sid, member in members.items():
        emit("message", {"name": session.get("name"),
                         "message": translations[member["language"]]}, to=sid)


@socketio.on("pdf_file")
def handle_pdf_file(data):
    room = session.get("room")
    if room not in rooms:
        return
    content = (data or {}).get("content")
    filename = (data or {}).get("filename", "")

    if not isinstance(content, (bytes, bytearray)) or not filename.lower().endswith(".pdf"):
        emit("server_error", {"message": "Please attach a .pdf file."})
        return
    if len(content) > MAX_PDF_BYTES:
        emit("server_error", {"message": "That PDF is too large (10 MB maximum)."})
        return

    try:
        reader = PdfReader(BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:
        log.warning("PDF read failed: %s", exc)
        emit("server_error", {"message": "That PDF could not be read."})
        return
    if not text:
        emit("server_error", {"message": "No text could be found in that PDF."})
        return

    emit("status", {"message": f"Translating {filename}…"})
    with rooms_lock:
        members = dict(rooms.get(room, {}).get("members", {}))
    translations = translate_for_members(text, members)

    os.makedirs(room_dir(room), exist_ok=True)
    stem = safe_filename(filename)
    file_id = generate_unique_id(6)
    for sid, member in members.items():
        lang = member["language"]
        out_name = f"{stem}_{lang}.txt"
        stored = f"{file_id}_{out_name}"
        path = os.path.join(room_dir(room), stored)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(translations[lang])
        emit("pdf_message", {"name": session.get("name"), "filename": out_name,
                             "file_url": url_for("download_file", room=room, filename=stored)}, to=sid)
    emit("status", {"message": ""})
    log.info("Translated %s for room %s into %s", filename, room, ", ".join(translations))


@socketio.on("disconnect")
def disconnect(reason=None):
    room = session.get("room")
    name = session.get("name")
    leave_room(room)
    with rooms_lock:
        if room not in rooms:
            return
        rooms[room]["members"].pop(request.sid, None)
        members = dict(rooms[room]["members"])
        if not members:
            log.info("Room %s is empty; deleting in %s seconds unless someone rejoins", room, ROOM_GRACE_SECONDS)
            socketio.start_background_task(delete_room_if_still_empty, room)
            return

    notices = translate_for_members("has left the room", members)
    for sid, member in members.items():
        emit("message", {"name": name, "message": notices[member["language"]], "notice": True}, to=sid)
    log.info("%s left room %s", name, room)


def port_in_use(port):
    """True if another program (often a second copy of this server) already holds the port.

    On Windows two servers can silently share one port; the browser is then
    sent to either of them at random, and each has its own separate rooms.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):          # Windows only
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    if port_in_use(port):
        raise SystemExit(
            f"\nPort {port} is already in use, probably by another copy of this server.\n"
            "Stop the other copy (Ctrl+C in its terminal, or the red stop button in VS Code),\n"
            f"or set a different PORT in .env, then run python app.py again.\n")
    log.info("Server starting on http://localhost:%s (process id %s)", port, os.getpid())
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    # use_reloader=False: the auto-reloader restarts the server when files change,
    # which wipes every room held in memory.
    socketio.run(app, host="0.0.0.0", port=port, debug=debug, use_reloader=False,
                 allow_unsafe_werkzeug=True)
