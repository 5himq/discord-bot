import asyncio
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass

import discord
from discord import app_commands

from casino import CasinoDatabase, register_casino_command

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("message-counter-bot")


@dataclass
class ScanResult:
    counts: Counter[int]
    character_counts: Counter[int]
    display_names: dict[int, str]
    scanned_channels: int
    skipped_channels: int
    failed_channels: list[str]
    total_messages: int
    total_characters: int
    elapsed_seconds: float


class MessageCounterBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = True

        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.scan_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        """Register slash commands with Discord."""
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d command(s) to guild %s.", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global command(s).", len(synced))

    async def on_ready(self) -> None:
        if self.user is not None:
            logger.info("Logged in as %s (ID: %s).", self.user, self.user.id)

    async def scan_guild(
        self,
        guild: discord.Guild,
        progress_callback,
    ) -> ScanResult:
        """Fetch all available text and forum-thread history and count authors."""
        started_at = time.monotonic()
        counts: Counter[int] = Counter()
        character_counts: Counter[int] = Counter()
        display_names: dict[int, str] = {}
        failed_channels: list[str] = []
        scanned_channels = 0
        skipped_channels = 0
        total_messages = 0
        total_characters = 0

        me = guild.me
        channel_targets: list[tuple[str, discord.abc.Messageable]] = []
        seen_channel_ids: set[int] = set()

        def add_target(
            label: str,
            target: discord.abc.Messageable,
        ) -> None:
            if target.id not in seen_channel_ids:
                seen_channel_ids.add(target.id)
                channel_targets.append((label, target))

        for channel in guild.text_channels:
            add_target(f"#{channel.name}", channel)

        for forum in guild.forums:
            if me is not None:
                permissions = forum.permissions_for(me)
                if not (
                    permissions.view_channel
                    and permissions.read_message_history
                ):
                    skipped_channels += 1
                    continue

            for thread in forum.threads:
                add_target(f"{forum.name}/{thread.name}", thread)

            try:
                async for thread in forum.archived_threads(limit=None):
                    add_target(f"{forum.name}/{thread.name}", thread)
            except (discord.Forbidden, discord.NotFound):
                skipped_channels += 1
                failed_channels.append(f"フォーラム #{forum.name} のアーカイブ")
            except discord.HTTPException:
                skipped_channels += 1
                failed_channels.append(f"フォーラム #{forum.name} のアーカイブ")

        channel_count = len(channel_targets)
        for channel_index, (channel_label, channel) in enumerate(
            channel_targets,
            start=1,
        ):
            if me is not None:
                permissions = channel.permissions_for(me)
                if not (
                    permissions.view_channel
                    and permissions.read_message_history
                ):
                    skipped_channels += 1
                    await progress_callback(
                        channel_index,
                        channel_count,
                        channel_label,
                        total_messages,
                    )
                    continue

            try:
                async for message in channel.history(
                    limit=None,
                    oldest_first=True,
                ):
                    if self.user is not None and message.author.id == self.user.id:
                        continue

                    author_id = message.author.id
                    counts[author_id] += 1
                    display_names[author_id] = message.author.display_name
                    total_messages += 1
                    message_characters = len(message.content)
                    if message_characters:
                        character_counts[author_id] += message_characters
                        total_characters += message_characters

                scanned_channels += 1
            except (discord.Forbidden, discord.NotFound):
                skipped_channels += 1
                failed_channels.append(channel_label)
            except discord.HTTPException:
                skipped_channels += 1
                failed_channels.append(channel_label)

            await progress_callback(
                channel_index,
                channel_count,
                channel_label,
                total_messages,
            )

        return ScanResult(
            counts=counts,
            character_counts=character_counts,
            display_names=display_names,
            scanned_channels=scanned_channels,
            skipped_channels=skipped_channels,
            failed_channels=failed_channels,
            total_messages=total_messages,
            total_characters=total_characters,
            elapsed_seconds=time.monotonic() - started_at,
        )


bot = MessageCounterBot()
casino_db = CasinoDatabase()
register_casino_command(bot.tree, casino_db)


async def run_top10(
    interaction: discord.Interaction,
    *,
    by_characters: bool,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはDiscordサーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if bot.scan_lock.locked():
        await interaction.response.send_message(
            "現在、別の集計を実行中です。完了してからもう一度お試しください。",
            ephemeral=True,
        )
        return

    await bot.scan_lock.acquire()

    try:
        await interaction.response.defer(thinking=True)
        last_progress_update = 0.0

        async def update_progress(
            channel_index: int,
            channel_count: int,
            channel_name: str,
            message_count: int,
        ) -> None:
            nonlocal last_progress_update
            now = time.monotonic()
            if (
                channel_index != channel_count
                and now - last_progress_update < 3.0
            ):
                return

            last_progress_update = now
            await interaction.edit_original_response(
                content=(
                    "集計中です。"
                    f" {channel_index}/{channel_count} チャンネルを確認中"
                    f"（#{channel_name}、{message_count:,}件）..."
                )
            )

        await interaction.edit_original_response(
            content="集計を開始しました。過去のメッセージを取得しています..."
        )
        result = await bot.scan_guild(interaction.guild, update_progress)

        ranking = (
            result.character_counts
            if by_characters
            else result.counts
        )
        if not ranking:
            await interaction.edit_original_response(
                content=None,
                embed=discord.Embed(
                    title="文字数 TOP10" if by_characters else "発言数 TOP10",
                    description="メッセージが見つかりませんでした。",
                    color=discord.Color.orange(),
                ),
            )
            return

        title = "文字数 TOP10" if by_characters else "発言数 TOP10"
        unit = "文字" if by_characters else "件"
        summary = (
            f"総計 {result.total_characters if by_characters else result.total_messages:,}{unit} を集計"
        )

        embed = discord.Embed(
            title=title,
            description=summary,
            color=discord.Color.blurple(),
        )

        lines = []
        for rank, (author_id, amount) in enumerate(
            ranking.most_common(10),
            start=1,
        ):
            name = discord.utils.escape_markdown(
                result.display_names.get(author_id, "名前不明")
            )
            lines.append(
                f"**{rank}.** {name} (<@{author_id}>) — **{amount:,}{unit}**"
            )

        embed.add_field(name="ランキング", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"集計時間: {result.elapsed_seconds:.1f}秒")

        await interaction.edit_original_response(content=None, embed=embed)
    except discord.NotFound:
        logger.warning("Interaction expired.")
    except Exception:
        logger.exception("Error during aggregation.")
    finally:
        bot.scan_lock.release()


@bot.tree.command(name="top10", description="サーバー全体の累計発言数TOP10を集計します")
@app_commands.guild_only()
async def top10(interaction: discord.Interaction) -> None:
    await run_top10(interaction, by_characters=False)


@bot.tree.command(name="top10chars", description="サーバー全体の累計文字数TOP10を集計します")
@app_commands.guild_only()
async def top10chars(interaction: discord.Interaction) -> None:
    await run_top10(interaction, by_characters=True)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN が設定されていません。")
    bot.run(token)


if __name__ == "__main__":
    main()