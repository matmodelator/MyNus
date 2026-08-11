# ========================================
# Processing типо 1.1.0.
# ========================================



# ========================================
# IMPORTS
# ========================================

import os
import re
import shutil
import subprocess
import threading
import uuid

from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename


# ========================================
# APPLICATION
# ========================================

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULT_DIR = os.path.join(BASE_DIR, "results")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)


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
        "minus": None,
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

        "--two-stems=vocals",

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


            # Demucs / tqdm outputs values such as:
            #
            # 23%
            # 51%
            # 100%

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
            "htdemucs"
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


        vocals_source = os.path.join(
            song_dir,
            "vocals.mp3"
        )


        minus_source = os.path.join(
            song_dir,
            "no_vocals.mp3"
        )


        vocals_target = os.path.join(
            job_result_dir,
            "vocals.mp3"
        )


        minus_target = os.path.join(
            job_result_dir,
            "minus.mp3"
        )


        shutil.copy(
            vocals_source,
            vocals_target
        )


        shutil.copy(
            minus_source,
            minus_target
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


        jobs[job_id]["minus"] = (
            f"/results/{job_id}/minus.mp3"
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
# SERVER START
# ========================================

if __name__ == "__main__":

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True,

        threaded=True
    )
