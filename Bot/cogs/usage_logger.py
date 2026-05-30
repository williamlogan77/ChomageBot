"""Audit-log slash commands, button clicks, and select-menu picks.

A single cog listens to ``on_interaction`` and writes one row per
non-autocomplete interaction to the ``command_usage`` table. The
``/usage_stats`` admin slash command surfaces top commands by window
so we can later prune the unused ones.

Schema lives in ``Bot/db/setup.sql``; existing DBs migrate via
``scripts/migrate_add_command_usage.py``.
"""

from __future__ import annotations

import datetime as dt
import logging

import aiosqlite as sqa
import discord
from discord import app_commands
from discord.ext import commands

# Match the role-name gate used in cogs.match_analysis for admin commands.
# Duplicated here (rather than imported) so this cog has no cross-cog
# import dependencies — keeps load order resilient.
CHOMAGE_KEEPER_ROLE = "Keeper of Chomage"

log = logging.getLogger(__name__)


def _is_chomage_keeper(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    return any(role.name == CHOMAGE_KEEPER_ROLE for role in user.roles)


def _name_from_interaction(interaction: discord.Interaction) -> str | None:
    """Derive a stable identifier from any interaction.

    Returns:
      - For application commands: the full command path, e.g.
        ``/me``, ``/refresh sync``, ``/champ``.
      - For button clicks: the ``custom_id``, e.g. ``ms:panel:c0``.
        Decoded to a human chart name at query time in ``/usage_stats``.
      - For string-select picks: ``<custom_id>=<selected_value>``. The
        suffix is critical for the More-menu select, where one custom_id
        would otherwise collapse all chart picks into a single row.
      - None for autocomplete / modal-submit / other low-signal types.
    """
    itype = interaction.type
    data = interaction.data or {}

    if itype is discord.InteractionType.application_command:
        # Build the full command name including subcommand groups.
        name = data.get("name", "")
        # Walk into options for subcommand groups / subcommands.
        opts = data.get("options", []) or []
        while opts:
            first = opts[0]
            opt_type = first.get("type")
            # 1 = SUB_COMMAND, 2 = SUB_COMMAND_GROUP
            if opt_type in (1, 2):
                name = f"{name} {first.get('name', '')}"
                opts = first.get("options", []) or []
            else:
                break
        return f"/{name.strip()}" if name else None

    if itype is discord.InteractionType.component:
        custom_id = data.get("custom_id", "")
        # component_type 2 = button, 3 = string select, 5-8 = other selects.
        # For selects, suffix the picked value so each pick is its own row
        # in /usage_stats (without this, the More-menu dropdown's single
        # custom_id swallows every chart pick into one undifferentiated row).
        component_type = data.get("component_type")
        values = data.get("values") or []
        if component_type in (3, 5, 6, 7, 8) and values:
            return f"{custom_id}={values[0]}"
        return custom_id or None

    # Skip autocomplete + modal_submit + ping — low value to log.
    return None


class UsageLogger(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.logging.info(f"{__name__} loaded")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        name = _name_from_interaction(interaction)
        if name is None:
            return
        itype = (
            interaction.type.name if hasattr(interaction.type, "name") else str(interaction.type)
        )
        try:
            async with sqa.connect(self.bot.db_path) as db:
                await db.execute(
                    "INSERT INTO command_usage "
                    "(command_name, user_id, guild_id, interaction_type) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        name,
                        str(interaction.user.id),
                        str(interaction.guild_id) if interaction.guild_id else None,
                        itype,
                    ),
                )
                await db.commit()
        except Exception as exc:
            # Never let logging failures break user interactions. The DB
            # might be locked or the table might not exist yet (pre-
            # migration). Log + carry on.
            log.debug(f"usage_logger insert failed for {name!r}: {exc!r}")

    @app_commands.command(
        name="usage_stats",
        description="Show top commands / buttons over a window (admin)",
    )
    @app_commands.guild_only()
    @app_commands.check(_is_chomage_keeper)
    @app_commands.describe(
        days="Window in days (default 7, max 365)",
        top="How many entries to show (default 25, max 50)",
    )
    async def usage_stats(
        self,
        interaction: discord.Interaction,
        days: int = 7,
        top: int = 25,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        days = max(1, min(days, 365))
        top = max(1, min(top, 50))

        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            async with sqa.connect(self.bot.db_path) as db:
                rows = await db.execute_fetchall(
                    "SELECT command_name, COUNT(*) AS n, "
                    "COUNT(DISTINCT user_id) AS uniq_users "
                    "FROM command_usage WHERE timestamp >= ? "
                    "GROUP BY command_name ORDER BY n DESC LIMIT ?",
                    (cutoff, top),
                )
                total_row = await db.execute_fetchall(
                    "SELECT COUNT(*) FROM command_usage WHERE timestamp >= ?",
                    (cutoff,),
                )
                total = total_row[0][0] if total_row else 0
        except Exception as exc:
            await interaction.followup.send(f"Failed to read usage stats: {exc!r}", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send(
                f"No usage data in the last {days} day(s). "
                "If the migration just ran, give it time to accumulate.",
                ephemeral=True,
            )
            return

        # Translate opaque custom_ids into human chart names. Lookups are
        # done lazily at query time so this cog has no import-time
        # dependency on cogs.match_analysis (which keeps load order safe).
        decoder = self._build_decoder()
        lines = [
            f"`{i + 1:>2}. {decoder(name):<32} {n:>4}  ({uniq} unique)`"
            for i, (name, n, uniq) in enumerate(rows)
        ]
        embed = discord.Embed(
            title=f"📊 Usage — last {days}d (top {len(rows)})",
            description="\n".join(lines),
        )
        embed.set_footer(text=f"{total:,} total events · cutoff {cutoff}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    def _build_decoder(self):
        """Map opaque interaction identifiers to human chart names.

        Looks up the match-analysis cog at query time (not import time)
        so we don't create a load-order coupling. If the cog isn't
        loaded, falls back to raw identifiers.
        """
        panel_labels: dict[str, str] = {}
        select_labels: dict[str, str] = {}
        try:
            ma_cog = self.bot.get_cog("MatchAnalysis")
            if ma_cog is not None:
                from cogs import match_analysis as ma

                # CHART_DEFS is (label, emoji, fn, title) — indexed by cN.
                # Both the panel (ms:panel:cN) and the in-explorer chart
                # buttons (ms:expl:cN) key by the same canonical index.
                for idx, entry in enumerate(ma.CHART_DEFS):
                    label = entry[0]
                    panel_labels[f"ms:panel:c{idx}"] = f"button: {label}"
                    panel_labels[f"ms:expl:c{idx}"] = f"explorer: {label}"
                panel_labels[ma.PANEL_SELECT_ID] = "panel person select"

                # MORE_CHART_DEFS is (stem, label, emoji, description)
                for entry in ma.MORE_CHART_DEFS:
                    stem, label = entry[0], entry[1]
                    select_labels[stem] = f"more: {label}"
                # The board's More-analytics dropdown sentinel that opens the
                # full paginated menu (logged as ms:panel:more=__see_all__).
                select_labels[ma.MORE_SEE_ALL_VALUE] = "more: open full analytics list"
        except Exception:
            pass

        def decode(name: str) -> str:
            if name in panel_labels:
                return panel_labels[name]
            # Select-pick rows look like "<custom_id>=<value>"
            if "=" in name:
                _, value = name.split("=", 1)
                if value in select_labels:
                    return select_labels[value]
            return name

        return decode


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UsageLogger(bot))
