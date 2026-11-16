"""Ruin Ügyfélszolgálat: ügyintézés, amire senki nem kért engedélyt."""

import asyncio
import logging
import re
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from utils.hivatal import Case, CaseBook, DECISIONS, OfficeError


log = logging.getLogger(__name__)


def safe_text(text):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def case_embed(case: Case):
    ruling = case.current
    colours = {"approved": 0x57A65A, "denied": 0xCD504D, "incomplete": 0xD4A23A}
    embed = discord.Embed(
        title=f"🏛️ RUIN ÜGYFÉLSZOLGÁLAT • #{case.id:06d}",
        description=f"**Kérelmező:** <@{case.user_id}>\n**Tárgy:** {safe_text(case.subject)}",
        colour=colours[ruling["decision"]],
    )
    if case.appeal:
        embed.add_field(name="Elsőfokú határozat", value=DECISIONS[case.ruling["decision"]][0])
    embed.add_field(
        name="⚖️ Jogerős határozat" if case.appeal else "⚖️ Határozat",
        value=f"**{DECISIONS[ruling['decision']][0]}**\n{ruling['instruction']}",
        inline=False,
    )
    embed.add_field(name="Indoklás", value=ruling["reason"], inline=False)
    embed.add_field(name="Bizonyítékok", value="\n".join(f"• {item}" for item in ruling["evidence"]), inline=False)
    embed.add_field(name="Eljáró szerv", value=f"{ruling['office']}\nÜgyintéző: {ruling['clerk']}", inline=False)
    embed.add_field(
        name="Jogorvoslat",
        value=("Az ítélet jogerős. Az ügyiratot egy képzeletbeli galamb őrzi."
               if case.appeal else
               "Nem tetszik? Egy fellebbezés jár, de a másodfok még rosszabb is lehet. "
               f"Gomb lent, vagy `/hivatal fellebbezes iktatoszam:{case.id}`."),
        inline=False,
    )
    embed.set_footer(text="PARÓDIA • Nincs valódi joghatása vagy díja • A pecsét helye: 🥔")
    return embed


class AppealButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"ruinbot:hivatal:appeal:(?P<case_id>[1-9][0-9]{0,18})"):
    def __init__(self, case_id: int):
        self.case_id = case_id
        super().__init__(discord.ui.Button(
            label="Ezt megfellebbezem", emoji="📎", style=discord.ButtonStyle.secondary,
            custom_id=f"ruinbot:hivatal:appeal:{case_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["case_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Hivatal")
        if cog is None:
            await interaction.response.send_message("A hivatal épp költözik. Próbáld újra később.", ephemeral=True)
            return
        await cog.handle_appeal(interaction, self.case_id, from_button=True)


def case_view(case: Case):
    view = discord.ui.View(timeout=None)
    if not case.appeal:
        view.add_item(AppealButton(case.id))
    return view


@app_commands.guild_only()
class Hivatal(commands.GroupCog, group_name="hivatal", group_description="Ruin Ügyfélszolgálat – a papír mindent elbír."):
    def __init__(self, bot: commands.Bot, book=None):
        self.bot = bot
        self.book = book if book is not None else CaseBook()

    async def cog_load(self):
        self.bot.add_dynamic_items(AppealButton)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(AppealButton)

    async def run_office(self, interaction, operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except OfficeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except (sqlite3.Error, OSError):
            log.exception("A hivatal iktatókönyve nem elérhető")
            await interaction.followup.send(
                "Elakadt az iktatógép. Próbáld újra később; az ügyeidet nem nullázzuk le.",
                ephemeral=True,
            )
        return None

    @app_commands.command(name="kerveny", description="Adj be bármit, a hivatal majd határoz róla. Napi 5 sorszám.")
    @app_commands.describe(targy="Pl.: engedélyt kérek még egy sörre (3–300 karakter)")
    async def kerveny(self, interaction: discord.Interaction, targy: app_commands.Range[str, 3, 300]):
        await interaction.response.defer(thinking=True)
        case = await self.run_office(interaction, self.book.create,
                                     interaction.guild_id, interaction.user.id, targy)
        if case is not None:
            await interaction.followup.send(embed=case_embed(case), view=case_view(case),
                                            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="ugy", description="Kérj másolatot egy ezen a szerveren iktatott ügy határozatáról.")
    @app_commands.describe(iktatoszam="A határozat tetején lévő szám, pl. 42")
    async def ugy(self, interaction: discord.Interaction, iktatoszam: app_commands.Range[int, 1, 9_007_199_254_740_991]):
        await interaction.response.defer(thinking=True, ephemeral=True)
        case = await self.run_office(interaction, self.book.get, interaction.guild_id, iktatoszam)
        if case is not None:
            await interaction.followup.send(embed=case_embed(case), view=case_view(case),
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="sajat", description="Az öt legutóbbi saját ügyed ezen a szerveren. Csak te látod.")
    async def sajat(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        cases = await self.run_office(interaction, self.book.recent, interaction.guild_id, interaction.user.id)
        if cases is None:
            return
        if not cases:
            await interaction.followup.send("Még tiszta az aktád. Gyanús. Kezdd az `/hivatal kerveny` paranccsal.", ephemeral=True)
            return
        embed = discord.Embed(title="🗃️ Saját aktáid", colour=0xD4A23A)
        for case in cases:
            status = DECISIONS[case.current["decision"]][0]
            stage = "jogerős" if case.appeal else "fellebbezhető"
            embed.add_field(name=f"#{case.id:06d} • {status} • {stage}",
                            value=safe_text(case.subject), inline=False)
        embed.set_footer(text="Másolat: /hivatal ugy iktatoszam:<szám>")
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="fellebbezes", description="Egyszer megfellebbezheted a saját ügyedet. Lehet, hogy megbánod.")
    @app_commands.describe(iktatoszam="A saját ügyed iktatószáma")
    async def fellebbezes(self, interaction: discord.Interaction, iktatoszam: app_commands.Range[int, 1, 9_007_199_254_740_991]):
        await self.handle_appeal(interaction, iktatoszam)

    async def handle_appeal(self, interaction, case_id, *, from_button=False):
        # A felülvizsgálat privát. Gombnál az eredeti nyilvános iratot is frissítjük.
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.guild_id is None:
            await interaction.followup.send("A hivatal csak szerveren tart ügyfélfogadást.", ephemeral=True)
            return
        case = await self.run_office(interaction, self.book.appeal,
                                     interaction.guild_id, interaction.user.id, case_id)
        if case is None:
            return
        embed = case_embed(case)
        if from_button and interaction.message is not None:
            try:
                await interaction.message.edit(embed=embed, view=None,
                                               allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                # A jogerős irat akkor is megmarad, ha az eredeti üzenetet törölték.
                log.warning("Nem frissíthető a hivatal #%s üzenete", case.id, exc_info=True)
        await interaction.followup.send(embed=embed, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Hivatal(bot))
