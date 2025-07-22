#!/usr/bin/env python3
"""
upload_on_youtube.py  ·  sélection, planification, upload & archivage
====================================================================
Fonctions clés
-------------
1. Liste les dossiers vidéo et en laisse choisir ≤ 6.
2. 4 modes de programmation ; `publishAt` ≥ 20 min dans le futur.
3. Coupe automatiquement les *tags* pour rester ≤ 500 caractères cumulés.
4. Catégorie par défaut : **28 Science & Technology** pour les vidéos IA.
5. Uploade en résumable avec barre de progression claire.
6. Déplace le dossier traité dans **`0.DONE/`** après succès.
"""
from __future__ import annotations

import json, logging, sys, shutil, time, math
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple

import pytz
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

# ───────── CONFIG ─────────
SCOPES         = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRET  = "client_secret.json"   # OAuth desktop credentials
TOKEN_FILE     = "token.json"           # cached credentials
VIDEO_EXT      = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm"}
CATEGORY_ID    = "28"   # 28 Science & Technology · alt: "27" Education, "24" Entertainment
UPLOAD_HOUR    = 10      # 10:00 Europe/Paris
MIN_MARGIN_MIN = 20      # marge mini avant publishAt
DONE_DIR       = "0.DONE"
PROGRESS_STEP  = 0.05    # afficher barre tous les 5 %
# ─────────────────────────
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S")

# ───────── OAuth ─────────

def youtube_service():
    if not Path(CLIENT_SECRET).exists():
        sys.exit(f"❌  {CLIENT_SECRET} manquant. Télécharge‑le depuis Google Cloud Console.")
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES) if Path(TOKEN_FILE).exists() else None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
        creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, static_discovery=False)

# ───────── Helpers ─────────

def find_video_meta(folder: Path) -> Tuple[Path, dict]:
    vids = [p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXT]
    if not vids:
        raise FileNotFoundError("vidéo absente")
    meta_fp = folder / "metadata.json"
    if not meta_fp.exists():
        raise FileNotFoundError("metadata.json manquant")
    return vids[0], json.loads(meta_fp.read_text("utf-8"))


def choose_folders(root: Path) -> List[Path]:
    logging.info("Scan des sous‑dossiers…")
    valid = {}
    for i, d in enumerate(sorted([p for p in root.iterdir() if p.is_dir()]), 1):
        try:
            find_video_meta(d)
            valid[str(i)] = d
            print(f"  {i}. {d.name}")
        except FileNotFoundError:
            continue
    if not valid:
        sys.exit("Aucun dossier complet trouvé (vidéo + metadata.json).")
    sel = input("Choisissez les numéros à uploader (max 6, séparés par virgules) : ").split(",")
    sel = [s.strip() for s in sel if s.strip()]
    if not sel or len(sel) > 6 or any(s not in valid for s in sel):
        sys.exit("Sélection invalide.")
    return [valid[s] for s in sel]


def next_valid(dt: datetime, now: datetime, margin: timedelta) -> datetime:
    return dt if dt >= now + margin else now + margin


def planner(n: int) -> List[datetime | None]:
    paris = pytz.timezone("Europe/Paris")
    now = datetime.now(paris)
    base_today = now.replace(hour=UPLOAD_HOUR, minute=0, second=0, microsecond=0)
    margin = timedelta(minutes=MIN_MARGIN_MIN)
    print("\nMode : 1 quotidien  2 +3j  3 immédiat  4 manuel")
    choice = input("Choix [1‑4] : ").strip()
    if choice not in {"1","2","3","4"}: sys.exit("Choix invalide")
    dates: List[datetime | None] = []
    if choice == "3":
        return [None]*n
    elif choice == "1":
        for i in range(n):
            dates.append(next_valid(base_today + timedelta(days=i), now, margin))
    elif choice == "2":
        start = next_valid(base_today + timedelta(days=1), now, margin)
        for i in range(n):
            dates.append(start + timedelta(days=3*i))
    else:
        for k in range(n):
            ds = input(f"Date YYYY-MM-DD pour vidéo {k+1} : ").strip()
            dt = paris.localize(datetime.strptime(ds, "%Y-%m-%d").replace(hour=UPLOAD_HOUR))
            dates.append(next_valid(dt, now, margin))
    return dates


def trim_tags(tags: List[str]) -> List[str]:
    total, out = 0, []
    for t in tags:
        l = len(t)
        if total + l + 1 > 500:  # +1 pour le séparateur compté par l’API
            break
        out.append(t)
        total += l + 1
    return out


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def progress_bar(p: float, width: int = 30) -> str:
    filled = math.floor(p*width)
    return "[" + "#"*filled + "-"*(width-filled) + "]" + f" {p*100:5.1f}%"


def upload(yt, video: Path, meta: dict, when: datetime | None):
    body = {
        "snippet": {
            "title": meta.get("title", video.stem),
            "description": meta.get("description", ""),
            "tags": trim_tags(meta.get("tags", [])),
            "categoryId": CATEGORY_ID,
            "defaultLanguage": "fr",
        },
        "status": {
            "privacyStatus": "public" if when is None else "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    if when is not None:
        body["status"]["publishAt"] = iso(when)

    media = MediaFileUpload(str(video), resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    last_shown = 0.0
    try:
        resp = None
        while resp is None:
            status, resp = request.next_chunk()
            if status and status.progress() - last_shown >= PROGRESS_STEP:
                last_shown = status.progress()
                print("   ", progress_bar(last_shown))
    except HttpError as e:
        logging.error("YouTube API error: %s", e)
        raise
    print("   ✅ Upload terminé : https://youtu.be/" + resp["id"])

# ───────── Main ─────────

def main():
    root = Path.cwd()
    folders = choose_folders(root)
    dates = planner(len(folders))
    yt = youtube_service()

    done_dir = root / DONE_DIR
    done_dir.mkdir(exist_ok=True)

    for folder, when in zip(folders, dates):
        try:
            video, meta = find_video_meta(folder)
            logging.info("Upload de %s (sched %s)", folder.name, when.strftime("%Y-%m-%d %H:%M") if when else "immédiat")
            upload(yt, video, meta, when)
            shutil.move(str(folder), done_dir / folder.name)
            logging.info("Dossier archivé dans %s", done_dir / folder.name)
        except Exception as e:
            logging.error("✖︎ Échec %s : %s", folder.name, e)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrompu par l’utilisateur")
