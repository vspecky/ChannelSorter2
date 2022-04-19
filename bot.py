# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""/r/ProgrammingLanguages discord channel management bot."""
import json
import os
import random
import re
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from itertools import chain, combinations
from pathlib import Path
from typing import Dict, List
from urllib.request import urlopen

import discord
from discord.ext import commands, tasks

channels_path = Path(__file__).parent / "categories.txt"


class ChannelBot(commands.Bot):
    """Discordpy bot subclass with convenience methods we need."""

    @tasks.loop(hours=1)
    async def hourly_update(self):
        """Clean up channel list and cycle presence."""
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="over the project channels",
            )
        )
        for guild in self.guilds:
            log_channel = get_log_channel(guild)
            if log_channel is None:
                continue
            print(f"Running hourly update in {guild.name}")
            await archive_inactive_inner(guild, log_channel, verbose=False)
            await sort_inner(guild, log_channel, verbose=False)
        await self.change_presence(
            activity=discord.Game(name=get_random_top100_steam_game())
        )

    async def on_ready(self):
        """Set initial presence."""
        print(f"Successfully logged in as {self.user}")
        self.hourly_update.start()


bot = ChannelBot(
    command_prefix="./",
    description="/r/proglangs discord helper bot",
    intents=discord.Intents.all(),
)

CHANNEL_OWNER_PERMS = discord.PermissionOverwrite(
    send_messages=True,
    read_messages=True,
    view_channel=True,
    manage_channels=True,
    manage_webhooks=True,
    # manage_threads=True,
    manage_messages=True,
)


def get_project_categories(guild) -> List[discord.CategoryChannel]:
    """Get a list of project categories in the server from the config file."""
    with channels_path.open() as f:
        return [
            discord.utils.get(guild.categories, id=int(line.strip()))
            for line in f
        ]


