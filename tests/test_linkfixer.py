import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from cogs.linkfixer import LinkFixer, LinkFixerView


class SettingsFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_file = Path(temporary.name) / "linkfixer.json"
        patcher = patch("cogs.linkfixer.DATA_FILE", self.data_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cog = LinkFixer(None)


class SettingsTest(SettingsFixture, unittest.TestCase):
    def test_missing_file_uses_enabled_defaults(self):
        self.assertEqual(self.cog.disabled, {"guilds": [], "channels": []})

    def test_settings_survive_reload(self):
        settings = {"guilds": [10], "channels": [20, 21]}
        self.cog.save_disabled(settings)
        self.assertEqual(LinkFixer(None).disabled, settings)
        self.assertEqual(list(self.data_file.parent.iterdir()), [self.data_file])

    def test_malformed_settings_do_not_prevent_loading(self):
        for content in (
            b"", b"{", b"\xff", b"null", b"[]",
            b'{"guilds": null}', b'{"channels": "20"}',
            b'{"guilds": [true]}', b'{"guilds": ["10"]}',
            b'{"channels": [-1]}',
        ):
            with self.subTest(content=content):
                self.data_file.write_bytes(content)
                with self.assertLogs("cogs.linkfixer", level="ERROR"):
                    cog = LinkFixer(None)
                self.assertEqual(cog.disabled, {"guilds": [], "channels": []})
                self.assertEqual(self.data_file.read_bytes(), content)

    def test_partial_settings_keep_defaults_for_missing_scope(self):
        self.data_file.write_text('{"guilds": [10]}', encoding="utf-8")
        self.assertEqual(LinkFixer(None).disabled, {"guilds": [10], "channels": []})

    def test_interrupted_write_preserves_previous_file(self):
        previous = {"guilds": [10], "channels": []}
        self.cog.save_disabled(previous)

        def interrupted_dump(data, file, **kwargs):
            file.write("{")
            raise OSError("write interrupted")

        with patch("cogs.linkfixer.json.dump", side_effect=interrupted_dump):
            with self.assertRaises(OSError):
                self.cog.save_disabled({"guilds": [], "channels": [20]})
        self.assertEqual(LinkFixer(None).disabled, previous)
        self.assertEqual(list(self.data_file.parent.iterdir()), [self.data_file])

    def test_failed_replace_preserves_previous_file(self):
        previous = {"guilds": [10], "channels": []}
        self.cog.save_disabled(previous)
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.cog.save_disabled({"guilds": [], "channels": [20]})
        self.assertEqual(LinkFixer(None).disabled, previous)
        self.assertEqual(list(self.data_file.parent.iterdir()), [self.data_file])


class CommandTest(SettingsFixture, unittest.IsolatedAsyncioTestCase):
    def context(self, *, thread=False, manage_guild=True, manage_channels=True):
        perms = discord.Permissions(manage_guild=manage_guild, manage_channels=manage_channels)
        channel = SimpleNamespace(id=21 if thread else 20, permissions_for=lambda _: perms)
        if thread:
            channel.parent_id = 20
        return SimpleNamespace(
            guild=SimpleNamespace(id=10), channel=channel,
            author=SimpleNamespace(), reply=AsyncMock(),
        )

    async def command(self, ctx, state="allapot", scope="szerver"):
        await LinkFixer.linkfixer.callback(self.cog, ctx, state, scope)
        return ctx.reply.call_args.args[0]

    async def test_hybrid_command_registers(self):
        async with commands.Bot(command_prefix="r!", intents=discord.Intents.none()) as bot:
            await bot.add_cog(LinkFixer(bot))
            self.assertIsNotNone(bot.get_command("linkfixer"))
            self.assertIsNotNone(bot.tree.get_command("linkfixer"))

    async def test_channel_status_reports_server_disable(self):
        ctx = self.context()
        await self.command(ctx, "ki")
        reply = await self.command(ctx, scope="csatorna")
        self.assertIn("kikapcsolva", reply)
        self.assertIn("a szerveren", reply)
        self.assertTrue(self.cog.is_disabled(ctx))

    async def test_thread_enable_reports_parent_disable(self):
        await self.command(self.context(), "ki", "csatorna")
        ctx = self.context(thread=True)
        reply = await self.command(ctx, "be", "csatorna")
        self.assertIn("kikapcsolva", reply)
        self.assertIn("szülőcsatornán", reply)
        self.assertTrue(self.cog.is_disabled(ctx))
        self.assertEqual(self.cog.disabled["channels"], [20])

    async def test_channel_enable_does_not_override_server_disable(self):
        ctx = self.context()
        await self.command(ctx, "ki", "csatorna")
        await self.command(ctx, "ki")
        reply = await self.command(ctx, "be", "csatorna")
        self.assertIn("kikapcsolva", reply)
        self.assertIn("a szerveren", reply)
        self.assertEqual(self.cog.disabled, {"guilds": [10], "channels": []})

    async def test_enabled_status_after_removing_inherited_disable(self):
        await self.command(self.context(), "ki", "csatorna")
        await self.command(self.context(), "be", "csatorna")
        reply = await self.command(self.context(thread=True), scope="csatorna")
        self.assertIn("bekapcsolva", reply)

    async def test_server_status_does_not_confuse_channel_disable_with_server_disable(self):
        ctx = self.context()
        await self.command(ctx, "ki", "csatorna")
        self.assertIn("bekapcsolva", await self.command(ctx))
        self.assertTrue(self.cog.is_disabled(ctx))

    async def test_permissions_are_scope_specific(self):
        ctx = self.context(manage_guild=False)
        self.assertIn("Manage Server", await self.command(ctx, "ki"))
        self.assertEqual(self.cog.disabled["guilds"], [])
        await self.command(ctx, "ki", "csatorna")
        self.assertEqual(self.cog.disabled["channels"], [20])
        ctx = self.context(manage_channels=False)
        self.assertIn("Manage Channels", await self.command(ctx, "be", "csatorna"))
        self.assertEqual(self.cog.disabled["channels"], [20])

    async def test_failed_save_keeps_memory_and_disk_unchanged(self):
        ctx = self.context()
        for initial, action in (([], "ki"), ([10], "be")):
            with self.subTest(action=action):
                previous = {"guilds": initial, "channels": []}
                self.cog.disabled = previous
                self.cog.save_disabled(previous)
                with patch.object(Path, "replace", side_effect=OSError("disk failure")):
                    with self.assertLogs("cogs.linkfixer", level="ERROR"):
                        reply = await self.command(ctx, action)
                self.assertIn("nem sikerült menteni", reply)
                self.assertEqual(self.cog.disabled, previous)
                self.assertEqual(LinkFixer(None).disabled, previous)


class FacebookTest(SettingsFixture, unittest.IsolatedAsyncioTestCase):
    def message(self, content):
        return SimpleNamespace(
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=20, send=AsyncMock()),
            author=SimpleNamespace(bot=False), content=content,
        )

    async def fixed_links(self, message):
        with patch("cogs.linkfixer.LinkFixerView") as view:
            view.return_value.build_message_content.return_value = "fixed links"
            await self.cog.on_message(message)
            return view.call_args.args[2] if view.called else []

    async def test_facebook_domains_keep_paths_queries_and_fragments(self):
        paths = (
            "user/posts/123", "share/p/abc", "permalink.php?story_fbid=123&id=456",
            "story.php?story_fbid=123&id=456", "groups/10/posts/20?comment_id=30#comment",
            "reel/123",
        )
        for host in ("facebook.com", "www.facebook.com", "m.facebook.com",
                     "mbasic.facebook.com", "web.facebook.com", "WWW.FACEBOOK.COM"):
            for path in paths:
                with self.subTest(host=host, path=path):
                    message = self.message(f"look <https://{host}/{path}>")
                    self.assertEqual(await self.fixed_links(message), [f"https://facebed.com/{path}"])
                    message.channel.send.assert_awaited_once()

    async def test_multiple_facebook_links_and_existing_platforms(self):
        message = self.message(
            "http://facebook.com/user/posts/1 https://m.facebook.com/reel/2 "
            "https://instagram.com/p/3 https://reddit.com/r/test/comments/4"
        )
        self.assertEqual(await self.fixed_links(message), [
            "https://kkinstagram.com/p/3", "https://vxreddit.com/r/test/comments/4",
            "https://facebed.com/user/posts/1", "https://facebed.com/reel/2",
        ])

    async def test_lookalike_domains_and_already_fixed_links_are_ignored(self):
        for link in ("https://facebook.com.evil.test/post", "https://evilfacebook.com/post",
                     "https://facebed.com/user/posts/1"):
            with self.subTest(link=link):
                message = self.message(link)
                self.assertEqual(await self.fixed_links(message), [])
                message.channel.send.assert_not_awaited()

    async def test_disables_and_bot_filter_apply_to_facebook(self):
        for scope in ("guilds", "channels", "bot"):
            with self.subTest(scope=scope):
                self.cog.disabled = {"guilds": [], "channels": []}
                message = self.message("https://facebook.com/user/posts/1")
                if scope == "bot":
                    message.author.bot = True
                else:
                    self.cog.disabled[scope] = [10 if scope == "guilds" else 20]
                self.assertEqual(await self.fixed_links(message), [])
                message.channel.send.assert_not_awaited()

    async def test_facebook_only_view_has_no_alternate_embedder_button(self):
        message = self.message("https://facebook.com/user/posts/1")
        view = LinkFixerView(self.cog, message, ["https://facebed.com/user/posts/1"])
        try:
            self.assertNotIn("try_another_embedder", [item.custom_id for item in view.children])
            self.assertIsNone(view._fallback_task)
            self.assertFalse(LinkFixerView.is_supported_embed_link("https://facebed.com/reel/1"))
            self.assertEqual(LinkFixerView.use_another_embedder("https://facebed.com/reel/1"),
                             "https://facebed.com/reel/1")
        finally:
            view._suppress_task.cancel()
            await asyncio.gather(view._suppress_task, return_exceptions=True)
            view.stop()


