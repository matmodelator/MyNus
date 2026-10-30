# ========================================
# MyNus 8.4.0 | Chats
# ========================================

# ========================================
# IMPORTS
# ========================================

import sys
from datetime import datetime, timezone
import os
import json
import re
import unicodedata
import glob
import shutil
import subprocess
import threading
import uuid

from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    send_file
)
from werkzeug.utils import secure_filename



# ========================================
# APPLICATION
# ========================================

app = Flask(__name__)

# The desktop application keeps its own local origin. Its session travels in a
# signed header rather than relying on third-party cookies.
from flask import session
from itsdangerous import BadSignature
DESKTOP_ORIGINS = {"https://mynus.local"}
DESKTOP_ORIGINS.update(origin.strip() for origin in os.environ.get("MYNUS_DESKTOP_ORIGINS", "").split(",") if origin.strip())

def _desktop_api_path(path):
    return path.startswith(("/api/", "/progress/", "/abort/", "/results/", "/preview/", "/debug/")) or path == "/separate"

@app.before_request
def desktop_session_and_preflight():
    origin = request.headers.get("Origin")
    if origin in DESKTOP_ORIGINS and request.method == "OPTIONS" and _desktop_api_path(request.path):
        return "", 204
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key=auth[7:]
        with db_connection() as db:
            row=db.execute('SELECT session_id,user_id FROM sessions WHERE login_key=? AND revoked_at IS NULL',(key,)).fetchone() if re.fullmatch(r'[A-Za-z0-9_-]{11}',key) else None
        if not row:
            session.clear()
            return jsonify(error='Войдите в аккаунт.',code=401),401
        session.update(user_id=row['user_id'],session_id=row['session_id'])

@app.after_request
def desktop_response_headers(response):
    origin = request.headers.get("Origin")
    if origin in DESKTOP_ORIGINS and _desktop_api_path(request.path):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Last-Event-ID, X-MyNus-Load-Trace, X-MyNus-Load-Track, X-MyNus-Client, X-MyNus-Background"
        response.headers["Access-Control-Expose-Headers"] = "X-MyNus-Session"
        response.vary.add("Origin")
    if request.path in ("/api/auth/login","/api/auth/register","/api/auth/logout","/api/current-user") and request.method != "OPTIONS":
        with db_connection() as db:
            row=db.execute('SELECT login_key FROM sessions WHERE session_id=? AND user_id=? AND revoked_at IS NULL',(session.get('session_id'),session.get('user_id'))).fetchone()
        response.headers['X-MyNus-Session']=row['login_key'] if row else ''
    return response



# Server stdout/stderr remain visible in the terminal and are mirrored to SSE.
import collections
import time
from flask import Response, stream_with_context
_server_log_condition = threading.Condition()
_server_log_records = collections.deque(maxlen=3000)
_server_log_sequence = 0

def _publish_server_line(message, level):
    global _server_log_sequence
    if "/api/admin/monitor" in message and re.search(r'HTTP/[^\"]*\"\s+(?:2\d\d|304)\b', message): return
    with _server_log_condition:
        _server_log_sequence += 1
        _server_log_records.append({"id": _server_log_sequence, "time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z"), "level": level, "message": re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)[:16000]})
        _server_log_condition.notify_all()

class _MirroredServerStream:
    def __init__(self, original, level):
        self.original, self.level = original, level
        self.pending = {}
        self.lock = threading.RLock()
    def write(self, value):
        result = self.original.write(value)
        with self.lock:
            key = threading.get_ident()
            text = self.pending.get(key, "") + str(value)
            lines = text.replace("\r", "\n").split("\n")
            self.pending[key] = lines.pop()[-16000:]
            for line in lines:
                if line.strip() and not any(path in line for path in ("/debug/server-logs","/api/admin/console","/api/device-logs","/api/current-user")):
                    _publish_server_line(line, self.level)
        return result
    def flush(self):
        self.original.flush()
    def __getattr__(self, name):
        return getattr(self.original, name)

sys.stdout = _MirroredServerStream(sys.stdout, "INFO")
sys.stderr = _MirroredServerStream(sys.stderr, "STDERR")
# Werkzeug may have created its logging handler before stderr was wrapped.
import logging
for _handler in logging.getLogger("werkzeug").handlers:
    if isinstance(_handler, logging.StreamHandler):
        _handler.setStream(sys.stderr)

