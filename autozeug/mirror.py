import asyncio
import logging
from collections.abc import Iterator
from difflib import get_close_matches
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

import click
from telethon import TelegramClient as TC
from telethon import events
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError
from telethon.helpers import add_surrogate
from telethon.tl.types import MessageEntityCustomEmoji, MessageMediaWebPage

from autozeug.exercise import chunks
from autozeug.telegram import load_config, video_attributes

logger = logging.getLogger(__name__)

GENERAL_TOPIC = 1
MAX_CAPTION = 1024


def kind(entity) -> str:
    """How Telegram sees a chat, which decides what can be done to it."""
    if not hasattr(entity, "megagroup"):
        return "basic group"
    if not entity.megagroup:
        return "channel"
    return "forum" if getattr(entity, "forum", False) else "supergroup"


async def alive(client, entity):
    """A migrated group lingers as a tombstone, follow it to the real one."""
    if not (migrated := getattr(entity, "migrated_to", None)):
        return entity
    logger.info(f"'{entity.title}' was migrated, following it")
    return await client.get_entity(migrated)


def named(dialog, wanted: str) -> bool:
    return (dialog.name or "").strip().lower() == wanted.strip().lower()


async def resolve_chat(client, title: str):
    """A group or a channel by name, @username, t.me link or id."""
    wanted = title.strip()
    if wanted.startswith(("@", "t.me/", "https://t.me/")):
        return await client.get_entity(wanted)
    if wanted.lstrip("-").isdigit():
        return await client.get_entity(int(wanted))

    seen = []
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        if named(dialog, wanted):
            entity = await alive(client, dialog.entity)
            logger.info(f"'{dialog.name}' is a {kind(entity)}")
            return entity
        seen.append(dialog.name)

    near = get_close_matches(wanted, seen, n=3) or seen[:3]
    raise ValueError(
        f"No chat named {wanted!r} in {len(seen)} dialogs of this session. "
        f"Closest: {near}. Run with --list to see them all."
    )


async def show_dialogs(client) -> None:
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        dead = " migrated" if getattr(entity, "migrated_to", None) else ""
        click.echo(
            f"{dialog.name!r:<44} {dialog.id:>15} {kind(entity)}{dead}"
        )


def topic_of(message) -> int:
    """The forum topic a message belongs to, `1` being General."""
    reply = message.reply_to
    if reply is None or not getattr(reply, "forum_topic", False):
        return GENERAL_TOPIC
    return reply.reply_to_top_id or reply.reply_to_msg_id


def forwardable(message) -> bool:
    """Joins, pins and topic events have no content to forward."""
    if (action := getattr(message, "action", None)) is None:
        return True
    logger.info(f"Skipping service {message.id}: {type(action).__name__}")
    return False


def selected(messages: list, topics: tuple[int, ...]) -> list:
    chosen = [m for m in messages if forwardable(m)]
    if not topics:
        return chosen
    return [m for m in chosen if topic_of(m) in topics]


def albums(messages: list) -> Iterator[list]:
    """The messages in order, the parts of an album kept together."""
    group: list = []
    for message in messages:
        same = group and message.grouped_id == group[0].grouped_id
        if same and message.grouped_id is not None:
            group.append(message)
            continue
        if group:
            yield group
        group = [message]
    if group:
        yield group


def plain(entities: list | None) -> list:
    # Custom emoji can only be sent from a premium account
    return [
        entity
        for entity in (entities or [])
        if not isinstance(entity, MessageEntityCustomEmoji)
    ]


def caption_of(messages: list) -> tuple[str, list]:
    """An album carries its text on one of its parts."""
    for message in messages:
        if message.message:
            return message.message, plain(message.entities)
    return "", []


async def downloaded(message, folder: Path) -> Path | None:
    media = message.media
    if media is None or isinstance(media, MessageMediaWebPage):
        return None

    if not (path := await message.download_media(file=str(folder))):
        logger.warning(f"Nothing downloadable in {message.id}")
        return None
    return Path(path)


