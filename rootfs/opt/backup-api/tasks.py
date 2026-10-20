import os
import subprocess
import sys
import threading
import time

from config import atomic_replace_lines, INCOMING_DIR, WEB_BACKUPS_DIR, AUTHORIZED_KEYS_PATH


def dir_total_size(path: str) -> int:
    # ponytail: du -sb over os.walk. Same result, 3 lines.
    try:
        result = subprocess.run(["du", "-sb", path], capture_output=True, text=True, check=True)
        return int(result.stdout.split()[0])
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0


def _tar_with_stall_detection(cmd, archive_path, stall_timeout=300):
    """ponytail: kill tar if archive size hasn't grown for stall_timeout seconds."""
    proc = subprocess.Popen(cmd, env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    last_size = -1
    stalled = 0

    def watch():
        nonlocal last_size, stalled
        while proc.poll() is None:
            time.sleep(30)
            try:
                s = os.path.getsize(archive_path)
            except OSError:
                continue
            if s == last_size:
                stalled += 30
                if stalled >= stall_timeout:
                    proc.kill()
                    return
            else:
                stalled = 0
                last_size = s

    threading.Thread(target=watch, daemon=True).start()
    return proc.wait()


def compress_and_cleanup(session_id: str):
    """Фонова задача: видаляє ключ, пакує файли в backup/ та очищує диск"""
    session_dir = os.path.join(INCOMING_DIR, session_id)
    archive_path = os.path.join(WEB_BACKUPS_DIR, f"{session_id}.tar.gz")
    status_path = os.path.join(WEB_BACKUPS_DIR, f"{session_id}.status")
    size_path = os.path.join(WEB_BACKUPS_DIR, f"{session_id}.size")

    # 1. Видаляємо SSH-ключ з authorized_keys атомарно
    comment = f"# {session_id}"
    atomic_replace_lines(AUTHORIZED_KEYS_PATH, lambda l: comment not in l)

    try:
        # 2. Підраховуємо розмір файлів до архівації
        total_size = dir_total_size(session_dir)
        with open(size_path, "w", encoding="utf-8") as f:
            f.write(str(total_size))

        # 3. Позначаємо що архівація в процесі
        with open(status_path, "w", encoding="utf-8") as f:
            f.write("processing")

        # 4. Пакуємо в tar.gz (LANG=C.UTF-8 для кириличних імен)
        rc = _tar_with_stall_detection(
            ["tar", "-czf", archive_path, "--transform", r"s|^\.|backup|", "-C", session_dir, "."],
            archive_path,
        )
        if rc != 0:
            raise subprocess.CalledProcessError(rc, ["tar"])

        # 5. Видаляємо статус — архів готовий
        if os.path.exists(status_path):
            os.remove(status_path)
        if os.path.exists(size_path):
            os.remove(size_path)

    except subprocess.CalledProcessError as e:
        print(f"tasks: compress_and_cleanup({session_id}): tar failed ({e})", file=sys.stderr)
        with open(status_path, "w", encoding="utf-8") as f:
            f.write("error")
    finally:
        # 6. Видаляємо вхідні файли
        subprocess.run(["rm", "-rf", session_dir])