@app.route("/debug/server-logs")
def server_log_stream():
    current_user_id()
    try:
        raw_cursor = request.headers.get("Last-Event-ID") or request.args.get("after") or "latest"
        with _server_log_condition:
            cursor = _server_log_sequence if raw_cursor == "latest" else max(0, int(raw_cursor))
    except ValueError:
        cursor = 0
    @stream_with_context
    def events():
        nonlocal cursor
        yield ": connected\n\n"
        while True:
            if not session_is_active760():
                return
            with _server_log_condition:
                batch = [record for record in _server_log_records if record["id"] > cursor]
                if not batch:
                    _server_log_condition.wait(timeout=15)
                    batch = [record for record in _server_log_records if record["id"] > cursor]
            if not session_is_active760():
                return
            if not batch:
                yield ": keepalive\n\n"
            for record in batch:
                cursor = record["id"]
                yield "id: " + str(cursor) + "\ndata: " + json.dumps(record, ensure_ascii=True) + "\n\n"
    response = Response(events(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    if request.headers.get("Origin") == "null" and request.remote_addr in ("127.0.0.1", "::1"):
        response.headers["Access-Control-Allow-Origin"] = "null"
    return response







BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULT_DIR = os.path.join(BASE_DIR, "results")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")
PROJECTS_DIR = os.path.join(BASE_DIR, "Projects")
PLAYLIST_STATE_PATH = os.path.join(BASE_DIR, "playlist.json")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(PROJECTS_DIR, exist_ok=True)


# ========================================
# JOB STATE
# ========================================

jobs = {}
job_controls = {}



# Arbitrary project folders selected via the Windows folder picker are exposed
# through short-lived in-memory tokens so the browser can fetch their tracks.
opened_project_folders = {}


# ========================================
# PROJECT LOAD TRACE (6.2.0)
# Diagnostic-only endpoint. It does not write, delete, move or replace files.
# ========================================

@app.route("/debug/load-trace", methods=["POST"])
def debug_load_trace():
    data = request.get_json(silent=True) or {}

    trace_id = str(data.get("trace_id") or "no-trace")
    stage = str(data.get("stage") or data.get("event") or "UNKNOWN")
    project_id = data.get("project_id")
    source = data.get("source")
    ui_mode = data.get("ui_mode")
    project_loading = data.get("project_loading")
    elapsed_ms = data.get("elapsed_ms")
    command = data.get("command")
    function = data.get("function")
    parameters = data.get("parameters")
    result = data.get("result")
    details = data.get("details")

    print("\n" + "-" * 72, flush=True)
    print(f"[LOAD TRACE][{trace_id}] {stage}", flush=True)
    print(f"  project_id      = {project_id}", flush=True)
    print(f"  source          = {source}", flush=True)
    print(f"  ui_mode         = {ui_mode}", flush=True)
    print(f"  project_loading = {project_loading}", flush=True)
    print(f"  elapsed_ms      = {elapsed_ms}", flush=True)
    if command is not None:
        print(f"  command         = {command}", flush=True)
    if function is not None:
        print(f"  function        = {function}", flush=True)
    if parameters is not None:
        print(f"  parameters      = {parameters}", flush=True)
    if result is not None:
        print(f"  result          = {result}", flush=True)
    if details is not None:
        print(f"  details         = {details}", flush=True)
    print("-" * 72, flush=True)

    return jsonify({"ok": True, "version": "7.9.0", "trace_id": trace_id})


# ========================================
# CLIENT CONSOLE -> SERVER CMD
# ========================================

# ========================================
# INDEX
# ========================================




from werkzeug.exceptions import HTTPException
@app.errorhandler(HTTPException)
def json_http_error(error):
    message = "Sign in or register to continue." if error.code == 401 else error.description
    return jsonify(error=message, code=error.code), error.code


@app.route("/", methods=["GET"])
def server_status():
    return jsonify(service="MyNus Server", version="7.9.0", status="ready")


# ========================================
# START SEPARATION
# ========================================

@app.route("/separate", methods=["POST"])
def separate():

    uid = current_user_id()

    if "audio" not in request.files:

        return jsonify({
            "error": "Audio file not received"
        }), 400


    audio = request.files["audio"]

    if audio.filename == "":

        return jsonify({
            "error": "Audio file not selected"
        }), 400


    job_id = str(uuid.uuid4())

    with db_connection() as db:
        separation_id = db.execute(
            """INSERT INTO separations
               (user_id,client_request_id,source_kind,source_name,status,started_at,session_id)
               VALUES (?,?,'local',?,'running',?,?)""",
            (uid, "job:" + job_id, audio.filename, utc_now(),session.get("session_id")),
        ).lastrowid
        write_log(db, uid, "separation.started", audio.filename, sid=separation_id)

    job_upload_dir = os.path.join(
        UPLOAD_DIR,
        job_id
    )

    job_result_dir = os.path.join(
        RESULT_DIR,
        job_id
    )

    os.makedirs(
        job_upload_dir,
        exist_ok=True
    )

    os.makedirs(
        job_result_dir,
        exist_ok=True
    )


    filename = secure_filename(
        audio.filename
    )

    if not filename:
        filename = "audio.mp3"


    input_path = os.path.join(
        job_upload_dir,
        filename
    )

    audio.save(
        input_path
    )


    jobs[job_id] = {
        "user_id": uid,
        "progress": 0,
        "status": "processing",
        "vocals": None,
        "drums": None,
        "bass": None,
        "guitar": None,
        "piano": None,
        "other": None,
        "vocal_start": None,
        "vocal_end": None,
        "error": None
    }

    job_controls[job_id] = {
        "lock": threading.RLock(), "stop_lock": threading.Lock(),
        "cancel": threading.Event(), "finished": threading.Event(), "process": None,
    }

    lyrics_language = request.form.get("lyrics_language", "auto")

    thread = threading.Thread(
        target=run_demucs,
        args=(
            job_id,
            input_path,
            job_result_dir,
            lyrics_language
        ),
        daemon=True
    )
    thread.start()

    threading.Thread(
        target=watch_separation,
        args=(job_id, uid, separation_id),
        daemon=True,
    ).start()

    return jsonify({
        "job_id": job_id,
        "separation_id": separation_id,
        "user_id": uid
    })


# ========================================
# VOCAL START\END DETECTION
# ========================================

# Локальная серверная операция этого блока.
def detect_vocal_range(
    vocals_path, job_id
):

    command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        vocals_path,
        "-af",
        "silencedetect=noise=-38dB:d=0.35",
        "-f",
        "null",
        "-"
    ]

    output = _run_job_command(job_id, command, capture_output=True)

    silence_ends = [
        float(value)
        for value in re.findall(
            r"silence_end:\s*([0-9.]+)",
            output
        )
    ]


    silence_starts = [
        float(value)
        for value in re.findall(
            r"silence_start:\s*([0-9.]+)",
            output
        )
    ]


    vocal_start = (
        silence_ends[0]
        if silence_ends
        else 0.0
    )


    vocal_end = None


    for value in silence_starts:

        if value > vocal_start:
            vocal_end = value


    return (
        max(
            0.0,
            vocal_start
        ),
        vocal_end
    )

# ========================================
# WHISPERX LYRICS
# ========================================

# Локальная серверная операция этого блока.
def detect_lyrics(vocal_path, lyrics_language="auto"):

    import whisperx
    import torch

    device = (
        "cuda"
        if torch.cuda.is_available()
        else "cpu"
    )

    compute_type = (
        "float16"
        if device == "cuda"
        else "int8"
    )

    print(
        f"WhisperX: {device}, "
        f"{compute_type}"
    )

    audio = whisperx.load_audio(
        vocal_path
    )

    requested_language = str(
        lyrics_language or "auto"
    ).strip().lower()

    language = (
        None
        if requested_language == "auto"
        else requested_language
    )

    model = whisperx.load_model(
        "small",
        device,
        compute_type=compute_type,
        language=language
    )

    result = model.transcribe(
        audio,
        batch_size=4
    )

    language = (
        result.get("language")
        if requested_language == "auto"
        else requested_language
    ) or "ru"

    if language not in {
        "ru",
        "en",
        "es",
        "it",
        "fr",
        "uk"
    }:

        language = "en"


    align_model, metadata = (
        whisperx.load_align_model(
            language_code=language,
            device=device
        )
    )


    result = whisperx.align(
        result["segments"],
        align_model,
        metadata,
        audio,
        device,
        return_char_alignments=False
    )

    words = []

    for segment in result["segments"]:

        for word in segment.get(
            "words",
            []
        ):

            if (
                "start" not in word
                or
                "end" not in word
            ):
                continue

            words.append({
                "word":
                    word.get(
                        "word",
                        ""
                    ).strip(),

                "start":
                    round(
                        float(
                            word["start"]
                        ),
                        3
                    ),

                "end":
                    round(
                        float(
                            word["end"]
                        ),
                        3
                    )
            })

    text = " ".join(
        segment.get(
            "text",
            ""
        ).strip()

        for segment
        in result["segments"]
    ).strip()

    return {
        "language": language,
        "text": text,
        "words": words
    }


# ========================================
# TONIC DETECTION
# ========================================

def detect_tonic(audio_path):
    import librosa
    import numpy as np

    notes = [
        "C", "C#", "D", "D#", "E", "F",
        "F#", "G", "G#", "A", "A#", "B"
    ]

    major_profile = np.array([
        6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
        2.52, 5.19, 2.39, 3.66, 2.29, 2.88
    ])

    minor_profile = np.array([
        6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
        2.54, 4.75, 3.98, 2.69, 3.34, 3.17
    ])

    y, sr = librosa.load(
        audio_path,
        sr=11025,
        mono=True
    )

    harmonic = librosa.effects.harmonic(y)

    chroma = librosa.feature.chroma_stft(
        y=harmonic,
        sr=sr
    )

    chroma_mean = np.mean(chroma, axis=1)

    best_score = float("-inf")
    best_tonic = None

    for tonic in range(12):
        for profile in (major_profile, minor_profile):

            rotated = np.roll(profile, tonic)

            score = np.corrcoef(
                chroma_mean,
                rotated
            )[0, 1]

            if np.isfinite(score) and score > best_score:
                best_score = score
                best_tonic = tonic

    return notes[best_tonic] if best_tonic is not None else None


# ========================================
# DEMUCS PROCESS
# ========================================

# Локальная серверная операция этого блока.
def _stop_job_process(process):
    """Stop the stage process AND its children (Demucs, ffmpeg). Never stop Flask."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode and process.poll() is None:
            raise RuntimeError("Could not stop job process tree: " + result.stderr.strip())
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)

class SeparationAborted(Exception):
    pass


def _check_job_cancelled(job_id):
    if job_controls[job_id]["cancel"].is_set():
        raise SeparationAborted()


def _run_job_command(job_id, command, on_line=None, capture_output=False):
    """Run one stage. The active process belongs only to this job_id."""
    control = job_controls[job_id]
    process = None
    output = []
    options = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   text=True, encoding="utf-8", errors="replace", bufsize=1,
                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        with control["lock"]:
            _check_job_cancelled(job_id)
            process = subprocess.Popen(command, **options)
            control["process"] = process
        for line in process.stdout:
            if capture_output:
                output.append(line)
            if on_line:
                on_line(line)
            elif not capture_output:
                print(line, end="", flush=True)
        code = process.wait()
        _check_job_cancelled(job_id)
        if code != 0:
            raise RuntimeError("Separation stage failed (exit " + str(code) + ")")
        return "".join(output)
    finally:
        if process is not None:
            # Do not leave a model running after a Python-side parsing/error path.
            if process.poll() is None:
                try:
                    with control["stop_lock"]:
                        _stop_job_process(process)
                except Exception:
                    app.logger.exception("Could not stop stage for job %s", job_id)
                    # Retain the handle while alive, so /abort can retry.
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
            with control["lock"]:
                if control["process"] is process:
                    control["process"] = None


def _run_whisperx(job_id, vocal_path, lyrics_language):
    import inspect
    result = None
    # Only the WhisperX function is executed in a fresh Python process.
    # No server import, no Flask/DB initialization, no extra .py file.
    source = inspect.getsource(detect_lyrics)
    script = ("import json, sys\n" + source +
              "\nresult = detect_lyrics(sys.argv[1], sys.argv[2])\n"
              "print('MYNUS_LYRICS ' + json.dumps(result, ensure_ascii=True), flush=True)\n")

    def read_line(line):
        nonlocal result
        if line.startswith("MYNUS_LYRICS "):
            result = json.loads(line[len("MYNUS_LYRICS "):])
        else:
            print(line, end="", flush=True)

    _run_job_command(job_id, [sys.executable, "-u", "-c", script, vocal_path, lyrics_language],
                     on_line=read_line)
    if not isinstance(result, dict):
        raise RuntimeError("WhisperX returned no lyrics result")
    return result



def check_initial_lyrics_spelling(lyrics):
    # One whole-text check per separation, never on project load or editor render.
    import language_tool_python
    words = lyrics.get("words") or []
    text = " ".join(str(word.get("word") or "") for word in words)
    language = str(lyrics.get("language") or "ru").split("-")[0]
    if not text.strip():
        return
    supported = {"ru": "ru-RU", "en": "en-US", "fr": "fr", "de": "de-DE", "es": "es", "it": "it", "uk": "uk"}
    if language not in supported:
        lyrics["initial_spellcheck"] = {"status": "unsupported", "language": language}
        return
    tool = None
    try:
        tool = language_tool_python.LanguageTool(supported[language])
        matches = tool.check(text)
        # LanguageTool positions use UTF-16 code units, like JavaScript strings.
        ranges = []
        position = 0
        for word in words:
            value = str(word.get("word") or "")
            length = len(value.encode("utf-16-le")) // 2
            ranges.append((position, position + length, word))
            position += length + 1
        for match_index, match in enumerate(matches):
            offset = int(match.offset)
            limit = offset + int(match.error_length)
            # Only word spelling issues can be safely replaced without changing phrasing.
            if str(getattr(match, "rule_issue_type", "")) != "misspelling":
                continue
            for start, end, word in ranges:
                if offset < end and limit > start:
                    word.setdefault("spell_issues", []).append({
                        "id": str(match_index), "checked_word": word.get("word", ""),
                        "message": str(match.message), "replacements": list(match.replacements or [])[:8],
                        "status": "pending"
                    })
        lyrics["initial_spellcheck"] = {"status": "done", "language": language}
        print("[SPELLCHECK] One initial check completed", flush=True)
    except Exception as exc:
        lyrics["initial_spellcheck"] = {"status": "failed", "message": str(exc)}
        print("[SPELLCHECK] Initial check failed: " + str(exc), flush=True)
    finally:
        if tool is not None:
            tool.close()

def run_demucs(job_id, input_path, job_result_dir, lyrics_language="auto"):
    control = job_controls[job_id]
    result = None
    error_message = None
    try:
        def demucs_line(line):
            print(line, end="", flush=True)
            matches = re.findall(r"(\d{1,3})%", line)
            if matches:
                with control["lock"]:
                    if not control["cancel"].is_set():
                        jobs[job_id]["progress"] = max(0, min(100, int(matches[-1])))

        _run_job_command(job_id, [sys.executable, "-m", "demucs", "-n", "htdemucs_6s",
                                  "--mp3", "-o", job_result_dir, input_path], on_line=demucs_line)
        _check_job_cancelled(job_id)
        model_dir = os.path.join(job_result_dir, "htdemucs_6s")
        song_dirs = sorted(name for name in os.listdir(model_dir)
                           if os.path.isdir(os.path.join(model_dir, name)))
        if not song_dirs:
            raise RuntimeError("Demucs output not found")
        stems = ("vocals", "drums", "bass", "guitar", "piano", "other")
        for stem in stems:
            _check_job_cancelled(job_id)
            source_path = os.path.join(model_dir, song_dirs[0], stem + ".mp3")
            if not os.path.isfile(source_path):
                raise RuntimeError("Demucs stem not found: " + stem)
            shutil.copy(source_path, os.path.join(job_result_dir, stem + ".mp3"))

        vocal_path = os.path.join(job_result_dir, "vocals.mp3")
        vocal_start, vocal_end = detect_vocal_range(vocal_path, job_id)

        _check_job_cancelled(job_id)

        try:
            tonic = detect_tonic(input_path)
            print(f"[TONIC] {tonic}", flush=True)
        except Exception as exc:
            tonic = None
            print(f"[TONIC] Detection failed: {exc}", flush=True)


        lyrics = _run_whisperx(job_id, vocal_path, lyrics_language)
        check_initial_lyrics_spelling(lyrics)
        prepare_initial_transcription(lyrics)
        result = {stem: f"/results/{job_id}/{stem}.mp3" for stem in stems}
        result.update(
            vocal_start=vocal_start,
            vocal_end=vocal_end,
            lyrics=lyrics,
            tonic=tonic
        )
        try:
            create_preview780(job_id,input_path,result,job_result_dir)
            result['preview_ready']=True
        except SeparationAborted:
            raise
        except Exception as preview_error:
            result['preview_ready']=False
            print('[PREVIEW] '+str(preview_error),flush=True)
        Path(job_result_dir, "result.json").write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
    except SeparationAborted:
        pass
    except Exception as exc:
        error_message = str(exc)
        app.logger.exception("Separation failed: %s", job_id)
    finally:
        # Every stage has exited before publishing its final state.
        with control["lock"]:
            if control["cancel"].is_set():
                jobs[job_id].update(status="aborted", error=None)
            elif error_message:
                jobs[job_id].update(status="error", error=error_message)
            elif result is not None:
                jobs[job_id].update(result)
                jobs[job_id].update(status="done", progress=100, error=None)
            else:
                jobs[job_id].update(status="error", error="Separation returned no result")
            control["finished"].set()



# ========================================
# PROCESS PROGRESS
# ========================================



@app.route("/abort/<job_id>", methods=["POST"])
def abort_separation(job_id):
    uid = current_user_id()
    job = jobs.get(job_id)
    control = job_controls.get(job_id)
    if job is None or control is None or job.get("user_id") != uid:
        return jsonify(error="Job not found"), 404
    with control["lock"]:
        if job["status"] in ("done", "error", "aborted"):
            return jsonify(job_id=job_id, status=job["status"])
        control["cancel"].set()
        job["status"] = "aborting"
        process = control["process"]
    try:
        # Repeated clicks/requests serialize termination for this one job only.
        with control["stop_lock"]:
            _stop_job_process(process)
    except Exception as exc:
        return jsonify(error=str(exc), job_id=job_id, status="aborting"), 500
    if not control["finished"].wait(timeout=2):
        return jsonify(job_id=job_id, status="aborting"), 202
    with control["lock"]:
        return jsonify(job_id=job_id, status=job["status"])

@app.route("/progress/<job_id>")
def progress(job_id):
    return jsonify(progress_payload760(job_id,current_user_id()))


# ========================================
# RESULT FILES
# ========================================

@app.route(
    "/results/<job_id>/<filename>"
)
# Локальная серверная операция этого блока.
def result_file(job_id,filename):
    abort(503,description='Result service is not initialized.')


# ========================================
# AUDIO EXPORT
# ========================================

# Локальная серверная операция этого блока.
def find_ffmpeg():

    ffmpeg = shutil.which(
        "ffmpeg"
    )


    if ffmpeg:

        return ffmpeg


    local_app_data = os.environ.get(
        "LOCALAPPDATA",
        ""
    )


    candidates = [

        os.path.join(
            local_app_data,
            "Microsoft",
            "WinGet",
            "Links",
            "ffmpeg.exe"
        )

    ]


    package_pattern = os.path.join(
        local_app_data,
        "Microsoft",
        "WinGet",
        "Packages",
        "*FFmpeg*",
        "**",
        "ffmpeg.exe"
    )


    candidates.extend(
        glob.glob(
            package_pattern,
            recursive=True
        )
    )


    for candidate in candidates:

        if os.path.isfile(
            candidate
        ):

            return candidate


    return None


@app.route(
    "/export-audio",
    methods=["POST"]
)
# Локальная серверная операция этого блока.
def export_audio():

    if "audio" not in request.files:

        return jsonify({
            "error":
                "Audio file not received"
        }), 400


    audio = request.files[
        "audio"
    ]


    export_format = (
        request.form
        .get(
            "format",
            ""
        )
        .lower()
    )


    allowed_formats = {
        "wav",
        "mp3",
        "flac",
        "m4a"
    }


    if export_format not in allowed_formats:

        return jsonify({
            "error":
                "Unsupported export format"
        }), 400


    track_name = secure_filename(
        request.form.get(
            "track",
            "audio"
        )
    )


    start_value = request.form.get(
        "start"
    )


    end_value = request.form.get(
        "end"
    )


    start_time = None
    end_time = None


    try:

        if start_value is not None:

            start_time = float(
                start_value
            )


        if end_value is not None:

            end_time = float(
                end_value
            )


    except ValueError:

        return jsonify({
            "error":
                "Invalid selection time"
        }), 400


    if (
        start_time is not None
        and
        end_time is not None
        and
        end_time <= start_time
    ):

        return jsonify({
            "error":
                "Invalid selection range"
        }), 400


    ffmpeg = find_ffmpeg()


    if not ffmpeg:

        return jsonify({
            "error":
                "FFmpeg not found"
        }), 500


    export_id = str(
        uuid.uuid4()
    )


    export_job_dir = os.path.join(
        EXPORT_DIR,
        export_id
    )


    os.makedirs(
        export_job_dir,
        exist_ok=True
    )


    incoming_name = secure_filename(
        audio.filename
        or
        "audio.bin"
    )


    extension = os.path.splitext(
        incoming_name
    )[1]


    if not extension:

        extension = ".bin"


    input_path = os.path.join(
        export_job_dir,
        "input"
        + extension
    )


    output_path = os.path.join(
        export_job_dir,
        (
            track_name
            or
            "audio"
        )
        + "."
        + export_format
    )


    audio.save(
        input_path
    )


    command = [
        ffmpeg,
        "-y"
    ]


    if start_time is not None:

        command += [
            "-ss",
            f"{start_time:.6f}"
        ]


    command += [
        "-i",
        input_path
    ]


    if (
        start_time is not None
        and
        end_time is not None
    ):

        command += [
            "-t",
            f"{(
                end_time
                - start_time
            ):.6f}"
        ]


    if export_format == "wav":

        command += [
            "-c:a",
            "pcm_s24le"
        ]


    elif export_format == "mp3":

        command += [
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k"
        ]


    elif export_format == "flac":

        command += [
            "-c:a",
            "flac"
        ]


    elif export_format == "m4a":

        command += [
            "-c:a",
            "aac",
            "-b:a",
            "256k"
        ]


    command.append(
        output_path
    )


    print(
        "FFmpeg command:",
        command
    )


    try:

        process = subprocess.run(

            command,

            capture_output=True,

            text=True
        )


        if process.returncode != 0:

            print(
                process.stdout
            )


            print(
                process.stderr
            )


            return jsonify({
                "error":
                    "FFmpeg export failed",
                "details":
                    process.stderr
            }), 500


        return send_file(

            output_path,

            as_attachment=True,

            download_name=(
                (
                    track_name
                    or
                    "audio"
                )
                + "."
                + export_format
            )

        )


    except Exception as error:

        print(error)


        return jsonify({
            "error":
                str(error)
        }), 500




# ========================================
# LYRICS AUTOFIX
# ========================================

SUPPORTED_LYRICS_LANGUAGES = {"ru","en","es","it","fr","uk"}

# Локальная серверная операция этого блока.
def _fallback_language(text):
    value = str(text or "").strip()
    if re.search(r"[ІіЇїЄєҐґ]", value):
        return "uk"
    if re.search(r"[А-Яа-яЁё]", value):
        return "ru"

    lower = " " + value.lower() + " "
    markers = {
        "it":[" che "," non "," per "," sono "," amore "," mio "," mia "],
        "es":[" que "," para "," soy "," amor "," corazón "," eres "],
        "fr":[" je "," pas "," pour "," avec "," amour "," suis "," mon "],
        "en":[" the "," i "," you "," and "," with "," love "," my "," is "]
    }
    scores = {
        lang: sum(lower.count(x) for x in words)
        for lang,words in markers.items()
    }
    return max(scores,key=scores.get) if max(scores.values()) else "en"

# Локальная серверная операция этого блока.
def detect_lyrics_line_language(text):
    value = str(text or "").strip()
    if not value:
        return "ru"
    if re.search(r"[ІіЇїЄєҐґ]", value):
        return "uk"
    if re.search(r"[А-Яа-яЁё]", value):
        return "ru"
    try:
        from langdetect import detect
        language = detect(value)
        if language in SUPPORTED_LYRICS_LANGUAGES:
            return language
    except Exception:
        pass
    return _fallback_language(value)

@app.route("/detect-lyrics-languages", methods=["POST"])
# Локальная серверная операция этого блока.
def detect_lyrics_languages():
    data = request.get_json(silent=True) or {}
    lines = data.get("lines", [])
    if not isinstance(lines,list):
        return jsonify({"error":"Invalid lyrics lines"}),400
    return jsonify({
        "languages":[detect_lyrics_line_language(line) for line in lines]
    })

_WORDS = {
"en":{"i":"ай","you":"ю","your":"йор","me":"ми","my":"май","we":"уи",
"they":"зэй","he":"хи","she":"ши","the":"зэ","and":"энд","with":"уиз",
"love":"лав","baby":"бэйби","heart":"харт","night":"найт","day":"дэй",
"time":"тайм","life":"лайф","world":"уёрлд","never":"нэвэр","want":"уонт",
"know":"ноу","think":"синк","feel":"фил","see":"си","go":"гоу",
"come":"кам","stay":"стэй","leave":"лив","lose":"луз","home":"хоум",
"don't":"доунт","can't":"кэнт","won't":"воунт"},
"it":{"io":"ио","tu":"ту","non":"нон","che":"кэ","per":"пэр","con":"кон",
"amore":"аморэ","mio":"мио","mia":"миа","sono":"соно","sei":"сэй",
"vita":"вита","cuore":"куорэ","notte":"ноттэ","giorno":"джорно"},
"es":{"yo":"йо","tu":"ту","no":"но","que":"кэ","para":"пара","con":"кон",
"amor":"амор","mi":"ми","soy":"сой","eres":"эрэс","vida":"вида",
"corazón":"корасон","noche":"ночэ","día":"диа"},
"fr":{"je":"жё","tu":"тю","il":"иль","elle":"эль","nous":"ну","vous":"ву",
"pas":"па","que":"кё","pour":"пур","avec":"авэк","amour":"амур",
"mon":"мон","ma":"ма","suis":"сюи","vie":"ви","cœur":"кёр",
"nuit":"нюи","jour":"жур"}
}

_RULES = {
"en":[("tion","шн"),("igh","ай"),("oo","у"),("ee","и"),("ea","и"),
("ai","эй"),("ay","эй"),("oa","оу"),("ow","оу"),("ou","ау"),
("ch","ч"),("sh","ш"),("th","з"),("ph","ф"),("ng","нг")],
"it":[("gli","льи"),("gn","нь"),("chi","ки"),("che","ке"),
("ci","чи"),("ce","че"),("gi","джи"),("ge","дже")],
"es":[("ll","й"),("ñ","нь"),("ch","ч"),("qu","к"),("j","х")],
"fr":[("eau","о"),("au","о"),("ou","у"),("oi","уа"),("ch","ш"),
("gn","нь"),("ph","ф"),("qu","к"),("ai","э")]
}

_CHARS = {
"a":"а","à":"а","á":"а","â":"а","ä":"а","b":"б","c":"к","ç":"с",
"d":"д","e":"э","è":"э","é":"э","ê":"э","ë":"э","f":"ф","g":"г",
"h":"х","i":"и","ì":"и","í":"и","î":"и","ï":"и","j":"ж","k":"к",
"l":"л","m":"м","n":"н","o":"о","ò":"о","ó":"о","ô":"о","ö":"о",
"p":"п","q":"к","r":"р","s":"с","t":"т","u":"у","ù":"у","ú":"у",
"û":"у","ü":"у","v":"в","w":"у","x":"кс","y":"й","z":"з"
}

# Локальная серверная операция этого блока.
def _latin_word(word,language):
    original=word
    value=word.lower()
    dictionary=_WORDS.get(language,{})
    if value in dictionary:
        result=dictionary[value]
    else:
        result=value
        for source,target in _RULES.get(language,[]):
            result=result.replace(source,target)
        result="".join(_CHARS.get(ch,ch) for ch in result)
    if original.isupper(): return result.upper()
    if original[:1].isupper(): return result[:1].upper()+result[1:]
    return result

# Локальная серверная операция этого блока.
def _uk_to_ru(text):
    result=str(text or "")
    for a,b in [("ї","йи"),("Ї","Йи"),("є","йэ"),("Є","Йэ"),
                ("і","и"),("І","И"),("ґ","г"),("Ґ","Г"),
                ("и","ы"),("И","Ы")]:
        result=result.replace(a,b)
    return result

# Локальная серверная операция этого блока.
def transcribe_line_to_ru(text,language):
    value=str(text or "")
    language=str(language or "").lower()
    if language=="ru": return value
    if language=="uk": return _uk_to_ru(value)
    pattern=re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’][A-Za-zÀ-ÖØ-öø-ÿ]+)?")
    return pattern.sub(lambda m:_latin_word(m.group(0),language),value)

def prepare_initial_transcription(lyrics):
    """Create an independent RU text once, after recognition and spelling check."""
    if "initial_transcription" in lyrics:
        return
    import copy
    try:
        ru_words = copy.deepcopy(lyrics.get("words", []))
        for word in ru_words:
            source = str(word.get("word", ""))
            language = lyrics.get("language") or detect_lyrics_line_language(source)
            word["word"] = transcribe_line_to_ru(source, language)
            for key in ("spell_issues", "source_word", "ru_word"):
                word.pop(key, None)
        lyrics["ru_transcription"] = {"language": "ru", "words": ru_words,
            "text": " ".join(w["word"] for w in ru_words)}
        lyrics["initial_transcription"] = {"status": "done"}
    except Exception as exc:
        lyrics["initial_transcription"] = {"status": "failed", "error": str(exc)}
        print(f"[TRANSCRIPTION] {exc}", flush=True)


# ========================================
# SERVER START
# ========================================



# ========================================
# MYNUS PlayList JSON state + standalone Projects | 6.2.0
# ========================================
def _project_id(value):
    value = secure_filename(str(value or "Project")) or "Project"
    return value[:120]


# Работа с сохранённым Project.
def _project_name_from_folder(folder, fallback):
    manifest_path = os.path.join(folder, "Project.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        return manifest.get("name") or fallback
    except Exception:
        return fallback


# Работа с состоянием Play List.
def _empty_playlist_state():
    return {"current": None, "queue": [], "history": []}


# Работа с состоянием Play List.
def _normalize_playlist_state(raw):
    state = _empty_playlist_state()
    if isinstance(raw, dict):
        current = raw.get("current")
        state["current"] = _project_id(current) if current else None
        for key in ("queue", "history"):
            values = raw.get(key)
            if isinstance(values, list):
                state[key] = [_project_id(value) for value in values if value]
    return state


# Работа с состоянием Play List.
def _write_playlist_state(state):
    state = _normalize_playlist_state(state)
    temp_path = PLAYLIST_STATE_PATH + ".saving"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(temp_path, PLAYLIST_STATE_PATH)
    return state


# Работа с состоянием Play List.
def _read_playlist_state():
    if not os.path.isfile(PLAYLIST_STATE_PATH):
        return _write_playlist_state(_empty_playlist_state())
    try:
        with open(PLAYLIST_STATE_PATH, "r", encoding="utf-8") as fh:
            return _normalize_playlist_state(json.load(fh))
    except Exception as exc:
        print(f"[PLAYLIST] invalid playlist.json, reset: {exc}", flush=True)
        return _write_playlist_state(_empty_playlist_state())


# Работа с сохранённым Project.
def _saved_project_folder(project_id):
    return os.path.join(PROJECTS_DIR, _project_id(project_id))


# Работа с сохранённым Project.
def _require_saved_project(project_id):
    project_id = _project_id(project_id)
    folder = _saved_project_folder(project_id)
    if not os.path.isdir(folder) or not os.path.isfile(os.path.join(folder, "Project.json")):
        raise FileNotFoundError("Project not found in Projects")
    return project_id, folder


# Работа с сохранённым Project.
def _set_current_project(project_id, source="projects", mode=None):
    project_id, _ = _require_saved_project(project_id)
    source = str(source or "projects").lower()
    mode = str(mode or "").lower()
    state = _read_playlist_state()

    old_current = state.get("current")

    if mode == "open":
        # Open Project starts a fresh Play List context:
        # only the opened Project is current (0); queue/history are empty.
        state = _empty_playlist_state()
        state["current"] = project_id
        _write_playlist_state(state)
        print(
            f"[PLAYLIST] OPEN PROJECT | current={project_id!r} | queue/history reset",
            flush=True
        )
        return project_id

    if mode == "takeover":
        # Emergency manual takeover from the visible Play List.
        # The interrupted current Track is not completed: it becomes +1.
        if source == "playlist":
            try:
                state["queue"].remove(project_id)
            except ValueError:
                pass

        if old_current and old_current != project_id:
            # Keep one immediate continuation copy at +1.
            state["queue"] = [item for item in state["queue"] if item != old_current]
            state["queue"].insert(0, old_current)
    else:
        # Normal LOAD / Karaoke Next: previous current becomes history.
        if old_current and old_current != project_id:
            state["history"].append(old_current)

        if source == "playlist":
            try:
                state["queue"].remove(project_id)
            except ValueError:
                pass

    state["current"] = project_id
    _write_playlist_state(state)
    print(
        f"[PLAYLIST] LOAD | current={project_id!r} | source={source} | mode={mode or 'normal'}",
        flush=True
    )
    return project_id


# Работа с состоянием Play List.
def _playlist_projects_payload():
    state = _read_playlist_state()
    projects = []

    history = state.get("history", [])
    history_count = len(history)
    for index, project_id in enumerate(history):
        folder = _saved_project_folder(project_id)
        projects.append({
            "id": project_id,
            "name": _project_name_from_folder(folder, project_id),
            "status": "executed",
            "source": "history",
            "position": index - history_count
        })

    for index, project_id in enumerate(state.get("queue", []), start=1):
        folder = _saved_project_folder(project_id)
        projects.append({
            "id": project_id,
            "name": _project_name_from_folder(folder, project_id),
            "status": "waiting",
            "source": "playlist",
            "position": index
        })

    current = state.get("current")
    current_name = None
    if current:
        current_name = _project_name_from_folder(_saved_project_folder(current), current)

    return {
        "projects": projects,
        "current": current,
        "current_name": current_name,
        "state_file": PLAYLIST_STATE_PATH
    }


@app.route("/projects", methods=["GET"])
# Работа с сохранённым Project.
def list_projects():
    return jsonify(_playlist_projects_payload())


@app.route("/projects/use", methods=["POST"])
# Работа с сохранённым Project.
def use_project():
    data = request.get_json(silent=True) or {}
    source = str(data.get("source") or "projects").lower()
    mode = str(data.get("mode") or "").lower()
    project_id = data.get("id")
    if source == "history":
        # History is a chronological log of completed/previous currents.
        # Loading an old item does not erase the historical occurrence.
        source = "history"
    try:
        current_id = _set_current_project(project_id, source, mode)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True, "current": current_id, **_playlist_projects_payload()})


@app.route("/playlist/add", methods=["POST"])
# Работа с состоянием Play List.
def playlist_add():
    data = request.get_json(silent=True) or {}
    try:
        project_id, _ = _require_saved_project(data.get("id"))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404

    state = _read_playlist_state()
    queue = state["queue"]
    raw_position = data.get("position")
    try:
        position = int(raw_position) if raw_position is not None else len(queue) + 1
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid queue position"}), 400
    position = max(1, min(position, len(queue) + 1))
    queue.insert(position - 1, project_id)
    _write_playlist_state(state)
    print(f"[PLAYLIST] ADD | {project_id!r} -> +{position}", flush=True)
    return jsonify({"ok": True, **_playlist_projects_payload()})


@app.route("/playlist/move", methods=["POST"])
# Работа с состоянием Play List.
def playlist_move():
    data = request.get_json(silent=True) or {}
    state = _read_playlist_state()
    queue = state["queue"]
    try:
        from_position = int(data.get("from_position"))
        to_position = int(data.get("to_position"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid queue position"}), 400
    if not (1 <= from_position <= len(queue) and 1 <= to_position <= len(queue)):
        return jsonify({"error": "Queue position out of range"}), 400
    item = queue.pop(from_position - 1)
    queue.insert(to_position - 1, item)
    _write_playlist_state(state)
    print(f"[PLAYLIST] MOVE | +{from_position} -> +{to_position}", flush=True)
    return jsonify({"ok": True, **_playlist_projects_payload()})


@app.route("/playlist/delete", methods=["POST"])
# Работа с состоянием Play List.
def playlist_delete():
    data = request.get_json(silent=True) or {}
    section = str(data.get("section") or "queue").lower()
    state = _read_playlist_state()

    if section == "queue":
        try:
            position = int(data.get("position"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid queue position"}), 400
        if not 1 <= position <= len(state["queue"]):
            return jsonify({"error": "Queue position out of range"}), 400
        removed = state["queue"].pop(position - 1)
    elif section == "history":
        try:
            position = int(data.get("position"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid history position"}), 400
        index = len(state["history"]) + position
        if position >= 0 or not 0 <= index < len(state["history"]):
            return jsonify({"error": "History position out of range"}), 400
        removed = state["history"].pop(index)
    else:
        return jsonify({"error": "Unknown playlist section"}), 400

    _write_playlist_state(state)
    print(f"[PLAYLIST] DELETE | section={section} | project={removed!r}", flush=True)
    return jsonify({"ok": True, **_playlist_projects_payload()})


# Работа с сохранённым Project.
def _project_payload_from_folder(folder, track_url_builder):
    project_path = os.path.join(folder, "Project.json")
    lyrics_path = os.path.join(folder, "Lyrics.json")
    if not os.path.isfile(project_path):
        raise FileNotFoundError("Project.json not found in selected folder")
    if not os.path.isfile(lyrics_path):
        raise FileNotFoundError("Lyrics.json not found in selected folder")

    with open(project_path, "r", encoding="utf-8") as fh:
        project = json.load(fh)
    with open(lyrics_path, "r", encoding="utf-8") as fh:
        lyrics = json.load(fh)

    tracks = {}
    for track_id, filename in (project.get("tracks") or {}).items():
        tracks[track_id] = track_url_builder(filename) if filename else None

    return project, lyrics, tracks


@app.route("/projects/open-folder", methods=["POST"])
# Работа с сохранённым Project.
def open_project_folder():
    """Windows folder picker used by Karaoke -> Load another project."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="Select MyNus project folder",
            initialdir=PROJECTS_DIR
        )
        root.destroy()

        if not selected:
            return jsonify({"ok": True, "cancelled": True})

        selected = os.path.abspath(selected)
        token = uuid.uuid4().hex
        project, lyrics, tracks = _project_payload_from_folder(
            selected,
            lambda filename: "/opened-projects/{}/track/{}".format(token, filename)
        )
        opened_project_folders[token] = selected

        return jsonify({
            "ok": True,
            "id": token,
            "name": project.get("name") or os.path.basename(selected),
            "project": project,
            "lyrics": lyrics,
            "tracks": tracks,
            "source_path": selected
        })
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        print(f"[PROJECT OPEN] ERROR: {exc}", flush=True)
        return jsonify({"error": "Project folder selection failed: " + str(exc)}), 500


