# ========================================
# JSON | 5.1.17
# ========================================

# ========================================
# IMPORTS
# ========================================

import sys
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
import language_tool_python


# ========================================
# APPLICATION
# ========================================

app = Flask(__name__)

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



# Arbitrary project folders selected via the Windows folder picker are exposed
# through short-lived in-memory tokens so the browser can fetch their tracks.
opened_project_folders = {}


# ========================================
# INDEX
# ========================================

@app.route("/")
def index():

    return send_from_directory(
        BASE_DIR,
        "index.html"
    )


# ========================================
# START SEPARATION
# ========================================

@app.route("/separate", methods=["POST"])
def separate():

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


    thread = threading.Thread(
        target=run_demucs,
        args=(
            job_id,
            input_path,
            job_result_dir
        ),
        daemon=True
    )

    thread.start()


    return jsonify({
        "job_id": job_id
    })


# ========================================
# VOCAL START\END DETECTION
# ========================================

def detect_vocal_range(
    vocals_path
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

    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace"
    )

    output = (
        process.stdout
        + "\n"
        + process.stderr
    )


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

def detect_lyrics(vocal_path):

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

    model = whisperx.load_model(
        "small",
        device,
        compute_type=compute_type
    )

    result = model.transcribe(
        audio,
        batch_size=4
    )

    language = result.get(
        "language"
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
# DEMUCS PROCESS
# ========================================

def run_demucs(
    job_id,
    input_path,
    job_result_dir
):

    command = [

        sys.executable,
        "-m",
        "demucs",

        "-n",
        "htdemucs_6s",

        "--mp3",

        "-o",
        job_result_dir,

        input_path
    ]


    try:

        process = subprocess.Popen(

            command,

            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,

            text=True,

            bufsize=1
        )


        # ========================================
        # READ DEMUCS OUTPUT
        # ========================================

        for line in process.stdout:

            print(line, end="")

            matches = re.findall(
                r"(\d{1,3})%",
                line
            )

            if matches:

                percent = int(
                    matches[-1]
                )

                percent = max(
                    0,
                    min(
                        100,
                        percent
                    )
                )

                jobs[job_id]["progress"] = (
                    percent
                )


        process.wait()


        if process.returncode != 0:

            jobs[job_id]["status"] = (
                "error"
            )

            jobs[job_id]["error"] = (
                "Demucs process failed"
            )

            return


        # ========================================
        # FIND DEMUCS OUTPUT
        # ========================================

        model_dir = os.path.join(
            job_result_dir,
            "htdemucs_6s"
        )


        song_dirs = [

            directory

            for directory
            in os.listdir(model_dir)

            if os.path.isdir(

                os.path.join(
                    model_dir,
                    directory
                )
            )
        ]


        if not song_dirs:

            raise Exception(
                "Demucs output not found"
            )


        song_dir = os.path.join(
            model_dir,
            song_dirs[0]
        )


        stem_names = [
            "vocals",
            "drums",
            "bass",
            "guitar",
            "piano",
            "other"
        ]


        stem_targets = {}


        for stem_name in stem_names:

            source_path = os.path.join(
                song_dir,
                f"{stem_name}.mp3"
            )

            target_path = os.path.join(
                job_result_dir,
                f"{stem_name}.mp3"
            )

            if not os.path.isfile(
                source_path
            ):

                raise Exception(
                    f"Demucs stem not found: {stem_name}"
                )

            shutil.copy(
                source_path,
                target_path
            )

            stem_targets[
                stem_name
            ] = target_path

        # ========================================
        # DETECT VOCAL START / END
        # ========================================

        (
            vocal_start,
            vocal_end
        ) = detect_vocal_range(
            stem_targets["vocals"]
        )


        # ========================================
        # DETECT LYRICS
        # ========================================

        lyrics = detect_lyrics(
            stem_targets["vocals"]
        )


        # ========================================
        # JOB COMPLETE
        # ========================================
      
        jobs[job_id]["progress"] = 100

        jobs[job_id]["status"] = (
            "done"
        )


        jobs[job_id]["vocals"] = (
            f"/results/{job_id}/vocals.mp3"
        )

        jobs[job_id]["drums"] = (
            f"/results/{job_id}/drums.mp3"
        )

        jobs[job_id]["bass"] = (
            f"/results/{job_id}/bass.mp3"
        )

        jobs[job_id]["guitar"] = (
            f"/results/{job_id}/guitar.mp3"
        )

        jobs[job_id]["piano"] = (
            f"/results/{job_id}/piano.mp3"
        )

        jobs[job_id]["other"] = (
            f"/results/{job_id}/other.mp3"
        )

        jobs[job_id]["vocal_start"] = (
            vocal_start
        )

        jobs[job_id]["vocal_end"] = (
            vocal_end
        )

        jobs[job_id]["lyrics"] = (
            lyrics
        )

    except Exception as error:

        print(error)

        jobs[job_id]["status"] = (
            "error"
        )

        jobs[job_id]["error"] = str(
            error
        )


# ========================================
# PROCESS PROGRESS
# ========================================

@app.route("/progress/<job_id>")
def progress(job_id):

    if job_id not in jobs:

        return jsonify({
            "error": "Job not found"
        }), 404


    return jsonify(
        jobs[job_id]
    )


# ========================================
# RESULT FILES
# ========================================

@app.route(
    "/results/<job_id>/<filename>"
)
def result_file(
    job_id,
    filename
):

    folder = os.path.join(
        RESULT_DIR,
        job_id
    )


    return send_from_directory(
        folder,
        filename
    )


# ========================================
# AUDIO EXPORT
# ========================================

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

_language_tools = {}

def normalize_language(language):
    value = str(language or "ru-RU").lower()
    if value.startswith("en"):
        return "en-US"
    return "ru-RU"



def get_language_tool(language="ru-RU"):

    if language not in _language_tools:

        _language_tools[language] = (
            language_tool_python.LanguageTool(
                language
            )
        )

    return _language_tools[language]



@app.route(
    "/spellcheck",
    methods=["POST"]
)
def spellcheck():

    data = request.get_json(
        silent=True
    ) or {}

    text = str(
        data.get(
            "text",
            ""
        )
    )

    try:

        language = normalize_language(
            data.get(
                "language",
                "ru-RU"
            )
        )

        tool = get_language_tool(
            language
        )

        matches = tool.check(
            text
        )

        result = []

        for match in matches:

            replacements = [
                str(value)
                for value in (
                    match.replacements
                    or []
                )[:8]
            ]

            result.append({
                "offset":
                    int(match.offset),
                "length":
                    int(match.error_length),
                "message":
                    str(match.message),
                "replacements":
                    replacements
            })

        return jsonify({
            "language": language,
            "matches": result
        })

    except Exception as error:

        print(error)

        return jsonify({
            "error": str(error)
        }), 500



# ========================================
# LYRICS LANGUAGE + RU TRANSCRIPTION
# ========================================

SUPPORTED_LYRICS_LANGUAGES = {"ru","en","es","it","fr","uk"}

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

def _uk_to_ru(text):
    result=str(text or "")
    for a,b in [("ї","йи"),("Ї","Йи"),("є","йэ"),("Є","Йэ"),
                ("і","и"),("І","И"),("ґ","г"),("Ґ","Г"),
                ("и","ы"),("И","Ы")]:
        result=result.replace(a,b)
    return result

def transcribe_line_to_ru(text,language):
    value=str(text or "")
    language=str(language or "").lower()
    if language=="ru": return value
    if language=="uk": return _uk_to_ru(value)
    pattern=re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’][A-Za-zÀ-ÖØ-öø-ÿ]+)?")
    return pattern.sub(lambda m:_latin_word(m.group(0),language),value)

