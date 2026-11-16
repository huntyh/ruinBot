"""Ruin Ügyfélszolgálat: ügyintézés, amire senki nem kért engedélyt."""

import asyncio
import logging
import re
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from utils.hivatal import Case, CaseBook, DECISIONS, MENTION, OfficeError


log = logging.getLogger(__name__)


def safe_text(text):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def subject_text(case: Case):
    # Csak az eltárolt felhasználói tageket hagyjuk meg; pinget a küldés tilt.
    parts = []
    start = 0
    for match in MENTION.finditer(case.subject):
        parts.append(safe_text(case.subject[start:match.start()]))
        user_id = int(match[1])
        parts.append(f"<@{user_id}>" if user_id in case.target_ids else safe_text(match[0]))
        start = match.end()
    parts.append(safe_text(case.subject[start:]))
    return "".join(parts)


def case_embed(case: Case):
    ruling = case.current
    colours = {"approved": 0x57A65A, "denied": 0xCD504D, "incomplete": 0xD4A23A}
    embed = discord.Embed(
        title=f"🏛️ RUIN ÜGYFÉLSZOLGÁLAT • #{case.id:06d}",
        description=f"**Kérelmező:** <@{case.user_id}>\n**Tárgy:** {subject_text(case)}",
        colour=colours[ruling["decision"]],
    )
    if case.target_ids:
        embed.description += "\n**Érintettek:** " + ", ".join(f"<@{user}>" for user in case.target_ids)
    if case.appeal:
        embed.add_field(name="Elsőfokú határozat", value=DECISIONS[case.ruling["decision"]][0])
        if case.appeal.get("appealed_by"):
            embed.description += f"\n**Fellebbező:** <@{case.appeal['appealed_by']}>"
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
               "A kérelmező vagy bármelyik megjelölt érintett egyszer, közösen fellebbezhet. "
               "A másodfok még rosszabb is lehet. "
               f"Gomb lent, vagy `/hivatal fellebbezes iktatoszam:{case.id}`."),
        inline=False,
    )
    embed.set_footer(text="A pecsét helye: 🥔")
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
        await cog.handle_appeal(interaction, self.case_id)


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
    @app_commands.describe(targy="Pl.: @Cserna-nak egy ban (3–300 karakter, a valódi tagek fellebbezhetnek)",
                           erintett="Opcionális érintett: a tárgyban megjelölteken felül ő is fellebbezhet")
    async def kerveny(self, interaction: discord.Interaction, targy: app_commands.Range[str, 3, 300],
                     erintett: discord.Member | None = None):
        await interaction.response.defer(thinking=True)
        case = await self.run_office(interaction, self.book.create,
                                     interaction.guild_id, interaction.user.id, targy,
                                     erintett.id if erintett is not None else None)
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

    @app_commands.command(name="sajat", description="Az öt legutóbbi ügy, amit beadtál, vagy amiben érintett vagy. Csak te látod.")
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
                            value=subject_text(case), inline=False)
        embed.set_footer(text="Másolat: /hivatal ugy iktatoszam:<szám>")
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="fellebbezes", description="Kérelmezőként vagy megjelölt érintettként egyszer fellebbezhetsz. Lehet, hogy megbánod.")
    @app_commands.describe(iktatoszam="Az ügy iktatószáma, amelyet beadtál vagy amelyben megjelöltek")
    async def fellebbezes(self, interaction: discord.Interaction, iktatoszam: app_commands.Range[int, 1, 9_007_199_254_740_991]):
        await self.handle_appeal(interaction, iktatoszam)

    async def handle_appeal(self, interaction, case_id):
        # A hibákat csak az ügyfél látja, a jogerős határozat külön közhírré tétetik.
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.guild_id is None:
            await interaction.followup.send("A hivatal csak szerveren tart ügyfélfogadást.", ephemeral=True)
            return
        case = await self.run_office(interaction, self.book.appeal,
                                     interaction.guild_id, interaction.user.id, case_id)
        if case is None:
            return
        # A privát defer első followupja örökli a privát láthatóságot.
        # Előbb lezárjuk azt, hogy a következő üzenet biztosan nyilvános legyen.
        await interaction.followup.send("A másodfok határozott. A döntést külön üzenetben hirdetjük ki.",
                                        ephemeral=True)
        try:
            await interaction.followup.send(embed=case_embed(case), ephemeral=False,
                                            allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Nem hirdethető ki a hivatal #%s határozata", case.id, exc_info=True)
            await interaction.followup.send(
                f"A kihirdetés elakadt, de a döntés megmaradt. Másolat: `/hivatal ugy iktatoszam:{case.id}`.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Hivatal(bot))
