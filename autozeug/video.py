import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import ffmpeg
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

logger = logging.getLogger(__name__)


def _to_stream(probe):
    return next(s for s in probe["streams"] if s["codec_type"] == "video")


@dataclass
class VideoMetadata:
    width: int
    height: int
    duration: float


def extract_metadata(video: Path) -> VideoMetadata | None:
    if video.suffix.lower() != ".mp4":
        return None
    probe = ffmpeg.probe(video)
    video_stream = _to_stream(probe)

    width = video_stream["width"]
    height = video_stream["height"]
    # Duration is often in seconds as a string, e.g., '123.456'
    duration = float(probe["format"]["duration"])
    return VideoMetadata(width, height, duration)


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