@app.route("/transcribe-to-ru", methods=["POST"])
def transcribe_to_ru():
    data=request.get_json(silent=True) or {}
    lines=data.get("lines",[])
    languages=data.get("languages",[])
    if not isinstance(lines,list):
        return jsonify({"error":"Invalid lyrics lines"}),400
    result=[]
    for i,line in enumerate(lines):
        language=(languages[i] if isinstance(languages,list) and i<len(languages)
                  else detect_lyrics_line_language(line))
        if language not in SUPPORTED_LYRICS_LANGUAGES:
            language=detect_lyrics_line_language(line)
        result.append(transcribe_line_to_ru(line,language))
    return jsonify({"lines":result})


# ========================================
# SERVER START
# ========================================



# ========================================
# MYNUS PlayList JSON state + standalone Projects | 5.1.17
# ========================================
def _project_id(value):
    value = secure_filename(str(value or "Project")) or "Project"
    return value[:120]


def _project_name_from_folder(folder, fallback):
    manifest_path = os.path.join(folder, "Project.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        return manifest.get("name") or fallback
    except Exception:
        return fallback


def _empty_playlist_state():
    return {"current": None, "queue": [], "history": []}


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


def _write_playlist_state(state):
    state = _normalize_playlist_state(state)
    temp_path = PLAYLIST_STATE_PATH + ".saving"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(temp_path, PLAYLIST_STATE_PATH)
    return state


def _read_playlist_state():
    if not os.path.isfile(PLAYLIST_STATE_PATH):
        return _write_playlist_state(_empty_playlist_state())
    try:
        with open(PLAYLIST_STATE_PATH, "r", encoding="utf-8") as fh:
            return _normalize_playlist_state(json.load(fh))
    except Exception as exc:
        print(f"[PLAYLIST] invalid playlist.json, reset: {exc}", flush=True)
        return _write_playlist_state(_empty_playlist_state())


def _saved_project_folder(project_id):
    return os.path.join(PROJECTS_DIR, _project_id(project_id))


def _require_saved_project(project_id):
    project_id = _project_id(project_id)
    folder = _saved_project_folder(project_id)
    if not os.path.isdir(folder) or not os.path.isfile(os.path.join(folder, "Project.json")):
        raise FileNotFoundError("Project not found in Projects")
    return project_id, folder


def _set_current_project(project_id, source="projects"):
    project_id, _ = _require_saved_project(project_id)
    source = str(source or "projects").lower()
    state = _read_playlist_state()

    old_current = state.get("current")
    if old_current:
        state["history"].append(old_current)

    if source == "playlist":
        try:
            state["queue"].remove(project_id)
        except ValueError:
            pass

    state["current"] = project_id
    _write_playlist_state(state)
    print(f"[PLAYLIST] LOAD | current={project_id!r} | source={source}", flush=True)
    return project_id


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
def list_projects():
    return jsonify(_playlist_projects_payload())


@app.route("/projects/use", methods=["POST"])
def use_project():
    data = request.get_json(silent=True) or {}
    source = str(data.get("source") or "projects").lower()
    project_id = data.get("id")
    if source == "history":
        # History is a chronological log of completed/previous currents.
        # Loading an old item does not erase the historical occurrence.
        source = "history"
    try:
        current_id = _set_current_project(project_id, source)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True, "current": current_id, **_playlist_projects_payload()})


