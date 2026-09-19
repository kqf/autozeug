import logging
from collections.abc import Iterator
from copy import copy
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

import click
from dataclasses_json import dataclass_json
from telethon.extensions import html as tghtml
from telethon.helpers import add_surrogate, del_surrogate, strip_text
from telethon.tl.types import MessageEntityCustomEmoji
from telethon.utils import get_extension

from autozeug.telegram import load_config, load_posts, ofile_for, pull, push

logger = logging.getLogger(__name__)

HTML = "html"
TEXT_TAGS = {
    "TextBold": "b",
    "TextItalic": "i",
    "TextUnderline": "u",
    "TextStrike": "s",
    "TextFixed": "code",
}
QUOTES = ("PageBlockBlockquote", "PageBlockPullquote")
LISTS = ("PageBlockList", "PageBlockOrderedList")
BULLET = "\u2022 "
DIVIDER = "\u2014" * 3
MAX_MESSAGE = 4096
SEPARATORS = ("\n\n", "\n", " ")


def cut(text: str, limit: int) -> int:
    """Length of the first chunk of at most `limit` utf-16 code units."""
    if len(text) <= limit:
        return len(text)

    for separator in SEPARATORS:
        if (stop := text.rfind(separator, limit // 2, limit)) > 0:
            return stop + len(separator)

    # Hard cut, but never in the middle of a surrogate pair
    return limit - 1 if "\ud800" <= text[limit - 1] <= "\udbff" else limit


def clip(entities: list, start: int, stop: int) -> list:
    """The entities of [start, stop), with the offsets moved to the chunk."""
    clipped = []
    for entity in entities:
        begin = max(entity.offset, start)
        end = min(entity.offset + entity.length, stop)
        if end <= begin:
            continue

        shifted = copy(entity)
        shifted.offset, shifted.length = begin - start, end - begin
        clipped.append(shifted)
    return clipped


def chunks(
    text: str,
    entities: list,
    limit: int = MAX_MESSAGE,
) -> Iterator[tuple[str, list]]:
    # Telegram counts the limit and the offsets in utf-16 code units
    encoded, start = add_surrogate(text), 0
    while start < len(encoded):
        stop = start + cut(encoded[start:], limit)
        clipped = clip(entities, start, stop)
        if body := strip_text(encoded[start:stop], clipped):
            yield del_surrogate(body), clipped
        start = stop


@dataclass_json
@dataclass
class MediaPost:
    number: int
    date: str
    text: str
    media: list[str] = field(default_factory=list)
    parse_mode: str = ""

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

    def chunks(self) -> Iterator[tuple[str, list]]:
        if self.parse_mode == HTML:
            return chunks(*tghtml.parse(self.text))
        return chunks(self.text, [])

    async def upload(self, client, entity):
        # Media goes first, always without a caption: captions are capped at
        # 1024 characters, a standalone message is not.
        if files := self.files():
            await client.send_file(
                entity,
                files[0] if len(files) == 1 else files,
                caption="",
            )

        message = None
        for text, entities in self.chunks():
            message = await client.send_message(
                entity,
                text,
                formatting_entities=entities,
            )
        return message


def rich_text(text) -> str:
    """One `TypeRichText` of a rich message as html."""
    name = type(text).__name__
    if text is None or name == "TextEmpty":
        return ""

    if name == "TextPlain":
        return escape(text.text)

    if name == "TextConcat":
        return "".join(rich_text(part) for part in text.texts)

    if name == "TextUrl":
        return f'<a href="{escape(text.url)}">{rich_text(text.text)}</a>'

    if name == "TextEmail":
        return (
            f'<a href="mailto:{escape(text.email)}">{rich_text(text.text)}</a>'
        )

    if tag := TEXT_TAGS.get(name):
        return f"<{tag}>{rich_text(text.text)}</{tag}>"

    # TextAnchor, TextCustomEmoji and friends: keep what they wrap
    return rich_text(getattr(text, "text", None))


def rich_prefix(item) -> str:
    return f"{item.num} " if getattr(item, "num", None) else BULLET


def rich_item(item) -> str:
    if blocks := getattr(item, "blocks", None):
        return "\n".join(rich_block(block) for block in blocks)
    return rich_text(item.text)


def rich_block(block) -> str:
    """One `TypePageBlock` as the html a plain message understands."""
    name = type(block).__name__
    if name.startswith("PageBlockHeading") or name in ("PageBlockTitle",):
        return f"<b>{rich_text(block.text).strip()}</b>"

    if name == "PageBlockParagraph":
        return rich_text(block.text)

    if name in QUOTES:
        return f"<blockquote>{rich_text(block.text).strip()}</blockquote>"

    if name == "PageBlockPreformatted":
        return f"<pre>{rich_text(block.text)}</pre>"

    if name == "PageBlockDivider":
        return DIVIDER

    if name in LISTS:
        # The photos travel as attached media, the numbering as plain text
        return "\n".join(
            f"{rich_prefix(item)}{rich_item(item)}" for item in block.items
        )

    if name == "PageBlockPhoto":
        return ""

    if blocks := getattr(block, "blocks", None):
        inner = "\n\n".join(rich_block(block) for block in blocks)
        return (
            f"<blockquote>{inner}</blockquote>"
            if "Blockquote" in name
            else inner
        )

    if text := getattr(block, "text", None):
        logger.warning(f"Unsupported rich block {name}, keeping its text")
        return rich_text(text)

    logger.warning(f"Dropping unsupported rich block {name}")
    return ""


def rich_html(rich) -> str:
    blocks = (rich_block(block).strip() for block in rich.blocks)
    return "\n\n".join(block for block in blocks if block)


async def download_rich(message, rich, folder: Path) -> list[str]:
    paths = []
    for photo in rich.photos:
        ofile = folder / f"rich{len(paths)}.jpg"
        if not (ofile.exists() and ofile.stat().st_size):
            folder.mkdir(parents=True, exist_ok=True)
            if not await message.client.download_media(photo, file=str(ofile)):
                logger.error(f"Failed to download '{ofile}'")
                continue

        paths.append(str(ofile))

    if rich.documents:
        logger.warning(
            f"Dropping {len(rich.documents)} documents of a rich message"
        )
    return paths


def as_html(message) -> str:
    # Custom emoji can only be sent from a premium account, keep the plain
    # fallback character instead.
    entities = [
        entity
        for entity in (message.entities or [])
        if not isinstance(entity, MessageEntityCustomEmoji)
    ]
    return tghtml.unparse(message.message or "", entities).strip()


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
        if (
            message.message
            or message.media
            or getattr(message, "rich_message", None)
        ):
            return True

        if action := getattr(message, "action", None):
            logger.info(
                f"Skipping service {message.id}: {type(action).__name__}"
            )
        else:
            logger.warning(f"Skipping empty {message.id} of {message.date}")
        return False

    def ofile(self, messages: list) -> Path:
        return ofile_for(messages)

    async def build(self, message, number: int, root: Path) -> MediaPost:
        folder = root / f"{number:04d}"
        post = MediaPost(
            number=number,
            date=message.date.isoformat(),
            text=as_html(message),
            parse_mode=HTML,
        )
        if rich := getattr(message, "rich_message", None):
            post.text = rich_html(rich)
            post.media.extend(await download_rich(message, rich, folder))

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
