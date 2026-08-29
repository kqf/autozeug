import re
from dataclasses import dataclass
from pathlib import Path

import click

from autozeug.telegram import (
    Post,
    PostBuilder,
    load_config,
    load_posts,
    pull,
    push,
    upload_video,
)
from autozeug.video import download_from_youtube, video_exists_and_valid


@dataclass
class VideoPost:
    youid: str
    date: str
    text: str
    link: str
    video: Path

    def __str__(self):
        return f"{self.date}: {self.video} \n {self.text}"

    def valid(self) -> bool:
        return self.video.exists()

    async def upload(self, client, entity):
        return await upload_video(
            client,
            entity,
            media=self.video,
            caption=self.text,
        )


def find_youtube_links(text: str) -> list[str]:
    youtube_regex = (
        r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/(?:[\w\-\?&=/%#\.]+))"
    )
    return re.findall(youtube_regex, text)


def extrac_youtube_id(url: str) -> str:
    patterns = [
        r"youtube\.com/watch\?v=([A-Za-z0-9_\-]+)",
        r"youtube\.com/live/([A-Za-z0-9_\-]+)",
        r"youtu\.be/([A-Za-z0-9_\-]+)",
    ]
    for pattern in patterns:
        if match := re.search(pattern, url):
            return match[1]
    return url


def extract_posts(posts: list[Post], course: Path) -> dict[str, VideoPost]:
    outposts: dict[str, VideoPost] = {}
    for post in posts:
        for link in find_youtube_links(post.text):
            youid = extrac_youtube_id(link)
            if youid in outposts:
                continue
            outposts[youid] = VideoPost(
                youid=youid,
                date=post.date,
                text=post.text,
                link=link,
                video=course / f"{youid}.mp4",
            )
    return outposts


def dowload_videos(posts: dict[str, VideoPost]):
    for post in posts.values():
        if not video_exists_and_valid(post.video):
            download_from_youtube(post.video, post.link)


@click.command()
@click.option("--dry-run/--no-dry-run", default=False, help="Do not push")
@click.option("--max-posts", type=int, default=100, help="Counting backwards")
def main(dry_run: bool, max_posts: int):
    config = load_config()
    cachefile = pull(builder=PostBuilder(), config=config, limit=max_posts)
    original = load_posts(cachefile)
    posts = extract_posts(original, cachefile.with_suffix(""))
    dowload_videos(posts)
    push(list(posts.values()), config=config, dry_run=dry_run)
    click.echo("Done.")


if __name__ == "__main__":
    main()