@app.route("/playlist/add", methods=["POST"])
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
def opened_project_track_file(token, filename):
    folder = opened_project_folders.get(str(token))
    if not folder:
        return jsonify({"error": "Opened project session expired"}), 404
    tracks_dir = os.path.abspath(os.path.join(folder, "tracks"))
    return send_from_directory(tracks_dir, filename)


@app.route("/projects/select-folder", methods=["POST"])
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
def save_project():
    project_name = str(request.form.get("name") or "Project").strip() or "Project"
    requested_project_id = str(request.form.get("project_id") or "").strip()
    project_id = _project_id(requested_project_id) if requested_project_id else _project_id(project_name)
    save_root_raw = str(request.form.get("save_path") or PROJECTS_DIR).strip() or PROJECTS_DIR
    save_root = os.path.abspath(os.path.expanduser(save_root_raw))
    conflict_action = str(request.form.get("conflict_action") or "").strip().lower()
    folder = os.path.join(save_root, project_id)

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
            "drums", "bass", "guitar", "piano", "other",
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

        project_json["version"] = "5.1.17"
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
        return jsonify({
            "ok": True,
            "id": project_id,
            "name": project_name,
            "path": folder
        })

    except Exception as exc:
        shutil.rmtree(temp_folder, ignore_errors=True)
        print(f"[PROJECT SAVE] ERROR | {type(exc).__name__}: {exc}", flush=True)
        print("=" * 72 + "\n", flush=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/lyrics/save-current", methods=["POST"])
def save_current_lyrics():
    data = request.get_json(silent=True) or {}
    lyrics = data.get("lyrics")
    if not isinstance(lyrics, dict) or not isinstance(lyrics.get("lines"), list):
        return jsonify({"error": "Invalid Lyrics.json"}), 400

    current_id = _read_playlist_state().get("current")
    if not current_id:
        return jsonify({"error": "No current project"}), 400

    folder = _saved_project_folder(current_id)
    if not os.path.isdir(folder) or not os.path.isfile(os.path.join(folder, "Project.json")):
        return jsonify({"error": "Current project folder not found in Projects"}), 404

    lyrics_path = os.path.join(folder, "Lyrics.json")
    temp_path = lyrics_path + ".saving"
    try:
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump(lyrics, fh, ensure_ascii=False, indent=2)
        os.replace(temp_path, lyrics_path)
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
def list_saved_projects():
    return jsonify(_saved_projects_payload())


@app.route("/saved-projects/<project_id>", methods=["GET"])
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
def saved_project_track_file(project_id, filename):
    return send_from_directory(
        os.path.join(PROJECTS_DIR, _project_id(project_id), "tracks"),
        filename
    )


@app.route("/projects/<project_id>", methods=["GET"])
def get_project(project_id):
    project_id = _project_id(project_id)
    folder = _saved_project_folder(project_id)
    try:
        project, lyrics, tracks = _project_payload_from_folder(
            folder,
            lambda filename: "/saved-projects/{}/track/{}".format(project_id, filename)
        )
    except FileNotFoundError:
        return jsonify({"error": "Project not found"}), 404
    return jsonify({
        "id": project_id,
        "name": project.get("name") or project_id,
        "project": project,
        "lyrics": lyrics,
        "tracks": tracks
    })


@app.route("/history-projects/<project_id>", methods=["GET"])
def get_history_project(project_id):
    # History stores only project references; project bytes live in Projects.
    return get_project(project_id)


@app.route("/projects/<project_id>/track/<path:filename>", methods=["GET"])
def project_track_file(project_id, filename):
    return send_from_directory(
        os.path.join(_saved_project_folder(project_id), "tracks"),
        filename
    )


@app.route("/history-projects/<project_id>/track/<path:filename>", methods=["GET"])
def history_project_track_file(project_id, filename):
    return project_track_file(project_id, filename)


def print_restart_command():
    print("\n" + "=" * 72)
    print("RESTART SERVER:")
    print(r".venv313\Scripts\python.exe server.py")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    print("\n" + "=" * 72)
    print("MyNus Server 5.1.17")
    print(r"5.1.17: PlayList state = C:\MyNus\playlist.json; project data lives only in C:\MyNus\Projects. current / queue / history are JSON state. Tab переключает Lyrics / Karaoke.")
    print("=" * 72 + "\n")

    try:
        app.run(
            host="127.0.0.1",
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
