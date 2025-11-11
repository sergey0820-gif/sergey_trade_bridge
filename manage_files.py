import argparse
import os
import shutil
import datetime
import glob

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
LOG_PATH = os.path.join(BASE_DIR, "logs", "manage_files.log")

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

CSV_FILES = ["candidates.csv", "live_candidates_public.csv", "live_candidates_ki.csv"]


def log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full = f"{timestamp} {message}"
    print(full)
    with open(LOG_PATH, "a") as f:
        f.write(full + "\n")


def copy_file(file):
    if not os.path.exists(file):
        log(f"❌ Файл не найден: {file}")
        return
    now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name, ext = os.path.splitext(file)
    new_name = f"{name}_{now}{ext}"
    shutil.copy(file, os.path.join(BACKUP_DIR, new_name))
    log(f"✅ Скопировано: {file} → {new_name}")


def clean():
    for file in CSV_FILES:
        if os.path.exists(file):
            os.remove(file)
            log(f"🗑️ Удалён: {file}")


def show_log(log_name):
    path = os.path.join("logs", log_name)
    if not os.path.exists(path):
        log(f"❌ Лог не найден: {log_name}")
        return
    with open(path) as f:
        print(f.read())


def delete_old(days):
    count = 0
    now = datetime.datetime.now()
    for file in glob.glob(os.path.join(BACKUP_DIR, "*.csv")):
        created = datetime.datetime.fromtimestamp(os.path.getctime(file))
        if (now - created).days > days:
            os.remove(file)
            count += 1
    log(f"🧹 Удалено старых архивов: {count}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    cp = sub.add_parser("copy")
    cp.add_argument("--file", required=True)

    sub.add_parser("clean")

    show = sub.add_parser("show")
    show.add_argument("--log", required=True)

    old = sub.add_parser("delete_old")
    old.add_argument("--days", type=int, required=True)

    args = ap.parse_args()

    if args.cmd == "copy":
        copy_file(args.file)
    elif args.cmd == "clean":
        clean()
    elif args.cmd == "show":
        show_log(args.log)
    elif args.cmd == "delete_old":
        delete_old(args.days)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
