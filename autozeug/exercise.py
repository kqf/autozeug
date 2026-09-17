import logging
from dataclasses import dataclass, field
from pathlib import Path

import click
from dataclasses_json import dataclass_json
from telethon.utils import get_extension

from autozeug.telegram import load_config, load_posts, ofile_for, pull, push

logger = logging.getLogger(__name__)

MAX_CAPTION = 1024


@dataclass_json
@dataclass
class MediaPost:
    number: int
    date: str
    text: str
    media: list[str] = field(default_factory=list)

    def __str__(self):
        head = self.text.splitlines()[0] if self.text else ""
        return f"{self.number}: {self.media} \n {head}"

    def files(self) -> list[Path]:
        return [Path(media) for media in self.media]

    def valid(self) -> bool:
        if missing := [f for f in self.files() if not f.exists()]:
            logger.error(f"Missing media for {self.number}: {missing}")
            return False
        return bool(self.text or self.media)

    async def upload(self, client, entity):
        if not (files := self.files()):
            return await client.send_message(entity, self.text)

        caption, extra = self.text, ""
        if len(caption) > MAX_CAPTION:
            caption, extra = "", self.text

        msg = await client.send_file(
            entity,
            files[0] if len(files) == 1 else files,
            caption=caption,
        )
        if extra:
            await client.send_message(entity, extra)
        return msg


async def download_media(message, folder: Path) -> Path | None:
    if not (suffix := get_extension(message.media)):
        logger.info(f"Nothing to download for '{folder}'")
        return None

    ofile = folder / f"media{suffix}"
    if ofile.exists() and ofile.stat().st_size:
        logger.info(f"Reusing '{ofile}'")
        return ofile

    folder.mkdir(parents=True, exist_ok=True)
    try:
        saved = await message.download_media(file=str(ofile))
    except Exception as e:
        logger.exception(f"Failed to download '{ofile}': {e}", exc_info=True)
        return None
    return Path(saved) if saved else None


def save_text(folder: Path, post: MediaPost) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    ofile = folder / "text.json"
    ofile.write_text(
        post.to_json(ensure_ascii=False, indent=4),  # type: ignore
        encoding="utf-8",
    )


class MediaPostBuilder:
    def valid(self, message) -> bool:
        return bool(message.message or message.media)

    def ofile(self, messages: list) -> Path:
        return ofile_for(messages)

    async def build(self, message, number: int, root: Path) -> MediaPost:
        folder = root / f"{number:04d}"
        post = MediaPost(
            number=number,
            date=message.date.isoformat(),
            text=(message.message or "").strip(),
        )
        if message.media is not None:
            if media := await download_media(message, folder):
                post.media.append(str(media))
        save_text(folder, post)
        return post


@click.command()
@click.option("--dry-run/--no-dry-run", default=False, help="Do not push")
@click.option("--max-posts", type=int, default=100, help="Counting backwards")
@click.option(
    "--cache",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Push this dump instead of pulling the channel again",
)
def main(dry_run: bool, max_posts: int, cache: Path | None):
    config = load_config(
        channel="X_CHANNEL_NAME",
        out_channel="XOUT_CHANNEL_NAME",
    )
    cachefile = cache or pull(
        builder=MediaPostBuilder(),
        config=config,
        limit=max_posts,
    )
    posts = load_posts(cachefile, MediaPost)
    push(posts, config=config, dry_run=dry_run)
    click.echo("Done.")


if __name__ == "__main__":
    main()