def get_archive_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Get the archive category for the guild."""
    return discord.utils.get(guild.categories, name="Archive")


def get_log_channel(guild: discord.Guild) -> discord.TextChannel:
    """Return the log channel for the guild."""
    return discord.utils.get(guild.channels, name="channelbot-logs")


def unbalancedness(separator_idxs: List[int]) -> int:
    """
    Return the sum of squares of differences between sublist lengths given a
    list of indices of sublist "borders".
    """
    score = 0
    for prev, nxt in zip(separator_idxs[:-1], separator_idxs[1:]):
        score += (nxt - prev) ** 2
    return score


def balanced_categories(
    categories: List[discord.CategoryChannel],
    channels: List[discord.TextChannel],
) -> Dict[int, List[discord.TextChannel]]:
    """
    Given a list of categories and channels, return a dict mapping category
    ID to a list of channels in that category so that the channels are
    sorted alphabetically, every starting letter is contained entirely
    within a single category, and each category has a similar number of
    channels.
    """
    prev_letter = channels[0].name.upper()[0]
    letter_change_idxs = [0]
    # Save indices with letter changes
    for i, channel in enumerate(channels):
        if channel.name.upper()[0] != prev_letter:
            prev_letter = channel.name.upper()[0]
            letter_change_idxs.append(i)

    best_partition = [
        0,
        # Find the partition with the smallest unbalancedness
        *min(
            combinations(letter_change_idxs, len(categories) - 1),
            key=lambda partition: unbalancedness(
                [0, *partition, len(channels)]
            ),
        ),
    ]
    assert len(best_partition) == len(categories)

    category_channels = {}
    # Map categories to their channels
    for start_idx, category in reversed(list(zip(best_partition, categories))):
        category_channels[category.id] = channels[start_idx:]
        channels = channels[:start_idx]

    assert all(category.id in category_channels for category in categories)
    return category_channels


@bot.command()
@commands.has_permissions(administrator=True)
async def get_categories(ctx):
    """Get the names of the project categories."""
    await ctx.send(
        f"Project categories: "
        f"{[c.name for c in get_project_categories(ctx.guild)]}"
    )


@bot.command()
@commands.is_owner()
async def set_categories(ctx, *categories: discord.CategoryChannel):
    """Set the ids of the project categories."""
    with channels_path.open("w") as f:
        for category in categories:
            f.write(str(category.id) + "\n")
    await ctx.send(f"New project categories: {[c.name for c in categories]}")


@bot.command()
@commands.has_permissions(administrator=True)
async def sort(ctx: discord.ext.commands.Context):
    """Sort project channels."""
    return await sort_inner(ctx.guild, ctx.channel)


async def sort_inner(
    guild: discord.Guild,
    log_channel: discord.TextChannel,
    verbose: bool = True,
):
    """Channel sorting logic."""
    if verbose:
        await log_channel.send("Sorting channels!")

    moves_made = 0
    renames_made = 0

    categories = get_project_categories(guild)
    channels = sorted(
        chain.from_iterable(c.channels for c in categories),
        key=lambda ch: ch.name,
    )
    category_channels = balanced_categories(categories, channels)
    # rename project categories
    for cat_id, cat_channels in category_channels.items():
        category = discord.utils.get(guild.categories, id=cat_id)
        start_letter = cat_channels[0].name.upper()[0]
        end_letter = cat_channels[-1].name.upper()[0]
        # Rename category if necessary
        new_cat_name = f"Projects {start_letter}-{end_letter}"
        print(
            f"{new_cat_name}: "
            f"channels {cat_channels[0].name}:{cat_channels[-1].name}"
        )
        if category.name != new_cat_name:
            renames_made += 1
            if verbose:
                await log_channel.send(
                    f"Renaming {category.name} to {new_cat_name}"
                )
            await category.edit(name=new_cat_name)

    # Shuffle channels around
    for category in categories:
        for i, channel in enumerate(category_channels[category.id]):
            cat_channels = category.channels
            if len(cat_channels) > i:
                target_channel = cat_channels[i]
                new_pos = target_channel.position
                needs_move = target_channel != channel
            else:
                new_pos = cat_channels[-1].position + 1
                needs_move = cat_channels[-1] != channel
            if channel.category_id != category.id or needs_move:
                old_pos = channel.position
                if old_pos > new_pos:
                    # moving channel up
                    for other_channel in channels:
                        if new_pos <= other_channel.position < old_pos:
                            other_channel.position += 1
                else:
                    # moving channel down
                    for other_channel in channels:
                        if old_pos < other_channel.position <= new_pos:
                            other_channel.position -= 1
                moves_made += 1
                channel.category_id = category.id
                channel.position = new_pos
                print(f"Moving channel {channel.name}.")
                await channel.edit(
                    category=category, position=category.channels[i].position
                )

    if verbose or renames_made > 0 or moves_made > 0:
        await log_channel.send(
            f"Channels sorted! Renamed {renames_made} categories and "
            f"moved {moves_made} channels."
        )


@bot.command()
@commands.has_permissions(administrator=True)
async def make_channel(ctx, owner: discord.Member, name: str):
    """Create a new project channel and role."""
    new_channel = await ctx.guild.create_text_channel(name=name)
    await reposition_channel(new_channel, get_project_categories(ctx.guild))
    await ctx.send(f"Created channel {new_channel.mention}.")
    role = await ctx.guild.create_role(
        name=f"lang: {name.capitalize()}",
        colour=discord.Colour.default(),
        mentionable=True,
    )
    lang_owner_role = discord.utils.get(
        ctx.guild.roles, name="Lang Channel Owner"
    )
    await ctx.send(f"Created and assigned role {role.mention}.")
    await owner.add_roles(role, lang_owner_role)
    channelbot_role = discord.utils.get(ctx.guild.roles, name="Channel Bot")
    muted_role = discord.utils.get(ctx.guild.roles, name="muted")
    overwrites = {
        role: CHANNEL_OWNER_PERMS,
        channelbot_role: discord.PermissionOverwrite(view_channel=False),
        muted_role: discord.PermissionOverwrite(
            send_messages=False, add_reactions=False
        ),
    }
    await new_channel.edit(overwrites=overwrites)
    await ctx.send(f"Set appropriate permissions for {new_channel.mention}.")
    await sort_inner(ctx.guild, ctx.channel, verbose=True)
    await ctx.send(f"Done!")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def archive(ctx):
    """Archive a channel."""
    await ctx.send("Archiving channel.")
    await get_log_channel(ctx.guild).send(
        f"Channel {ctx.channel.mention} archived manually by owner."
    )
    await ctx.channel.edit(category=get_archive_category(ctx.guild))
    everyone = discord.utils.get(ctx.guild.roles, name="@everyone")
    await ctx.channel.set_permissions(everyone, send_messages=False)
    for role in ctx.channel.overwrites:
        if role.name.startswith("lang: "):
            await ctx.channel.set_permissions(
                role, overwrite=CHANNEL_OWNER_PERMS
            )
            break


@bot.command()
@commands.has_permissions(administrator=True)
async def archive_inactive(ctx):
    """Archive inactive channels."""
    return await archive_inactive_inner(ctx.guild, ctx.channel)


async def archive_inactive_inner(
    guild: discord.Guild,
    log_channel: discord.TextChannel,
    verbose: bool = True,
):
    """Archive project channels that have been inactive for over 90 days."""
    if verbose:
        await log_channel.send("Archiving inactive project channels.")
    archived = 0
    channel: discord.TextChannel
    for channel in chain.from_iterable(
        c.channels for c in get_project_categories(guild)
    ):
        try:
            last_message, *_ = await channel.history(limit=1).flatten()
        except IndexError:
            continue
        time_since = datetime.now() - last_message.created_at
        if time_since.days <= 90:
            continue
        await log_channel.send(
            f"Archiving {channel.mention} due to inactivity."
        )
        await channel.send(
            "Archiving channel due to inactivity. "
            "If you're the channel owner, send a message here to unarchive."
        )
        await channel.edit(category=get_archive_category(guild))
        everyone = discord.utils.get(guild.roles, name="@everyone")
        await channel.set_permissions(everyone, send_messages=False)
        for role in channel.overwrites:
            if role.name.startswith("lang: "):
                await channel.set_permissions(
                    role, overwrite=CHANNEL_OWNER_PERMS
                )
                break
        archived += 1

    if verbose or archived > 0:
        await log_channel.send(f"Archived {archived} inactive channels.")


@bot.command()
@commands.is_owner()
async def run_python(ctx, *, code):
    """Run arbitrary Python."""

    async def aexec(source, globals_, locals_):
        exec(
            "async def __ex(ctx, globals, locals): "
            + "".join(f"\n {line}" for line in source.split("\n")),
            globals_,
            locals_,
        )
        try:
            return await locals_["__ex"](ctx, globals_, locals_)
        except Exception as e:
            await ctx.reply(f"⚠️ {e}")
            raise e

    code = re.match("```(python)?(.*?)```", code, flags=re.DOTALL).group(2)
    print(f"Running ```{code}```")
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        await aexec(code, globals(), locals())
    await ctx.send(f"```\n{stdout.getvalue()}\n\n{stderr.getvalue()}```")


@bot.listen("on_message")
async def on_message(message: discord.Message) -> None:
    """Listen for messages in archived channels to unarchive them."""
    if (
        not isinstance(message.guild, discord.Guild)
        or message.channel.category is None
        or message.channel.category != get_archive_category(message.guild)
    ):
        return
    everyone = discord.utils.get(message.guild.roles, name="@everyone")
    await message.channel.set_permissions(everyone, overwrite=None)
    await reposition_channel(
        message.channel, get_project_categories(message.guild)
    )
    await message.channel.send("Channel unarchived!")


async def reposition_channel(channel, project_categories):
    """
    Try to position a channel where it should be in the projects
    categories without resorting everything.
    """
    channels = sorted(
        (
            ch
            for c in project_categories
            for ch in c.channels
            if ch.id != channel.id
        ),
        key=lambda ch: ch.name,
    )
    category = None
    position = 0
    for c in channels:
        position = c.position
        if c.name > channel.name:
            if not category:
                category = c.category
            break
        category = c.category
    else:
        # Channel should be sorted last
        position += 1
    await channel.edit(category=category, position=position)
    print(f"Moved channel {channel.name}")


@bot.event
async def on_command_error(ctx, error):
    """Handle command errors by sending the stringified exception back."""
    traceback.print_exception(error)
    await ctx.reply(str(error))


@bot.event
async def on_guild_channel_update(before, after):
    """Move channels to the correct position if they got renamed."""
    if not isinstance(after, discord.TextChannel):
        return
    categories = get_project_categories(before.guild)
    if not (after.category in categories and after.name != before.name):
        return
    await get_log_channel(before.guild).send(
        f"Channel {before.mention} was renamed: {before.name} -> {after.name}"
    )
    await reposition_channel(after, categories)


# Fun stuff
def get_random_top100_steam_game() -> str:
    """Get the name of a random top 100 steam game."""
    games = json.load(
        urlopen("https://steamspy.com/api.php?request=top100in2weeks")
    )
    game = random.choice(list(games.keys()))
    return games[game]["name"]


bot.run(os.getenv("CHANNELSORTER_TOKEN"))
