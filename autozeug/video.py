import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

logger = logging.getLogger(__name__)


@dataclass
class VideoMetadata:
    width: int
    height: int
    duration: float


def extract_metadata(video: Path) -> Optional[VideoMetadata]:
    if video.suffix.lower() != ".mp4":
        return None

    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            return None
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    finally:
        cap.release()

    return VideoMetadata(width, height, frames / fps) if fps else None


def is_readable(video):
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return False

        # Check if it's an mp4 file by extension and try to read a frame
    if video.suffix.lower() != ".mp4":
        return False

    ret, _ = cap.read()
    cap.release()
    return ret


def video_exists_and_valid(video: Path) -> bool:
    if not video.exists():
        return False
    try:
        return is_readable(video)
    except Exception:
        return False


def download_from_youtube(video: Path, url: str):
    video.parent.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "format": "mp4[height=360]",  # same as -f "mp4[height=360]"
        "outtmpl": str(video),  # same as -o <path>
        "quiet": False,  # show progress (optional)
        "noprogress": False,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:  # type: ignore
            ydl.download([url])
    except DownloadError:
        logger.error(f"Failed to download {video} at {url}")
