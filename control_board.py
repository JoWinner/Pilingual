
"""Run the local πlingual control board and persist session events."""
import argparse
import ctypes
import json
import os
import sqlite3
import sys
import webbrowser
from contextlib import closing, contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from threading import Event, Lock, Thread
from time import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

UI = Path(__file__).resolve().with_name("board")
DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Pi-lingual"
DEFAULTS = {"mode": "dub", "language": "en", "model": "base", "seconds": 20,
            "pause": 0.9, "denoise": False}
INSTANCE = {"app": "pilingual-control-board", "version": 1}


def validate_settings(values):
    if not isinstance(values, dict) or values.keys() - DEFAULTS.keys():
        raise ValueError("Unknown session settings.")
    settings = DEFAULTS | values
    for field, choices in [("mode", ("dub", "transcribe")), ("language", ("en", "zh")),
                           ("model", ("base", "small"))]:
        if settings[field] not in choices:
            raise ValueError(f"Unsupported {field}.")
    seconds, pause = settings["seconds"], settings["pause"]
    if type(seconds) is not int or not 2 <= seconds <= 25:
        raise ValueError("Phrase limit must be an integer from 2 to 25 seconds.")
    if type(pause) not in (float, int) or not 0.3 <= pause <= 3:
        raise ValueError("Pause must be between 0.3 and 3 seconds.")
    if type(settings["denoise"]) is not bool:
        raise ValueError("Noise cleanup must be on or off.")
    return settings


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL,
                    task TEXT NOT NULL, status TEXT NOT NULL, settings TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, session INTEGER NOT NULL REFERENCES sessions(id),
                    at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS session_events ON events (session, id);
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    def create(self, settings, task="session"):
        with self.connect() as db:
            return db.execute(
                "INSERT INTO sessions (started, task, status, settings) VALUES (?, ?, ?, ?)",
                (time(), task, "in progress", json.dumps(settings)),
            ).lastrowid

    def finish(self, session, status):
        with self.connect() as db:
            db.execute("UPDATE sessions SET status = ?, ended = ? WHERE id = ?",
                       (status, time(), session))

    def event(self, session, kind, **detail):
        with self.connect() as db:
            db.execute("INSERT INTO events (session, at, kind, detail) VALUES (?, ?, ?, ?)",
                       (session, time(), kind, json.dumps(detail, ensure_ascii=False)))

    @staticmethod
    def session(row):
        return dict(row) | {"settings": json.loads(row["settings"])}

    def recent(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 50").fetchall()
        return [self.session(row) for row in rows]

    def report(self, session, *, recent_only=False):
        with self.connect() as db:
            record = db.execute("SELECT * FROM sessions WHERE id = ?", (session,)).fetchone()
            if record is None:
                return None
            if recent_only:
                rows = db.execute("""
                    SELECT * FROM events WHERE session = ? ORDER BY id DESC LIMIT 121
                """, (session,)).fetchall()
                truncated = len(rows) > 120
                rows = rows[:120]
                rows.reverse()
            else:
                truncated = False
                rows = db.execute("""
                    SELECT * FROM events WHERE session = ? ORDER BY id
                """, (session,)).fetchall()
            delays = [row[0] for row in db.execute("""
                SELECT json_extract(detail, '$.delay') FROM events
                WHERE session = ? AND kind = 'playback'
            """, (session,))]
            phrases = db.execute("""
                SELECT count(*) FROM events WHERE session = ? AND kind = 'phrase'
            """, (session,)).fetchone()[0]
            opened = db.execute("""
                SELECT 1 FROM events WHERE session = ? AND kind IN ('ready', 'check') LIMIT 1
            """, (session,)).fetchone() is not None
        return {
            "session": self.session(record),
            "truncated": truncated,
            "opened": opened,
            "metrics": {"phrases": phrases, "dubbed": len(delays),
                        "median_delay": median(delays) if delays else None},
            "events": [json.loads(row["detail"]) | {"kind": row["kind"], "at": row["at"],
                       "id": row["id"]} for row in rows],
        }


class Conflict(ValueError):
    pass


class Board:
    def __init__(self, store):
        self.store = store
        self.lock = Lock()
        self.current = None
        self.closing = False

    def start(self, settings, task):
        with self.lock:
            if self.closing:
                raise Conflict("The board is closing.")
            if self.current is not None:
                raise Conflict("A session or audio check is already running.")
            session = self.store.create(settings, task)
            self.current = {"id": session, "stop": Event(), "level": 0, "muted": False,
                            "status": "checking" if task == "check" else "starting"}
            Thread(target=self.run, args=(session, settings, task, self.current["stop"]),
                   daemon=True).start()
            return session

    def stop(self):
        with self.lock:
            if self.current is None:
                raise Conflict("There is no running session.")
            self.current["status"] = "stopping"
            self.current["stop"].set()

    def run(self, session, settings, task, stopped):
        def emit(kind, **detail):
            with self.lock:
                if kind == "level":
                    if detail["route"] != "You hear":
                        self.current["level"] = detail["level"]
                        self.current["muted"] = detail["suppressed"]
                    return
                self.store.event(session, kind, **detail)
                if kind == "ready" and not stopped.is_set():
                    self.current["status"] = "listening"

        status = "failed"
        try:
            from airpods_bench import find_endpoint
            from airpods_call import check_audio, run_call, run_transcription, select_routes, single_session
            from airpods_translate import open_input
            from sounddevice import WasapiSettings

            with single_session():
                emit("start", message=("Checking Windows audio streams." if task == "check"
                                       else "Opening audio and loading offline models."))
                if stopped.is_set():
                    status = "stopped"
                    return
                dubbing = settings["mode"] == "dub"
                device = select_routes() if dubbing else find_endpoint("input")[0]
                if task == "check":
                    if dubbing:
                        check_audio(device)
                    else:
                        with open_input(device, WasapiSettings(auto_convert=True)):
                            pass
                    status = "passed"
                    emit("check", message=("All four audio streams opened together." if dubbing
                                           else "The AirPods microphone opened successfully."))
                else:
                    if dubbing:
                        check_audio(device)
                    options = {key: value for key, value in settings.items() if key != "mode"}
                    runner = run_call if dubbing else run_transcription
                    runner(device, **options, stopped=stopped, emit=emit)
                    status = "stopped"
                    emit("stop", message="Session stopped. Transcript and activity saved locally.")
        except Exception as error:
            emit("error", message=str(error) or type(error).__name__)
        finally:
            with self.lock:
                try:
                    self.store.finish(session, status)
                finally:
                    self.current = None

    def state(self, selected, *, full=False):
        with self.lock:
            active = {key: value for key, value in self.current.items() if key != "stop"} if self.current else None
            recent = self.store.recent()
            session = selected or (active["id"] if active else (recent[0]["id"] if recent else None))
            report = self.store.report(session, recent_only=not full) if session else None
        return {"active": active, "recent": recent, "report": report, "defaults": DEFAULTS}

    def busy(self):
        with self.lock:
            return self.current is not None

    def close(self):
        with self.lock:
            if self.current is not None:
                raise Conflict("Stop the session before closing the board.")
            self.closing = True


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        pass

    def reply(self, status, body, mime, *, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, value, *, filename=None):
        self.reply(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", filename=filename)

    @property
    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def verify_host(self):
        if self.headers.get("Host") != self.host:
            self.json(HTTPStatus.FORBIDDEN, {"error": "Open the board on its local address."})
            return False
        return True

    def do_GET(self):
        if not self.verify_host():
            return
        url = urlsplit(self.path)
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/board.css": ("board.css", "text/css; charset=utf-8"),
                 "/board.js": ("board.js", "text/javascript; charset=utf-8"),
                 "/earbuds.png": ("earbuds.png", "image/png")}
        if url.path in files:
            name, mime = files[url.path]
            self.reply(HTTPStatus.OK, (UI / name).read_bytes(), mime)
            return
        if url.path == "/api/instance":
            self.json(HTTPStatus.OK, INSTANCE)
            return
        try:
            query = parse_qs(url.query)
            selected = int(query["session"][0]) if "session" in query else None
            if selected is not None and not 0 < selected < 2**63:
                raise ValueError
        except ValueError:
            self.json(HTTPStatus.BAD_REQUEST, {"error": "Invalid session."})
            return
        if url.path == "/api/state":
            self.json(HTTPStatus.OK, self.server.board.state(selected, full=query.get("full") == ["1"]))
        elif url.path == "/api/report" and selected:
            report = self.server.board.store.report(selected)
            if report:
                self.json(HTTPStatus.OK, report, filename=f"pilingual-session-{selected}.json")
            else:
                self.json(HTTPStatus.NOT_FOUND, {"error": "Session not found."})
        else:
            self.json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

    def do_POST(self):
        if not self.verify_host():
            return
        if self.headers.get("Origin") != "http://" + self.host:
            self.json(HTTPStatus.FORBIDDEN, {"error": "Start this action from the control board."})
            return
        if self.headers.get_content_type() != "application/json":
            self.json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Expected JSON settings."})
            return
        board = self.server.board
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length))
            if self.path in ("/api/start", "/api/check"):
                task = "check" if self.path == "/api/check" else "session"
                session = board.start(validate_settings(body), task)
                self.json(HTTPStatus.ACCEPTED, {"session": session})
            elif self.path == "/api/stop":
                board.stop()
                self.json(HTTPStatus.ACCEPTED, {})
            elif self.path == "/api/quit":
                board.close()
                self.json(HTTPStatus.OK, {})
                Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.json(HTTPStatus.NOT_FOUND, {"error": "Action not found."})
        except Conflict as error:
            self.json(HTTPStatus.CONFLICT, {"error": str(error)})
        except ValueError as error:
            self.json(HTTPStatus.BAD_REQUEST, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    args = parser.parse_args()
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        try:
            with urlopen(url + "/api/instance", timeout=2) as response:
                if json.loads(response.read(512)) == INSTANCE and not args.no_browser:
                    webbrowser.open(url)
                    return
        except (OSError, URLError, ValueError):
            pass
        raise
    with server:
        server.board = Board(Store(args.data_dir / "control-board.sqlite3"))
        url = f"http://127.0.0.1:{server.server_port}"
        print(url, flush=True)
        if not args.no_browser:
            Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            if server.board.busy():
                server.board.stop()
                while server.board.busy():
                    Event().wait(0.1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if Path(sys.executable).stem.lower() == "pythonw":
            ctypes.windll.user32.MessageBoxW(None, str(error), "πlingual Control Board", 0x10)
        else:
            raise

