import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from cogs.emoji import EmojiImporter, EmojiImportView, EmojiNameModal


class EmojiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = EmojiImporter(None)
        self.cog.download_emoji = AsyncMock(return_value=b"image")
        self.user = SimpleNamespace(
            id=42, guild_permissions=discord.Permissions(create_expressions=True),
        )
        self.guild = SimpleNamespace(
            id=1, me=self.user, emoji_limit=50, fetch_emojis=AsyncMock(return_value=[]),
            create_custom_emoji=AsyncMock(return_value=discord.PartialEmoji(name="party", id=222222222222222222)),
        )
        self.source = discord.PartialEmoji(name="party", id=111111111111111111, animated=True)
        self.ctx = SimpleNamespace(
            guild=self.guild, author=self.user, defer=AsyncMock(), reply=AsyncMock(),
        )

    def interaction(self, user=None):
        return SimpleNamespace(
            guild=self.guild, user=user or self.user,
            response=SimpleNamespace(
                send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(),
            ), followup=SimpleNamespace(send=AsyncMock()),
        )

    async def command(self, value=None):
        await EmojiImporter.emoji.callback(self.cog, self.ctx, emoji=value or str(self.source))

    async def test_registers_prefix_and_slash_commands(self):
        async with commands.Bot(command_prefix="r!", intents=discord.Intents.none()) as bot:
            await bot.add_cog(EmojiImporter(bot))
            self.assertIsNotNone(bot.get_command("emoji"))
            self.assertIsNotNone(bot.tree.get_command("emoji"))

    async def test_adds_original_name_and_preserves_animated_source(self):
        await self.command()
        self.cog.download_emoji.assert_awaited_once_with(self.source)
        self.assertEqual(self.guild.create_custom_emoji.await_args.kwargs["name"], "party")
        self.assertIn("Hozzáadtam", self.ctx.reply.call_args.args[0])

    async def test_local_emoji_is_not_imported(self):
        self.guild.fetch_emojis.return_value = [self.source]
        await self.command()
        self.guild.create_custom_emoji.assert_not_awaited()
        self.assertIn("már itt van", self.ctx.reply.call_args.args[0])

    async def test_collision_shows_both_previews_without_creating(self):
        local = discord.PartialEmoji(name="PARTY", id=333333333333333333)
        self.guild.fetch_emojis.return_value = [local]
        await self.command()
        self.guild.create_custom_emoji.assert_not_awaited()
        kwargs = self.ctx.reply.call_args.kwargs
        self.assertEqual([embed.image.url for embed in kwargs["embeds"]], [self.source.url, local.url])
        view = kwargs["view"]
        self.addCleanup(view.stop)
        interaction = self.interaction()
        await view.confirm.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        self.addCleanup(modal.stop)
        self.assertIsInstance(modal, EmojiNameModal)
        modal.name._value = "party_new"
        await modal.on_submit(interaction)
        self.assertEqual(self.guild.create_custom_emoji.await_args.kwargs["name"], "party_new")
        self.assertTrue(view.is_finished())

    async def test_only_requester_can_confirm(self):
        view = EmojiImportView(self.cog, self.user.id, self.source)
        self.addCleanup(view.stop)
        stranger = SimpleNamespace(id=999)
        interaction = self.interaction(stranger)
        self.assertFalse(await view.interaction_check(interaction))
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_modal_rechecks_permissions_and_name_availability(self):
        view = EmojiImportView(self.cog, self.user.id, self.source)
        self.addCleanup(view.stop)
        modal = EmojiNameModal(view)
        self.addCleanup(modal.stop)
        modal.name._value = "party_new"
        self.guild.fetch_emojis.return_value = [discord.PartialEmoji(name="party_new", id=333333333333333333)]
        interaction = self.interaction()
        await modal.on_submit(interaction)
        self.assertIn("foglalt", interaction.followup.send.call_args.args[0])
        self.assertFalse(view.is_finished())
        self.user.guild_permissions = discord.Permissions.none()
        await modal.on_submit(interaction)
        self.assertIn("jog kell", interaction.response.send_message.call_args.args[0])
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_cancel_and_timeout_never_add_emoji(self):
        view = EmojiImportView(self.cog, self.user.id, self.source)
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.cancel.callback(self.interaction())
        self.assertTrue(view.is_finished())
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(view=None)
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_rejects_unicode_invalid_input_and_dm(self):
        for value in ("😀", "party", "https://example.com/emoji.png", str(self.source) + " extra"):
            await self.command(value)
        self.ctx.guild = None
        await self.command()
        self.guild.fetch_emojis.assert_not_awaited()
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_permission_and_capacity_failures(self):
        self.guild.me = SimpleNamespace(guild_permissions=discord.Permissions.none())
        result, ok = await self.cog.import_emoji(self.guild, self.user, self.source, "party")
        self.assertFalse(ok)
        self.assertIn("Nekem", result)
        self.guild.me = self.user
        self.guild.emoji_limit = 0
        result, ok = await self.cog.import_emoji(self.guild, self.user, self.source, "party")
        self.assertFalse(ok)
        self.assertIn("hely", result)
        self.guild.create_custom_emoji.assert_not_awaited()

    async def test_concurrent_imports_cannot_create_same_name_twice(self):
        async def create(**kwargs):
            created = discord.PartialEmoji(name=kwargs["name"], id=222222222222222222)
            self.guild.fetch_emojis.return_value = [created]
            return created

        self.guild.create_custom_emoji.side_effect = create
        results = await asyncio.gather(*(
            self.cog.import_emoji(self.guild, self.user, self.source, "party") for _ in range(2)
        ))
        self.assertEqual(sum(ok for _, ok in results), 1)
        self.guild.create_custom_emoji.assert_awaited_once()

    async def test_download_and_discord_errors_are_reported(self):
        self.cog.download_emoji.side_effect = asyncio.TimeoutError()
        result, ok = await self.cog.import_emoji(self.guild, self.user, self.source, "party")
        self.assertFalse(ok)
        self.assertIn("letölteni", result)
        self.cog.download_emoji.side_effect = None
        self.guild.create_custom_emoji.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "missing permissions",
        )
        result, ok = await self.cog.import_emoji(self.guild, self.user, self.source, "party")
        self.assertFalse(ok)
        self.assertIn("jogom", result)


if __name__ == "__main__":
    unittest.main()
