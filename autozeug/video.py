import logging
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import cv2
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

logger = logging.getLogger(__name__)


@dataclass
class VideoMetadata:
    width: int
    height: int
    duration: float


@contextmanager
def capture(video: Path) -> Generator[cv2.VideoCapture, None, None]:
    cap = cv2.VideoCapture(str(video))
    with suppress(Exception):
        yield cap
    cap.release()


def extract_metadata(video: Path) -> VideoMetadata | None:
    if video.suffix.lower() != ".mp4":
        return None

    with capture(video) as cap:
        if not cap.isOpened():
            return None

        return VideoMetadata(
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            cap.get(cv2.CAP_PROP_FRAME_COUNT) / cap.get(cv2.CAP_PROP_FPS),
        )
    return None


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
        "format": (
            "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]"
            "/best[height<=360][ext=mp4]"
            "/best[height<=360]"
            "/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(video),  # same as -o <path>
        "quiet": False,  # show progress (optional)
        "noprogress": False,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:  # type: ignore
            ydl.download([url])
    except DownloadError:
        logger.error(f"Failed to download {video} at {url}")
