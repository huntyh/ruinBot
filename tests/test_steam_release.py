import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from cogs.steam_release import SteamLookupError, app_id_from_query, parse_release_date
from cogs.steam_release import SteamGame, SteamRelease, ReleaseConfirmation, CustomEventModal, parse_event_date


class CustomEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_game_uses_custom_title_time_and_original_metadata(self):
        cog = SteamRelease(None)
        cog.get_image = AsyncMock(return_value=b"art")
        guild = MagicMock()
        guild.create_scheduled_event = AsyncMock()
        game = SteamGame(238960, "Path of Exile", "Description", "https://example.org/art",
                         datetime(2013, 1, 1, tzinfo=timezone.utc))
        start = parse_event_date("2099-01-15 20:00")
        await cog.create_event(guild, game, event_name="New league", start_time=start)
        args = guild.create_scheduled_event.call_args.kwargs
        self.assertEqual(args["name"], "New league")
        self.assertEqual(args["start_time"], start)
        self.assertEqual(args["location"], game.store_url)
        self.assertEqual(args["image"], b"art")
        self.assertIn(game.description, args["description"])
        with self.assertRaises(SteamLookupError):
            await cog.create_event(guild, game)

    async def test_released_game_confirmation_opens_private_form(self):
        game = SteamGame(0, "Diablo III", "", "", datetime(2012, 1, 1, tzinfo=timezone.utc),
                         "https://diablo3.blizzard.com/en-us/")
        view = ReleaseConfirmation(SteamRelease(None), game, 123)
        interaction = MagicMock()
        interaction.response.send_modal = AsyncMock()
        await view.confirm.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, CustomEventModal)
        self.assertEqual(modal.event_name.default, "Diablo III event")
        self.assertEqual(game.store_url, game.official_url)

    def test_rejects_past_or_invalid_dates(self):
        for value in ("2000-01-01 20:00", "tomorrow"):
            with self.assertRaises(SteamLookupError):
                parse_event_date(value)

    def test_central_european_winter_and_summer(self):
        self.assertEqual(parse_event_date("2030-01-15 20:00").astimezone(timezone.utc),
                         datetime(2030, 1, 15, 19, tzinfo=timezone.utc))
        self.assertEqual(parse_event_date("2030-07-15 20:00").astimezone(timezone.utc),
                         datetime(2030, 7, 15, 18, tzinfo=timezone.utc))

    def test_clock_change_requires_unambiguous_time(self):
        for value in ("2030-03-31 02:30", "2030-10-27 02:30"):
            with self.assertRaises(SteamLookupError):
                parse_event_date(value)
        self.assertEqual(parse_event_date("2030-10-27 02:30+01:00").astimezone(timezone.utc),
                         datetime(2030, 10, 27, 1, 30, tzinfo=timezone.utc))


class SteamReleaseHelpersTest(unittest.TestCase):
    def test_extracts_app_id_from_store_url(self):
        self.assertEqual(
            app_id_from_query("https://store.steampowered.com/app/1245620/ELDEN_RING/"),
            1245620,
        )

    def test_accepts_bare_app_id(self):
        self.assertEqual(app_id_from_query("1245620"), 1245620)

    def test_game_name_is_not_an_app_id(self):
        self.assertIsNone(app_id_from_query("Elden Ring"))

    def test_parses_steam_release_date(self):
        self.assertEqual(
            parse_release_date("21 Aug, 2026"),
            datetime(2026, 8, 21, 12, tzinfo=timezone.utc),
        )

    def test_rejects_imprecise_release_date(self):
        with self.assertRaises(SteamLookupError):
            parse_release_date("Coming soon")


if __name__ == "__main__":
    unittest.main()
