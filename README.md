# Multi-Language Translator

A real-time chat app where everyone in a room writes and reads in their own language.
Messages, join/leave notices and shared PDF files are translated automatically for each member.

Built with Flask, Flask-SocketIO, deep-translator (Google Translate) and pypdf.

## Run it in VS Code

1. Install Python 3.10 or newer and the VS Code **Python** extension.
2. Open this folder in VS Code: **File → Open Folder…**
3. Open a terminal: **Terminal → New Terminal**, then create a virtual environment and install the packages.

   Windows:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

   macOS / Linux:
   ```
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and change `SECRET_KEY` to any long random text.
5. Start the server, either with `python app.py` in the terminal or by pressing **F5**
   (the included launch configuration runs `app.py`).
6. Open http://localhost:5000 in your browser.

To test with two users, open a second browser or a private window, create a room in one,
and join with the room code in the other using a different language. Other devices on the
same Wi-Fi can join through `http://<your-computer's-IP>:5000`.

Translation needs an internet connection, since it goes through Google Translate.

## How it works

| Step | What happens |
|---|---|
| Create room | Server generates a unique 4-letter code and stores the room in memory |
| Join room | Name, language and room code are saved in the Flask session |
| Connect | Socket.IO joins the room; earlier chat history is sent translated into the newcomer's language |
| Message | Source language is auto-detected; the text is translated once per language in the room and sent to each member privately |
| PDF | pypdf extracts the text, it is translated per language, saved as `<name>_<lang>.txt` and each member gets a download link |
| Leave | Remaining members see a translated "has left the room" notice; when the last member leaves, the room and its files are deleted |

## Project structure

```
app.py               Flask server, routes and Socket.IO events
templates/
  base.html          Shared page layout
  home.html          Name, language and room code form
  room.html          Chat room and client-side Socket.IO code
static/
  styles.css         Styling
  socket.io.min.js   Socket.IO client (bundled, no CDN needed)
requirements.txt     Python packages
render.yaml          Deployment settings for Render
.env.example         Environment variable template
```

## Deploy to Render

1. Push this folder to a GitHub repository.
2. On render.com, choose **New → Blueprint** and select the repository. Render reads `render.yaml`,
   installs the requirements and starts the app with gunicorn.

Keep it to one worker (`-w 1`, as in `render.yaml`): rooms live in memory, so
multiple workers would each have their own separate set of rooms.

## Improvements over the original version

- Uses **deep-translator** instead of the unstable `googletrans` package.
- Long PDFs are split into chunks under Google Translate's 5,000-character limit.
- Each message is translated once per language, not once per member.
- Messages are displayed as plain text, closing a script-injection hole.
- Translated files are only downloadable by members of that room, and are deleted with the room.
- File size and type are validated; a translation failure falls back to the original text instead of crashing.

## Limitations

- Rooms are held in memory, so restarting the server clears them.
- The free Google Translate endpoint can be rate-limited under heavy use.
- Scanned (image-only) PDFs have no extractable text.
