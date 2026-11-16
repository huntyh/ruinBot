import json
import random
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands

from cogs.hivatal import AppealButton, Hivatal, case_embed, case_view
from utils.hivatal import (
    CaseBook, DAILY_LIMIT, DECISIONS, HISTORY_WINDOW, OfficeError, adjectives, make_ruling, mentioned_users,
)
from utils.hivatal_content import APPEAL_REASONS, CONCLUSIONS, EVIDENCE, FIRST_NAMES, OFFICES, REASONS, TITLES


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

    def test_tagged_users_are_persistent_participants_and_share_one_appeal(self):
        for user in (3, 4):
            case = self.book.create(1, 2, "<@3>-nak és <@!4>-nek egy ban <@3>")
            self.assertEqual(case.target_ids, (3, 4))
            reopened = CaseBook(self.path)
            self.assertEqual(reopened.recent(1, user)[0], case)
            self.assertEqual(reopened.recent(9, user), [])
            with self.assertRaises(OfficeError):
                reopened.appeal(9, user, case.id)
            with self.assertRaises(OfficeError):
                reopened.appeal(1, 5, case.id)
            final = reopened.appeal(1, user, case.id)
            self.assertEqual(final.appeal["appealed_by"], user)
            for other in (2, 3, 4):
                with self.assertRaises(OfficeError):
                    reopened.appeal(1, other, case.id)

    def test_explicit_recipient_combines_with_mentions_and_preserves_applicant_rights(self):
        case = self.book.create(1, 2, "<@3>-nak egy ban", 4)
        self.assertEqual(case.target_ids, (3, 4))
        self.assertEqual(self.book.recent(1, 4), [case])
        self.book.appeal(1, 2, case.id)
        deduplicated = self.book.create(1, 2, "<@3>-nak még egy ban", 3)
        self.assertEqual(deduplicated.target_ids, (3,))

    def test_roles_everyone_and_plain_names_do_not_grant_appeals(self):
        subject = "@everyone @here @Cserna <@&3> <#4> <@0> <@9999999999999999999>"
        self.assertEqual(mentioned_users(subject), ())
        case = self.book.create(1, 2, subject)
        self.assertEqual(case.target_ids, ())
        with self.assertRaises(OfficeError):
            self.book.appeal(1, 3, case.id)

    def test_concurrent_participants_cannot_each_get_an_appeal(self):
        case = self.book.create(1, 2, "<@3> <@4> <@5> egy ban")
        def appeal(user):
            try:
                return self.book.appeal(1, user, case.id)
            except OfficeError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(appeal, (2, 3, 4, 5)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_upgrade_backfills_tags_without_changing_old_rulings_or_quota(self):
        case = self.book.create(1, 2, "<@!3>-nak egy ban <@3>")
        original_ruling = {
            "decision": "denied", "office": "Majd Hétfőn Osztály", "clerk": "Pecsét Elek",
            "reason": "A régi hivatal így döntött.", "evidence": ["régi papír", "régi pecsét"],
            "instruction": "Várj hétfőig.",
        }
        with closing(self.book.connect()) as db, db:
            db.execute("UPDATE hivatal_cases SET ruling = ? WHERE id = ?", (json.dumps(original_ruling), case.id))
            # A régi kiadás adatbázisa pontosan az ügytáblát tartalmazta.
            db.execute("DROP TABLE hivatal_targets")
            db.execute("DROP TABLE hivatal_history")
        reopened = CaseBook(self.path)
        migrated = reopened.get(1, case.id)
        self.assertEqual(migrated.ruling, original_ruling)
        self.assertEqual(migrated.target_ids, (3,))
        self.assertEqual(migrated.subject, case.subject)
        self.assertEqual(migrated.created_at, case.created_at)
        with closing(reopened.connect()) as db:
            self.assertEqual(reopened.history(db, 1), [original_ruling])
        for _ in range(DAILY_LIMIT - 1):
            reopened.create(1, 2, "új kérelem")
        with self.assertRaises(OfficeError):
            reopened.create(1, 2, "még egy kérelem")
        final = reopened.appeal(1, 3, case.id)
        self.assertEqual(CaseBook(self.path).get(1, case.id), final)
        self.assertEqual(CaseBook(self.path).get(1, case.id).target_ids, (3,))

    def test_recent_text_is_not_repeated_across_restarts_or_appeals(self):
        history = []
        with patch("utils.hivatal.RNG", random.Random(42)):
            for number in range(55):
                book = CaseBook(self.path) if number % 7 == 0 else self.book
                case = book.create(1, 100 + number, "kérek egy új ötletet")
                rulings = [case.ruling]
                if number % 3 == 0:
                    rulings.append(book.appeal(1, case.user_id, case.id).appeal)
                for ruling in rulings:
                    previous = history[-HISTORY_WINDOW:]
                    for key in ("office_base", "first_name", "clerk_title", "clerk_adjective", "instruction"):
                        self.assertNotIn(ruling[key], [old[key] for old in previous], key)
                    used_evidence = {item for old in previous for item in old["evidence"]}
                    self.assertTrue(set(ruling["evidence"]).isdisjoint(used_evidence))
                    for part in (*REASONS, *APPEAL_REASONS, *CONCLUSIONS):
                        if part in ruling["reason"]:
                            self.assertFalse(any(part in old["reason"] for old in previous), part)
                    history.append(ruling)
        with closing(self.book.connect()) as db:
            self.assertEqual(self.book.history(db, 1), list(reversed(history[-HISTORY_WINDOW:])))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hivatal_history").fetchone()[0], HISTORY_WINDOW)

    def test_appealing_an_old_case_is_in_recent_generation_history(self):
        oldest = self.book.create(1, 2, "egy régi kérelem")
        for index in range(25):
            self.book.create(1, 100 + index, "egy új kérelem")
        final = self.book.appeal(1, 2, oldest.id)
        with closing(self.book.connect()) as db:
            self.assertEqual(self.book.history(db, 1)[0], final.appeal)
            self.assertEqual(self.book.history(db, 9), [])

    def test_failure_recording_history_rolls_back_case_and_targets(self):
        with closing(self.book.connect()) as db, db:
            db.execute("""CREATE TRIGGER broken_history BEFORE INSERT ON hivatal_history
                BEGIN SELECT RAISE(ABORT, 'paper jam'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.book.create(1, 2, "<@3>-nak egy ban")
        self.assertEqual(self.book.recent(1, 2), [])
        self.assertEqual(self.book.recent(1, 3), [])
        with closing(self.book.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hivatal_targets").fetchone()[0], 0)

    def test_recent_text_history_is_scoped_to_guild(self):
        with patch("utils.hivatal.RNG", random.Random(100)):
            first = self.book.create(1, 2, "egy sör")
        with patch("utils.hivatal.RNG", random.Random(100)):
            second = self.book.create(9, 2, "egy sör")
        self.assertEqual(first.ruling, second.ruling)


class OfficeContentTests(unittest.TestCase):
    def test_content_pools_are_large_and_have_no_internal_duplicates(self):
        for pool, minimum in ((OFFICES, 40), (FIRST_NAMES, 32), (TITLES, 24), (EVIDENCE, 70),
                              (REASONS, 40), (CONCLUSIONS, 32), (APPEAL_REASONS, 24)):
            self.assertGreaterEqual(len(pool), minimum)
            self.assertEqual(len(pool), len(set(pool)))
        for _, instructions in DECISIONS.values():
            self.assertGreaterEqual(len(instructions), 32)
            self.assertEqual(len(instructions), len(set(instructions)))
        self.assertGreater(len(adjectives()), 4500)

    def test_clerk_uses_the_existing_adjective_dictionary(self):
        ruling = make_ruling()
        self.assertIn(ruling["clerk_adjective"], adjectives())
        self.assertTrue(ruling["clerk"].startswith(f"dr. {ruling['clerk_adjective'].capitalize()} "))


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
        options = next(command for command in payload["options"] if command["name"] == "kerveny")["options"]
        recipient = next(option for option in options if option["name"] == "erintett")
        self.assertEqual(recipient["type"], discord.AppCommandOptionType.user.value)
        self.assertFalse(recipient["required"])

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

    async def test_tagged_recipient_can_use_reconstructed_button(self):
        interaction = self.interaction()
        await self.cog.kerveny.callback(self.cog, interaction, "<@3>-nak egy ban")
        case = self.book.recent(1, 3)[0]
        embed = interaction.followup.send.call_args.kwargs["embed"]
        self.assertIn(r"**Tárgy:** <@3>\-nak egy ban", embed.description)
        self.assertIn("**Érintettek:** <@3>", embed.description)
        self.assertEqual(embed.footer.text, "A pecsét helye: 🥔")
        original = AppealButton(case.id)
        match = AppealButton.__discord_ui_compiled_template__.fullmatch(original.custom_id)
        button = await AppealButton.from_custom_id(self.interaction(), original.item, match)
        recipient = self.interaction(user_id=3)
        await button.callback(recipient)
        self.assertEqual(self.book.get(1, case.id).appeal["appealed_by"], 3)
        self.assertIn("**Fellebbező:** <@3>", recipient.followup.send.call_args.kwargs["embed"].description)
        recipient.message.edit.assert_awaited_once()

    async def test_explicit_member_can_use_appeal_command(self):
        member = MagicMock(spec=discord.Member)
        member.id = 3
        interaction = self.interaction()
        await self.cog.kerveny.callback(self.cog, interaction, "egy ban", member)
        case = self.book.recent(1, 3)[0]
        self.assertEqual(case.target_ids, (3,))
        recipient = self.interaction(user_id=3)
        await self.cog.fellebbezes.callback(self.cog, recipient, case.id)
        self.assertEqual(self.book.get(1, case.id).appeal["appealed_by"], 3)
        recipient.message.edit.assert_not_awaited()

    async def test_participants_see_the_case_in_their_private_list(self):
        self.book.create(1, 2, "<@3>-nak egy ban")
        interaction = self.interaction(user_id=3)
        await self.cog.sajat.callback(self.cog, interaction)
        kwargs = interaction.followup.send.call_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(len(kwargs["embed"].fields), 1)
        self.assertIn("<@3>", kwargs["embed"].fields[0].value)
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])

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

    async def test_longest_new_content_and_many_mentions_fit_discord_limits(self):
        subject = " ".join(f"<@{user}>" for user in range(10, 55))
        case = self.book.create(1, 2, subject)
        for decision in DECISIONS:
            ruling = {
                "decision": decision,
                "office": f"{max(adjectives(), key=len)} {max(OFFICES, key=len)}",
                "clerk": f"dr. {max(adjectives(), key=len)} {max(FIRST_NAMES, key=len)}, {max(TITLES, key=len)}",
                "reason": f"{max((*REASONS, *APPEAL_REASONS), key=len)} {max(CONCLUSIONS, key=len)}",
                "evidence": sorted(EVIDENCE, key=len)[-2:],
                "instruction": max(DECISIONS[decision][1], key=len),
                "appealed_by": 3,
            }
            embed = case_embed(replace(case, appeal=ruling))
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(embed.description), 4096)
            self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
            self.assertTrue(all(len(field.name) <= 256 for field in embed.fields))


if __name__ == "__main__":
    unittest.main()