class LinkRoutingTest(SettingsFixture, unittest.IsolatedAsyncioTestCase):
    async def test_tiktok_short_and_full_links(self):
        for host, path in (
            ("vm.tiktok.com", "ZMabc123/?share=1#video"),
            ("vt.tiktok.com", "ZMabc123/"),
            ("www.tiktok.com", "@user/video/123"),
            ("tiktok.com", "t/ZMabc123/"),
            ("VM.TIKTOK.COM", "ZMabc123/"),
        ):
            with self.subTest(host=host):
                message = SimpleNamespace(
                    guild=SimpleNamespace(id=10),
                    channel=SimpleNamespace(id=20, send=AsyncMock()),
                    author=SimpleNamespace(bot=False),
                    content=f"look <https://{host}/{path}>",
                )
                with patch("cogs.linkfixer.LinkFixerView") as view:
                    await self.cog.on_message(message)
                    self.assertEqual(view.call_args.args[2], [f"https://tnktok.com/{path}"])
                message.channel.send.assert_awaited_once()

    async def test_translation_uses_fixvx_and_toggle_restores_original(self):
        links = [
            "https://girlcockx.com/user/status/123/photo/1?foo=bar#media",
            "https://tnktok.com/ZMabc123/",
        ]
        message = SimpleNamespace(author=SimpleNamespace(display_name="user"))
        view = LinkFixerView(self.cog, message, links)
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        try:
            await view._apply_lang("hu", interaction)
            self.assertIn(
                "https://fixvx.com/user/status/123/photo/1/hu?foo=bar#media",
                interaction.response.edit_message.call_args.kwargs["content"],
            )
            self.assertIn(links[1], view.build_message_content())
            view.fixed_links = [view.use_another_embedder(link) for link in links]
            self.assertIn("https://fixvx.com/", view.build_message_content())
            await view._apply_lang("hu", interaction)
            self.assertIsNone(view.language)
            self.assertTrue(view.build_message_content().endswith("\n".join(view.fixed_links)))
            self.assertEqual(links[0], "https://girlcockx.com/user/status/123/photo/1?foo=bar#media")
        finally:
            view._suppress_task.cancel()
            view._fallback_task.cancel()
            await asyncio.gather(view._suppress_task, view._fallback_task, return_exceptions=True)
            view.stop()


if __name__ == "__main__":
    unittest.main()
