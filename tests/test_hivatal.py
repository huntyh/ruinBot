import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands

from cogs.hivatal import AppealButton, Hivatal, case_embed, case_view
from utils.hivatal import CaseBook, DAILY_LIMIT, DECISIONS, OfficeError, make_ruling


class CaseBookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "hivatal.sqlite3"
        self.book = CaseBook(self.path)

    def test_case_and_appeal_survive_restart(self):
        case = self.book.create(1, 2, "  még   egy\n sör  ")
        self.assertEqual(case.subject, "még egy sör")
        self.assertEqual(CaseBook(self.path).get(1, case.id), case)
        appealed = CaseBook(self.path).appeal(1, 2, case.id)
        self.assertNotEqual(appealed.current["decision"], case.current["decision"])
        self.assertEqual(appealed.ruling, case.ruling)
        self.assertEqual(CaseBook(self.path).get(1, case.id), appealed)
        with self.assertRaises(OfficeError):
            self.book.appeal(1, 2, case.id)

    def test_guild_and_owner_isolation(self):
        case = self.book.create(1, 2, "engedély kávéra")
        for operation, args in (
            (self.book.get, (9, case.id)),
            (self.book.appeal, (9, 2, case.id)),
            (self.book.appeal, (1, 9, case.id)),
        ):
            with self.assertRaises(OfficeError):
                operation(*args)
        self.assertEqual(self.book.recent(9, 2), [])
        self.assertEqual(self.book.recent(1, 9), [])
        self.assertIsNone(self.book.get(1, case.id).appeal)

    def test_daily_limit_is_persistent_and_scoped_to_applicant_and_guild(self):
        now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        for _ in range(DAILY_LIMIT):
            self.book.create(1, 2, "még egy sör", now=now)
        with self.assertRaises(OfficeError):
            CaseBook(self.path).create(1, 2, "még egy sör", now=now)
        self.book.create(9, 2, "másik szerver", now=now)
        self.book.create(1, 9, "másik kérelmező", now=now)
        # A sorszámhiány nem akadályozza a fellebbezést vagy az olvasást.
        case = self.book.recent(1, 2)[0]
        self.book.appeal(1, 2, case.id)

    def test_quota_resets_at_budapest_midnight_in_winter_and_summer(self):
        for month, utc_hour, user in ((1, 23, 2), (7, 22, 3)):
            before = datetime(2030, month, 1, utc_hour - 1, 59, tzinfo=timezone.utc)
            after = datetime(2030, month, 1, utc_hour, tzinfo=timezone.utc)
            for _ in range(DAILY_LIMIT):
                self.book.create(1, user, "kérek egy napot", now=before)
            with self.assertRaises(OfficeError):
                self.book.create(1, user, "kérek egy napot", now=before)
            self.book.create(1, user, "új nap új papír", now=after)

    def test_concurrent_requests_cannot_bypass_quota(self):
        def create(_):
            try:
                return self.book.create(1, 2, "beadvány")
            except OfficeError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            cases = list(pool.map(create, range(12)))
        saved = [case for case in cases if case is not None]
        self.assertEqual(len(saved), DAILY_LIMIT)
        self.assertEqual(len({case.id for case in saved}), DAILY_LIMIT)

    def test_concurrent_appeals_are_one_shot(self):
        case = self.book.create(1, 2, "beadvány")
        def appeal(_):
            try:
                return self.book.appeal(1, 2, case.id)
            except OfficeError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(appeal, range(6)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_failed_write_does_not_consume_quota_or_leave_partial_case(self):
        with closing(self.book.connect()) as db, db:
            db.execute("""CREATE TRIGGER broken_printer BEFORE INSERT ON hivatal_cases
                BEGIN SELECT RAISE(ABORT, 'paper jam'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.book.create(1, 2, "kérem a papírt")
        self.assertEqual(self.book.recent(1, 2), [])
        with closing(self.book.connect()) as db, db:
            db.execute("DROP TRIGGER broken_printer")
        for _ in range(DAILY_LIMIT):
            self.book.create(1, 2, "most már működik")

    def test_failed_appeal_keeps_original_and_allows_retry(self):
        case = self.book.create(1, 2, "kávét kérek")
        with patch("utils.hivatal.make_ruling", side_effect=RuntimeError("printer exploded")):
            with self.assertRaises(RuntimeError):
                self.book.appeal(1, 2, case.id)
        self.assertEqual(self.book.get(1, case.id), case)
        self.assertIsNotNone(self.book.appeal(1, 2, case.id).appeal)

    def test_empty_short_and_long_requests_are_rejected(self):
        for text in ("", " \n\t ", "ab", "x" * 301):
            with self.assertRaises(OfficeError):
                self.book.create(1, 2, text)
        self.assertEqual(self.book.recent(1, 2), [])
        self.book.create(1, 2, "x" * 300)

    def test_recent_is_newest_five(self):
        for user in (2, 3):
            for day in (1, 2):
                for _ in range(3):
                    self.book.create(1, user, "még egy papír", now=datetime(2030, 1, day, tzinfo=timezone.utc))
        cases = self.book.recent(1, 2)
        self.assertEqual(len(cases), 5)
        self.assertEqual([case.id for case in cases], [6, 5, 4, 3, 2])
        self.assertTrue(all(case.user_id == 2 for case in cases))


class OfficeDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = CaseBook(Path(self.temp.name) / "hivatal.sqlite3")
        self.bot = commands.Bot(command_prefix="r!", intents=discord.Intents.none())
        self.cog = Hivatal(self.bot, self.book)
        await self.bot.add_cog(self.cog)
        self.addAsyncCleanup(self.bot.close)

    def interaction(self, user_id=2, guild_id=1):
        interaction = MagicMock()
        interaction.client = self.bot
        interaction.guild_id = guild_id
        interaction.user.id = user_id
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.message.edit = AsyncMock()
        return interaction

    async def test_commands_register_as_guild_only_group_and_serialize(self):
        group = self.bot.tree.get_command("hivatal")
        self.assertTrue(group.guild_only)
        self.assertEqual({cmd.name for cmd in group.commands}, {"kerveny", "ugy", "sajat", "fellebbezes"})
        payload = group.to_dict(self.bot.tree)
        self.assertEqual(len(payload["options"]), 4)
        json.dumps(payload)

    async def test_request_posts_public_embed_and_persistent_button_without_mentions(self):
        interaction = self.interaction()
        await self.cog.kerveny.callback(self.cog, interaction, "@everyone **sör** <@123>")
        kwargs = interaction.followup.send.call_args.kwargs
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertTrue(kwargs["view"].is_persistent())
        self.assertNotIn("@everyone", kwargs["embed"].description)
        self.assertIn(r"\*\*sör\*\*", kwargs["embed"].description)
        self.assertEqual(len(self.book.recent(1, 2)), 1)

    async def test_reconstructed_button_appeals_and_updates_original(self):
        case = self.book.create(1, 2, "sörengedély")
        original = AppealButton(case.id)
        match = AppealButton.__discord_ui_compiled_template__.fullmatch(original.custom_id)
        button = await AppealButton.from_custom_id(self.interaction(), original.item, match)
        interaction = self.interaction()
        await button.callback(interaction)
        self.assertIsNotNone(self.book.get(1, case.id).appeal)
        interaction.message.edit.assert_awaited_once()
        self.assertIsNone(interaction.message.edit.call_args.kwargs["view"])
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])

    async def test_wrong_user_and_repeated_click_do_not_update_message(self):
        case = self.book.create(1, 2, "sörengedély")
        wrong = self.interaction(user_id=3)
        await AppealButton(case.id).callback(wrong)
        wrong.message.edit.assert_not_awaited()
        self.assertIsNone(self.book.get(1, case.id).appeal)
        await self.cog.handle_appeal(self.interaction(), case.id)
        duplicate = self.interaction()
        await AppealButton(case.id).callback(duplicate)
        duplicate.message.edit.assert_not_awaited()
        self.assertIn("jogerős", duplicate.followup.send.call_args.args[0])

    async def test_deleted_message_does_not_lose_final_ruling(self):
        case = self.book.create(1, 2, "sörengedély")
        interaction = self.interaction()
        response = MagicMock(status=404, reason="Not Found")
        interaction.message.edit.side_effect = discord.NotFound(response, "deleted")
        with self.assertLogs("cogs.hivatal", level="WARNING"):
            await self.cog.handle_appeal(interaction, case.id, from_button=True)
        self.assertIsNotNone(self.book.get(1, case.id).appeal)
        self.assertIn("embed", interaction.followup.send.call_args.kwargs)

    async def test_missing_cog_and_dm_button_have_friendly_responses(self):
        interaction = self.interaction()
        await self.bot.remove_cog("Hivatal")
        await AppealButton(1).callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        dm = self.interaction(guild_id=None)
        await self.cog.handle_appeal(dm, 1)
        self.assertIn("szerveren", dm.followup.send.call_args.args[0])

    async def test_lookup_and_recent_are_private(self):
        case = self.book.create(1, 2, "sörengedély")
        interaction = self.interaction()
        await self.cog.ugy.callback(self.cog, interaction, case.id)
        self.assertTrue(interaction.response.defer.call_args.kwargs["ephemeral"])
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        other = self.interaction(user_id=3)
        await self.cog.sajat.callback(self.cog, other)
        self.assertIn("tiszta", other.followup.send.call_args.args[0])
        own = self.interaction()
        await self.cog.sajat.callback(self.cog, own)
        self.assertEqual(len(own.followup.send.call_args.kwargs["embed"].fields), 1)

    async def test_final_case_lookup_sends_valid_view_without_appeal_button(self):
        case = self.book.create(1, 2, "sörengedély")
        self.book.appeal(1, 2, case.id)
        interaction = self.interaction()
        await self.cog.ugy.callback(self.cog, interaction, case.id)
        view = interaction.followup.send.call_args.kwargs["view"]
        # Webhook.send rejects view=None; a final file needs an empty View.
        self.assertIsInstance(view, discord.ui.View)
        self.assertEqual(view.children, [])

    async def test_storage_failure_is_logged_and_reported(self):
        interaction = self.interaction()
        with self.assertLogs("cogs.hivatal", level="ERROR"):
            result = await self.cog.run_office(interaction, MagicMock(side_effect=sqlite3.OperationalError("locked")))
        self.assertIsNone(result)
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])

    async def test_each_possible_ruling_and_appeal_fits_discord_limits(self):
        case = self.book.create(1, 2, "*" * 300)
        for decision in DECISIONS:
            with patch("utils.hivatal.RNG.choice", side_effect=lambda items: decision if decision in items else items[0]):
                ruling = make_ruling()
            case.ruling.update(ruling)
            embed = case_embed(case)
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(embed.title), 256)
            self.assertLessEqual(len(embed.description), 4096)
            self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
            self.assertEqual(len(set(ruling["evidence"])), 2)
            appealed = make_ruling(previous=decision)
            self.assertNotEqual(appealed["decision"], decision)
        final = self.book.appeal(1, 2, case.id)
        self.assertEqual(case_view(final).children, [])
        self.assertIn("Jogerős", case_embed(final).fields[1].name)


if __name__ == "__main__":
    unittest.main()
