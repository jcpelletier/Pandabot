import discord
from discord import app_commands
from discord.ext import commands
import logging
from pandabot.family import family_cache

logger = logging.getLogger("panda-bot")

class FamilyCommands(commands.GroupCog, name="family"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def _check_cache(self, interaction: discord.Interaction) -> bool:
        if family_cache is None:
            await interaction.followup.send(
                "Sorry, the family information tool is not configured. "
                "The `FAMILY_SHEET_ID` environment variable might be missing."
            )
            return False
        return True

    @app_commands.command(name="query", description="Search for family members matching the query text")
    @app_commands.describe(text="Search text")
    async def query(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer()
        if not await self._check_cache(interaction):
            return

        try:
            import asyncio
            results = await asyncio.to_thread(family_cache.search, text)

            if not results:
                await interaction.followup.send(f"No family members found matching '{text}'.")
                return

            if len(results) == 1:
                member = results[0]
                embed = self._create_member_embed(member)
                await interaction.followup.send(embed=embed)
            else:
                description = ""
                for m in results[:10]:
                    name = f"{m['first_name']} {m['last_name']}".strip() or "Unknown"
                    discord_name = m['discord_name'] or "N/A"
                    rel = m['relationship'] or "N/A"
                    description += f"• **{name}** ({discord_name}) - {rel}\n"

                if len(results) > 10:
                    description += f"\n*...and {len(results) - 10} more*"

                embed = discord.Embed(
                    title=f"Search results for '{text}'",
                    description=description,
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in /family query: {e}")
            await interaction.followup.send("Sorry, I couldn't query the family sheet right now. The sheet might be unavailable.")

    @app_commands.command(name="member", description="Look up a specific family member by name")
    @app_commands.describe(name="Name or Discord name")
    async def member(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        if not await self._check_cache(interaction):
            return

        try:
            import asyncio
            member = await asyncio.to_thread(family_cache.find_member, name)

            if not member:
                await interaction.followup.send(f"No family members found matching '{name}'.")
                return

            embed = self._create_member_embed(member)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in /family member: {e}")
            await interaction.followup.send("Sorry, I couldn't query the family sheet right now. The sheet might be unavailable.")

    @app_commands.command(name="refresh", description="Manually refresh the family information cache")
    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await self._check_cache(interaction):
            return

        try:
            import asyncio
            await asyncio.to_thread(family_cache.refresh)
            await interaction.followup.send("Family information cache refreshed.")
        except Exception as e:
            logger.error(f"Error in /family refresh: {e}")
            await interaction.followup.send("Failed to refresh the family sheet. Check logs.")

    def _create_member_embed(self, member):
        name = f"{member['first_name']} {member['last_name']}".strip() or "Unknown"
        embed = discord.Embed(
            title=name,
            color=discord.Color.green()
        )

        fields = [
            ("Discord Name", member["discord_name"]),
            ("DOB", member["dob"]),
            ("Relationship", member["relationship"]),
            ("Location", member["location"]),
            ("Phone", member["phone"]),
            ("Email", member["email"]),
            ("Address", member["address"]),
            ("Notes", member["notes"]),
        ]

        for label, value in fields:
            if value:
                embed.add_field(name=label, value=value, inline=True)

        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(FamilyCommands(bot))
