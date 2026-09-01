# ========================================
# выбор языка WhisperX 4.7.16
# ========================================

# ========================================
# IMPORTS
# ========================================

import sys
import os
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

    lyrics_language = (
        request.form.get(
            "lyrics_language",
            "auto"
        )
        .strip()
        .lower()
    )

    allowed_lyrics_languages = {
        "auto",
        "ru",
        "en",
        "it",
        "es",
        "fr",
        "uk"
    }

    if lyrics_language not in allowed_lyrics_languages:
        lyrics_language = "auto"

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
            job_result_dir,
            lyrics_language
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

def detect_lyrics(
    vocal_path,
    lyrics_language="auto"
):

    import whisperx
    import torch
    import unicodedata

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

    # ========================================
    # TRANSCRIBE FULL VOCALS
    # ========================================

    transcribe_kwargs = {
        "batch_size": 4
    }

    if lyrics_language != "auto":
        transcribe_kwargs[
            "language"
        ] = lyrics_language

    result = model.transcribe(
        audio,
        **transcribe_kwargs
    )

    # ========================================
    # RAW WHISPERX DIAGNOSTICS
    # ========================================

    print()
    print(
        "========================================"
    )
    print(
        "WHISPERX RAW FULL AUDIO"
    )
    print(
        "========================================"
    )

    print(
        "LANGUAGE:",
        result.get(
            "language"
        )
    )

    for index, segment in enumerate(
        result.get(
            "segments",
            []
        )
    ):

        print(
            f"{index:03d}",
            f"{float(segment.get('start', 0)):.2f}",
            "-",
            f"{float(segment.get('end', 0)):.2f}",
            repr(
                segment.get(
                    "text",
                    ""
                )
            )
        )

    print(
        "========================================"
    )
    print()

    # ========================================
    # DROP SYMBOL-GARBAGE SEGMENTS
    # ========================================

    filtered_segments = []

    for segment in result.get(
        "segments",
        []
    ):

        segment_text = str(
            segment.get(
                "text",
                ""
            )
            or
            ""
        ).strip()

        if not segment_text:
            continue

        # ----------------------------------------
        # RULE:
        # ANY SAME UNICODE SYMBOL REPEATED 3+ TIMES,
        # DIRECTLY OR THROUGH SPACES / PUNCTUATION.
        # NORMAL DOUBLE LETTERS ARE NOT TOUCHED.
        # ----------------------------------------

        repeated_symbol_garbage = False

        content_chars = [
            char
            for char in segment_text
            if (
                not char.isspace()
                and
                not unicodedata.category(
                    char
                ).startswith("P")
            )
        ]

        if len(content_chars) >= 3:

            run_char = None
            run_length = 0

            for char in content_chars:

                if char == run_char:
                    run_length += 1
                else:
                    run_char = char
                    run_length = 1

                if run_length >= 3:
                    repeated_symbol_garbage = True
                    break

        if repeated_symbol_garbage:

            print(
                "SKIP REPEATED SYMBOL GARBAGE:",
                repr(
                    segment_text
                )
            )

            continue

        filtered_segments.append(
            segment
        )

    language = (
        result.get(
            "language"
        )
        or
        "en"
    )

    raw_result = {
        "language":
            language,

        "segments":
            filtered_segments
    }

    # ========================================
    # LANGUAGE
    # ========================================

    supported_languages = {
        "ru",
        "en",
        "it",
        "es",
        "fr",
        "uk"
    }


    if language not in supported_languages:

        raw_text = " ".join(
            str(
                segment.get(
                    "text",
                    ""
                )
                or
                ""
            )
            for segment
            in raw_result.get(
                "segments",
                []
            )
        ).lower()

        if re.search(
            r"[ІіЇїЄєҐґ]",
            raw_text
        ):

            language = "uk"

        elif re.search(
            r"[А-Яа-яЁё]",
            raw_text
        ):

            language = "ru"

        else:

            padded_text = (
                " "
                + raw_text
                + " "
            )

            language_markers = {

                "it": [
                    " che ",
                    " non ",
                    " per ",
                    " con ",
                    " sono ",
                    " sei ",
                    " amore ",
                    " mio ",
                    " mia ",
                    " come ",
                    " una ",
                    " il ",
                    " gli "
                ],

                "es": [
                    " que ",
                    " para ",
                    " con ",
                    " soy ",
                    " eres ",
                    " amor ",
                    " mi ",
                    " como ",
                    " una ",
                    " el ",
                    " los "
                ],

                "fr": [
                    " je ",
                    " tu ",
                    " pas ",
                    " pour ",
                    " avec ",
                    " suis ",
                    " amour ",
                    " mon ",
                    " ma ",
                    " une ",
                    " le ",
                    " les "
                ],

                "en": [
                    " the ",
                    " i ",
                    " you ",
                    " and ",
                    " with ",
                    " my ",
                    " love ",
                    " is ",
                    " are ",
                    " to ",
                    " of ",
                    " for "
                ]
            }

            language_scores = {
                code:
                    sum(
                        padded_text.count(
                            marker
                        )
                        for marker
                        in markers
                    )
                for code, markers
                in language_markers.items()
            }

            language = max(
                language_scores,
                key=language_scores.get
            )

            if (
                language_scores[
                    language
                ]
                == 0
            ):
                language = "en"

    raw_result["language"] = (
        language
    )

    # ========================================
    # RAW DIAGNOSTICS
    # ========================================

    print()
    print(
        "========================================"
    )
    print(
        "RAW WHISPER"
    )
    print(
        "========================================"
    )

    for index, segment in enumerate(
        raw_result.get(
            "segments",
            []
        )
    ):

        print(
            f"{index:03d}",
            f"{float(segment.get('start', 0)):.2f}",
            "-",
            f"{float(segment.get('end', 0)):.2f}",
            repr(
                segment.get(
                    "text",
                    ""
                )
            )
        )

    print(
        "========================================"
    )
    print()

    # ========================================
    # ALIGN
    # ========================================

    align_model, metadata = (
        whisperx.load_align_model(
            language_code=language,
            device=device
        )
    )

    aligned_result = whisperx.align(
        raw_result["segments"],
        align_model,
        metadata,
        audio,
        device,
        return_char_alignments=False
    )

    print()
    print(
        "========================================"
    )
    print(
        "ALIGNED WHISPER"
    )
    print(
        "========================================"
    )

    for index, segment in enumerate(
        aligned_result.get(
            "segments",
            []
        )
    ):

        print(
            f"{index:03d}",
            f"{float(segment.get('start', 0)):.2f}",
            "-",
            f"{float(segment.get('end', 0)):.2f}",
            repr(
                segment.get(
                    "text",
                    ""
                )
            )
        )

    print(
        "========================================"
    )
    print()

    # ========================================
    # COLLECT WORD TIMINGS
    # ========================================

    raw_words = []

    for segment in aligned_result.get(
        "segments",
        []
    ):

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

            word_text = (
                word.get(
                    "word",
                    ""
                )
                or
                ""
            ).strip()

            if not word_text:
                continue

            start = float(
                word["start"]
            )

            end = float(
                word["end"]
            )

            if end <= start:
                continue

            raw_words.append({
                "word":
                    word_text,

                "start":
                    start,

                "end":
                    end
            })

    # ========================================
    # SORT WORDS BY REAL TIME
    # ========================================

    raw_words.sort(
        key=lambda item: (
            float(
                item["start"]
            ),
            float(
                item["end"]
            )
        )
    )

    # ========================================
    # REMOVE TEMPORAL DUPLICATES
    # WITHOUT LOSING NEW WORDS
    # ========================================

    words = []

    for word in raw_words:

        word_text = str(
            word.get(
                "word",
                ""
            )
            or
            ""
        ).strip()

        start = float(
            word["start"]
        )

        end = float(
            word["end"]
        )

        if end <= start:
            continue

        duplicate = False

        # Проверяем только несколько
        # последних слов — дубли окон
        # всегда находятся рядом по времени.
        for previous in reversed(
            words[-8:]
        ):

            previous_start = float(
                previous["start"]
            )

            previous_end = float(
                previous["end"]
            )

            if (
                start
                - previous_end
                > 2.0
            ):
                break

            same_text = (
                word_text.lower()
                ==
                str(
                    previous.get(
                        "word",
                        ""
                    )
                ).strip().lower()
            )

            overlap_start = max(
                start,
                previous_start
            )

            overlap_end = min(
                end,
                previous_end
            )

            overlap = max(
                0.0,
                overlap_end
                - overlap_start
            )

            word_duration = max(
                0.001,
                end - start
            )

            previous_duration = max(
                0.001,
                previous_end
                - previous_start
            )

            overlap_ratio = (
                overlap
                /
                min(
                    word_duration,
                    previous_duration
                )
            )

            if (
                same_text
                and
                overlap_ratio >= 0.5
            ):

                duplicate = True
                break

        if duplicate:
            continue

        words.append({
            "word":
                word_text,

            "start":
                round(
                    start,
                    3
                ),

            "end":
                round(
                    end,
                    3
                )
        })

    # ========================================
    # GUARANTEE MONOTONIC WORD TIMINGS
    # ========================================

    normalized_words = []

    last_start = -1.0

    for word in words:

        start = float(
            word["start"]
        )

        end = float(
            word["end"]
        )

        if start < last_start:
            start = last_start

        if end <= start:
            continue

        normalized_words.append({
            "word":
                word["word"],

            "start":
                round(
                    start,
                    3
                ),

            "end":
                round(
                    end,
                    3
                )
        })

        last_start = start

    words = normalized_words

    # ========================================
    # TEXT
    # ========================================

    text = " ".join(
        segment.get(
            "text",
            ""
        ).strip()
        for segment
        in aligned_result.get(
            "segments",
            []
        )
    ).strip()

    return {
        "language":
            language,

        "text":
            text,

        "words":
            words
    }


