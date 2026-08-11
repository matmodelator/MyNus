# ========================================
# Version: GPT  1.0.0.
# ========================================




# ========================================
# ИМПОРТЫ
# ========================================

import os
import shutil
import subprocess
import uuid

from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename


# ========================================
# СОЗДАНИЕ СЕРВЕРА
# ========================================

app = Flask(__name__)


# ========================================
# ПАПКИ ПРОЕКТА
# ========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULT_DIR = os.path.join(BASE_DIR, "results")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)


# ========================================
# ГЛАВНАЯ СТРАНИЦА
# ========================================

@app.route("/")
def index():

    return send_from_directory(
        BASE_DIR,
        "index.html"
    )


# ========================================
# ЗАГРУЗКА И РАЗДЕЛЕНИЕ ПЕСНИ
# ========================================

@app.route("/separate", methods=["POST"])
def separate():

    # ----------------------------------------
    # ПРОВЕРЯЕМ, ЧТО ФАЙЛ ПРИШЁЛ
    # ----------------------------------------

    if "audio" not in request.files:

        return jsonify({
            "error": "Файл не получен"
        }), 400


    audio = request.files["audio"]


    if audio.filename == "":

        return jsonify({
            "error": "Файл не выбран"
        }), 400


    # ----------------------------------------
    # СОЗДАЁМ УНИКАЛЬНЫЙ ID ОБРАБОТКИ
    # ----------------------------------------

    job_id = str(uuid.uuid4())


    # ----------------------------------------
    # СОЗДАЁМ ПАПКИ ДЛЯ ЭТОЙ ПЕСНИ
    # ----------------------------------------

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


    # ----------------------------------------
    # СОХРАНЯЕМ ЗАГРУЖЕННУЮ ПЕСНЮ
    # ----------------------------------------

    filename = secure_filename(
        audio.filename
    )


    # Если имя оказалось неподходящим
    # после обработки secure_filename

    if not filename:
        filename = "audio.mp3"


    input_path = os.path.join(
        job_upload_dir,
        filename
    )


    audio.save(
        input_path
    )


    # ========================================
    # ЗАПУСК DEMUCS
    # ========================================

    # --two-stems=vocals
    #
    # создаёт две дорожки:
    #
    # vocals.mp3
    # no_vocals.mp3
    #
    # --mp3
    #
    # заставляет Demucs сохранить результат
    # сразу в MP3


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

        # ----------------------------------------
        # ЗАПУСКАЕМ DEMUCS И ЖДЁМ РЕЗУЛЬТАТ
        # ----------------------------------------

        process = subprocess.run(

            command,

            capture_output=True,

            text=True
        )


        # ----------------------------------------
        # ЕСЛИ DEMUCS ВЕРНУЛ ОШИБКУ
        # ----------------------------------------

        if process.returncode != 0:

            print(process.stdout)
            print(process.stderr)

            return jsonify({

                "error": "Ошибка Demucs",

                "details": process.stderr

            }), 500


        # ========================================
        # ИЩЕМ РЕЗУЛЬТАТ DEMUCS
        # ========================================

        # Demucs создаёт структуру:
        #
        # results/
        #     job_id/
        #         htdemucs/
        #             название_песни/
        #
        #                 vocals.mp3
        #                 no_vocals.mp3


        model_dir = os.path.join(
            job_result_dir,
            "htdemucs"
        )


        # ----------------------------------------
        # НАХОДИМ ПАПКУ ПЕСНИ
        # ----------------------------------------

        song_dirs = [

            directory

            for directory in os.listdir(
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

            return jsonify({

                "error":
                    "Demucs не создал результат"

            }), 500


        song_dir = os.path.join(
            model_dir,
            song_dirs[0]
        )


        # ========================================
        # ИСХОДНЫЕ ФАЙЛЫ DEMUCS
        # ========================================

        vocals_source = os.path.join(
            song_dir,
            "vocals.mp3"
        )


        minus_source = os.path.join(
            song_dir,
            "no_vocals.mp3"
        )


        # ========================================
        # ФАЙЛЫ, КОТОРЫЕ ОТДАДИМ САЙТУ
        # ========================================

        vocals_target = os.path.join(
            job_result_dir,
            "vocals.mp3"
        )


        minus_target = os.path.join(
            job_result_dir,
            "minus.mp3"
        )


        # ----------------------------------------
        # КОПИРУЕМ РЕЗУЛЬТАТЫ
        # ----------------------------------------

        shutil.copy(
            vocals_source,
            vocals_target
        )


        shutil.copy(
            minus_source,
            minus_target
        )


        # ========================================
        # ВОЗВРАЩАЕМ ССЫЛКИ В INDEX.HTML
        # ========================================

        return jsonify({

            "vocals":
                f"/results/{job_id}/vocals.mp3",

            "minus":
                f"/results/{job_id}/minus.mp3"
        })


    # ========================================
    # НЕПРЕДВИДЕННАЯ ОШИБКА
    # ========================================

    except Exception as error:

        print(error)

        return jsonify({

            "error": str(error)

        }), 500


# ========================================
# ВЫДАЧА ГОТОВЫХ АУДИОФАЙЛОВ
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
# ЗАПУСК СЕРВЕРА
# ========================================

if __name__ == "__main__":

    app.run(

        host="127.0.0.1",

        port=5000,

        debug=True
    )
