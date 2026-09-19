"""Multi-Language Translator: a real-time multilingual chat server.

Users create or join a room and pick a language. The server relays each
message (and the text of each shared PDF) to everyone in the room, and each
person's browser translates it into their own language.

Translation normally runs in the browser, so every user's requests go to
Google from their own internet connection. Shared cloud hosts such as Render
are often rate-limited by Google; if a browser can't translate, it asks this
server to try instead.
"""

import logging
import os
import random
import secrets
import threading
from functools import lru_cache
from io import BytesIO
from string import ascii_uppercase, digits

import requests
from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from pypdf import PdfReader

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mlt")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

MAX_PDF_BYTES = 10 * 1024 * 1024       # largest PDF accepted
MAX_PDF_CHARS = 200_000                # longest PDF text relayed to the room
MAX_MESSAGE_CHARS = 2000               # longest chat message accepted
CHUNK_CHARS = 1500                     # keeps each server translation request small
ROOM_GRACE_SECONDS = int(os.environ.get("ROOM_GRACE_SECONDS", 120))  # how long an empty room survives

socketio = SocketIO(app, async_mode="threading", max_http_buffer_size=MAX_PDF_BYTES + 1024 * 1024)

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
    """Split long text into chunks under the limit, preferring line breaks."""
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


# ------------------------------------------------- server-side translation
# Used only when a browser can't translate on its own.

GTX_URL = "https://translate.googleapis.com/translate_a/single"
HTTP = requests.Session()
HTTP.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
last_translation_error = {"error": None}


def translate_gtx(text, dest):
    """Google Translate's public JSON endpoint."""
    resp = HTTP.post(GTX_URL, params={"client": "gtx", "sl": "auto", "tl": dest, "dt": "t"},
                     data={"q": text}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return "".join(part[0] for part in data[0] if part and part[0])


def translate_deep(text, dest):
    """Backup: deep-translator's Google Translate web scraper."""
    return GoogleTranslator(source="auto", target=dest).translate(text)


@lru_cache(maxsize=4096)
def translate_chunk(text, dest):
    errors = []
    for engine in (translate_gtx, translate_deep):
        try:
            result = engine(text, dest)
            if result:
                return result
            errors.append(f"{engine.__name__}: empty result")
        except Exception as exc:
            errors.append(f"{engine.__name__}: {type(exc).__name__}: {exc}")
    raise RuntimeError(" | ".join(errors))


def translate(text, dest):
    """Translate text into `dest`, auto-detecting the source. Raises on failure."""
    if not text or not text.strip():
        return text
    try:
        result = "".join(translate_chunk(chunk, dest) for chunk in split_text(text))
        last_translation_error["error"] = None
        return result
    except Exception as exc:
        last_translation_error["error"] = str(exc)[:500]
        log.warning("Server translation to %s failed: %s", dest, exc)
        raise


# ------------------------------------------------------------ room cleanup

def delete_room(room):
    rooms.pop(room, None)
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


@app.route("/translate-test")
def translate_test():
    """Check whether the SERVER can reach Google Translate.

    On shared hosts this often fails with 429 (Too Many Requests). That is
    fine: browsers translate for themselves and only fall back to the server.
    """
    results = {}
    for engine in (translate_gtx, translate_deep):
        try:
            results[engine.__name__] = engine("Hello, how is your day going?", "de")
        except Exception as exc:
            results[engine.__name__] = f"FAILED: {type(exc).__name__}: {exc}"[:300]
    return {"input": "Hello, how is your day going?", "target": "de", "server_results": results,
            "last_server_error": last_translation_error["error"],
            "note": "Server failures are OK: translation runs in each user's browser first."}


# ---------------------------------------------------------- socket events

@socketio.on("connect")
def connect(auth=None):
    room = session.get("room")
    name = session.get("name")
    if not room or not name:
        return False
    with rooms_lock:
        if room not in rooms:
            return False
        join_room(room)
        rooms[room]["members"][request.sid] = {
            "name": name, "language": session.get("language"), "user_id": session.get("user_id")}
        history = list(rooms[room]["messages"])

    # Earlier chat history for the newcomer; their browser translates it.
    for message in history:
        emit("message", {"name": message["name"], "message": message["message"]}, to=request.sid)

    emit("message", {"name": name, "message": "has entered the room", "notice": True}, to=room)
    log.info("%s joined room %s (%s)", name, room, session.get("language"))


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
    emit("message", {"name": session.get("name"), "message": text}, to=room)


@socketio.on("translate")
def handle_translate(data):
    """Fallback translation for a browser that couldn't reach Google itself."""
    text = str((data or {}).get("text", ""))[:20000]
    dest = (data or {}).get("dest") or session.get("language") or "en"
    if dest not in LANGUAGE_CODES:
        return {"ok": False, "error": "unknown language"}
    try:
        return {"ok": True, "text": translate(text, dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@socketio.on("pdf_file")
def handle_pdf_file(data):
    room = session.get("room")
    if room not in rooms:
        return
    content = (data or {}).get("content")
    filename = os.path.basename(str((data or {}).get("filename", "")))

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

    emit("status", {"message": ""})
    emit("pdf_message", {"name": session.get("name"), "filename": filename,
                         "text": text[:MAX_PDF_CHARS]}, to=room)
    log.info("%s shared %s in room %s", session.get("name"), filename, room)


@socketio.on("disconnect")
def disconnect(reason=None):
    room = session.get("room")
    name = session.get("name")
    leave_room(room)
    with rooms_lock:
        if room not in rooms:
            return
        rooms[room]["members"].pop(request.sid, None)
        if not rooms[room]["members"]:
            log.info("Room %s is empty; deleting in %s seconds unless someone rejoins", room, ROOM_GRACE_SECONDS)
            socketio.start_background_task(delete_room_if_still_empty, room)
            return

    emit("message", {"name": name, "message": "has left the room", "notice": True}, to=room)
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