# ========================================
# DEMUCS PROCESS
# ========================================

def run_demucs(
    job_id,
    input_path,
    job_result_dir,
    lyrics_language="auto"
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

            print(
                line,
                end=""
            )

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

                jobs[
                    job_id
                ][
                    "progress"
                ] = percent


        process.wait()


        if process.returncode != 0:

            jobs[
                job_id
            ][
                "status"
            ] = "error"

            jobs[
                job_id
            ][
                "error"
            ] = (
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
            in os.listdir(
                model_dir
            )

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

            source_path = (
                os.path.join(
                    song_dir,
                    f"{stem_name}.mp3"
                )
            )

            target_path = (
                os.path.join(
                    job_result_dir,
                    f"{stem_name}.mp3"
                )
            )

            if not os.path.isfile(
                source_path
            ):

                raise Exception(
                    f"Demucs stem not found: "
                    f"{stem_name}"
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
            stem_targets[
                "vocals"
            ]
        )


        # ========================================
        # DETECT LYRICS
        # ========================================

        lyrics = detect_lyrics(
            stem_targets[
                "vocals"
            ],
            lyrics_language
        )


        # ========================================
        # JOB COMPLETE
        # ========================================

        jobs[
            job_id
        ][
            "progress"
        ] = 100

        jobs[
            job_id
        ][
            "status"
        ] = "done"


        jobs[
            job_id
        ][
            "vocals"
        ] = (
            f"/results/"
            f"{job_id}/"
            f"vocals.mp3"
        )

        jobs[
            job_id
        ][
            "drums"
        ] = (
            f"/results/"
            f"{job_id}/"
            f"drums.mp3"
        )

        jobs[
            job_id
        ][
            "bass"
        ] = (
            f"/results/"
            f"{job_id}/"
            f"bass.mp3"
        )

        jobs[
            job_id
        ][
            "guitar"
        ] = (
            f"/results/"
            f"{job_id}/"
            f"guitar.mp3"
        )

        jobs[
            job_id
        ][
            "piano"
        ] = (
            f"/results/"
            f"{job_id}/"
            f"piano.mp3"
        )

        jobs[
            job_id
        ][
            "other"
        ] = (
            f"/results/"
            f"{job_id}/"
            f"other.mp3"
        )

        jobs[
            job_id
        ][
            "vocal_start"
        ] = vocal_start

        jobs[
            job_id
        ][
            "vocal_end"
        ] = vocal_end

        jobs[
            job_id
        ][
            "lyrics"
        ] = lyrics

    except Exception as error:

        print(
            error
        )

        jobs[
            job_id
        ][
            "status"
        ] = "error"

        jobs[
            job_id
        ][
            "error"
        ] = str(
            error
        )


# ========================================
# PROCESS PROGRESS
# ========================================

@app.route(
    "/progress/<job_id>"
)
def progress(
    job_id
):

    if job_id not in jobs:

        return jsonify({
            "error":
                "Job not found"
        }), 404


    return jsonify(
        jobs[
            job_id
        ]
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

        print(
            error
        )


        return jsonify({
            "error":
                str(error)
        }), 500


# ========================================
# LYRICS AUTOFIX
# ========================================

_language_tools = {}


def normalize_language(
    language
):

    value = str(
        language
        or
        "ru-RU"
    ).lower()

    if value.startswith(
        "en"
    ):

        return "en-US"

    return "ru-RU"


def get_language_tool(
    language="ru-RU"
):

    if language not in _language_tools:

        _language_tools[
            language
        ] = (
            language_tool_python
            .LanguageTool(
                language
            )
        )

    return _language_tools[
        language
    ]


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
                str(
                    value
                )
                for value
                in (
                    match.replacements
                    or
                    []
                )[:8]
            ]

            result.append({
                "offset":
                    int(
                        match.offset
                    ),

                "length":
                    int(
                        match.error_length
                    ),

                "message":
                    str(
                        match.message
                    ),

                "replacements":
                    replacements
            })

        return jsonify({
            "language":
                language,

            "matches":
                result
        })

    except Exception as error:

        print(
            error
        )

        return jsonify({
            "error":
                str(
                    error
                )
        }), 500


# ========================================
# LYRICS LANGUAGE + RU TRANSCRIPTION
# ========================================

SUPPORTED_LYRICS_LANGUAGES = {
    "ru",
    "en",
    "es",
    "it",
    "fr",
    "uk"
}


def _fallback_language(
    text
):

    value = str(
        text
        or
        ""
    ).strip()

    if re.search(
        r"[ІіЇїЄєҐґ]",
        value
    ):

        return "uk"

    if re.search(
        r"[А-Яа-яЁё]",
        value
    ):

        return "ru"


    lower = (
        " "
        + value.lower()
        + " "
    )


    markers = {

        "it": [
            " che ",
            " non ",
            " per ",
            " sono ",
            " amore ",
            " mio ",
            " mia "
        ],

        "es": [
            " que ",
            " para ",
            " soy ",
            " amor ",
            " corazón ",
            " eres "
        ],

        "fr": [
            " je ",
            " pas ",
            " pour ",
            " avec ",
            " amour ",
            " suis ",
            " mon "
        ],

        "en": [
            " the ",
            " i ",
            " you ",
            " and ",
            " with ",
            " love ",
            " my ",
            " is "
        ]
    }


    scores = {

        lang:
            sum(
                lower.count(
                    x
                )
                for x
                in words
            )

        for lang, words
        in markers.items()
    }


    return (
        max(
            scores,
            key=scores.get
        )
        if max(
            scores.values()
        )
        else
        "en"
    )


def detect_lyrics_line_language(
    text
):

    value = str(
        text
        or
        ""
    ).strip()

    if not value:

        return "ru"


    if re.search(
        r"[ІіЇїЄєҐґ]",
        value
    ):

        return "uk"


    if re.search(
        r"[А-Яа-яЁё]",
        value
    ):

        return "ru"


    try:

        from langdetect import detect

        language = detect(
            value
        )

        if (
            language
            in
            SUPPORTED_LYRICS_LANGUAGES
        ):

            return language

    except Exception:

        pass


    return _fallback_language(
        value
    )


@app.route(
    "/detect-lyrics-languages",
    methods=["POST"]
)
def detect_lyrics_languages():

    data = request.get_json(
        silent=True
    ) or {}

    lines = data.get(
        "lines",
        []
    )

    if not isinstance(
        lines,
        list
    ):

        return jsonify({
            "error":
                "Invalid lyrics lines"
        }), 400


    return jsonify({
        "languages": [
            detect_lyrics_line_language(
                line
            )
            for line
            in lines
        ]
    })


_WORDS = {

    "en": {
        "i": "ай",
        "you": "ю",
        "your": "йор",
        "me": "ми",
        "my": "май",
        "we": "уи",
        "they": "зэй",
        "he": "хи",
        "she": "ши",
        "the": "зэ",
        "and": "энд",
        "with": "уиз",
        "love": "лав",
        "baby": "бэйби",
        "heart": "харт",
        "night": "найт",
        "day": "дэй",
        "time": "тайм",
        "life": "лайф",
        "world": "уёрлд",
        "never": "нэвэр",
        "want": "уонт",
        "know": "ноу",
        "think": "синк",
        "feel": "фил",
        "see": "си",
        "go": "гоу",
        "come": "кам",
        "stay": "стэй",
        "leave": "лив",
        "lose": "луз",
        "home": "хоум",
        "don't": "доунт",
        "can't": "кэнт",
        "won't": "уоунт"
    },

    "it": {
        "io": "ио",
        "tu": "ту",
        "non": "нон",
        "che": "кэ",
        "per": "пэр",
        "con": "кон",
        "amore": "аморэ",
        "mio": "мио",
        "mia": "миа",
        "sono": "соно",
        "sei": "сэй",
        "vita": "вита",
        "cuore": "куорэ",
        "notte": "ноттэ",
        "giorno": "джорно"
    },

    "es": {
        "yo": "йо",
        "tu": "ту",
        "no": "но",
        "que": "кэ",
        "para": "пара",
        "con": "кон",
        "amor": "амор",
        "mi": "ми",
        "soy": "сой",
        "eres": "эрэс",
        "vida": "вида",
        "corazón": "корасон",
        "noche": "ночэ",
        "día": "диа"
    },

    "fr": {
        "je": "жё",
        "tu": "тю",
        "il": "иль",
        "elle": "эль",
        "nous": "ну",
        "vous": "ву",
        "pas": "па",
        "que": "кё",
        "pour": "пур",
        "avec": "авэк",
        "amour": "амур",
        "mon": "мон",
        "ma": "ма",
        "suis": "сюи",
        "vie": "ви",
        "cœur": "кёр",
        "nuit": "нюи",
        "jour": "жур"
    }
}


_RULES = {

    "en": [
        ("tion", "шн"),
        ("igh", "ай"),
        ("oo", "у"),
        ("ee", "и"),
        ("ea", "и"),
        ("ai", "эй"),
        ("ay", "эй"),
        ("oa", "оу"),
        ("ow", "оу"),
        ("ou", "ау"),
        ("ch", "ч"),
        ("sh", "ш"),
        ("th", "з"),
        ("ph", "ф"),
        ("ng", "нг")
    ],

    "it": [
        ("gli", "льи"),
        ("gn", "нь"),
        ("chi", "ки"),
        ("che", "ке"),
        ("ci", "чи"),
        ("ce", "че"),
        ("gi", "джи"),
        ("ge", "дже")
    ],

    "es": [
        ("ll", "й"),
        ("ñ", "нь"),
        ("ch", "ч"),
        ("qu", "к"),
        ("j", "х")
    ],

    "fr": [
        ("eau", "о"),
        ("au", "о"),
        ("ou", "у"),
        ("oi", "уа"),
        ("ch", "ш"),
        ("gn", "нь"),
        ("ph", "ф"),
        ("qu", "к"),
        ("ai", "э")
    ]
}


_CHARS = {
    "a": "а",
    "à": "а",
    "á": "а",
    "â": "а",
    "ä": "а",
    "b": "б",
    "c": "к",
    "ç": "с",
    "d": "д",
    "e": "э",
    "è": "э",
    "é": "э",
    "ê": "э",
    "ë": "э",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "ì": "и",
    "í": "и",
    "î": "и",
    "ï": "и",
    "j": "ж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "ò": "о",
    "ó": "о",
    "ô": "о",
    "ö": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "ù": "у",
    "ú": "у",
    "û": "у",
    "ü": "у",
    "v": "в",
    "w": "у",
    "x": "кс",
    "y": "й",
    "z": "з"
}


def _latin_word(
    word,
    language
):

    original = word
    value = word.lower()

    dictionary = _WORDS.get(
        language,
        {}
    )

    if value in dictionary:

        result = dictionary[
            value
        ]

    else:

        result = value

        for source, target in _RULES.get(
            language,
            []
        ):

            result = result.replace(
                source,
                target
            )

        result = "".join(
            _CHARS.get(
                ch,
                ch
            )
            for ch
            in result
        )


    if original.isupper():

        return result.upper()


    if original[:1].isupper():

        return (
            result[:1].upper()
            + result[1:]
        )


    return result


def _uk_to_ru(
    text
):

    result = str(
        text
        or
        ""
    )

    for a, b in [
        ("ї", "йи"),
        ("Ї", "Йи"),
        ("є", "йэ"),
        ("Є", "Йэ"),
        ("і", "и"),
        ("І", "И"),
        ("ґ", "г"),
        ("Ґ", "Г"),
        ("и", "ы"),
        ("И", "Ы")
    ]:

        result = result.replace(
            a,
            b
        )

    return result


def transcribe_line_to_ru(
    text,
    language
):

    value = str(
        text
        or
        ""
    )

    language = str(
        language
        or
        ""
    ).lower()


    if language == "ru":

        return value


    if language == "uk":

        return _uk_to_ru(
            value
        )


    pattern = re.compile(
        r"[A-Za-zÀ-ÖØ-öø-ÿ]+"
        r"(?:['’][A-Za-zÀ-ÖØ-öø-ÿ]+)?"
    )


    return pattern.sub(
        lambda m:
            _latin_word(
                m.group(0),
                language
            ),
        value
    )


@app.route(
    "/transcribe-to-ru",
    methods=["POST"]
)
def transcribe_to_ru():

    data = request.get_json(
        silent=True
    ) or {}

    lines = data.get(
        "lines",
        []
    )

    languages = data.get(
        "languages",
        []
    )


    if not isinstance(
        lines,
        list
    ):

        return jsonify({
            "error":
                "Invalid lyrics lines"
        }), 400


    result = []


    for i, line in enumerate(
        lines
    ):

        language = (
            languages[i]

            if (
                isinstance(
                    languages,
                    list
                )
                and
                i < len(
                    languages
                )
            )

            else
            detect_lyrics_line_language(
                line
            )
        )


        if (
            language
            not in
            SUPPORTED_LYRICS_LANGUAGES
        ):

            language = (
                detect_lyrics_line_language(
                    line
                )
            )


        result.append(
            transcribe_line_to_ru(
                line,
                language
            )
        )


    return jsonify({
        "lines":
            result
    })


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
