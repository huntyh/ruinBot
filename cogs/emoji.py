import asyncio
import logging
import re

import aiohttp
import discord
from discord.ext import commands


logger = logging.getLogger(__name__)
EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,20}>")
NAME_PATTERN = re.compile(r"[A-Za-z0-9_]{2,32}")


def permission_error(guild, user):
    if guild is None:
        return "Ezt csak szerveren tudod használni."
    for member, who in ((user, "Neked"), (guild.me, "Nekem")):
        if member is None:
            return "Nem találom a botot a szerveren, próbáld újra."
        permissions = member.guild_permissions
        if not (permissions.create_expressions or permissions.manage_expressions):
            return f"{who} Create Expressions vagy Manage Expressions jog kell ehhez."
    return None


class EmojiNameModal(discord.ui.Modal, title="Másik név az emojinak"):
    name = discord.ui.TextInput(
        label="Új név", placeholder="2–32 betű (ékezet nélkül), szám vagy _",
        min_length=2, max_length=32,
    )

    def __init__(self, view):
        super().__init__(timeout=180)
        self.import_view = view

    async def on_submit(self, interaction: discord.Interaction):
        view = self.import_view
        if not await view.interaction_check(interaction):
            return
        name = self.name.value.strip()
        if not NAME_PATTERN.fullmatch(name):
            await interaction.response.send_message(
                "A név 2–32 betű (ékezet nélkül), szám vagy _ lehet. Próbáld újra a gombbal.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with view.submit_lock:
            if view.is_finished():
                await interaction.followup.send("Ez a kérés már lejárt vagy lezárult.", ephemeral=True)
                return
            result, success = await view.cog.import_emoji(
                interaction.guild, interaction.user, view.emoji, name,
            )
            if success:
                view.stop()
                await view.close()
            await interaction.followup.send(result, ephemeral=True)


class EmojiImportView(discord.ui.View):
    def __init__(self, cog, user_id, emoji):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.emoji = emoji
        self.message = None
        self.submit_lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ezt az döntheti el, aki kérte.", ephemeral=True)
            return False
        if self.is_finished():
            await interaction.response.send_message("Ez a kérés már lejárt vagy lezárult.", ephemeral=True)
            return False
        error = permission_error(interaction.guild, interaction.user)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return False
        return True

    async def close(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                logger.warning("Nem sikerült lezárni az emoji kérdését", exc_info=True)

    async def on_timeout(self):
        await self.close()

    @discord.ui.button(label="Igen, másik névvel", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmojiNameModal(self))

    @discord.ui.button(label="Mégse", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.submit_lock:
            if self.is_finished():
                await interaction.response.send_message("Ez a kérés már lezárult.", ephemeral=True)
                return
            await interaction.response.edit_message(content="Oké, nem adom hozzá.", embeds=[], view=None)
            self.stop()


class EmojiImporter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.guild_locks = {}

    async def download_emoji(self, emoji):
        # A GIF-et külön kérjük, hogy az animáció biztosan megmaradjon.
        extension = "gif" if emoji.animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{emoji.id}.{extension}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                image = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    image.extend(chunk)
                    if len(image) > 256 * 1024:
                        raise ValueError("Ez az emoji túl nagy (maximum 256 KiB).")
                return bytes(image)

    async def import_emoji(self, guild, user, emoji, name):
        error = permission_error(guild, user)
        if error:
            return error, False
        if not NAME_PATTERN.fullmatch(name):
            return "A név 2–32 betű (ékezet nélkül), szám vagy _ lehet.", False
        lock = self.guild_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            try:
                # Friss lista kell: a kérdés óta más is hozzáadhatott emojit.
                existing = await guild.fetch_emojis()
                if any(item.id == emoji.id for item in existing):
                    return "Ez az emoji már itt van a szerveren.", False
                if any(item.name.casefold() == name.casefold() for item in existing):
                    return "Ez a név már foglalt, válassz másikat a gombbal.", False
                if sum(item.animated == emoji.animated for item in existing) >= guild.emoji_limit:
                    return "Nincs több hely ilyen típusú emojinak a szerveren.", False
                image = await self.download_emoji(emoji)
                created = await guild.create_custom_emoji(
                    name=name, image=image, reason=f"Emoji import: {user} ({user.id})",
                )
            except discord.Forbidden:
                return "Nincs jogom hozzáadni az emojit.", False
            except discord.HTTPException:
                logger.warning("Discord hiba emoji import közben", exc_info=True)
                return "Nem sikerült hozzáadni az emojit. Ellenőrizd a jogokat és a szabad helyet, aztán próbáld újra.", False
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return "Nem sikerült letölteni az emojit. Lehet, hogy már törölték; próbáld újra.", False
            except ValueError as error:
                return str(error), False
        return f"Hozzáadtam: {created} (`:{created.name}:`)", True

    @commands.hybrid_command(name="emoji", description="Másik szerver emojijának hozzáadása")
    async def emoji(self, ctx: commands.Context, *, emoji: str):
        error = permission_error(ctx.guild, ctx.author)
        if error:
            await ctx.reply(error, ephemeral=True)
            return
        if not EMOJI_PATTERN.fullmatch(emoji.strip()):
            await ctx.reply("Egy másik szerver egyéni emojiját küldd, pl. `r!emoji <:nev:123456789012345678>`.", ephemeral=True)
            return
        source = discord.PartialEmoji.from_str(emoji.strip())
        await ctx.defer()
        try:
            existing = await ctx.guild.fetch_emojis()
        except discord.HTTPException:
            await ctx.reply("Nem sikerült lekérni a szerver emojijait, próbáld újra.")
            return
        if any(item.id == source.id for item in existing):
            await ctx.reply("Ez az emoji már itt van a szerveren.")
            return
        conflicts = [item for item in existing if item.name.casefold() == source.name.casefold()]
        if conflicts:
            incoming = discord.Embed(title=f"Új emoji: :{source.name}:")
            incoming.set_image(url=source.url)
            current = discord.Embed(title=f"Már itt van: :{conflicts[0].name}:")
            current.set_image(url=conflicts[0].url)
            view = EmojiImportView(self, ctx.author.id, source)
            view.message = await ctx.reply(
                "Ilyen nevű emoji már van, lehet, hogy ugyanaz. Biztos hozzáadod? Ha igen, adj neki másik nevet.",
                embeds=[incoming, current], view=view,
            )
            return
        result, _ = await self.import_emoji(ctx.guild, ctx.author, source, source.name)
        await ctx.reply(result)

    @emoji.error
    async def emoji_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("Küldj egy emojit is: `r!emoji <emoji>`.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(EmojiImporter(bot))
