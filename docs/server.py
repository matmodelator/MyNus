# ========================================
# lirycs | 3.0.0
# ========================================

# ========================================
# IMPORTS
# ========================================

import os
import re
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULT_DIR = os.path.join(BASE_DIR, "results")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)


# ========================================
# JOB STATE
# ========================================

jobs = {}


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
        compute_type=compute_type,
        language="ru"
    )

    result = model.transcribe(
        audio,
        batch_size=4
    )

    language = result.get(
        "language",
        "ru"
    )

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

        "python",
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
# SERVER START
# ========================================

if __name__ == "__main__":

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True,

        threaded=True
    )
