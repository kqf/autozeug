import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from dataclasses_json import dataclass_json
from environs import env
from telethon import TelegramClient as TC
from telethon.tl.types import DocumentAttributeVideo

from autozeug.video import extract_metadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def resolve_channel(client, title: str):
    async for dialog in client.iter_dialogs():
        matched = dialog.name.strip().lower() == title.strip().lower()
        if dialog.is_channel and matched:
            return dialog.entity
    raise ValueError(f"Channel '{title}' not found")


def video_attributes(media: Path) -> dict:
    if not (metadata := extract_metadata(media)):
        return {}

    return {
        "attributes": [
            DocumentAttributeVideo(
                duration=metadata.duration,
                w=metadata.width,
                h=metadata.height,
                supports_streaming=True,
            )
        ]
    }


async def upload_video(client, entity, media, caption):
    return await client.send_file(
        entity,
        media,
        caption=caption,
        **video_attributes(media),
    )


@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    channel_name: str
    out_channel_name: str


def load_config() -> TelegramConfig:
    env.read_env()
    return TelegramConfig(
        api_id=env.int("TELEGRAM_API_ID"),
        api_hash=env("TELEGRAM_API_HASH"),
        channel_name=env("CHANNEL_NAME"),
        out_channel_name=env("OUT_CHANNEL_NAME"),
    )


@dataclass_json
@dataclass
class Post:
    date: str
    text: str


def save_posts(filename: Path, posts: list[Post]) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(
            [post.to_dict() for post in posts],  # type: ignore
            f,
            ensure_ascii=False,
            indent=4,
        )


def load_posts(filename: Path) -> list[Post]:
    with open(filename, encoding="utf-8") as f:
        data = json.load(f)
    return [Post.from_dict(item) for item in data]  # type: ignore


class PostBuilder:
    def valid(self, message) -> bool:
        return message.message and "youtube" in message.message

    def build(self, message) -> Post:
        return Post(
            date=message.date.isoformat(),
            text=message.message.strip(),
        )

    def ofile(self, posts: list[Post]) -> Path:
        if not posts:
            raise RuntimeError("No posts to save")
        dt = datetime.fromisoformat(posts[0].date)
        return Path(dt.strftime("%d-%m-%Y.json"))


def pull(
    builder: PostBuilder,
    config: TelegramConfig,
    limit: int = 100,
) -> Path:
    async def main():
        logger.info(f"Fetching messages from {config.channel_name}...")
        async with TC("pull", config.api_id, config.api_hash) as client:
            entity = await resolve_channel(client, config.channel_name)
            messages = []

            async for message in client.iter_messages(entity, limit=limit):
                if not builder.valid(message):
                    continue
                messages.append(builder.build(message))

            # Restore the chronological order
            messages = messages[::-1]
            ofile = builder.ofile(messages)
            save_posts(ofile, messages)
            logger.info(f"Saved {len(messages)} text posts to '{ofile}'")
        return ofile

    return asyncio.run(main())


class OutPost(Protocol):
    def valid(self) -> bool: ...
    async def upload(self, client, entity) -> None: ...


def push(
    posts: Sequence[OutPost],
    config: TelegramConfig,
    dry_run: bool = False,
) -> None:
    async def main():
        logger.info(f"Uploading messages to {config.out_channel_name}...")
        async with TC("push", config.api_id, config.api_hash) as client:
            entity = await resolve_channel(client, config.out_channel_name)
            for post in posts:
                if not post.valid():
                    continue

                if dry_run:
                    logger.info(f"Would upload {post}")
                    continue

                try:
                    await post.upload(client, entity)
                    logger.info(f"Uploaded: {post}")
                except Exception as e:
                    logger.error(f"Push failed {post}: {e}", exc_info=True)

    asyncio.run(main())