@app.route("/opened-projects/<token>/track/<path:filename>", methods=["GET"])
# Работа с сохранённым Project.
def opened_project_track_file(token, filename):
    folder = opened_project_folders.get(str(token))
    if not folder:
        return jsonify({"error": "Opened project session expired"}), 404
    tracks_dir = os.path.abspath(os.path.join(folder, "tracks"))
    return send_from_directory(tracks_dir, filename)


@app.route("/projects/select-folder", methods=["POST"])
# Работа с сохранённым Project.
def select_project_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="Select folder for MyNus project",
            initialdir=PROJECTS_DIR
        )
        root.destroy()
        return jsonify({"ok": True, "path": selected or ""})
    except Exception as exc:
        print(f"[PROJECT SAVE] FOLDER PICKER ERROR: {exc}", flush=True)
        return jsonify({"error": "Folder selection failed: " + str(exc)}), 500


@app.route("/projects/save", methods=["POST"])
# Работа с сохранённым Project.
def save_project():
    uid = current_user_id()
    project_name = str(request.form.get("name") or "Project").strip() or "Project"
    requested_project_id = str(request.form.get("project_id") or "").strip()
    project_id = _project_id(requested_project_id) if requested_project_id else _project_id(project_name)
    save_root_raw = str(request.form.get("save_path") or PROJECTS_DIR).strip() or PROJECTS_DIR
    save_root = os.path.abspath(os.path.expanduser(save_root_raw))
    conflict_action = str(request.form.get("conflict_action") or "").strip().lower()
    folder = os.path.join(save_root, project_id)

    with db_connection() as db:
        if conflict_action != "copy":
            other_owner = db.execute(
                """SELECT 1 FROM projects
                   WHERE client_request_id=? AND user_id<>?""",
                (project_path_key(folder), uid),
            ).fetchone()
            if other_owner:
                return jsonify(error=(
                    "Эта папка принадлежит другому тестовому пользователю. "
                    "Сохраните под другим именем."
                )), 409

    if os.path.isdir(folder):
        if conflict_action == "copy":
            base_name = project_name
            copy_number = 2
            while True:
                copy_name = f"{base_name} ({copy_number})"
                copy_id = _project_id(copy_name)
                copy_folder = os.path.join(save_root, copy_id)
                if not os.path.exists(copy_folder):
                    project_name = copy_name
                    project_id = copy_id
                    folder = copy_folder
                    break
                copy_number += 1
        elif conflict_action != "overwrite":
            return jsonify({
                "error": "Project already exists",
                "conflict": True,
                "id": project_id,
                "name": project_name,
                "path": folder
            }), 409

    temp_folder = folder + ".saving"

    print("\n" + "=" * 72, flush=True)
    print(f"[PROJECT SAVE] START | name={project_name!r} | id={project_id!r}", flush=True)
    print(f"[PROJECT SAVE] TARGET | {folder}", flush=True)

    try:
        os.makedirs(save_root, exist_ok=True)
        if os.path.isdir(temp_folder):
            shutil.rmtree(temp_folder)
        os.makedirs(temp_folder, exist_ok=True)
        tracks_dir = os.path.join(temp_folder, "tracks")
        os.makedirs(tracks_dir, exist_ok=True)

        lyrics_json = json.loads(request.form.get("lyrics_json") or "{}")
        project_json = json.loads(request.form.get("project_json") or "{}")
        print("[PROJECT SAVE] JSON | Lyrics.json received", flush=True)
        print("[PROJECT SAVE] JSON | Project.json received", flush=True)

        track_ids = [
            "original", "vocals", "pitchCorrection", "harmonizer",
            "drums", "bass", "guitar", "piano", "other", "master",
            "reserve1", "reserve2", "reserve3"
        ]
        track_files = {}
        saved_count = 0
        for track_id in track_ids:
            upload = request.files.get("track_" + track_id)
            if upload is None or not upload.filename:
                track_files[track_id] = None
                print(f"[PROJECT SAVE] TRACK | {track_id}: empty", flush=True)
                continue
            ext = os.path.splitext(upload.filename)[1].lower() or ".bin"
            if ext == ".audio":
                ext = ".bin"
            filename = track_id + ext
            target_file = os.path.join(tracks_dir, filename)
            upload.save(target_file)
            size = os.path.getsize(target_file)
            track_files[track_id] = filename
            saved_count += 1
            print(f"[PROJECT SAVE] TRACK | {track_id}: {filename} | {size} bytes", flush=True)

        if not track_files.get("original"):
            raise ValueError("Original track not received")

        project_json["version"] = re.search(
            r"ToneCheck\s*\|\s*([\d.]+)",
            open(__file__, encoding="utf-8").read(256)
        ).group(1)

        project_json["id"] = project_id
        project_json["name"] = project_name
        project_json["tracks"] = track_files

        lyrics_path = os.path.join(temp_folder, "Lyrics.json")
        project_path = os.path.join(temp_folder, "Project.json")
        with open(lyrics_path, "w", encoding="utf-8") as fh:
            json.dump(lyrics_json, fh, ensure_ascii=False, indent=2)
        with open(project_path, "w", encoding="utf-8") as fh:
            json.dump(project_json, fh, ensure_ascii=False, indent=2)
        print(f"[PROJECT SAVE] FILE | Lyrics.json | {os.path.getsize(lyrics_path)} bytes", flush=True)
        print(f"[PROJECT SAVE] FILE | Project.json | {os.path.getsize(project_path)} bytes", flush=True)

        if os.path.isdir(folder):
            shutil.rmtree(folder)
        os.replace(temp_folder, folder)

        print(f"[PROJECT SAVE] SUCCESS | tracks={saved_count} | {folder}", flush=True)
        print("=" * 72 + "\n", flush=True)

        key = project_path_key(folder)
        with db_connection() as db:
            separation_id = source_separation(db, uid)
            db.execute(
                """INSERT INTO projects
                   (user_id,client_request_id,separation_id,name)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user_id,client_request_id)
                   DO UPDATE SET
                     name=excluded.name,
                     separation_id=COALESCE(excluded.separation_id,projects.separation_id),
                     updated_at=?,deleted_at=NULL""",
                (uid, key, separation_id, project_name, utc_now()),
            )
            row = db.execute(
                """SELECT project_id,separation_id FROM projects
                   WHERE user_id=? AND client_request_id=?""",
                (uid, key),
            ).fetchone()
            write_log(
                db, uid, "project.saved", project_name,
                sid=row["separation_id"], pid=row["project_id"],
            )

        return jsonify({
            "ok": True,
            "id": project_id,
            "name": project_name,
            "path": folder,
            "project_id": row["project_id"],
            "separation_id": row["separation_id"],
            "user_id": uid
        })

    except Exception as exc:
        shutil.rmtree(temp_folder, ignore_errors=True)
        print(f"[PROJECT SAVE] ERROR | {type(exc).__name__}: {exc}", flush=True)
        print("=" * 72 + "\n", flush=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/lyrics/save-current", methods=["POST"])
# Локальная серверная операция этого блока.
def save_current_lyrics():
    uid = current_user_id()
    data = request.get_json(silent=True) or {}
    lyrics = data.get("lyrics")
    if not isinstance(lyrics, dict) or not isinstance(lyrics.get("lines"), list):
        return jsonify({"error": "Invalid Lyrics.json"}), 400

    current_id = _read_playlist_state().get("current")
    if not current_id:
        return jsonify({"error": "No current project"}), 400

    folder = _saved_project_folder(current_id)
    key = project_path_key(folder)
    with db_connection() as db:
        other_owner = db.execute(
            """SELECT 1 FROM projects
               WHERE client_request_id=? AND user_id<>?""",
            (key, uid),
        ).fetchone()
        if other_owner:
            return jsonify(error="Проект другого пользователя"), 403

    if not os.path.isdir(folder) or not os.path.isfile(os.path.join(folder, "Project.json")):
        return jsonify({"error": "Current project folder not found in Projects"}), 404

    lyrics_path = os.path.join(folder, "Lyrics.json")
    temp_path = lyrics_path + ".saving"
    try:
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump(lyrics, fh, ensure_ascii=False, indent=2)
        os.replace(temp_path, lyrics_path)

        with db_connection() as db:
            db.execute(
                """UPDATE projects SET updated_at=?
                   WHERE user_id=? AND client_request_id=?""",
                (utc_now(), uid, key),
            )
            row = db.execute(
                """SELECT project_id,separation_id FROM projects
                   WHERE user_id=? AND client_request_id=?""",
                (uid, key),
            ).fetchone()
            if row:
                write_log(
                    db, uid, "project.lyrics_saved", "Текст обновлён",
                    sid=row["separation_id"], pid=row["project_id"],
                )

        print(f"[LYRICS SAVE] SUCCESS | {lyrics_path}", flush=True)
        return jsonify({"ok": True, "id": current_id, "path": lyrics_path})
    except Exception as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        print(f"[LYRICS SAVE] ERROR | {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": str(exc)}), 500


# Работа с сохранённым Project.
def _saved_projects_payload():
    projects = []
    for project_id in sorted(os.listdir(PROJECTS_DIR), key=str.lower):
        folder = os.path.join(PROJECTS_DIR, project_id)
        project_path = os.path.join(folder, "Project.json")
        lyrics_path = os.path.join(folder, "Lyrics.json")
        if not os.path.isdir(folder) or not os.path.isfile(project_path) or not os.path.isfile(lyrics_path):
            continue
        try:
            with open(project_path, "r", encoding="utf-8") as fh:
                project = json.load(fh)
            project_name = project.get("name") or project_id
        except Exception:
            project_name = project_id
        projects.append({"id": project_id, "name": project_name})
    return {"projects": projects, "root": PROJECTS_DIR}


@app.route("/saved-projects", methods=["GET"])
# Работа с сохранённым Project.
def list_saved_projects():
    return jsonify(_saved_projects_payload())


@app.route("/saved-projects/<project_id>", methods=["GET"])
# Работа с сохранённым Project.
def get_saved_project(project_id):
    project_id = _project_id(project_id)
    folder = os.path.join(PROJECTS_DIR, project_id)
    project_path = os.path.join(folder, "Project.json")
    lyrics_path = os.path.join(folder, "Lyrics.json")
    if not os.path.isfile(project_path) or not os.path.isfile(lyrics_path):
        return jsonify({"error": "Project not found"}), 404
    with open(project_path, "r", encoding="utf-8") as fh:
        project = json.load(fh)
    with open(lyrics_path, "r", encoding="utf-8") as fh:
        lyrics = json.load(fh)
    tracks = {}
    for track_id, filename in (project.get("tracks") or {}).items():
        tracks[track_id] = (
            "/saved-projects/{}/track/{}".format(project_id, filename)
            if filename else None
        )
    return jsonify({
        "id": project_id,
        "name": project.get("name") or project_id,
        "project": project,
        "lyrics": lyrics,
        "tracks": tracks
    })


@app.route("/saved-projects/<project_id>/track/<path:filename>", methods=["GET"])
# Работа с сохранённым Project.
def saved_project_track_file(project_id, filename):
    return send_from_directory(
        os.path.join(PROJECTS_DIR, _project_id(project_id), "tracks"),
        filename
    )



# Локальная серверная операция этого блока.
def print_restart_command():
    print("\n" + "=" * 72)
    print("RESTART SERVER:")
    print(r".venv313\Scripts\python.exe server.py")
    print("=" * 72 + "\n")




# ===== SQLite: пользователи, сепарации и проекты =====
import sqlite3
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, unquote
from flask import session, abort

DB_PATH = Path(BASE_DIR) / "mynus.db"
def _server_code790():
    path=Path(BASE_DIR)/'.session-key'
    try:
        with path.open('x',encoding='ascii') as handle:
            handle.write(secrets.token_urlsafe(16));handle.flush();os.fsync(handle.fileno())
    except FileExistsError:pass
    code=path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{22}',code):raise RuntimeError('Неверный код сервера в .session-key: нужен 22-символьный код.')
    return code
app.secret_key = _server_code790()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


@contextmanager
def db_connection():
    db = sqlite3.connect(
        DB_PATH.resolve().as_uri() + "?mode=rw",
        uri=True,
        timeout=30,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def utc_now():
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def current_user_id():
    if not session_is_active760():
        session.clear()
        abort(401, description="Sign in or register to continue.")
    uid = session.get("user_id")
    with db_connection() as db:
        row = db.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (uid,),
        ).fetchone()
    if row is None:
        abort(401, description="Please log in to MyNus.")
    return row["user_id"]


def project_path_key(folder):
    return os.path.normcase(os.path.realpath(folder))


def write_log(db, uid, event, message, sid=None, pid=None, level="info"):
    db.execute(
        """INSERT INTO diagnostic_logs
           (user_id,separation_id,project_id,app_version,level,event,message)
           VALUES (?,?,?,'7.0.2',?,?,?)""",
        (uid, sid, pid, level, event, str(message)[:8000]),
    )


# MyNus 7.0.2: email REG/LOG, session cookie only (no remember token).
import smtplib
import ssl
import hashlib
from email.message import EmailMessage

AUTH_CONFIG_PATH = Path(BASE_DIR) / "mynus_config_7.0.2.json"
if not AUTH_CONFIG_PATH.exists():
    AUTH_CONFIG_PATH = Path(BASE_DIR) / "mynus_config_7.0.0.json"
pending_registrations = {}
auth_lock = threading.Lock()


def migrate_registration_fields():
    # Extend existing users without replacing IDs, projects or subscriptions.
    with db_connection() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        for column in ("email", "phone", "email_verified_at", "first_name", "last_name", "country"):
            if column not in columns:
                db.execute("ALTER TABLE users ADD COLUMN " + column + " TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_normalized "
                   "ON users(lower(trim(email))) WHERE email IS NOT NULL AND trim(email) <> ''")


def auth_email(value):
    email = str(value or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a valid email address.")
    return email


def send_registration_code(email, code):
    with AUTH_CONFIG_PATH.open(encoding="utf-8-sig") as file:
        config = json.load(file)
    smtp = config["smtp"]
    password = os.environ.get("MYNUS_SMTP_PASSWORD") or smtp.get("password", "")
    if not password or password == "PASTE_SMTP_KEY_HERE":
        raise RuntimeError("Configure the SMTP key on the server.")
    message = EmailMessage()
    message["From"] = smtp["sender"]
    message["To"] = email
    message["Subject"] = "MyNus — email verification"
    message.set_content("MyNus verification code: " + code +
                        "\nThis code expires in 10 minutes. If you did not register, ignore this email.")
    with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=20) as client:
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        client.login(smtp["login"], password)
        client.send_message(message)