async def send_media(client, entity, files: list[Path], text, entities):
    # Only a single file can carry the attributes that make it streamable
    extra = video_attributes(files[0]) if len(files) == 1 else {}
    await client.send_file(
        entity,
        files[0] if len(files) == 1 else files,
        caption=text,
        formatting_entities=entities or None,
        **extra,
    )


async def copy(client, entity, messages: list, folder: Path) -> None:
    """A protected chat forbids forwarding, so re-upload the content."""
    files = [
        path
        for message in messages
        if (path := await downloaded(message, folder))
    ]
    text, entities = caption_of(messages)
    if not files and not text:
        logger.warning(f"Nothing to copy from {messages[0].id}")
        return

    # A caption is capped at 1024 characters, a standalone message is not
    fits = len(add_surrogate(text)) <= MAX_CAPTION
    if files:
        await send_media(
            client,
            entity,
            files,
            text if fits else "",
            entities if fits else [],
        )
        if fits:
            return

    for part, clipped in chunks(text, entities):
        await client.send_message(entity, part, formatting_entities=clipped)


async def mirror(
    client,
    entity,
    messages: list,
    *,
    drop_author: bool,
    folder: Path,
    protected: bool,
) -> None:
    if not messages:
        return

    try:
        if protected:
            await copy(client, entity, messages, folder)
        else:
            # One call per album keeps the grouping on the other side
            await client.forward_messages(
                entity,
                messages,
                drop_author=drop_author,
            )
    except ChatForwardsRestrictedError:
        logger.warning("Forwarding is restricted, copying instead")
        await copy(client, entity, messages, folder)
    except FloodWaitError as e:
        logger.warning(f"Flood wait for {e.seconds}s")
        await asyncio.sleep(e.seconds + 1)
        await mirror(
            client,
            entity,
            messages,
            drop_author=drop_author,
            folder=folder,
            protected=protected,
        )


@click.command()
@click.option("--session", default="mirror", help="Telethon session name")
@click.option("--list", "listing", is_flag=True, help="Show the dialogs")
@click.option("--topic", "topics", type=int, multiple=True, help="Topic ids")
@click.option("--drop-author/--keep-author", default=False)
@click.option("--backlog", type=int, default=0, help="Forward the last N")
@click.option("--follow/--no-follow", default=True, help="Keep listening")
def main(
    session: str,
    listing: bool,
    topics: tuple[int, ...],
    drop_author: bool,
    backlog: int,
    follow: bool,
):
    config = load_config(
        channel="X_CHANNEL_NAME",
        out_channel="XOUT_CHANNEL_NAME",
    )

    async def run(folder: Path):
        async with TC(session, config.api_id, config.api_hash) as client:
            me = await client.get_me()
            logger.info(f"Signed in as {me.first_name} (@{me.username})")
            if listing:
                await show_dialogs(client)
                return

            source = await resolve_chat(client, config.channel_name)
            target = await resolve_chat(client, config.out_channel_name)

            protected = getattr(source, "noforwards", False)
            if protected:
                logger.warning(
                    f"'{config.channel_name}' protects its content, "
                    "re-uploading every post instead of forwarding"
                )
            send = partial(
                mirror,
                client,
                target,
                drop_author=drop_author,
                folder=folder,
                protected=protected,
            )

            if backlog:
                messages = [
                    m
                    async for m in client.iter_messages(source, limit=backlog)
                ][::-1]
                for group in albums(selected(messages, topics)):
                    await send(group)
                    logger.info(f"Mirrored {group[0].id}")

            if not follow:
                return

            @client.on(events.NewMessage(chats=source))
            async def on_message(event):
                # Albums arrive message by message, handled below instead
                if event.message.grouped_id:
                    return
                if not selected([event.message], topics):
                    return
                await send([event.message])
                logger.info(f"Mirrored {event.message.id}")

            @client.on(events.Album(chats=source))
            async def on_album(event):
                if not (group := selected(event.messages, topics)):
                    return
                await send(group)
                logger.info(f"Mirrored album of {len(group)}")

            logger.info(f"Listening on {config.channel_name}...")
            await client.run_until_disconnected()

    with TemporaryDirectory() as folder:
        asyncio.run(run(Path(folder)))


if __name__ == "__main__":
    main()