def auth_request_data():
    # JSON plus same-origin check prevents cross-site login/registration forms.
    if not request.is_json:
        abort(415)
    origin = request.headers.get("Origin")
    expected_origin = request.host_url.rstrip("/")
    # Local cloudflared terminates HTTPS and forwards HTTP to Flask.
    # Accept its forwarded scheme only when the connection is from loopback.
    if request.remote_addr in ("127.0.0.1", "::1"):
        forwarded_scheme = request.headers.get("X-Forwarded-Proto", "")
        if forwarded_scheme in ("http", "https"):
            expected_origin = forwarded_scheme + "://" + request.host
    if origin and origin.rstrip("/") != expected_origin and origin not in DESKTOP_ORIGINS:
        abort(403, description="This page is not allowed to access MyNus accounts. Use the MyNus application.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


@app.route("/api/auth/register-code", methods=["POST"])
def register_code():
    data = auth_request_data()
    try:
        email = auth_email(data.get("email"))
        profile = {field: str(data.get(field) or "").strip()
                   for field in ("phone", "first_name", "last_name", "country")}
        if any(not value or len(value) > 120 for value in profile.values()):
            raise ValueError("Complete phone, first name, last name and country (up to 120 characters).")
        if not re.fullmatch(r"[+()\d .-]{5,40}", profile["phone"]):
            raise ValueError("Enter a valid phone number.")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with db_connection() as db:
        if db.execute("SELECT 1 FROM users WHERE lower(trim(email))=?", (email,)).fetchone():
            return jsonify(error="This email is already registered. Use LOG."), 409
    now = time.monotonic()
    with auth_lock:
        for key in list(pending_registrations):
            if pending_registrations[key]["expires"] < now:
                del pending_registrations[key]
        previous = pending_registrations.get(email)
        if previous and now - previous["sent"] < 60:
            return jsonify(error="Please wait one minute before resending."), 429
        if len(pending_registrations) >= 1000:
            return jsonify(error="Please try again later."), 429
        code = f"{secrets.randbelow(1000000):06d}"
        pending = dict(profile, digest=hashlib.sha256(code.encode()).hexdigest(),
                       expires=now + 600, sent=now, attempts=0, ready=False)
        pending_registrations[email] = pending
    try:
        send_registration_code(email, code)
    except Exception as exc:
        with auth_lock:
            if pending_registrations.get(email) is pending:
                del pending_registrations[email]
        # Do not log SMTP credentials, message body or verification code.
        print("[REG EMAIL] Sending failed: " + type(exc).__name__, flush=True)
        return jsonify(error="Unable to send the email. Check the server SMTP configuration."), 503
    with auth_lock:
        pending["ready"] = True
    return jsonify(ok=True, message="Code sent to your email. It expires in 10 minutes.")


@app.route("/api/auth/register", methods=["POST"])
def register_user():
    data = auth_request_data()
    try:
        email = auth_email(data.get("email"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    code = str(data.get("code") or "").strip()
    with auth_lock:
        pending = pending_registrations.get(email)
        if not pending or not pending["ready"] or pending["expires"] < time.monotonic():
            return jsonify(error="Request a new code."), 400
        pending["attempts"] += 1
        if pending["attempts"] > 5:
            del pending_registrations[email]
            return jsonify(error="Слишком много попыток. Request a new code.."), 429
        if not secrets.compare_digest(pending["digest"], hashlib.sha256(code.encode()).hexdigest()):
            return jsonify(error="Incorrect code."), 400
        try:
            with db_connection() as db:
                uid = db.execute(
                    "INSERT INTO users (name,email,phone,email_verified_at,first_name,last_name,country,max_active_sessions) "
                    "VALUES (?,?,?,?,?,?,?,1)",
                    (pending["first_name"] + " " + pending["last_name"], email,
                     pending["phone"], utc_now(), pending["first_name"],
                     pending["last_name"], pending["country"]),
                ).lastrowid
                write_log(db, uid, "user.registered", "Email подтверждён")
        except sqlite3.IntegrityError:
            return jsonify(error="This email is already registered."), 409
        del pending_registrations[email]
    start_session760(uid)
    return jsonify(ok=True, user_id=uid)


@app.route("/api/auth/login", methods=["POST"])
def login_email():
    data = auth_request_data()
    try:
        email = auth_email(data.get("email"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with db_connection() as db:
        row = db.execute("SELECT user_id FROM users WHERE lower(trim(email))=?", (email,)).fetchone()
    if row is None:
        return jsonify(error="This email is not registered. Use REG."), 404
    start_session760(row["user_id"])
    return jsonify(ok=True, user_id=row["user_id"])


@app.route("/api/auth/logout", methods=["POST"])
def logout_user():
    auth_request_data()
    sid,uid=session.get("session_id"),session.get("user_id")
    closed=0
    if sid:
        with db_connection() as db:
            closed=db.execute("UPDATE sessions SET revoked_at=? WHERE session_id=? AND user_id=? AND revoked_at IS NULL",(_utc760(),sid,uid)).rowcount
    session.clear()
    if closed:publish_account_event790(uid,sid,'Выход из аккаунта')
    return jsonify(ok=True)


migrate_registration_fields()


# MyNus 7.0.2: staged account changes. The live profile stays unchanged
# until the current address confirms and, for an email change, the new one verifies.
def migrate_account_changes():
    with db_connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS account_changes (
            request_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(user_id),
            session_key TEXT NOT NULL,
            original_email TEXT NOT NULL,
            original_updated_at TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ('confirmation','verification')),
            digest TEXT NOT NULL,
            expires_at REAL NOT NULL,
            sent_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            ready INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS account_changes_user ON account_changes(user_id)")


def account_send_code(email, code, stage):
    with AUTH_CONFIG_PATH.open(encoding="utf-8-sig") as file:
        smtp = json.load(file)["smtp"]
    password = os.environ.get("MYNUS_SMTP_PASSWORD") or smtp.get("password", "")
    if not password or password == "PASTE_SMTP_KEY_HERE":
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["From"] = smtp["sender"]
    message["To"] = email
    label = "Confirmation" if stage == "confirmation" else "Verification"
    message["Subject"] = "MyNus — " + label + " code"
    message.set_content(label + " code: " + code + "\nExpires in 10 minutes. "
                        "If you did not request an account change, ignore this email.")
    with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=20) as client:
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        client.login(smtp["login"], password)
        client.send_message(message)


def account_code_digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def account_deliver(request_id, email, code, stage):
    try:
        account_send_code(email, code, stage)
    except Exception as exc:
        app.logger.warning("Account code delivery failed: %s", type(exc).__name__)
        return jsonify(error="Unable to send the email. Wait one minute and use Resend code.", request_id=request_id, stage=stage), 503
    with db_connection() as db:
        db.execute("UPDATE account_changes SET ready=1 WHERE request_id=? AND digest=?",
                   (request_id, account_code_digest(code)))
    return jsonify(ok=True, request_id=request_id, stage=stage,
                   message=("Confirmation code sent to your current verified email."
                            if stage == "confirmation" else
                            "Verification code sent to your new email. Your account is unchanged until verification."))


@app.route("/api/account/change-code", methods=["POST"])
def account_change_code():
    data = auth_request_data()
    uid = current_user_id()
    try:
        profile = {field: str(data.get(field) or "").strip()
                   for field in ("first_name", "last_name", "phone", "country")}
        if any(not value or len(value) > 120 for value in profile.values()):
            raise ValueError("Complete first name, last name, phone and country (up to 120 characters).")
        if not re.fullmatch(r"[+()\d .-]{5,40}", profile["phone"]):
            raise ValueError("Enter a valid phone number.")
        try:
            profile["email"] = auth_email(data.get("email"))
        except ValueError:
            raise ValueError("Enter a valid email address.")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    session_key = session.get("account_change_key") or secrets.token_urlsafe(32)
    session["account_change_key"] = session_key
    now = time.time()
    request_id, code = secrets.token_urlsafe(32), f"{secrets.randbelow(1000000):06d}"
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM account_changes WHERE expires_at<?", (now,))
        user = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not user["email"] or not user["email_verified_at"]:
            return jsonify(error="A current verified email is required to change this account."), 403
        if all(profile[field] == (user[field] or "") for field in profile):
            return jsonify(error="No changes to save."), 400
        if db.execute("SELECT 1 FROM users WHERE lower(trim(email))=? AND user_id<>?",
                      (profile["email"], uid)).fetchone():
            return jsonify(error="This email is already registered."), 409
        previous = db.execute("SELECT sent_at FROM account_changes WHERE user_id=? ORDER BY sent_at DESC LIMIT 1", (uid,)).fetchone()
        if previous and now - previous["sent_at"] < 60:
            return jsonify(error="Please wait one minute before requesting another code."), 429
        db.execute("DELETE FROM account_changes WHERE user_id=?", (uid,))
        db.execute("""INSERT INTO account_changes
            (request_id,user_id,session_key,original_email,original_updated_at,profile_json,
             stage,digest,expires_at,sent_at) VALUES (?,?,?,?,?,?,'confirmation',?,?,?)""",
            (request_id, uid, session_key, user["email"], user["updated_at"],
             json.dumps(profile), account_code_digest(code), now + 600, now))
    return account_deliver(request_id, user["email"], code, "confirmation")


@app.route("/api/account/confirm", methods=["POST"])
def account_confirm_change():
    data = auth_request_data()
    uid = current_user_id()
    request_id = str(data.get("request_id") or "")
    code = str(data.get("code") or "").strip()
    now = time.time()
    delivery = None
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT * FROM account_changes WHERE request_id=? AND user_id=? AND session_key=?",
            (request_id, uid, session.get("account_change_key", ""))).fetchone()
        if not pending or not pending["ready"] or pending["expires_at"] < now:
            return jsonify(error="Request a new code. This request is unavailable or expired."), 400
        if pending["attempts"] >= 5:
            return jsonify(error="Too many attempts. Request a new code."), 429
        db.execute("UPDATE account_changes SET attempts=attempts+1 WHERE request_id=?", (request_id,))
        if not secrets.compare_digest(pending["digest"], account_code_digest(code)):
            return jsonify(error="Incorrect code."), 400
        user = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if (user["email"] != pending["original_email"] or
                user["updated_at"] != pending["original_updated_at"] or not user["email_verified_at"]):
            db.execute("DELETE FROM account_changes WHERE request_id=?", (request_id,))
            return jsonify(error="Your account has changed. Start again."), 409
        profile = json.loads(pending["profile_json"])
        if pending["stage"] == "confirmation" and profile["email"] != user["email"]:
            next_code = f"{secrets.randbelow(1000000):06d}"
            while next_code == code:
                next_code = f"{secrets.randbelow(1000000):06d}"
            db.execute("""UPDATE account_changes SET stage='verification',digest=?,
                expires_at=?,sent_at=?,attempts=0,ready=0 WHERE request_id=?""",
                (account_code_digest(next_code), now+600, now, request_id))
            delivery = (request_id, profile["email"], next_code, "verification")
        else:
            try:
                db.execute("""UPDATE users SET first_name=?,last_name=?,name=?,phone=?,country=?,
                    email=?,email_verified_at=?,updated_at=? WHERE user_id=?""",
                    (profile["first_name"], profile["last_name"],
                     profile["first_name"] + " " + profile["last_name"], profile["phone"],
                     profile["country"], profile["email"],
                     utc_now() if pending["stage"] == "verification" else user["email_verified_at"], utc_now(), uid))
            except sqlite3.IntegrityError:
                db.execute("DELETE FROM account_changes WHERE request_id=?", (request_id,))
                return jsonify(error="This email is already registered. Start again."), 409
            db.execute("DELETE FROM account_changes WHERE user_id=?", (uid,))
            write_log(db, uid, "account.updated", "Account change confirmed")
    if delivery:
        return account_deliver(*delivery)
    return jsonify(ok=True, stage="complete", message="Account changes saved.")


@app.route("/api/account/resend-code", methods=["POST"])
def account_resend_code():
    data = auth_request_data()
    uid = current_user_id()
    request_id = str(data.get("request_id") or "")
    now = time.time()
    code = f"{secrets.randbelow(1000000):06d}"
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT * FROM account_changes WHERE request_id=? AND user_id=? AND session_key=?",
            (request_id, uid, session.get("account_change_key", ""))).fetchone()
        if not pending or pending["expires_at"] < now or pending["attempts"] >= 5:
            return jsonify(error="Start again to request a new code."), 400
        if now - pending["sent_at"] < 60:
            return jsonify(error="Please wait one minute before resending."), 429
        profile = json.loads(pending["profile_json"])
        target = pending["original_email"] if pending["stage"] == "confirmation" else profile["email"]
        db.execute("UPDATE account_changes SET digest=?,expires_at=?,sent_at=?,ready=0 WHERE request_id=?",
                   (account_code_digest(code), now+600, now, request_id))
    return account_deliver(request_id, target, code, pending["stage"])


@app.route("/api/account/cancel", methods=["POST"])
def account_cancel_change():
    data = auth_request_data()
    uid = current_user_id()
    with db_connection() as db:
        db.execute("UPDATE account_changes SET ready=0,attempts=5 WHERE request_id=? AND user_id=? AND session_key=?",
                   (str(data.get("request_id") or ""), uid, session.get("account_change_key", "")))
    return jsonify(ok=True)


@app.route("/api/account/payments")
def account_payments():
    uid = current_user_id()
    with db_connection() as db:
        subscription = db.execute("SELECT plan_name,status,starts_at,ends_at FROM subscriptions WHERE user_id=? ORDER BY subscription_id DESC LIMIT 1", (uid,)).fetchone()
        access_row = db.execute("SELECT status FROM users WHERE user_id=?", (uid,)).fetchone()
        payments = db.execute("SELECT amount_minor,currency,status,created_at,refunded_minor FROM payments WHERE user_id=? ORDER BY payment_id DESC LIMIT 20", (uid,)).fetchall()
    # This installation has no Paddle IDs, customer/subscription mapping or API integration.
    # Never substitute local subscription_id for a Paddle subscription ID.
    return jsonify(access_status=access_row["status"] if access_row else None,
                   subscription=dict(subscription) if subscription else None,
                   payments=[dict(row) for row in payments],
                   change_plan_available=False, manage_subscription_available=False,
                   message="Test mode: confirm or check the token below. No real charge is made.")


migrate_account_changes()


from werkzeug.exceptions import HTTPException


@app.errorhandler(HTTPException)
def auth_http_error(error):
    message = "Sign in or register to continue." if error.code == 401 else error.description
    return jsonify(error=message, code=error.code), error.code


@app.errorhandler(500)
def auth_server_error(error):
    if request.path.startswith(("/api/auth/", "/api/account/")) or request.path == "/api/current-user":
        return jsonify(error="Server error. Please check the server log."), 500
    return error


@app.route("/api/current-user")
def current_user():
    uid = current_user_id()
    with db_connection() as db:
        row = db.execute(
            "SELECT user_id,name,email,phone,first_name,last_name,country,created_at,email_verified_at,status "
            "FROM users WHERE user_id=?",
            (uid,),
        ).fetchone()
        user = dict(row)
        subscription = db.execute(
            "SELECT plan_name,status FROM subscriptions WHERE user_id=? "
            "ORDER BY subscription_id DESC LIMIT 1", (uid,),
        ).fetchone()
        user["subscription"] = dict(subscription) if subscription else {"plan_name": None, "status": "none"}
        return jsonify(user)


# Проверяем существующую БД. Пустую базу вместо отсутствующей не создаём.
with db_connection() as database:
    for table_name in ("users", "separations", "projects", "diagnostic_logs"):
        database.execute("SELECT * FROM " + table_name + " LIMIT 0")


def watch_separation(job_id, uid, sid):
    # Учёт завершения работает независимо от открытого браузера.
    try:
        while jobs.get(job_id, {}).get("status") not in ("done", "error", "aborted"):
            time.sleep(0.5)

        state = jobs[job_id]
        status = {"done": "completed", "error": "failed", "aborted": "cancelled"}[state["status"]]

        with db_connection() as db:
            db.execute(
                """UPDATE separations
                   SET status=?,finished_at=?,error_message=?
                   WHERE separation_id=? AND user_id=?""",
                (status, utc_now(), state.get("error"), sid, uid),
            )
            finish_session_operation790(db,sid)
            write_log(
                db, uid,
                "separation." + status,
                state.get("error") or status,
                sid=sid,
                level="error" if status == "failed" else "info",
            )
    except Exception:
        app.logger.exception(
            "Не удалось обновить сепарацию %s в БД", sid
        )


def source_separation(db, uid):
    state = json.loads(request.form.get("project_json") or "{}")
    sources = state.get("_sources", [])
    found = set()

    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, str):
            continue

        parts = unquote(urlsplit(source).path).strip("/").split("/")
        row = None

        if len(parts) >= 3 and parts[0] == "results":
            row = db.execute(
                """SELECT separation_id FROM separations
                   WHERE user_id=? AND client_request_id=?""",
                (uid, "job:" + parts[1]),
            ).fetchone()

        elif len(parts) >= 4 and parts[0] == "saved-projects":
            key = project_path_key(
                os.path.join(PROJECTS_DIR, _project_id(parts[1]))
            )
            row = db.execute(
                """SELECT separation_id FROM projects
                   WHERE user_id=? AND client_request_id=?""",
                (uid, key),
            ).fetchone()

        elif len(parts) >= 4 and parts[0] == "opened-projects":
            folder = opened_project_folders.get(parts[1])
            if folder:
                row = db.execute(
                    """SELECT separation_id FROM projects
                       WHERE user_id=? AND client_request_id=?""",
                    (uid, project_path_key(folder)),
                ).fetchone()

        if row and row[0] is not None:
            found.add(row[0])

    # Не назначаем произвольную сепарацию, если связь неизвестна.
    return next(iter(found)) if len(found) == 1 else None


# ===== КОНЕЦ ВСТАВКИ =====










@app.before_request
def reject_legacy_project_storage():
    path = request.path
    if (path == "/projects" or path.startswith("/projects/") or
        path == "/saved-projects" or path.startswith("/saved-projects/") or
        path.startswith("/opened-projects/") or path.startswith("/playlist/") or
        path == "/lyrics/save-current"):
        return jsonify(error="Project save/load is local in MyNus 7.9.0"), 410


# 7.9.0 schema extension; SQLite backup before the first migration.
from datetime import datetime, timezone, timedelta
def columns(db,table):
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
def migrate(db):
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        if 'max_active_sessions' not in columns(db,'users'):
            db.execute('ALTER TABLE users ADD COLUMN max_active_sessions INTEGER NOT NULL DEFAULT 3 CHECK(max_active_sessions >= 1)')
        db.execute('''CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY CHECK(length(session_id) >= 32),
            user_id INTEGER NOT NULL REFERENCES users(user_id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            last_seen_at TEXT,
            CHECK(expires_at > created_at),
            CHECK(revoked_at IS NULL OR revoked_at >= created_at),
            UNIQUE(session_id,user_id)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS sessions_user_active ON sessions(user_id,revoked_at,expires_at)')
        if 'separation_id' not in columns(db,'payments'):
            db.execute('''ALTER TABLE payments ADD COLUMN separation_id INTEGER
                REFERENCES separations(separation_id)
                CHECK(separation_id IS NULL OR subscription_id IS NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS payments_separation_access ON payments(user_id,separation_id,status)')
        # ALTER TABLE cannot add a composite foreign key to an existing table.
        # These guards enforce payment ownership for both insert and update.
        for action in ('INSERT','UPDATE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS payments_separation_owner_{action.lower()}
                BEFORE {action} ON payments
                WHEN NEW.separation_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM separations
                    WHERE separation_id=NEW.separation_id AND user_id=NEW.user_id
                )
                BEGIN SELECT RAISE(ABORT,'Payment separation must belong to the same user'); END''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS separations_payment_owner_update
            BEFORE UPDATE OF user_id ON separations
            WHEN EXISTS (SELECT 1 FROM payments WHERE separation_id=OLD.separation_id AND user_id<>NEW.user_id)
            BEGIN SELECT RAISE(ABORT,'Cannot change owner of a separation with linked payments'); END''')
        if 'delete_after' not in columns(db,'separations'):
            db.execute('ALTER TABLE separations ADD COLUMN delete_after TEXT')
        if 'files_deleted_at' not in columns(db,'separations'):
            db.execute('ALTER TABLE separations ADD COLUMN files_deleted_at TEXT')
        db.execute('CREATE INDEX IF NOT EXISTS separations_cleanup ON separations(files_deleted_at,delete_after)')
        db.execute('''UPDATE separations
            SET delete_after=strftime('%Y-%m-%dT%H:%M:%fZ',finished_at,'+100 hours')
            WHERE delete_after IS NULL AND finished_at IS NOT NULL
              AND status IN ('completed','failed','cancelled')''')
        # New completion timestamps written by the existing server get a deadline.
        db.execute('''CREATE TRIGGER IF NOT EXISTS separations_cleanup_deadline_update
            AFTER UPDATE OF status,finished_at ON separations
            WHEN NEW.status IN ('completed','failed','cancelled')
              AND NEW.finished_at IS NOT NULL AND NEW.delete_after IS NULL
            BEGIN UPDATE separations
                SET delete_after=strftime('%Y-%m-%dT%H:%M:%fZ',NEW.finished_at,'+100 hours')
                WHERE separation_id=NEW.separation_id; END''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS separations_cleanup_deadline_insert
            AFTER INSERT ON separations
            WHEN NEW.status IN ('completed','failed','cancelled')
              AND NEW.finished_at IS NOT NULL AND NEW.delete_after IS NULL
            BEGIN UPDATE separations
                SET delete_after=strftime('%Y-%m-%dT%H:%M:%fZ',NEW.finished_at,'+100 hours')
                WHERE separation_id=NEW.separation_id; END''')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Foreign key validation failed')
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise RuntimeError('Integrity validation failed')
        db.commit()
    except BaseException:
        db.rollback()
        raise
def ensure_schema760():
    db=sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=rw',uri=True,timeout=30)
    try:
        needed=not columns(db,'sessions') or 'max_active_sessions' not in columns(db,'users') or 'separation_id' not in columns(db,'payments') or 'files_deleted_at' not in columns(db,'separations')
        if needed:
            backup=DB_PATH.with_name('mynus-before-7.9.0-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.db')
            with sqlite3.connect(backup) as target:db.backup(target)
            print('[DATABASE] Backup: '+str(backup),flush=True)
        if needed:migrate(db)
    finally:db.close()
ensure_schema760()
# MyNus 7.9.0: sessions, paid result access and scheduled media cleanup.
from datetime import datetime, timezone, timedelta
from pathlib import Path

def _utc760():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

def session_is_active760():
    sid,uid=session.get('session_id'),session.get('user_id')
    if not sid or uid is None:return False
    with db_connection() as db:
        row=db.execute('SELECT 1 FROM sessions WHERE session_id=? AND user_id=? AND revoked_at IS NULL AND expires_at>?',(sid,uid,_utc760())).fetchone()
    return row is not None

@app.before_request
def protected_operations760():
    if request.path in ('/admin','/admin.html') or request.path.startswith('/api/admin/'):
        return
    if request.path in ('/admin','/admin.html') or request.path.startswith('/api/admin/'):
        return
    public={'/','/api/auth/login','/api/auth/register','/api/auth/register-code','/api/auth/logout','/api/component-versions'}
    if request.method=='OPTIONS' or request.path in public:return
    if not session_is_active760():
        session.clear()
        abort(401,description='Sign in or register to use server functions.')

def start_session760(uid):
    now=_utc760();expires=(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat(timespec='milliseconds').replace('+00:00','Z')
    sid=secrets.token_hex(32)
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        limit=db.execute('SELECT max_active_sessions FROM users WHERE user_id=?',(uid,)).fetchone()[0]
        count=db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=? AND revoked_at IS NULL AND expires_at>?',(uid,now)).fetchone()[0]
        if count>=limit:abort(409,description='Active session limit reached. Sign out on another device and try again.')
        db.execute('INSERT INTO sessions(session_id,user_id,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?)',(sid,uid,now,expires,now))
    session.clear();session.permanent=False;session.update(user_id=uid,session_id=sid)

def separation_row760(job_id,uid):
    with db_connection() as db:
        row=db.execute('SELECT * FROM separations WHERE user_id=? AND client_request_id=?',(uid,'job:'+job_id)).fetchone()
    if row is None:abort(404,description='Separation not found.')
    return dict(row)

# Keep existing assignments. New accounts have no paid access by default.
with db_connection() as database:
    if 'status' not in {row[1] for row in database.execute('PRAGMA table_info(users)')}:
        database.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT NULL CHECK(status IN ('Subscription','OneTime'))")


_media_lock760=threading.RLock()
_media_readers760={}

def progress_payload760(job_id,uid):
    row=separation_row760(job_id,uid)
    if row['files_deleted_at']:abort(410,description='Separation files have expired and were deleted.')
    job=jobs.get(job_id)
    if job is not None:
        if job.get('user_id')!=uid:abort(404)
        with job_controls[job_id]['lock']:payload=dict(job)
    else:
        metadata=Path(RESULT_DIR)/job_id/'result.json'
        if not metadata.is_file():abort(404,description='Result is unavailable. Restarted unfinished jobs cannot be resumed.')
        payload=json.loads(metadata.read_text(encoding='utf-8'))
        payload.update(status='done',progress=100,user_id=uid)
    if payload.get('status')=='done' and row['status']!='completed':
        # Publish completion to the DB before the client immediately downloads.
        with db_connection() as db:
            db.execute("UPDATE separations SET status='completed',finished_at=COALESCE(finished_at,?) WHERE separation_id=?",(_utc760(),row['separation_id']))
        row=separation_row760(job_id,uid)
    allowed,plan=result_access760(row)
    token=token_access780(uid,row['separation_id'],issue=False) if allowed else None
    payload['access_token_id']=token['token_id'] if token else None
    payload.update(job_id=job_id,separation_id=row['separation_id'],download_allowed=allowed,active_plan=plan,delete_after=row['delete_after'])
    return payload

@app.route('/api/separations')
def list_separations760():
    uid=current_user_id()
    with db_connection() as db:
        rows=db.execute('SELECT separation_id,client_request_id,source_name,status,finished_at,delete_after,files_deleted_at FROM separations WHERE user_id=? ORDER BY separation_id DESC LIMIT 100',(uid,)).fetchall()
    return jsonify(separations=[dict(row) for row in rows])

@app.route('/api/account/sessions')
def list_sessions760():
    uid=current_user_id()
    with db_connection() as db:
        rows=db.execute('SELECT session_id,created_at,expires_at,revoked_at FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 100',(uid,)).fetchall()
        limit=db.execute('SELECT max_active_sessions FROM users WHERE user_id=?',(uid,)).fetchone()[0]
    return jsonify(sessions=[dict(row) for row in rows],current_session_id=session['session_id'],max_active_sessions=limit)

@app.route('/api/account/sessions/revoke',methods=['POST'])
def revoke_session760():
    uid=current_user_id();data=auth_request_data();sid=str(data.get('session_id') or '')
    with db_connection() as db:
        count=db.execute('UPDATE sessions SET revoked_at=COALESCE(revoked_at,?) WHERE user_id=? AND session_id=?',(_utc760(),uid,sid)).rowcount
    if not count:abort(404,description='Session not found.')
    if sid==session.get('session_id'):session.clear()
    return jsonify(ok=True)

@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(410)
def access_error760(error):
    return jsonify(error=error.description,code=error.code),error.code

def _safe_media_folder760(root,job_id):
    if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',job_id):raise ValueError('Invalid job directory')
    base=Path(root).resolve();folder=base/job_id
    if folder.is_symlink() or folder.resolve().parent!=base:raise ValueError('Unsafe media directory')
    return folder

def _remove_media_folder760(folder):
    # Reject links/junctions anywhere in a job folder before recursive deletion.
    if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in folder.rglob('*')):raise ValueError('Linked media directory')
    shutil.rmtree(folder)

def cleanup_media760():
    now=_utc760();removed=0
    with db_connection() as db:
        rows=db.execute("SELECT separation_id,client_request_id FROM separations WHERE status IN ('completed','failed','cancelled') AND delete_after IS NOT NULL AND delete_after<=? AND files_deleted_at IS NULL",(now,)).fetchall()
    for row in rows:
        job_id=row['client_request_id'].removeprefix('job:')
        try:
            with _media_lock760:
                control=job_controls.get(job_id)
                if _media_readers760.get(job_id,0) or (control and not control['finished'].is_set()):continue
                for root in (UPLOAD_DIR,RESULT_DIR):
                    folder=_safe_media_folder760(root,job_id)
                    if folder.exists():_remove_media_folder760(folder)
                with db_connection() as db:db.execute('UPDATE separations SET files_deleted_at=? WHERE separation_id=?',(now,row['separation_id']))
                jobs.pop(job_id,None);job_controls.pop(job_id,None);removed+=1
        except Exception:app.logger.exception('Cleanup failed for separation %s',row['separation_id'])
    # Legacy untracked uploads/results/exports: keep recent or active folders.
    with db_connection() as db:known={r[0].removeprefix('job:') for r in db.execute('SELECT client_request_id FROM separations')}
    cutoff=time.time()-86400
    for root in (UPLOAD_DIR,RESULT_DIR,EXPORT_DIR):
        for candidate in Path(root).iterdir():
            if not candidate.is_dir() or candidate.name in known or candidate.name in jobs:continue
            try:
                folder=_safe_media_folder760(root,candidate.name)
                latest=max([folder.stat().st_mtime]+[p.stat().st_mtime for p in folder.rglob('*')])
                if latest<=cutoff:
                    with _media_lock760:
                        if not _media_readers760.get(candidate.name,0):_remove_media_folder760(folder);removed+=1
            except Exception:app.logger.exception('Cleanup skipped legacy media %s',candidate.name)
    if removed:
        print('[CLEANUP] Removed '+str(removed)+' expired media tasks.',flush=True)

def cleanup_worker760():
    # Startup catches cleanup missed while the server was stopped.
    cleanup_media760()
    previous=datetime.now().date()
    while True:
        time.sleep(30)
        today=datetime.now().date()
        if today!=previous:
            cleanup_media760();previous=today


# Result-access history is separate from login sessions.
with db_connection() as original:
    exists=original.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tokens'").fetchone()
    needs_extension=not exists or 'received_files' not in {row[1] for row in original.execute('PRAGMA table_info(tokens)')}
    if needs_extension:
        backup=Path(DB_PATH).with_name('mynus-before-tokens-780-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.db')
        with sqlite3.connect(backup) as saved:original.backup(saved)
with db_connection() as db:
    db.execute('''CREATE TABLE IF NOT EXISTS tokens (
        token_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        token TEXT NOT NULL UNIQUE CHECK(length(token)=14),
        type TEXT NOT NULL CHECK(type IN ('Subscription','OneTime')),
        separation_id INTEGER REFERENCES separations(separation_id),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','used','expired','revoked')),
        issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        expires_at TEXT,closed_at TEXT,
        CHECK((type='Subscription' AND substr(token,1,3)='SUB') OR (type='OneTime' AND substr(token,1,3)='ONE')),
        CHECK(type='OneTime' OR separation_id IS NULL))''')
    names={row[1] for row in db.execute('PRAGMA table_info(tokens)')}
    if 'received_files' not in names:
        db.execute("ALTER TABLE tokens ADD COLUMN received_files TEXT NOT NULL DEFAULT '[]'")
    db.execute('CREATE INDEX IF NOT EXISTS idx_tokens_user_status ON tokens(user_id,status)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_tokens_separation ON tokens(separation_id)')

RESULT_FILES780=('vocals.mp3','drums.mp3','bass.mp3','guitar.mp3','piano.mp3','other.mp3')

def token_public780(row):
    return {key:row[key] for key in ('token_id','token','type','separation_id','status','issued_at','expires_at','closed_at')}

def token_access780(uid,separation_id=None,issue=True,renew=False):
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE tokens SET status='expired',closed_at=? WHERE user_id=? AND status='active' AND expires_at IS NOT NULL AND julianday(expires_at)<=julianday('now')",(_utc760(),uid))
        user=db.execute('SELECT status FROM users WHERE user_id=?',(uid,)).fetchone()
        kind=user['status'] if user else None
        if kind not in ('Subscription','OneTime'):return None
        # A bound OneTime cannot be transferred to a different separation.
        row=db.execute("SELECT * FROM tokens WHERE user_id=? AND type=? AND status='active' ORDER BY token_id DESC LIMIT 1",(uid,kind)).fetchone()
        if row is None and issue:
            # Expiry is not silently replaced by a new permanent grant.
            last=db.execute('SELECT status FROM tokens WHERE user_id=? AND type=? ORDER BY token_id DESC LIMIT 1',(uid,kind)).fetchone()
            if last and last['status'] in ('expired','revoked') and not renew:return None
            for attempt in range(8):
                value=('SUB' if kind=='Subscription' else 'ONE')+secrets.token_urlsafe(8)
                try:
                    number=db.execute('INSERT INTO tokens(user_id,token,type) VALUES(?,?,?)',(uid,value,kind)).lastrowid
                    row=db.execute('SELECT * FROM tokens WHERE token_id=?',(number,)).fetchone()
                    break
                except sqlite3.IntegrityError:
                    if db.execute('SELECT 1 FROM tokens WHERE token=?',(value,)).fetchone() is None:raise
            if row is None:raise RuntimeError('Could not generate a unique token.')
        if row is None:return None
        if kind=='OneTime' and separation_id is not None:
            if row['separation_id'] is not None and row['separation_id']!=separation_id:return None
            if row['separation_id'] is None:
                db.execute('UPDATE tokens SET separation_id=? WHERE token_id=? AND separation_id IS NULL',(separation_id,row['token_id']))
                row=db.execute('SELECT * FROM tokens WHERE token_id=?',(row['token_id'],)).fetchone()
        return dict(row)

def result_access760(row):
    if row['status']!='completed':return False,False
    token=token_access780(row['user_id'],row['separation_id'])
    return bool(token),bool(token and token['type']=='Subscription')

@app.route('/api/account/tokens')
def account_tokens780():
    uid = current_user_id()

    with db_connection() as db:
        db.execute(
            """UPDATE tokens
               SET status='expired', closed_at=?
               WHERE user_id=?
                 AND status='active'
                 AND expires_at IS NOT NULL
                 AND julianday(expires_at) <= julianday('now')""",
            (_utc760(), uid)
        )

        rows = db.execute(
            """SELECT * FROM tokens
               WHERE user_id=?
               ORDER BY token_id DESC
               LIMIT 100""",
            (uid,)
        ).fetchall()

        user = db.execute(
            "SELECT status FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

    return jsonify(
        access_status=user["status"],
        tokens=[token_public780(row) for row in rows]
    )


@app.route('/api/account/tokens/confirm', methods=['POST'])
def confirm_token780():
    uid = current_user_id()
    data = auth_request_data()

    with db_connection() as db:
        user = db.execute(
            "SELECT status FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

        kind = data.get("type") or user["status"] or "OneTime"

        if kind not in ("Subscription", "OneTime"):
            return jsonify(
                error="Выберите Subscription или OneTime."
            ), 400

        db.execute(
            "UPDATE users SET status=? WHERE user_id=?",
            (kind, uid)
        )

    token = token_access780(uid, renew=True)

    if token is None:
        return jsonify(error="Не удалось создать токен."), 500

    return jsonify(
        access_status=kind,
        token=token_public780(token),
        test_mode=True
    )



def mark_file_received780(uid,sid,token_id,filename):
    if not token_id:return
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT received_files FROM tokens WHERE token_id=? AND user_id=? AND separation_id=? AND status='active'",(token_id,uid,sid)).fetchone()
        if row:
            names=set(json.loads(row['received_files']));names.add(filename)
            db.execute('UPDATE tokens SET received_files=? WHERE token_id=?',(json.dumps(sorted(names)),token_id))

@app.route('/api/separations/<job_id>/received',methods=['POST'])
def acknowledge_result780(job_id):
    uid=current_user_id();data=auth_request_data();row=separation_row760(job_id,uid)
    token_id=data.get('token_id')
    with _media_lock760,db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        token=db.execute('SELECT * FROM tokens WHERE token_id=? AND user_id=?',(token_id,uid)).fetchone()
        if not token:abort(404,description='Token not found.')
        if token['type']=='Subscription':
            if token['status']!='active':abort(403,description='Token is not active.')
            return jsonify(token=token_public780(token))
        if token['separation_id']!=row['separation_id']:abort(403,description='Token belongs to a different separation.')
        if token['status']=='used':return jsonify(token=token_public780(token))
        if token['status']!='active':abort(403,description='Token is not active.')
        if set(json.loads(token['received_files']))!=set(RESULT_FILES780):abort(409,description='The complete result has not been transferred yet. Retry incomplete downloads.')
        supplied={item.get('name'):item.get('size') for item in data.get('files',[]) if isinstance(item,dict)}
        folder=_safe_media_folder760(RESULT_DIR,job_id)
        if set(supplied)!=set(RESULT_FILES780) or any(supplied[name]!=(folder/name).stat().st_size for name in RESULT_FILES780):
            abort(409,description='Complete file receipt confirmation is required.')
        db.execute("UPDATE tokens SET status='used',closed_at=? WHERE token_id=?",(_utc760(),token_id))
        db.execute("UPDATE users SET status=NULL WHERE user_id=? AND status='OneTime'",(uid,))
        final=db.execute('SELECT * FROM tokens WHERE token_id=?',(token_id,)).fetchone()
    return jsonify(token=token_public780(final),access_status=None)

def result_file(job_id,filename):
    uid=current_user_id()
    if filename not in RESULT_FILES780:abort(404,description='Result file not found.')
    with _media_lock760:
        row=separation_row760(job_id,uid)
        if row['files_deleted_at']:abort(410,description='Separation files were deleted.')
        if row['status']!='completed':abort(409,description='Separation is not complete.')
        token=token_access780(uid,row['separation_id'])
        if token is None:return jsonify(error='An active result-access token is required.',code=402,separation_id=row['separation_id']),402
        folder=_safe_media_folder760(RESULT_DIR,job_id)
        response=send_from_directory(folder,filename)
        _media_readers760[job_id]=_media_readers760.get(job_id,0)+1
    original=response.response
    state={'complete':False,'released':False}
    def release():
        if state['released']:return
        state['released']=True
        try:
            if state['complete'] and response.status_code==200 and token['type']=='OneTime':mark_file_received780(uid,row['separation_id'],token['token_id'],filename)
        finally:
            if hasattr(original,'close'):original.close()
            with _media_lock760:
                count=_media_readers760.get(job_id,1)-1
                if count:_media_readers760[job_id]=count
                else:_media_readers760.pop(job_id,None)
    def transfer():
        try:
            yield from original
            state['complete']=True
        finally:
            release()
    from werkzeug.wsgi import ClosingIterator
    response.response=ClosingIterator(transfer(),release)
    return response

# Replace the earlier registered view, retaining its route and auth middleware.
app.view_functions['result_file']=result_file

import bisect
_preview_locks780={}

def preview_window780(lyrics,total):
    length=min(30.,max(0.,float(total)))
    words=[w for w in (lyrics or {}).get('words',[]) if isinstance(w,dict) and isinstance(w.get('start'),(int,float)) and isinstance(w.get('end'),(int,float)) and w['end']>w['start']]
    times=sorted(max(0.,float(w['start'])) for w in words)
    if not times:return min(max(0.,total-length),max(0.,total/3)),length
    candidates={max(0.,min(total-length,t-2.)) for t in times}
    start=max(candidates,key=lambda x:(bisect.bisect_right(times,x+length)-bisect.bisect_left(times,x),-x))
    return start,length

def create_preview780(job_id,input_path,result,job_result_dir):
    folder=Path(job_result_dir);manifest=folder/'preview.json'
    if manifest.is_file():return json.loads(manifest.read_text(encoding='utf-8'))
    ffmpeg=find_ffmpeg()
    if not ffmpeg:raise RuntimeError('FFmpeg is required for preview generation.')
    probe=shutil.which('ffprobe') or str(Path(ffmpeg).with_name('ffprobe.exe' if os.name=='nt' else 'ffprobe'))
    measured=subprocess.run([probe,'-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(input_path)],capture_output=True,text=True,timeout=30)
    if measured.returncode:raise RuntimeError('Cannot measure preview source duration: '+measured.stderr[-300:])
    total=float(measured.stdout.strip());start,length=preview_window780(result.get('lyrics'),total)
    if length<=0:raise RuntimeError('Preview source is empty.')
    timed=any(isinstance(w,dict) and isinstance(w.get('start'),(int,float)) and isinstance(w.get('end'),(int,float)) and w['end']>w['start'] for w in (result.get('lyrics') or {}).get('words',[]))
    if not timed:
        # No transcript: use the most energetic vocal window instead of guessing.
        import array
        decoded=subprocess.run([ffmpeg,'-v','error','-i',str(folder/'vocals.mp3'),'-f','f32le','-ac','1','-ar','1000','pipe:1'],capture_output=True,timeout=120)
        if decoded.returncode==0:
            samples=array.array('f');samples.frombytes(decoded.stdout)
            if sys.byteorder!='little':samples.byteswap()
            energy=[sum(v*v for v in samples[i:i+1000]) for i in range(0,len(samples),1000)]
            width=max(1,int(length));scores=[0.]
            for e in energy:scores.append(scores[-1]+e)
            if energy:start=float(max(range(max(1,len(energy)-width+1)),key=lambda i:scores[min(len(energy),i+width)]-scores[i]))
            start=min(max(0.,total-length),start)
    def command(arguments):
        cmd=[ffmpeg,'-hide_banner','-loglevel','error','-y']+arguments
        if job_id in job_controls and not job_controls[job_id]['finished'].is_set():_run_job_command(job_id,cmd)
        else:
            run=subprocess.run(cmd,capture_output=True,text=True,timeout=120)
            if run.returncode:raise RuntimeError(run.stderr[-500:])
    encoding=['-t',str(length),'-vn','-ac','2','-ar','44100','-codec:a','libmp3lame','-b:a','128k']
    for name,source in [('original',input_path),('vocals',folder/'vocals.mp3')]:
        command(['-ss',str(start),'-i',str(source)]+encoding+[str(folder/('preview-'+name+'.mp3'))])
    inputs=[]
    for stem in ('drums','bass','guitar','piano','other'):inputs+=['-ss',str(start),'-i',str(folder/(stem+'.mp3'))]
    command(inputs+['-filter_complex','amix=inputs=5:normalize=0,alimiter=limit=0.98:level=0']+encoding+[str(folder/'preview-minus.mp3')])
    words=[]
    for word in (result.get('lyrics') or {}).get('words',[]):
        if not isinstance(word,dict) or not isinstance(word.get('start'),(int,float)) or not isinstance(word.get('end'),(int,float)):continue
        if word['end']>start and word['start']<start+length:
            words.append({'word':str(word.get('word','')),'start':max(0.,word['start']-start),'end':min(length,word['end']-start)})
    payload={'start':start,'duration':length,'words':words,'audio':{name:f'/preview/{job_id}/{name}.mp3' for name in ('original','vocals','minus')}}
    temporary=folder/'preview.tmp';temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8');temporary.replace(manifest)
    return payload

@app.route('/api/separations/<job_id>/preview')
def preview_metadata780(job_id):
    uid=current_user_id()
    with _media_lock760:
        row=separation_row760(job_id,uid)
        if row['files_deleted_at']:abort(410,description='Preview has expired.')
        if row['status']!='completed':abort(409,description='Separation is not complete.')
        folder=_safe_media_folder760(RESULT_DIR,job_id)
        lock=_preview_locks780.setdefault(job_id,threading.Lock())
        _media_readers760[job_id]=_media_readers760.get(job_id,0)+1
    try:
        with lock:
            saved=folder/'preview.json'
            if saved.is_file():return jsonify(json.loads(saved.read_text(encoding='utf-8')))
            result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
            upload=_safe_media_folder760(UPLOAD_DIR,job_id)
            sources=[p for p in upload.iterdir() if p.is_file() and not p.is_symlink()]
            if not sources:abort(404,description='Preview original audio is unavailable.')
            return jsonify(create_preview780(job_id,sources[0],result,folder))
    except FileNotFoundError:abort(404,description='Preview source is unavailable.')
    finally:
        with _media_lock760:
            count=_media_readers760.get(job_id,1)-1
            if count:_media_readers760[job_id]=count
            else:_media_readers760.pop(job_id,None)

@app.route('/preview/<job_id>/<filename>')
def preview_audio780(job_id,filename):
    uid=current_user_id()
    if filename not in ('original.mp3','vocals.mp3','minus.mp3'):abort(404)
    with _media_lock760:
        row=separation_row760(job_id,uid)
        if row['files_deleted_at']:abort(410)
        if row['status']!='completed':abort(409)
        folder=_safe_media_folder760(RESULT_DIR,job_id)
        response=send_from_directory(folder,'preview-'+filename)
        _media_readers760[job_id]=_media_readers760.get(job_id,0)+1
    def release():
        with _media_lock760:
            count=_media_readers760.get(job_id,1)-1
            if count:_media_readers760[job_id]=count
            else:_media_readers760.pop(job_id,None)
    from werkzeug.wsgi import ClosingIterator
    response.response=ClosingIterator(response.response,release)
    return response

SESSION_IDLE790 = 3600

def _deadline790():
    return (datetime.now(timezone.utc)+timedelta(seconds=SESSION_IDLE790)).isoformat(timespec='milliseconds').replace('+00:00','Z')

def _running790(db,sid):
    for row in db.execute("SELECT client_request_id FROM separations WHERE session_id=? AND status IN ('running','queued','pending')",(sid,)):
        job=jobs.get((row[0] or '').removeprefix('job:'))
        if job and job.get('status') not in ('done','error','aborted'):return True
    return False

def session_is_active760():
    sid,uid=session.get('session_id'),session.get('user_id')
    if not sid or uid is None:return False
    with db_connection() as db:
        row=db.execute('SELECT s.*,u.is_blocked FROM sessions s JOIN users u ON u.user_id=s.user_id WHERE s.session_id=? AND s.user_id=?',(sid,uid)).fetchone()
        if not row or row['revoked_at'] or row['is_blocked'] or not row['login_key']:return False
        if row['expires_at']<=_utc760():
            if _running790(db,sid):
                db.execute('UPDATE sessions SET expires_at=? WHERE session_id=?',(_deadline790(),sid))
            else:
                db.execute('UPDATE sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL',(_utc760(),sid));return False
        return True

def _device790():
    ua=request.headers.get('User-Agent','')[:1000]
    if 'Android' in ua:os_name='Android'
    elif any(x in ua for x in ('iPhone','iPad','iPod')):os_name='iOS'
    elif 'Windows' in ua:os_name='Windows'
    elif 'Macintosh' in ua:os_name='macOS'
    elif 'Linux' in ua:os_name='Linux'
    else:os_name='Не определено'
    kind='Планшет' if 'iPad' in ua or ('Android' in ua and 'Mobile' not in ua) else 'Телефон' if any(x in ua for x in ('Mobile','iPhone','iPod')) else 'Компьютер'
    if request.headers.get('X-MyNus-Client')=='WebView2':browser='WebView2'
    elif 'Edg/' in ua:browser='Edge'
    elif 'OPR/' in ua:browser='Opera'
    elif 'Firefox/' in ua:browser='Firefox'
    elif any(x in ua for x in ('Chrome/','CriOS/')):browser='Chrome'
    elif 'Safari/' in ua:browser='Safari'
    else:browser='Не определено'
    return kind,os_name,browser

def start_session760(uid):
    now=_utc760();device=_device790()
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        user=db.execute('SELECT is_blocked,max_active_sessions FROM users WHERE user_id=?',(uid,)).fetchone()
        if not user or user['is_blocked']:abort(403,description='Аккаунт заблокирован.')
        rows=db.execute('SELECT * FROM sessions WHERE user_id=? AND revoked_at IS NULL',(uid,)).fetchall()
        for row in rows:
            if row['expires_at']<=now and not _running790(db,row['session_id']):
                db.execute('UPDATE sessions SET revoked_at=? WHERE session_id=?',(now,row['session_id']))
        presented=request.headers.get('Authorization','').removeprefix('Bearer ')
        same=next((row for row in rows if row['login_key'] and secrets.compare_digest(row['login_key'],presented) and (row['expires_at']>now or _running790(db,row['session_id']))),None)
        if same:
            db.execute('UPDATE sessions SET last_seen_at=?,expires_at=?,device_type=?,os=?,browser=? WHERE session_id=?',(now,_deadline790(),*device,same['session_id']))
            sid=same['session_id']
        else:
            count=db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=? AND revoked_at IS NULL',(uid,)).fetchone()[0]
            if count>=user['max_active_sessions']:abort(409,description='Вход в аккаунт уже выполнен.')
            sid=secrets.token_hex(32)
            for attempt in range(8):
                key=secrets.token_urlsafe(8)
                try:
                    db.execute('INSERT INTO sessions(session_id,user_id,created_at,expires_at,last_seen_at,login_key,device_type,os,browser) VALUES(?,?,?,?,?,?,?,?,?)',(sid,uid,now,_deadline790(),now,key,*device));break
                except sqlite3.IntegrityError:
                    if attempt==7:raise
    session.clear();session.permanent=False;session.update(user_id=uid,session_id=sid)
    publish_account_event790(uid,sid,'Вход в аккаунт')

@app.before_request
def activity790():
    if request.path.startswith('/api/admin/') or request.path in ('/admin','/admin.html'):return
    if not session.get('session_id') or not session_is_active760():return
    background=request.headers.get('X-MyNus-Background')=='1' or request.path.startswith('/debug/') or request.path=='/api/device-logs'
    if not background and request.path not in ('/','/api/auth/logout') and not request.path.startswith('/api/auth/'):
        with db_connection() as db:
            db.execute('UPDATE sessions SET last_seen_at=?,expires_at=? WHERE session_id=? AND revoked_at IS NULL',(_utc760(),_deadline790(),session['session_id']))

def finish_session_operation790(db,separation_id):
    db.execute('UPDATE sessions SET last_seen_at=?,expires_at=? WHERE revoked_at IS NULL AND session_id=(SELECT session_id FROM separations WHERE separation_id=?)',(_utc760(),_deadline790(),separation_id))

def ensure_sessions790():
    expected={'users':['console_color','is_blocked'],'sessions':['login_key','device_type','os','browser'],'separations':['session_id'],'diagnostic_logs':['session_id']}
    with db_connection() as db:
        missing={table:[name for name in names if name not in columns(db,table)] for table,names in expected.items()}
        if any(missing.values()):raise RuntimeError('Выполните SQL обновления базы: '+str(missing))
        old=db.execute('SELECT COUNT(*) FROM sessions WHERE login_key IS NULL AND revoked_at IS NULL').fetchone()[0]
        if old:
            db.execute('UPDATE sessions SET revoked_at=? WHERE login_key IS NULL AND revoked_at IS NULL',(_utc760(),))
            print('[SERVER] Старые входы завершены при переходе на ключи 7.9.0: '+str(old))
ensure_sessions790()

# Simple administrator console. Independent of ordinary account/session limits.
from flask import send_file
from functools import wraps
from urllib.parse import urlsplit

def _admin_local_only():
    host=urlsplit('http://'+request.host).hostname
    proxied=any(request.headers.get(key) for key in ('Forwarded','X-Forwarded-For','X-Forwarded-Host','X-Forwarded-Proto','CF-Connecting-IP'))
    if request.remote_addr not in ('127.0.0.1','::1') or host not in ('127.0.0.1','localhost','::1') or proxied:
        abort(403,description='Админка без пароля доступна только через http://127.0.0.1:5000/admin на компьютере сервера.')
    origin=request.headers.get('Origin')
    if origin and origin.rstrip('/')!=request.host_url.rstrip('/'):
        abort(403,description='Откройте админку непосредственно на этом сервере.')

def admin_required(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        _admin_local_only()
        if not session.get('admin_csrf'):session['admin_csrf']=secrets.token_urlsafe(24)
        if request.method!='GET':
            csrf=request.headers.get('X-MyNus-Admin-CSRF','')
            if not csrf or not secrets.compare_digest(csrf,session['admin_csrf']):
                abort(403,description='Обновите страницу админки и повторите действие.')
        return function(*args,**kwargs)
    return wrapped

@app.route('/admin')
@app.route('/admin.html')
def admin_page():
    _admin_local_only()
    response=send_file(Path(BASE_DIR)/'admin.html')
    response.headers['Cache-Control']='no-store'
    response.headers['X-Frame-Options']='DENY'
    return response

def _ensure_admin_schema():
    with db_connection() as db:
        if 'is_blocked' in columns(db,'users'):return
        backup=Path(BASE_DIR)/('mynus-before-admin-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
        with sqlite3.connect(backup) as saved:db.backup(saved)
        db.execute('ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0 CHECK(is_blocked IN (0,1))')

def _admin_audit(db, event, target, details):
    if type(target) is int:
        uid=target
    else:
        row=db.execute('SELECT user_id FROM sessions WHERE session_id=?',(target,)).fetchone()
        if row is None:abort(404,description='Сессия не найдена.')
        uid=row[0]
    if not db.execute('SELECT 1 FROM users WHERE user_id=?',(uid,)).fetchone():abort(404,description='Пользователь не найден.')
    db.execute('''INSERT INTO diagnostic_logs(user_id,app_version,level,event,message,details_json)
                  VALUES (?,'7.8.4','info',?,?,?)''',
               (uid,'admin.'+event, 'Администратор: '+event, json.dumps({'target':target, **details}, ensure_ascii=False)))

def _admin_session(row):
    item=dict(row)
    item['current'] = item['session_id'] == session.get('session_id')
    item['device'] = 'Не определено'
    return item

ADMIN_PALETTE790=['#38BDF8','#4ADE80','#FACC15','#F472B6','#A78BFA','#FB923C','#2DD4BF','#F87171','#60A5FA','#A3E635','#C084FC','#FBBF24','#22D3EE','#34D399','#FB7185','#818CF8','#FDBA74','#5EEAD4','#C4B5FD','#F9A8D4','#FDE047','#7DD3FC','#86EFAC','#FCA5A5']
from collections import deque
_device_console790=deque(maxlen=6000)
_device_seq790=0
_device_lock790=threading.RLock()
_device_rates790={}

def _color790(user):return user['console_color'] or ADMIN_PALETTE790[(user['user_id']-1)%24]

def publish_account_event790(uid,sid,message):
    # Record account events before returning the login/logout response.
    # The logout client has already disconnected, so it cannot deliver this event.
    global _device_seq790
    with db_connection() as db:
        user=db.execute('SELECT user_id,email,console_color FROM users WHERE user_id=?',(uid,)).fetchone()
        device=db.execute('SELECT device_type,os,browser FROM sessions WHERE session_id=? AND user_id=?',(sid,uid)).fetchone()
    if not user or not device:return
    with _device_lock790:
        _device_seq790+=1
        _device_console790.append(dict(id=_device_seq790,time=_utc760(),user_id=uid,session_id=sid,device=' · '.join(device[name] or 'Не определено' for name in ('device_type','os','browser')),message='EVENT: '+message+' · '+(user['email'] or '#'+str(uid)),color=_color790(user)))



def _session_admin790(db,row):
    item=dict(row)
    item['device']=' · '.join(item.get(name) or 'Не определено' for name in ('device_type','os','browser'))
    item['separations']=[dict(r) for r in db.execute('SELECT separation_id,source_name,status FROM separations WHERE session_id=? ORDER BY separation_id DESC',(item['session_id'],))]
    item['operation_running']=_running790(db,item['session_id'])
    return item

@app.route('/api/admin/state')
@admin_required
def admin_state():
    q=request.args.get('q','').strip()[:200];uid=request.args.get('user_id','').strip()
    offset=max(0,request.args.get('offset',0,type=int) or 0)
    if uid and not uid.isdigit():abort(400,description='Неверный номер пользователя.')
    where='';params=[]
    if uid:where=' WHERE u.user_id=?';params=[int(uid)]
    if q:
        where+=(' AND ' if where else ' WHERE ')+"(instr(lower(coalesce(u.email,'')),lower(?))>0 OR instr(lower(coalesce(u.name,'')),lower(?))>0)";params += [q,q]
    with db_connection() as db:
        users=[]
        for row in db.execute('SELECT u.* FROM users u'+where+' ORDER BY u.user_id DESC LIMIT 200 OFFSET ?',params+[offset]):
            item=dict(row);item['color']=_color790(item);users.append(item)
        uc=db.execute('SELECT COUNT(*) FROM users u'+where,params).fetchone()[0]
        sw=where+(' AND ' if where else ' WHERE ')+'s.revoked_at IS NULL AND u.is_blocked=0'
        sessions=[]
        for row in db.execute('SELECT s.*,u.email FROM sessions s JOIN users u ON u.user_id=s.user_id'+sw+' ORDER BY s.created_at DESC',params):
            if row['expires_at']>_utc760() or _running790(db,row['session_id']):sessions.append(_session_admin790(db,row))
        sc=len(sessions);sessions=sessions[offset:offset+200]
        separations=[]
        for row in db.execute('SELECT s.*,u.email FROM separations s JOIN users u ON u.user_id=s.user_id'+where+' ORDER BY s.separation_id DESC LIMIT 200 OFFSET ?',params+[offset]):
            item=dict(row);job=jobs.get((item['client_request_id'] or '').removeprefix('job:'),{})
            item['progress']=job.get('progress',100 if item['status']=='completed' else None)
            item['file_status']='Удалены' if item['files_deleted_at'] else 'Результат готов' if item['status']=='completed' else 'Нет результата'
            separations.append(item)
        pc=db.execute('SELECT COUNT(*) FROM separations s JOIN users u ON u.user_id=s.user_id'+where,params).fetchone()[0]
        tokens=[dict(row) for row in db.execute('SELECT t.*,u.email FROM tokens t JOIN users u ON u.user_id=t.user_id'+where+' ORDER BY t.token_id DESC LIMIT 200 OFFSET ?',params+[offset])]
        tc=db.execute('SELECT COUNT(*) FROM tokens t JOIN users u ON u.user_id=t.user_id'+where,params).fetchone()[0]
        subs=[dict(row) for row in db.execute('SELECT s.*,u.email FROM subscriptions s JOIN users u ON u.user_id=s.user_id'+where+' ORDER BY s.subscription_id DESC',params)]
        for row in db.execute("SELECT u.* FROM users u"+where+(' AND ' if where else ' WHERE ')+"u.status='Subscription' AND NOT EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=u.user_id)",params):
            token=db.execute("SELECT * FROM tokens WHERE user_id=? AND type='Subscription' ORDER BY token_id DESC LIMIT 1",(row['user_id'],)).fetchone()
            subs.append(dict(subscription_id=None,user_id=row['user_id'],email=row['email'],plan_name='Subscription',status=token['status'] if token else 'Без токена',starts_at=token['issued_at'] if token else None,ends_at=token['expires_at'] if token else None))
        counts=dict(users=uc,sessions=sc,separations=pc,tokens=tc,subscriptions=len(subs))
    result=jsonify(csrf=session['admin_csrf'],server_code=app.secret_key,users=users,sessions=sessions,separations=separations,tokens=tokens,subscriptions=subs[offset:offset+200],counts=counts,offset=offset,limit=200,palette=ADMIN_PALETTE790)
    result.headers['Cache-Control']='no-store';return result

def _validate_user790(data):
    updates={}
    if 'status' in data:
        if data['status'] not in (None,'Subscription','OneTime'):abort(400,description='Неверный статус доступа.')
        updates['status']=data['status']
    if 'max_active_sessions' in data:
        value=data['max_active_sessions']
        if type(value) is not int or not 1<=value<=100:abort(400,description='Лимит сессий: от 1 до 100.')
        updates['max_active_sessions']=value
    if 'console_color' in data:
        value=data['console_color']
        if value is not None and (not isinstance(value,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value)):abort(400,description='Неверный цвет.')
        updates['console_color']=value
    if 'is_blocked' in data:
        if type(data['is_blocked']) is not bool:abort(400,description='Неверное значение блокировки.')
        updates['is_blocked']=int(data['is_blocked'])
    for key in ('first_name','last_name','email','phone','country'):
        if key in data:
            value=data[key]
            if not isinstance(value,str) or len(value)>254:abort(400,description='Неверное поле профиля: '+key)
            updates[key]=auth_email(value) if key=='email' else value.strip()
    if not updates:abort(400,description='Нет изменений.')
    return updates

def _update_user790(db,uid,updates):
    original=db.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone()
    if not original:abort(404,description='Пользователь не найден.')
    if 'first_name' in updates or 'last_name' in updates:updates['name']=(updates.get('first_name',original['first_name'] or '')+' '+updates.get('last_name',original['last_name'] or '')).strip()
    db.execute('UPDATE users SET '+','.join(name+'=?' for name in updates)+' WHERE user_id=?',list(updates.values())+[uid])
    if updates.get('is_blocked'):db.execute('UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(_utc760(),uid))
    _admin_audit(db,'user.update',uid,updates)

@app.route('/api/admin/users/<int:uid>',methods=['POST'])
@admin_required
def admin_update_user(uid):
    updates=_validate_user790(auth_request_data())
    try:
        with db_connection() as db:_update_user790(db,uid,updates)
    except sqlite3.IntegrityError:return jsonify(error='Эти данные уже используются другим аккаунтом.'),409
    return jsonify(ok=True)

@app.route('/api/admin/users/save-all',methods=['POST'])
@admin_required
def admin_save_all():
    data=auth_request_data();items=data.get('users')
    if not isinstance(items,list) or len(items)>200:abort(400,description='Допустимо до 200 изменений за раз.')
    checked=[]
    for item in items:
        if not isinstance(item,dict) or type(item.get('user_id')) is not int:abort(400,description='Неверный пользователь.')
        checked.append((item['user_id'],_validate_user790(item)))
    try:
        with db_connection() as db:
            for uid,updates in checked:_update_user790(db,uid,updates)
    except sqlite3.IntegrityError:return jsonify(error='Данные одного из пользователей уже используются. Изменения не сохранены.'),409
    return jsonify(ok=True,count=len(checked))

@app.route('/api/admin/sessions/revoke',methods=['POST'])
@admin_required
def admin_revoke_sessions():
    data=auth_request_data();uid=data.get('user_id');sid=data.get('session_id')
    with db_connection() as db:
        if sid:
            if not isinstance(sid,str) or len(sid)>128:abort(400)
            count=db.execute('UPDATE sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL',(_utc760(),sid)).rowcount
        elif type(uid) is int:count=db.execute('UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(_utc760(),uid)).rowcount
        else:abort(400)
        _admin_audit(db,'sessions.revoke',sid or uid,{'count':count})
    return jsonify(ok=True,count=count)

@app.route('/api/admin/users/<int:uid>/account')
@admin_required
def admin_account(uid):
    with db_connection() as db:
        row=db.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone()
        if not row:abort(404)
        user=dict(row);user['color']=_color790(user)
        payments=[dict(r) for r in db.execute('SELECT * FROM payments WHERE user_id=? ORDER BY payment_id DESC LIMIT 25',(uid,))]
        subscriptions=[dict(r) for r in db.execute('SELECT * FROM subscriptions WHERE user_id=? ORDER BY subscription_id DESC',(uid,))]
        tokens=[dict(r) for r in db.execute('SELECT * FROM tokens WHERE user_id=? ORDER BY token_id DESC LIMIT 50',(uid,))]
    return jsonify(user=user,payments=payments,subscriptions=subscriptions,tokens=tokens)

@app.route('/api/admin/users/<int:uid>/tokens/confirm',methods=['POST'])
@admin_required
def admin_confirm_token(uid):
    data=auth_request_data();kind=data.get('type')
    if kind not in ('Subscription','OneTime'):abort(400)
    with db_connection() as db:
        if not db.execute('SELECT 1 FROM users WHERE user_id=?',(uid,)).fetchone():abort(404)
        db.execute('UPDATE users SET status=? WHERE user_id=?',(kind,uid))
    token=token_access780(uid,renew=True)
    if not token:abort(409,description='Не удалось создать токен.')
    with db_connection() as db:_admin_audit(db,'payment.confirm',uid,{'type':kind,'token_id':token['token_id']})
    return jsonify(ok=True,token=token_public780(token),test_mode=True)

@app.route('/api/admin/separations/<int:sid>')
@admin_required
def admin_separation(sid):
    with db_connection() as db:
        row=db.execute('SELECT * FROM separations WHERE separation_id=?',(sid,)).fetchone()
        if not row:abort(404)
        tokens=[dict(r) for r in db.execute("SELECT * FROM tokens WHERE type='OneTime' AND separation_id=? ORDER BY token_id DESC",(sid,))]
        if not tokens:
            sub=db.execute("SELECT * FROM tokens WHERE user_id=? AND type='Subscription' ORDER BY CASE WHEN status='active' AND (expires_at IS NULL OR expires_at>?) THEN 0 ELSE 1 END, token_id DESC LIMIT 1",(row['user_id'],_utc760())).fetchone()
            if sub:tokens=[dict(sub)]
    return jsonify(separation=dict(row),tokens=tokens)

@app.route('/api/device-logs',methods=['POST'])
def device_logs790():
    uid=current_user_id();data=auth_request_data();records=data.get('records')
    if not isinstance(records,list) or len(records)>60:abort(400)
    if request.content_length and request.content_length>1200000:abort(413)
    with db_connection() as db:
        user=db.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone()
        row=db.execute('SELECT * FROM sessions WHERE session_id=?',(session['session_id'],)).fetchone()
    now=time.monotonic();sid=row['session_id']
    global _device_seq790
    with _device_lock790:
        start,count=_device_rates790.get(sid,(now,0))
        if now-start>=60:start,count=now,0
        if count+len(records)>1000:return jsonify(error='Слишком много сообщений.'),429
        _device_rates790[sid]=(start,count+len(records))
        for item in records:
            if not isinstance(item,dict) or not isinstance(item.get('message'),str):continue
            message=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',item['message'])[:16000]
            if re.search(r'\bEVENT:\s*Account signed (?:in|out)\b',message,re.IGNORECASE):continue
            _device_seq790+=1
            _device_console790.append(dict(id=_device_seq790,time=_utc760(),user_id=uid,session_id=sid,device=' · '.join(row[key] or 'Не определено' for key in ('device_type','os','browser')),message=message,color=_color790(user)))
    return jsonify(ok=True)

@app.route('/api/admin/console')
@admin_required
def admin_console790():
    after=max(0,request.args.get('device_after',0,type=int) or 0);server_after=max(0,request.args.get('server_after',0,type=int) or 0)
    uid=request.args.get('user_id','');sid=request.args.get('session_id','')[:128]
    if uid and not uid.isdigit():abort(400)
    with _device_lock790:
        records=[dict(r) for r in _device_console790 if r['id']>after and (not uid or r['user_id']==int(uid)) and (not sid or r['session_id']==sid)]
        cursor=_device_seq790
    with _server_log_condition:
        server=[dict(r,server=True,color='#FFFFFF') for r in _server_log_records if r['id']>server_after] if not uid and not sid else []
        server_cursor=_server_log_sequence
    with db_connection() as db:
        colors={row['user_id']:_color790(row) for row in db.execute('SELECT user_id,console_color FROM users')}
    for record in records:record['color']=colors.get(record['user_id'],record['color'])
    response=jsonify(devices=records,server=server,device_cursor=cursor,server_cursor=server_cursor)
    response.headers['Cache-Control']='no-store';return response


# MyNus 8.2.0: one CPU job, persistent timing/resource history, retry and app licence.
import contextlib
import math
import psutil
from collections import deque

_queue800 = deque()
_queue_condition800 = threading.Condition(threading.RLock())
_active800 = None
_metrics800 = {}
_local800 = threading.local()
_monitor_stop800 = threading.Event()
_system800 = {}

with db_connection() as db:
    db.execute('''CREATE TABLE IF NOT EXISTS tasks800(job_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,input_path TEXT NOT NULL,result_dir TEXT NOT NULL,language TEXT NOT NULL,phase TEXT NOT NULL,metrics_json TEXT NOT NULL)''')
    db.execute("CREATE TABLE IF NOT EXISTS licences800(user_id INTEGER PRIMARY KEY,level TEXT NOT NULL CHECK(level IN ('Basic','Advanced','Pro')))")
    # Preserve retry information for unfinished tasks created before 8.2.0 too.
    for row in db.execute("SELECT * FROM separations WHERE status IN ('running','queued','pending')").fetchall():
        job_id=row['client_request_id'].removeprefix('job:')
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',job_id):continue
        folder=_safe_media_folder760(UPLOAD_DIR,job_id)
        files=[p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
        path=next((p for p in files if p.name==secure_filename(row['source_name'] or '')),files[0] if files else folder/'missing-source')
        data={'phase':'Interrupted','stage':'Interrupted','retry_available':True,'stages':{},'resources':{}}
        db.execute('INSERT OR IGNORE INTO tasks800 VALUES(?,?,?,?,?,?,?)',(job_id,row['user_id'],str(path),str(_safe_media_folder760(RESULT_DIR,job_id)),'auto','Interrupted',json.dumps(data)))
    # Only unfinished work becomes Interrupted. Completed results and tokens are untouched.
    for row in db.execute("SELECT job_id,metrics_json FROM tasks800 WHERE phase IN ('Queued','Running')").fetchall():
        data=json.loads(row['metrics_json']);data.update(phase='Interrupted',stage='Interrupted',retry_available=True)
        db.execute("UPDATE tasks800 SET phase='Interrupted',metrics_json=? WHERE job_id=?",(json.dumps(data),row['job_id']))
    db.execute("UPDATE separations SET status='failed',finished_at=?,error_message='Interrupted: server restarted' WHERE status IN ('running','queued','pending')",(_utc760(),))


def _persist800(job_id):
    with _queue_condition800:
        m=dict(_metrics800[job_id]);spec=m.pop('_spec');m.pop('_started',None)
        m['stages']=dict(m.get('stages',{}));m['resources']=dict(m.get('resources',{}))
    with db_connection() as db:
        db.execute('INSERT OR REPLACE INTO tasks800 VALUES(?,?,?,?,?,?,?)',(job_id,spec['uid'],spec['input'],spec['output'],spec['language'],m['phase'],json.dumps(m)))


def _public800(job_id):
    with _queue_condition800:
        m=dict(_metrics800.get(job_id,{}))
        if not m:return None
        m.pop('_spec',None);started=m.pop('_started',None)
        m['stages']=dict(m.get('stages',{}));m['resources']=dict(m.get('resources',{}))
        if started is not None:m['elapsed_seconds']=round(time.perf_counter()-started,3)
        m['queue_position']=list(_queue800).index(job_id)+1 if job_id in _queue800 else 0
        return m


@contextlib.contextmanager
def _stage800(job_id,name):
    started=time.perf_counter()
    with _queue_condition800:_metrics800[job_id]['stage']=name
    try:yield
    finally:
        seconds=time.perf_counter()-started
        with _queue_condition800:
            stages=_metrics800[job_id]['stages'];stages[name]=round(stages.get(name,0)+seconds,3)
        print('[TIMING] '+job_id+' | '+name+' | '+str(round(seconds,3))+' s',flush=True)


_command_before800=_run_job_command

def _run_job_command(job_id,command,*args,**kwargs):
    # Called in the task thread; preview includes ffmpeg and is timed by its outer stage.
    if not getattr(_local800,'job',None):return _command_before800(job_id,command,*args,**kwargs)
    label='Separation' if 'demucs' in command else None
    if label:
        with _stage800(job_id,label):return _command_before800(job_id,command,*args,**kwargs)
    return _command_before800(job_id,command,*args,**kwargs)


def _timed800(fn,label):
    def call(*args,**kwargs):
        job_id=getattr(_local800,'job',None)
        if not job_id:return fn(*args,**kwargs)
        with _stage800(job_id,label):return fn(*args,**kwargs)
    return call

_run_whisperx=_timed800(_run_whisperx,'Lyrics and timings')
check_initial_lyrics_spelling=_timed800(check_initial_lyrics_spelling,'Spelling')
prepare_initial_transcription=_timed800(prepare_initial_transcription,'Transcription')
detect_tonic=_timed800(detect_tonic,'Tonic')
detect_vocal_range=_timed800(detect_vocal_range,'Vocal range')
create_preview780=_timed800(create_preview780,'Preview')


def _duration800(path):
    probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',path],capture_output=True,text=True,timeout=30,**({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    try:value=float(probe.stdout.strip())
    except (ValueError,TypeError):raise ValueError('Cannot read audio duration.')
    if probe.returncode or not math.isfinite(value) or value<=0:raise ValueError('Invalid audio.')
    if value>600:raise ValueError('Audio exceeds the 10 minute limit.')
    return value


_run_before800=run_demucs

def run_demucs(job_id,input_path,job_result_dir,lyrics_language='auto'):
    global _active800
    control=job_controls[job_id];started=time.perf_counter();acquired=False
    with _queue_condition800:
        _metrics800[job_id]={'phase':'Queued','stage':'Queued','submitted_at':_utc760(),'stages':{},'resources':{},'retry_available':False,'_started':started,'_spec':{'uid':jobs[job_id]['user_id'],'input':input_path,'output':job_result_dir,'language':lyrics_language}}
        jobs[job_id]['status']='queued';_queue800.append(job_id)
    try:
        _persist800(job_id)
        with db_connection() as db:db.execute("UPDATE separations SET status='pending' WHERE client_request_id=?",('job:'+job_id,))
        with _queue_condition800:
            while _active800 is not None or _queue800[0]!=job_id:
                _check_job_cancelled(job_id);_queue_condition800.wait(.25)
            _check_job_cancelled(job_id);_queue800.popleft();_active800=job_id;acquired=True
            _metrics800[job_id].update(phase='Running',stage='Validation',started_at=_utc760(),queue_seconds=round(time.perf_counter()-started,3))
            jobs[job_id]['status']='processing'
        _persist800(job_id)
        with db_connection() as db:db.execute("UPDATE separations SET status='running' WHERE client_request_id=?",('job:'+job_id,))
        _local800.job=job_id
        with _stage800(job_id,'Validation'):_metrics800[job_id]['audio_seconds']=_duration800(input_path)
        _run_before800(job_id,input_path,job_result_dir,lyrics_language)
    except SeparationAborted:
        jobs[job_id].update(status='aborted',error=None)
    except Exception as exc:
        jobs[job_id].update(status='error',error=str(exc));app.logger.exception('Task failed: %s',job_id)
    finally:
        _local800.job=None
        state=jobs[job_id].get('status');phase={'done':'Completed','aborted':'Interrupted','error':'Error'}.get(state,'Error')
        with _queue_condition800:
            m=_metrics800[job_id];m.update(phase=phase,stage=phase,finished_at=_utc760(),elapsed_seconds=round(time.perf_counter()-started,3),retry_available=phase in ('Interrupted','Error'))
            m['processing_seconds']=round(m['elapsed_seconds']-m.get('queue_seconds',m['elapsed_seconds']),3);m.pop('_started',None)
            if job_id in _queue800:_queue800.remove(job_id)
            if acquired:_active800=None
            _queue_condition800.notify_all()
        _persist800(job_id)
        final=_public800(job_id);jobs[job_id]['measurements']=final
        if phase=='Completed':
            path=Path(job_result_dir)/'result.json'
            try:
                result=json.loads(path.read_text(encoding='utf-8'));result['measurements']=final;path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
            except Exception:app.logger.exception('Cannot persist measurements for %s',job_id)
        print('[TIMING] '+job_id+' | '+phase+' | total '+str(final['elapsed_seconds'])+' s',flush=True)
        control['finished'].set()


_progress_before800=progress_payload760

def progress_payload760(job_id,uid):
    row=separation_row760(job_id,uid)
    m=_public800(job_id)
    if m is None:
        with db_connection() as db:stored=db.execute('SELECT metrics_json FROM tasks800 WHERE job_id=? AND user_id=?',(job_id,uid)).fetchone()
        m=json.loads(stored['metrics_json']) if stored else None
    if m and m['phase'] in ('Interrupted','Error'):
        return dict(job_id=job_id,status='error' if m['phase']=='Error' else 'interrupted',error=row.get('error_message') or m['phase'],measurements=m,retry_available=True,download_allowed=False)
    payload=_progress_before800(job_id,uid)
    if m:payload['measurements']=m;payload['queue_position']=m.get('queue_position',0)
    return payload


@app.route('/api/separations/<job_id>/retry',methods=['POST'])
def retry800(job_id):
    uid=current_user_id();auth_request_data();row=separation_row760(job_id,uid)
    with _queue_condition800:
        live=_metrics800.get(job_id)
        if live and live['phase'] in ('Queued','Running'):abort(409,description='Task is already active.')
        with db_connection() as db:stored=db.execute('SELECT * FROM tasks800 WHERE job_id=? AND user_id=?',(job_id,uid)).fetchone()
        if not stored or stored['phase'] not in ('Interrupted','Error'):abort(409,description='Only Interrupted and Error tasks can be retried.')
        if not Path(stored['input_path']).is_file():abort(410,description='Source audio has expired. Upload it again; no token was consumed.')
        # Preserve job ownership and token; no access token is created or consumed here.
        with db_connection() as db:
            db.execute("UPDATE tasks800 SET phase='Queued' WHERE job_id=?",(job_id,))
            db.execute("UPDATE separations SET status='running',finished_at=NULL,error_message=NULL,delete_after=NULL WHERE separation_id=?",(row['separation_id'],))
        job_controls[job_id]={'lock':threading.RLock(),'stop_lock':threading.Lock(),'cancel':threading.Event(),'finished':threading.Event(),'process':None}
        jobs[job_id]={'user_id':uid,'status':'queued','progress':0,'error':None}
        (Path(stored['result_dir'])/'result.json').unlink(missing_ok=True)
        threading.Thread(target=run_demucs,args=(job_id,stored['input_path'],stored['result_dir'],stored['language']),daemon=True).start()
        threading.Thread(target=watch_separation,args=(job_id,uid,row['separation_id']),daemon=True).start()
    return jsonify(job_id=job_id,status='queued')


def _monitor800():
    root=psutil.Process();previous={};last=time.perf_counter();disk=psutil.disk_io_counters();psutil.cpu_percent(None)
    while not _monitor_stop800.wait(1):
        now=time.perf_counter();dt=max(.001,now-last);last=now
        current_disk=psutil.disk_io_counters();memory=psutil.virtual_memory()
        _system800.update(cpu_percent=psutil.cpu_percent(None),ram_total_mb=round(memory.total/1048576),ram_available_mb=round(memory.available/1048576),disk_read_mb_s=round(max(0,current_disk.read_bytes-disk.read_bytes)/dt/1048576,3) if current_disk and disk else None,disk_write_mb_s=round(max(0,current_disk.write_bytes-disk.write_bytes)/dt/1048576,3) if current_disk and disk else None)
        disk=current_disk
        with _queue_condition800:job_id=_active800
        if not job_id:previous={};continue
        try:
            control=job_controls.get(job_id,{});process=control.get('process');procs=[root]
            if process:
                try:
                    child=psutil.Process(process.pid);procs += [child]+child.children(recursive=True)
                except psutil.Error:pass
            rss=cpu=read=write=0;current={}
            for proc in procs:
                try:
                    key=(proc.pid,proc.create_time());t=proc.cpu_times();total=t.user+t.system;io=proc.io_counters();current[key]=(total,io.read_bytes,io.write_bytes)
                    old=previous.get(key)
                    if old:cpu+=max(0,total-old[0]);read+=max(0,io.read_bytes-old[1]);write+=max(0,io.write_bytes-old[2])
                    rss+=proc.memory_info().rss
                except (psutil.Error,OSError):pass
            previous=current;memory=psutil.virtual_memory();sample={'cpu_percent':round(100*cpu/dt/(psutil.cpu_count() or 1),1),'ram_mb':round(rss/1048576,1),'read_mb_s':round(read/dt/1048576,3),'write_mb_s':round(write/dt/1048576,3)}
            with _queue_condition800:
                resources=_metrics800[job_id]['resources'];resources.update(sample);resources['samples']=resources.get('samples',0)+1
                for key in ('cpu_percent','ram_mb','read_mb_s','write_mb_s'):resources['peak_'+key]=max(resources.get('peak_'+key,0),sample[key])
                resources['cpu_sum']=resources.get('cpu_sum',0)+sample['cpu_percent'];resources['average_cpu_percent']=round(resources['cpu_sum']/resources['samples'],1)
        except Exception:app.logger.exception('Resource monitor sample failed')


@app.route('/api/admin/monitor')
@admin_required
def admin_monitor800():
    memory=psutil.virtual_memory()
    with _queue_condition800:ids=list(_metrics800)
    with db_connection() as db:
        stored=[json.loads(r['metrics_json'])|{'job_id':r['job_id'],'user_id':r['user_id']} for r in db.execute('SELECT * FROM tasks800 ORDER BY rowid DESC LIMIT 100')]
        users=[dict(r) for r in db.execute("SELECT u.user_id,u.email,COALESCE(l.level,'Basic') AS level FROM users u LEFT JOIN licences800 l ON l.user_id=u.user_id")]
    live={id:_public800(id) for id in ids}
    with db_connection() as db:
        names={r['client_request_id'].removeprefix('job:'):r['source_name'] for r in db.execute('SELECT client_request_id,source_name FROM separations')}
    for item in stored:
        if item['job_id'] in live:item.update(live[item['job_id']])
        item['source_name']=names.get(item['job_id'],item['job_id'])
    return jsonify(server_version='8.4.0',system=dict(_system800),cpu_percent=_system800.get('cpu_percent',0),ram_total_mb=round(memory.total/1048576),ram_available_mb=round(memory.available/1048576),tasks=stored,licences=users)


@app.route('/api/admin/licence',methods=['POST'])
@admin_required
def admin_licence800():
    data=auth_request_data();level=data.get('level');uid=data.get('user_id')
    if level not in ('Basic','Advanced','Pro') or type(uid) is not int:abort(400)
    with db_connection() as db:
        if not db.execute('SELECT 1 FROM users WHERE user_id=?',(uid,)).fetchone():abort(404)
        db.execute('INSERT OR REPLACE INTO licences800 VALUES(?,?)',(uid,level));_admin_audit(db,'licence.change',uid,{'level':level})
    return jsonify(ok=True,level=level)


@app.route('/api/licence')
def licence800():
    uid=current_user_id()
    with db_connection() as db:row=db.execute('SELECT level FROM licences800 WHERE user_id=?',(uid,)).fetchone()
    return jsonify(level=row['level'] if row else 'Basic',server_version='8.4.0')

threading.Thread(target=_monitor800,daemon=True).start()

# Add persistent phase information to the existing user's separation list.
_list_before800=list_separations760

def list_separations760():
    uid=current_user_id();response=_list_before800();payload=response.get_json()
    with db_connection() as db:
        states={r['job_id']:(r['phase'],json.loads(r['metrics_json'])) for r in db.execute('SELECT job_id,phase,metrics_json FROM tasks800 WHERE user_id=?',(uid,))}
    for item in payload.get('separations',[]):
        job_id=item['client_request_id'].removeprefix('job:');state=states.get(job_id)
        if state:item.update(phase=state[0],retry_available=state[0] in ('Interrupted','Error'),measurements=_public800(job_id) or state[1])
    return jsonify(payload)

app.view_functions['list_separations760']=list_separations760


# MyNus 8.2.0 — retention, queue estimate, latest completed result and chart history.
from statistics import median
from zoneinfo import ZoneInfo
_israel801=ZoneInfo('Asia/Jerusalem')
_history801=deque(maxlen=900)

with db_connection() as db:
    # Replace only the retention triggers containing the old +24 hours policy.
    for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall():
        if row['sql'] and '+24 hours' in row['sql']:
            db.execute('DROP TRIGGER "'+row['name'].replace('"','""')+'"')
            db.execute(row['sql'].replace('+24 hours','+100 hours'))
    db.execute("UPDATE separations SET delete_after=strftime('%Y-%m-%dT%H:%M:%fZ',finished_at,'+100 hours') WHERE finished_at IS NOT NULL AND files_deleted_at IS NULL")

# Silence successful monitor polling at the logging source; retain HTTP errors.
class MonitorFilter801(logging.Filter):
    def filter(self,record):
        message=record.getMessage()
        return not ('/api/admin/monitor' in message and re.search(r'HTTP/[^\"]*\"\s+(?:2\d\d|304)\b',message))
logging.getLogger('werkzeug').addFilter(MonitorFilter801())


def _sample_history801():
    while not _monitor_stop800.wait(1):
        with _queue_condition800:
            sample=dict(_system800)
            if sample:
                sample['time']=_utc760();_history801.append(sample)
threading.Thread(target=_sample_history801,daemon=True).start()


def _queue_info801():
    with _queue_condition800:
        active=_active800;queued=list(_queue800);live={key:_public800(key) for key in ([active] if active else [])+queued}
    with db_connection() as db:
        rows=db.execute("SELECT metrics_json FROM tasks800 WHERE phase='Completed' ORDER BY rowid DESC LIMIT 100").fetchall()
        users={r['user_id']:(r['name'] or r['email'] or '#'+str(r['user_id'])) for r in db.execute('SELECT user_id,name,email FROM users')}
    totals=[];ratios=[]
    for row in rows:
        data=json.loads(row['metrics_json']);seconds=data.get('processing_seconds')
        if seconds and seconds>0:
            totals.append(seconds)
            if data.get('audio_seconds',0)>0:ratios.append(seconds/data['audio_seconds'])
    average=median(totals) if totals else 300
    def expected(key):
        data=live.get(key) or {};audio=data.get('audio_seconds')
        return median(ratios)*audio if audio and ratios else average
    remaining=0
    if active:
        data=live.get(active) or {};elapsed=max(0,data.get('elapsed_seconds',0)-data.get('queue_seconds',0));remaining=max(0,expected(active)-elapsed)
    wait=remaining+sum(expected(key) for key in queued[:-1]) if queued else 0
    uid=jobs.get(active,{}).get('user_id') if active else None
    return {'active_job':active,'active_username':users.get(uid,'#'+str(uid)) if active else None,'queued':len(queued),'max_wait_seconds':round(wait),'estimate_based_on':len(totals)}


_monitor_before801=admin_monitor800

def admin_monitor801():
    response=_monitor_before801();data=response.get_json();data['server_version']='8.4.0';data.pop('licences',None)
    data['queue']=_queue_info801()
    with _queue_condition800:data['history']=list(_history801)
    return jsonify(data)
app.view_functions['admin_monitor800']=admin_monitor801


_account_before801=admin_account

def admin_account801(uid):
    response=_account_before801(uid);data=response.get_json()
    with db_connection() as db:row=db.execute('SELECT level FROM licences800 WHERE user_id=?',(uid,)).fetchone()
    data['user']['app_level']=row['level'] if row else 'Basic'
    return jsonify(data)
app.view_functions['admin_account']=admin_account801


@app.route('/api/separations/latest')
def latest_result801():
    uid=current_user_id()
    with db_connection() as db:
        rows=db.execute("SELECT * FROM separations WHERE user_id=? AND status='completed' AND files_deleted_at IS NULL AND (delete_after IS NULL OR delete_after>?) ORDER BY finished_at DESC,separation_id DESC",(uid,_utc760())).fetchall()
    for row in rows:
        job_id=row['client_request_id'].removeprefix('job:')
        try:folder=_safe_media_folder760(RESULT_DIR,job_id)
        except ValueError:continue
        if all((folder/name).is_file() for name in RESULT_FILES780):
            return jsonify(job_id=job_id,source=row['source_name'],separation_id=row['separation_id'],finished_at=row['finished_at'],delete_after=row['delete_after'])
    return jsonify(error='No completed separation result is available.'),404

# Enforce expiry promptly, rather than waiting for the next midnight.
def cleanup_worker760():
    while True:
        cleanup_media760()
        time.sleep(60)

# Console request timestamps follow Israel regardless of Windows timezone.
from werkzeug.serving import WSGIRequestHandler
WSGIRequestHandler.log_date_time_string=lambda self:datetime.now(_israel801).strftime('%d.%m.%Y %H:%M:%S')

for _logger801 in (app.logger,logging.getLogger('werkzeug'),logging.getLogger()):
    for _handler801 in _logger801.handlers:
        if _handler801.formatter:
            _handler801.formatter.datefmt='%d.%m.%Y %H:%M:%S'
            _handler801.formatter.converter=lambda timestamp:datetime.fromtimestamp(timestamp,_israel801).timetuple()


# MyNus 8.2.0 — explicit token application and account-wide access notifications.
import calendar
_access_condition820=threading.Condition()
with db_connection() as db:
    if 'applied_at' not in columns(db,'tokens'):db.execute('ALTER TABLE tokens ADD COLUMN applied_at TEXT')
    db.execute('CREATE TABLE IF NOT EXISTS access_events820(event_id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS access_events_user820 ON access_events820(user_id,event_id)')
    db.execute('CREATE TABLE IF NOT EXISTS admin_grants820(request_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,kind TEXT NOT NULL,term TEXT,token_id INTEGER NOT NULL)')

_token_public_before820=token_public780

def token_public780(row):
    data=_token_public_before820(row);data['received_at']=row['issued_at'];data['used_at']=row['closed_at'] if row['status']=='used' else None
    return data

_token_before820=token_access780

def token_access780(uid,separation_id=None,issue=True,renew=False):
    # Preserve the explicit debug confirmation flow. Reading a result never binds or issues a token.
    if separation_id is None and issue:return _token_before820(uid,None,issue,renew)
    with db_connection() as db:
        if separation_id is not None:
            paid = db.execute(
                "SELECT * FROM tokens WHERE user_id=? AND type='OneTime' AND status='used' AND separation_id=? ORDER BY token_id LIMIT 1",
                (uid, separation_id)
            ).fetchone()
            if paid:
                return dict(paid)
        rows=db.execute("SELECT * FROM tokens WHERE user_id=? AND status='active' AND (expires_at IS NULL OR julianday(expires_at)>julianday('now')) ORDER BY CASE type WHEN 'Subscription' THEN 0 ELSE 1 END,token_id",(uid,)).fetchall()
    for row in rows:
        if row['type']=='Subscription' or (separation_id is not None and row['separation_id']==separation_id):return dict(row)
    return None


def _access_summary820(uid):
    with db_connection() as db:
        db.execute("UPDATE subscriptions SET status='expired' WHERE user_id=? AND status='active' AND julianday(ends_at)<=julianday('now')",(uid,))
        db.execute("UPDATE tokens SET status='expired',closed_at=? WHERE user_id=? AND status='active' AND expires_at IS NOT NULL AND julianday(expires_at)<=julianday('now')",(_utc760(),uid))
        rows=db.execute("SELECT * FROM tokens WHERE user_id=? AND status='active' AND (expires_at IS NULL OR julianday(expires_at)>julianday('now')) ORDER BY token_id DESC",(uid,)).fetchall()
    subscription=next((row for row in rows if row['type']=='Subscription'),None)
    return {'access_status':'Subscription' if subscription else 'OneTime' if rows else None,'available_tokens':sum(1 for row in rows if row['type']=='OneTime' and row['separation_id'] is None),'subscription_expires_at':subscription['expires_at'] if subscription else None}


def _event820(db,uid,message):
    db.execute('INSERT INTO access_events820(user_id,message,created_at) VALUES(?,?,?)',(uid,message,_utc760()))


@app.route('/api/account/access-events')
def access_events820():
    uid=current_user_id();after=request.args.get('after',type=int)
    def read():
        with db_connection() as db:
            if after is None:
                cursor=db.execute('SELECT COALESCE(MAX(event_id),0) FROM access_events820 WHERE user_id=?',(uid,)).fetchone()[0]
                return cursor,[]
            rows=db.execute('SELECT * FROM access_events820 WHERE user_id=? AND event_id>? ORDER BY event_id LIMIT 100',(uid,after)).fetchall()
            return (rows[-1]['event_id'] if rows else after),[dict(row) for row in rows]
    with _access_condition820:
        cursor,events=read()
        if after is not None and not events:
            _access_condition820.wait(timeout=20);cursor,events=read()
    if not session_is_active760():abort(401)
    response=jsonify(cursor=cursor,events=events,**_access_summary820(uid));response.headers['Cache-Control']='no-store';return response


@app.route('/api/admin/users/<int:uid>/grant-access',methods=['POST'])
@admin_required
def grant_access820(uid):
    data=auth_request_data();kind=data.get('type');term=data.get('term');key=data.get('request_id')
    if kind not in ('OneTime','Subscription') or (kind=='Subscription' and term not in ('Month','Year','Lifetime')):abort(400,description='Select a valid access type and duration.')
    try:uuid.UUID(str(key))
    except (ValueError,TypeError):abort(400,description='Invalid request ID.')
    action='Send Token' if kind=='OneTime' else 'Set Subscription '+str(term)
    try:
        with db_connection() as db:
            db.execute('BEGIN IMMEDIATE');user=db.execute('SELECT email FROM users WHERE user_id=?',(uid,)).fetchone()
            if not user:abort(404)
            previous=db.execute('SELECT * FROM admin_grants820 WHERE request_id=?',(key,)).fetchone()
            if previous:
                if (previous['user_id'],previous['kind'],previous['term'])!=(uid,kind,term):abort(409,description='Request ID already used.')
                row=db.execute('SELECT * FROM tokens WHERE token_id=?',(previous['token_id'],)).fetchone()
                return jsonify(ok=True,token=token_public780(row),already_processed=True)
            now=datetime.now(timezone.utc);expiry=None
            if kind=='Subscription':
                if term=='Month':
                    year=now.year+(now.month==12);month=now.month%12+1;end=now.replace(year=year,month=month,day=min(now.day,calendar.monthrange(year,month)[1]))
                elif term=='Year':end=now.replace(year=now.year+1,day=min(now.day,calendar.monthrange(now.year+1,now.month)[1]))
                else:end=None
                expiry=end.isoformat(timespec='milliseconds').replace('+00:00','Z') if end else None
                db.execute("UPDATE tokens SET status='revoked',closed_at=? WHERE user_id=? AND type='Subscription' AND status='active'",(_utc760(),uid))
                db.execute("UPDATE subscriptions SET status='cancelled' WHERE user_id=? AND status='active'",(uid,))
                db.execute("INSERT INTO subscriptions(user_id,plan_name,status,starts_at,ends_at) VALUES(?,?,'active',?,?)",(uid,term,_utc760(),expiry or '9999-12-31T23:59:59.999Z'))
            value=('ONE' if kind=='OneTime' else 'SUB')+secrets.token_urlsafe(8)
            token_id=db.execute("INSERT INTO tokens(user_id,token,type,expires_at) VALUES(?,?,?,?)",(uid,value,kind,expiry)).lastrowid
            db.execute('UPDATE users SET status=? WHERE user_id=?',(kind,uid))
            db.execute('INSERT INTO admin_grants820 VALUES(?,?,?,?,?)',(key,uid,kind,term,token_id))
            message='Admin → '+user['email']+' → '+action+' → Success · token #'+str(token_id)
            _admin_audit(db,'access.grant',uid,{'action':action,'token_id':token_id,'term':term});_event820(db,uid,message)
            row=db.execute('SELECT * FROM tokens WHERE token_id=?',(token_id,)).fetchone()
        with _access_condition820:_access_condition820.notify_all()
        _publish_server_line(message,'INFO')
        return jsonify(ok=True,token=token_public780(row))
    except Exception:
        _publish_server_line('Admin → user #'+str(uid)+' → '+action+' → Failed','ERROR');raise


@app.route('/api/separations/<job_id>/apply-token',methods=['POST'])
def apply_token820(job_id):
    uid=current_user_id();auth_request_data();separation=separation_row760(job_id,uid)
    if separation['status']!='completed' or separation['files_deleted_at'] or (separation['delete_after'] and separation['delete_after']<=_utc760()):abort(409,description='This result is not available.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        paid = db.execute(
            "SELECT * FROM tokens WHERE user_id=? AND type='OneTime' AND status='used' AND separation_id=? ORDER BY token_id LIMIT 1",
            (uid, separation['separation_id'])
        ).fetchone()
        if paid:
            return jsonify(ok=True, token=token_public780(paid))
        row=db.execute("SELECT * FROM tokens WHERE user_id=? AND status='active' AND (expires_at IS NULL OR julianday(expires_at)>julianday('now')) AND (type='Subscription' OR separation_id=?) ORDER BY CASE type WHEN 'Subscription' THEN 0 ELSE 1 END,token_id LIMIT 1",(uid,separation['separation_id'])).fetchone()
        if row is None:
            row=db.execute("SELECT * FROM tokens WHERE user_id=? AND type='OneTime' AND status='active' AND separation_id IS NULL AND (expires_at IS NULL OR julianday(expires_at)>julianday('now')) ORDER BY token_id LIMIT 1",(uid,)).fetchone()
            if row is None:
                bound=db.execute("SELECT token_id,separation_id FROM tokens WHERE user_id=? AND type='OneTime' AND status='active' AND separation_id IS NOT NULL ORDER BY token_id DESC LIMIT 1",(uid,)).fetchone()
                if bound:abort(409,description='Token #'+str(bound['token_id'])+' is already applied to Separation #'+str(bound['separation_id'])+'. Open Account / Payments for another token.')
                abort(409,description='No available token. Open Account / Payments to view access.')
            db.execute('UPDATE tokens SET separation_id=?,applied_at=? WHERE token_id=? AND separation_id IS NULL',(separation['separation_id'],_utc760(),row['token_id']))
            row=db.execute('SELECT * FROM tokens WHERE token_id=?',(row['token_id'],)).fetchone()
            _event820(db,uid,'Token #'+str(row['token_id'])+' applied to Separation #'+str(separation['separation_id']))
    with _access_condition820:_access_condition820.notify_all()
    return jsonify(ok=True,token=token_public780(row))


_ack_before820=acknowledge_result780

def acknowledge_result820(job_id):
    response=_ack_before820(job_id);data=response.get_json()
    if data.get('token',{}).get('status')=='used':
        uid=current_user_id();message='Token #'+str(data['token']['token_id'])+' used for Separation #'+str(data['token']['separation_id'])
        with db_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM access_events820 WHERE user_id=? AND message=?',(uid,message)).fetchone():_event820(db,uid,message)
        with _access_condition820:_access_condition820.notify_all()
    return response
app.view_functions['acknowledge_result780']=acknowledge_result820

_tokens_view820=account_tokens780

def account_tokens820():
    response=_tokens_view820();data=response.get_json();data.update(_access_summary820(current_user_id()));return jsonify(data)
app.view_functions['account_tokens780']=account_tokens820

_payments_view820=account_payments

def account_payments820():
    response=_payments_view820();data=response.get_json();data.update(_access_summary820(current_user_id()))
    sub=data.get('subscription')
    if data['access_status']!='Subscription' or not sub or sub['status']!='active':data['subscription']=None
    return jsonify(data)
app.view_functions['account_payments']=account_payments820
# Keep the existing debug buttons functional and notify other sessions too.
_debug_confirm820=confirm_token780

def debug_confirm820():
    response=_debug_confirm820()
    if isinstance(response,tuple):return response
    data=response.get_json()
    if data.get('token'):
        uid=current_user_id()
        with db_connection() as db:_event820(db,uid,'Access updated · token #'+str(data['token']['token_id']))
        with _access_condition820:_access_condition820.notify_all()
    return response
app.view_functions['confirm_token780']=debug_confirm820

# Long-poll transport is silent on success; actual access events are logged above.
class AccessTransportFilter820(logging.Filter):
    def filter(self,record):
        text=record.getMessage()
        return not ('/api/account/access-events' in text and re.search(r'HTTP/[^\"]*\"\s+(?:2\d\d|304)\b',text))
logging.getLogger('werkzeug').addFilter(AccessTransportFilter820())

_admin_account_before820=app.view_functions['admin_account']
def admin_account_access820(uid):
    response=_admin_account_before820(uid);data=response.get_json();data['user']['access_status']=_access_summary820(uid)['access_status'];return jsonify(data)
app.view_functions['admin_account']=admin_account_access820



@app.route('/api/separations/<job_id>/source',methods=['GET'])
def separation_source820(job_id):
    from urllib.parse import quote
    uid=current_user_id()
    with _media_lock760:
        row=separation_row760(job_id,uid)
        if row['files_deleted_at'] or (row['delete_after'] and row['delete_after']<=_utc760()):abort(410,description='The original audio has expired.')
        if row['status']!='completed':abort(409,description='Separation is not complete.')
        if token_access780(uid,row['separation_id'],issue=False) is None:return jsonify(error='Apply access to this result first.'),402
        folder=_safe_media_folder760(UPLOAD_DIR,job_id)
        files=[p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
        source=next((p for p in files if p.name==secure_filename(row['source_name'] or '')),files[0] if len(files)==1 else None)
        if source is None:abort(410,description='The original audio is no longer available.')
        response=send_file(source,conditional=False)
        response.headers['X-MyNus-Source-Name']=quote(row['source_name'] or source.name,safe='')
        response.headers['Access-Control-Expose-Headers']='X-MyNus-Source-Name'
        response.headers['Cache-Control']='no-store'
        return response

# MyNus 8.3.0 — private user/admin messaging; independent from media tasks.
CHAT_FILES830=Path(BASE_DIR)/'messages'
CHAT_FILE_LIMIT830=256*1024*1024
CHAT_EXT830={'.log','.txt','.json','.wav','.mynus','.zip','.png','.jpg','.jpeg','.webp'}
with db_connection() as db:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_messages830'").fetchone():
        backup=Path(BASE_DIR)/('mynus-before-messages-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
        with sqlite3.connect(backup) as saved:db.backup(saved)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS chat_messages830(message_id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(user_id),sender TEXT NOT NULL CHECK(sender IN ('user','admin')),text TEXT NOT NULL,created_at TEXT NOT NULL,nonce TEXT NOT NULL,UNIQUE(user_id,sender,nonce));
    CREATE INDEX IF NOT EXISTS chat_user830 ON chat_messages830(user_id,message_id);
    CREATE TABLE IF NOT EXISTS chat_files830(file_id TEXT PRIMARY KEY,message_id INTEGER NOT NULL REFERENCES chat_messages830(message_id),name TEXT NOT NULL,size INTEGER NOT NULL,mime TEXT NOT NULL,stored_name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS chat_reads830(user_id INTEGER PRIMARY KEY REFERENCES users(user_id),user_read INTEGER NOT NULL DEFAULT 0,admin_read INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS chat_presence830(session_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(user_id),seen_at TEXT NOT NULL);
    ''')

def _chat_number830(name,default=0):
    try:n=int(request.args.get(name,default))
    except (ValueError,TypeError):abort(400,description='Invalid message cursor.')
    if n<0:abort(400,description='Invalid message cursor.')
    return n

def _chat_origin830():
    origin=request.headers.get('Origin')
    expected=request.host_url.rstrip('/')
    if request.remote_addr in ('127.0.0.1','::1') and request.headers.get('X-Forwarded-Proto') in ('http','https'):
        expected=request.headers['X-Forwarded-Proto']+'://'+request.host
    if origin and origin.rstrip('/')!=expected and origin not in DESKTOP_ORIGINS:abort(403,description='Use the MyNus application.')

def _chat_online830(db,uid):
    cutoff=(datetime.now(timezone.utc)-timedelta(seconds=45)).isoformat(timespec='milliseconds').replace('+00:00','Z')
    return db.execute('''SELECT 1 FROM chat_presence830 p JOIN sessions s ON s.session_id=p.session_id WHERE p.user_id=? AND p.seen_at>? AND s.revoked_at IS NULL AND s.expires_at>? LIMIT 1''',(uid,cutoff,_utc760())).fetchone() is not None

def _chat_messages830(uid):
    after=_chat_number830('after');before=_chat_number830('before')
    with db_connection() as db:
        if not db.execute('SELECT 1 FROM users WHERE user_id=?',(uid,)).fetchone():abort(404)
        if before:rows=db.execute('SELECT * FROM chat_messages830 WHERE user_id=? AND message_id<? ORDER BY message_id DESC LIMIT 101',(uid,before)).fetchall();older=len(rows)>100;rows=list(reversed(rows[:100]))
        elif after:rows=db.execute('SELECT * FROM chat_messages830 WHERE user_id=? AND message_id>? ORDER BY message_id LIMIT 100',(uid,after)).fetchall();older=False
        else:rows=db.execute('SELECT * FROM chat_messages830 WHERE user_id=? ORDER BY message_id DESC LIMIT 101',(uid,)).fetchall();older=len(rows)>100;rows=list(reversed(rows[:100]))
        result=[]
        for row in rows:
            item=dict(row);item.pop('nonce',None)
            item['files']=[{k:f[k] for k in ('file_id','name','size','mime')} for f in db.execute('SELECT * FROM chat_files830 WHERE message_id=?',(row['message_id'],))]
            result.append(item)
        read=db.execute('SELECT * FROM chat_reads830 WHERE user_id=?',(uid,)).fetchone()
        ur=read['user_read'] if read else 0;ar=read['admin_read'] if read else 0
        unread_user=db.execute("SELECT count(*) FROM chat_messages830 WHERE user_id=? AND sender='admin' AND message_id>?",(uid,ur)).fetchone()[0]
        unread_admin=db.execute("SELECT count(*) FROM chat_messages830 WHERE user_id=? AND sender='user' AND message_id>?",(uid,ar)).fetchone()[0]
        user=db.execute('SELECT user_id,email FROM users WHERE user_id=?',(uid,)).fetchone()
        return jsonify(messages=result,has_older=older,user=dict(user),online=_chat_online830(db,uid),user_read=ur,admin_read=ar,unread_user=unread_user,unread_admin=unread_admin)

def _chat_send830(uid,sender):
    import uuid,mimetypes
    _chat_origin830()
    if request.content_length and request.content_length>CHAT_FILE_LIMIT830*2:abort(413,description='Attachments exceed 512 MB.')
    data=request.get_json(silent=True) if request.is_json else request.form
    if data is None:abort(400)
    text=str(data.get('text','')).strip();nonce=str(data.get('nonce',''))
    try:uuid.UUID(nonce)
    except (ValueError,AttributeError):abort(400,description='Invalid message ID.')
    if len(text)>10000:abort(400,description='Message is longer than 10000 characters.')
    files=request.files.getlist('files')
    if len(files)>10:abort(400,description='Attach no more than 10 files at a time.')
    if not text and not files:abort(400,description='Enter a message or attach a file.')
    prepared=[];committed=False
    try:
        with db_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE user_id=?',(uid,)).fetchone():abort(404)
            old=db.execute('SELECT message_id FROM chat_messages830 WHERE user_id=? AND sender=? AND nonce=?',(uid,sender,nonce)).fetchone()
            if old:return jsonify(ok=True,message_id=old[0],duplicate=True)
            for source in files:
                name=re.sub(r'[\x00-\x1f\x7f]','',str(source.filename or '').replace('\\','/').split('/')[-1])
                ext=Path(name).suffix.lower()
                if not name or len(name)>240 or ext not in CHAT_EXT830:abort(400,description='Unsupported attachment. Use LOG, TXT, JSON, WAV, MyNus, ZIP, PNG, JPG or WebP.')
                CHAT_FILES830.mkdir(parents=True,exist_ok=True)
                file_id=uuid.uuid4().hex;path=CHAT_FILES830/(file_id+ext);size=0
                prepared.append((file_id,name,path,size,mimetypes.guess_type(name)[0] or 'application/octet-stream'))
                with path.open('xb') as output:
                    while True:
                        chunk=source.stream.read(262144)
                        if not chunk:break
                        size+=len(chunk)
                        if size>CHAT_FILE_LIMIT830:abort(413,description='An attachment exceeds 256 MB.')
                        output.write(chunk)
                if sum(x[3] for x in prepared)+size>CHAT_FILE_LIMIT830*2:abort(413,description='Attachments exceed 512 MB.')
                prepared[-1]=(file_id,name,path,size,prepared[-1][4])
            now=_utc760()
            mid=db.execute('INSERT INTO chat_messages830(user_id,sender,text,created_at,nonce) VALUES(?,?,?,?,?)',(uid,sender,text,now,nonce)).lastrowid
            for fid,name,path,size,mime in prepared:db.execute('INSERT INTO chat_files830 VALUES(?,?,?,?,?,?)',(fid,mid,name,size,mime,path.name))
        committed=True
        _publish_server_line('Messages: '+('Admin → user #'+str(uid) if sender=='admin' else 'user #'+str(uid)+' → Admin')+' · message #'+str(mid),'INFO')
        return jsonify(ok=True,message_id=mid)
    finally:
        if not committed:
            for item in prepared:
                try:item[2].unlink(missing_ok=True)
                except OSError:pass

def _chat_read830(uid,role):
    data=auth_request_data();mid=data.get('message_id')
    if type(mid) is not int or mid<0:abort(400)
    field='admin_read' if role=='admin' else 'user_read'
    with db_connection() as db:
        maximum=db.execute('SELECT COALESCE(max(message_id),0) FROM chat_messages830 WHERE user_id=?',(uid,)).fetchone()[0]
        mid=min(mid,maximum)
        db.execute('INSERT OR IGNORE INTO chat_reads830(user_id) VALUES(?)',(uid,))
        db.execute('UPDATE chat_reads830 SET '+field+'=MAX('+field+',?) WHERE user_id=?',(mid,uid))
    return jsonify(ok=True)

def _chat_file830(file_id,uid=None):
    with db_connection() as db:
        row=db.execute('SELECT f.*,m.user_id FROM chat_files830 f JOIN chat_messages830 m ON m.message_id=f.message_id WHERE f.file_id=?',(file_id,)).fetchone()
    if row is None or (uid is not None and row['user_id']!=uid):abort(404)
    path=CHAT_FILES830/row['stored_name']
    if not path.is_file():abort(404)
    response=send_file(path,as_attachment=True,download_name=row['name'],mimetype=row['mime'])
    response.headers['X-Content-Type-Options']='nosniff';response.headers['Cache-Control']='private, no-store'
    return response

@app.route('/api/messages/poll')
def chat_user_poll830():
    uid=current_user_id()
    with db_connection() as db:
        db.execute('INSERT INTO chat_presence830 VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET seen_at=excluded.seen_at',(session['session_id'],uid,_utc760()))
    return _chat_messages830(uid)

@app.route('/api/messages/send',methods=['POST'])
def chat_user_send830():return _chat_send830(current_user_id(),'user')

@app.route('/api/messages/read',methods=['POST'])
def chat_user_read830():return _chat_read830(current_user_id(),'user')

@app.route('/api/messages/files/<file_id>')
def chat_user_file830(file_id):return _chat_file830(file_id,current_user_id())

@app.route('/api/admin/messages/threads')
@admin_required
def chat_admin_threads830():
    with db_connection() as db:
        rows=db.execute('''SELECT u.user_id,u.email,COALESCE(MAX(m.message_id),0) AS latest_id,COALESCE(MAX(CASE WHEN m.sender='user' THEN m.message_id ELSE 0 END),0) AS latest_incoming_id,COALESCE(SUM(CASE WHEN m.sender='user' AND m.message_id>COALESCE(r.admin_read,0) THEN 1 ELSE 0 END),0) AS unread FROM users u LEFT JOIN chat_messages830 m ON m.user_id=u.user_id LEFT JOIN chat_reads830 r ON r.user_id=u.user_id GROUP BY u.user_id ORDER BY latest_id DESC,u.user_id''').fetchall()
        result=[dict(r,online=_chat_online830(db,r['user_id'])) for r in rows]
    return jsonify(threads=result,unread=sum(r['unread'] for r in result),latest_id=max((r['latest_id'] for r in result),default=0),latest_incoming_id=max((r['latest_incoming_id'] for r in result),default=0))

@app.route('/api/admin/messages/<int:uid>/poll')
@admin_required
def chat_admin_poll830(uid):return _chat_messages830(uid)

@app.route('/api/admin/messages/<int:uid>/send',methods=['POST'])
@admin_required
def chat_admin_send830(uid):return _chat_send830(uid,'admin')

@app.route('/api/admin/messages/<int:uid>/read',methods=['POST'])
@admin_required
def chat_admin_read830(uid):return _chat_read830(uid,'admin')

@app.route('/api/admin/messages/files/<file_id>')
@admin_required
def chat_admin_file830(file_id):return _chat_file830(file_id)

class ChatTransportFilter830(logging.Filter):
    def filter(self,record):
        line=record.getMessage()
        return not (('/api/messages/poll' in line or '/api/admin/messages/threads' in line or ('/api/admin/messages/' in line and '/poll' in line)) and re.search(r'HTTP/[^\"]*\"\s+(?:2\d\d|304)\b',line))
logging.getLogger('werkzeug').addFilter(ChatTransportFilter830())


# Real version identity of the running server; cached at module load, not on each request.
import hashlib as _hashlib830
_version_source830=Path(__file__).read_text(encoding='utf-8-sig')
_version_header830=re.search(r'^#.*?\b(\d+\.\d+\.\d+)\b',_version_source830,re.M)
_running_component830={'version':_version_header830.group(1) if _version_header830 else None,'sha256':_hashlib830.sha256(Path(__file__).read_bytes()).hexdigest()}

@app.route('/api/component-versions')
def component_versions830():
    admin_path=Path(BASE_DIR)/'admin.html';admin={'version':None,'sha256':None}
    try:
        raw=admin_path.read_bytes();text=raw.decode('utf-8-sig')
        match=re.search(r'<span\b[^>]*class=[\"\']version[\"\'][^>]*>\s*(\d+\.\d+\.\d+)',text,re.I)
        if match is None:match=re.search(r'<title>[^<]*?\b(\d+\.\d+\.\d+)\b',text,re.I)
        admin={'version':match.group(1) if match else None,'sha256':_hashlib830.sha256(raw).hexdigest()}
    except (OSError,UnicodeError):pass
    response=jsonify(server=_running_component830,admin=admin)
    response.headers['Cache-Control']='no-store'
    return response


# 8.4.0 — active conversations remain listed for one day after all incoming messages are read.
with db_connection() as db:
    db.execute('''CREATE TABLE IF NOT EXISTS chat_activity840(user_id INTEGER PRIMARY KEY REFERENCES users(user_id),cleared_at TEXT NOT NULL)''')
    for row in db.execute("SELECT DISTINCT m.user_id FROM chat_messages830 m JOIN chat_reads830 r ON r.user_id=m.user_id WHERE m.sender='user' AND NOT EXISTS(SELECT 1 FROM chat_messages830 n WHERE n.user_id=m.user_id AND n.sender='user' AND n.message_id>r.admin_read)").fetchall():
        db.execute('INSERT OR IGNORE INTO chat_activity840 VALUES(?,?)',(row['user_id'],_utc760()))

_chat_read_before840=_chat_read830

def _chat_read830(uid,role):
    before=0
    if role=='admin':
        with db_connection() as db:
            before=db.execute("SELECT count(*) FROM chat_messages830 m WHERE m.user_id=? AND m.sender='user' AND m.message_id>COALESCE((SELECT admin_read FROM chat_reads830 WHERE user_id=?),0)",(uid,uid)).fetchone()[0]
    response=_chat_read_before840(uid,role)
    if role=='admin' and before:
        with db_connection() as db:
            remaining=db.execute("SELECT count(*) FROM chat_messages830 m WHERE m.user_id=? AND m.sender='user' AND m.message_id>COALESCE((SELECT admin_read FROM chat_reads830 WHERE user_id=?),0)",(uid,uid)).fetchone()[0]
            if not remaining:db.execute('INSERT INTO chat_activity840 VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET cleared_at=excluded.cleared_at',(uid,_utc760()))
    return response

_threads_before840=chat_admin_threads830

def chat_admin_threads840():
    response=_threads_before840();data=response.get_json()
    cutoff=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='milliseconds').replace('+00:00','Z')
    with db_connection() as db:cleared={r['user_id']:r['cleared_at'] for r in db.execute('SELECT * FROM chat_activity840')}
    for item in data['threads']:
        item['cleared_at']=cleared.get(item['user_id'])
        item['active']=bool(item['latest_incoming_id'] and (item['unread'] or (item['cleared_at'] and item['cleared_at']>cutoff)))
    data['active_count']=sum(bool(t['active']) for t in data['threads'])
    return jsonify(data)
app.view_functions['chat_admin_threads830']=admin_required(chat_admin_threads840)


if __name__ == "__main__":
    with open(__file__, "r", encoding="utf-8-sig") as f:
        next(f)
        header = next(f).strip().removeprefix("#").strip()

    print("\n" + "=" * 72)
    print(header)
    print("=" * 72 + "\n")
   

    threading.Thread(target=cleanup_worker760,daemon=True).start()
    try:
        app.run(
    host="0.0.0.0",
    port=5000,
    debug=True,
    threaded=True,
    use_reloader=False
)
    except KeyboardInterrupt:
        pass
    except BaseException as error:
        print(f"SERVER ERROR: {error}", file=sys.stderr)
        raise
    finally:
        print_restart_command()
