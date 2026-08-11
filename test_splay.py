import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import splay as splay_module

from splay import (
    Splay,
    SplayError,
    TidalSearch,
    _tidal_session_from_env,
    parse_command,
    rank_tracks,
    tidal_track_url,
    tidal_album_url,
    _track_from_tidalapi,
    _album_from_tidalapi,
)


class FakeSpeaker:
    def __init__(self):
        self.player_name = "Office"
        self.ip_address = "192.0.2.10"
        self.volume = 32
        self.mute = False
        self.group = None
        self.calls = []

    def clear_queue(self):
        self.calls.append("clear_queue")

    def add_to_queue(self, item, position=0, as_next=False):
        self.calls.append(("add_to_queue", item, as_next))
        return 1

    def play_from_queue(self, index):
        self.calls.append(("play_from_queue", index))

    def set_relative_volume(self, amount):
        self.volume += amount
        return self.volume

    def pause(self):
        self.calls.append("pause")

    def play(self):
        self.calls.append("play")

    def stop(self):
        self.calls.append("stop")

    def next(self):
        self.calls.append("next")

    def previous(self):
        self.calls.append("previous")

    def get_current_transport_info(self):
        return {"current_transport_state": "STOPPED"}

    def get_current_track_info(self):
        return {"title": "", "artist": "", "album": ""}


class FakeService:
    def __init__(self, items):
        self.items = items

    def search(self, category, term, count):
        assert category == "tracks"
        return self.items


class SplayTests(unittest.TestCase):
    def test_tidal_credentials_are_required(self):
        with self.assertRaisesRegex(SplayError, "TIDAL_CLIENT_ID"):
            _tidal_session_from_env({})

    def test_tidal_credentials_configure_the_session(self):
        class FakeConfig:
            client_id = "bundled-id"
            client_secret = "bundled-secret"

        class FakeSession:
            def __init__(self, config):
                self.config = config

        fake_tidalapi = SimpleNamespace(Config=FakeConfig, Session=FakeSession)
        with patch.object(splay_module, "_tidalapi", fake_tidalapi):
            session = _tidal_session_from_env({
                "TIDAL_CLIENT_ID": "user-id",
                "TIDAL_CLIENT_SECRET": "user-secret",
            })
        self.assertEqual(session.config.client_id, "user-id")
        self.assertEqual(session.config.client_secret, "user-secret")

    def test_tidal_session_file_is_private(self):
        class FakeSession:
            def check_login(self):
                return False

            def login_oauth_simple(self, fn_print):
                pass

            def save_session_to_file(self, path):
                path.write_text("token")

        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / ".splay" / "tidal-session.json"
            TidalSearch(session_file, session_factory=FakeSession)._ensure()
            self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(session_file.parent.stat().st_mode), 0o700)

    def test_command_and_url_parsing(self):
        self.assertEqual(parse_command('play "Nights by Frank Ocean"'), ("play", ["Nights by Frank Ocean"]))
        self.assertEqual(
            tidal_track_url("https://tidal.com/browse/track/123-abc?foo=bar"),
            ("https://tidal.com/browse/track/123-abc", "123-abc"),
        )
        self.assertEqual(
            tidal_album_url("https://tidal.com/browse/album/2381489"),
            ("https://tidal.com/browse/album/2381489", "2381489"),
        )
        self.assertIsNone(tidal_track_url("https://tidal.com/browse/album/2381489"))

    def test_rank_prefers_exact_original(self):
        items = [
            SimpleNamespace(title="Nights (Live)", artist="Frank Ocean", album="Live"),
            SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde"),
            SimpleNamespace(title="Night", artist="Frank Ocean", album="Other"),
        ]
        tracks = rank_tracks(items, "Nights by Frank Ocean")
        self.assertEqual(tracks[0].title, "Nights")
        self.assertEqual(tracks[0].album, "Blonde")

    def test_play_plays_immediately_no_prompt(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(
            title="Nights", artist="Frank Ocean", album="Blonde", uri="soco://track", item_id="1"
        )
        answers = iter(["1", "play Nights by Frank Ocean", "exit"])
        output = []
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": next(answers),
            output_fn=output.append,
        )
        prompts = []
        app.input_fn = lambda prompt="": (prompts.append(prompt) or next(answers))
        self.assertEqual(app.run(), 0)
        self.assertIn("clear_queue", speaker.calls)
        self.assertTrue(all("?" not in prompt for prompt in prompts))

    def test_confirmed_play_uses_sonos_queue(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde", item_id="1")
        answers = iter(["1", "play Nights by Frank Ocean", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(speaker.calls[0], "clear_queue")
        self.assertEqual(speaker.calls[1][0], "add_to_queue")
        self.assertEqual(speaker.calls[2], ("play_from_queue", 0))

    def test_tidal_login_error_is_actionable(self):
        output = []
        app = Splay(output_fn=output.append)
        app._report(Exception("('LoginInvalid', 'Client.LoginInvalid')"))
        self.assertEqual(output, ["TIDAL authorization failed. Run a play command to start the link flow."])

    def test_login_invalid_triggers_link_flow_and_retries(self):
        calls = []

        class AuthOnceService:
            def search(self, category, term, count):
                calls.append("search")
                if len(calls) == 1:
                    raise Exception("('LoginInvalid', 'Client.LoginInvalid')")
                return [SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde")]

            def begin_authentication(self):
                calls.append("begin_authentication")
                self.link_code = "1234"
                return "https://example.com/link"

            def complete_authentication(self):
                calls.append("complete_authentication")

        answers = iter(["1", "play Nights by Frank Ocean", "", "exit"])
        speaker = FakeSpeaker()
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            music_service_factory=lambda room: AuthOnceService(),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(calls, ["search", "begin_authentication", "complete_authentication", "search"])
        self.assertEqual(speaker.calls[0], "clear_queue")

    def test_api_resolution_plays_via_share_link(self):
        speaker = FakeSpeaker()

        class FakeShareLink:
            def add_share_link_to_queue(self, url, as_next=False):
                speaker.calls.append(("share_link", url))
                return 1

        class FakeTidalApi:
            def search(self, query):
                return [
                    {
                        "id": 71061291,
                        "title": "Nights",
                        "artists": [{"name": "Frank Ocean"}],
                        "album": {"title": "Blonde"},
                    }
                ]

        answers = iter(["1", "play Nights by Frank Ocean", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            tidal_api=FakeTidalApi(),
            sharelink_factory=lambda room: FakeShareLink(),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(
            speaker.calls[1],
            ("share_link", "https://tidal.com/browse/track/71061291"),
        )
        self.assertEqual(speaker.calls[2], ("play_from_queue", 0))

    def test_track_from_api_keeps_id_and_url(self):
        track = _track_from_tidalapi(
            {"id": 123, "name": "Nights", "artist": {"name": "Frank Ocean"},
             "album": {"name": "Blonde"}}
        )
        self.assertEqual(track.item_id, "123")
        self.assertEqual(track.source_url, "https://tidal.com/browse/track/123")
        self.assertEqual(track.artist, "Frank Ocean")

    def test_album_from_api_conversion(self):
        track = _album_from_tidalapi(
            {"id": 2381489, "name": "Blonde", "artist": {"name": "Frank Ocean"}}
        )
        self.assertEqual(track.kind, "album")
        self.assertEqual(track.source_url, "https://tidal.com/browse/album/2381489")
        self.assertEqual(track.title, "Blonde")

    def test_play_album_uses_share_link(self):
        speaker = FakeSpeaker()

        class FakeShareLink:
            def add_share_link_to_queue(self, url, as_next=False):
                speaker.calls.append(("share_link", url))
                return 1

        class FakeTidalApi:
            def search_albums(self, query):
                return [{"id": 2381489, "name": "Blonde", "artist": {"name": "Frank Ocean"}}]

        answers = iter(["1", "play album Blonde by Frank Ocean", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            tidal_api=FakeTidalApi(),
            sharelink_factory=lambda room: FakeShareLink(),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(
            speaker.calls[1],
            ("share_link", "https://tidal.com/browse/album/2381489"),
        )
        self.assertEqual(speaker.calls[2], ("play_from_queue", 0))

    def test_play_album_url(self):
        speaker = FakeSpeaker()

        class FakeShareLink:
            def add_share_link_to_queue(self, url, as_next=False):
                speaker.calls.append(("share_link", url))
                return 1

        class FakeTidalApi:
            def album(self, album_id):
                assert album_id == "2381489"
                return {"id": 2381489, "name": "Blonde", "artist": {"name": "Frank Ocean"}}

        answers = iter(["1", "play https://tidal.com/browse/album/2381489", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            tidal_api=FakeTidalApi(),
            sharelink_factory=lambda room: FakeShareLink(),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(
            speaker.calls[1],
            ("share_link", "https://tidal.com/browse/album/2381489"),
        )

    def test_scripted_add_never_prompts(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde", item_id="1")
        output = []
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            room_choice="1",
            single_command="add Nights by Frank Ocean",
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=output.append,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(speaker.calls[0][0], "add_to_queue")
        self.assertNotIn("Add “Nights” — Frank Ocean to the queue? [y/N] ", output)

    def test_add_queues_without_playing(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde", item_id="1")
        answers = iter(["1", "add Nights by Frank Ocean", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(speaker.calls[0][0], "add_to_queue")
        self.assertNotIn("play_from_queue", speaker.calls)
        self.assertNotIn("clear_queue", speaker.calls)

    def test_add_next_plays_next(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde", item_id="1")
        answers = iter(["1", "add next Nights by Frank Ocean", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(speaker.calls[0], ("add_to_queue", item, True))

    def test_add_next_album_via_share_link(self):
        speaker = FakeSpeaker()

        class FakeShareLink:
            def add_share_link_to_queue(self, url, as_next=False):
                speaker.calls.append(("share_link", url, as_next))
                return 1

        class FakeTidalApi:
            def search_albums(self, query):
                return [{"id": 2381489, "name": "Blonde", "artist": {"name": "Frank Ocean"}}]

        answers = iter(["1", "add next album Blonde by Frank Ocean", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            tidal_api=FakeTidalApi(),
            sharelink_factory=lambda room: FakeShareLink(),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(
            speaker.calls[0],
            ("share_link", "https://tidal.com/browse/album/2381489", True),
        )

    def test_add_album_queues_via_share_link(self):
        speaker = FakeSpeaker()

        class FakeShareLink:
            def add_share_link_to_queue(self, url, as_next=False):
                speaker.calls.append(("share_link", url))
                return 1

        class FakeTidalApi:
            def search_albums(self, query):
                return [{"id": 2381489, "name": "Blonde", "artist": {"name": "Frank Ocean"}}]

        answers = iter(["1", "add album Blonde by Frank Ocean", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            tidal_api=FakeTidalApi(),
            sharelink_factory=lambda room: FakeShareLink(),
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(
            speaker.calls[0],
            ("share_link", "https://tidal.com/browse/album/2381489"),
        )
        self.assertNotIn("play_from_queue", speaker.calls)

    def test_rooms_show_group_members(self):
        office = FakeSpeaker()
        office.player_name = "Office"
        bath = FakeSpeaker()
        bath.player_name = "Bathroom"
        bath.ip_address = "192.0.2.11"

        class FakeGroup:
            coordinator = office
            members = {office, bath}

        office.group = bath.group = FakeGroup()
        output = []
        app = Splay(
            discover_fn=lambda timeout: [office, bath],
            input_fn=lambda prompt="": "exit",
            output_fn=output.append,
        )
        app.refresh_rooms()
        app._show_rooms()
        self.assertIn("1. Bathroom (+Office)", output)
        self.assertIn("2. Office (+Bathroom)", output)

    def test_transport_uses_group_coordinator(self):
        office = FakeSpeaker()
        bath = FakeSpeaker()
        bath.player_name = "Bathroom"
        bath.ip_address = "192.0.2.11"

        class FakeGroup:
            coordinator = office
            members = {office, bath}

        office.group = bath.group = FakeGroup()
        answers = iter(["2", "pause", "exit"])
        app = Splay(
            discover_fn=lambda timeout: [office, bath],
            input_fn=lambda prompt="": next(answers),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)
        self.assertIn("pause", office.calls)
        self.assertNotIn("pause", bath.calls)

    def test_scripted_play_never_prompts(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde", item_id="1")
        output = []
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            room_choice="1",
            single_command="play Nights by Frank Ocean",
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=output.append,
        )
        self.assertEqual(app.run(), 0)
        self.assertIn("clear_queue", speaker.calls)
        self.assertNotIn("Play “Nights” — Frank Ocean? [y/N] ", output)

    def test_scriptable_room_and_command(self):
        office = FakeSpeaker()
        office.player_name = "Office"
        office.calls = []
        output = []
        app = Splay(
            discover_fn=lambda timeout: [office],
            room_choice="Office",
            single_command="pause",
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=output.append,
        )
        self.assertEqual(app.run(), 0)
        self.assertEqual(office.calls, ["pause"])
        self.assertEqual(output, ["Paused."])

    def test_scriptable_room_by_number(self):
        office = FakeSpeaker()
        office.player_name = "Office"
        app = Splay(
            discover_fn=lambda timeout: [office],
            room_choice="1",
            single_command="status",
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=lambda message: None,
        )
        self.assertEqual(app.run(), 0)

    def test_scriptable_unknown_room_fails(self):
        output = []
        app = Splay(
            discover_fn=lambda timeout: [FakeSpeaker()],
            room_choice="Nope",
            single_command="status",
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=output.append,
        )
        self.assertEqual(app.run(), 1)
        self.assertTrue(any("No Sonos room named" in line for line in output))

    def test_auto_pick_picks_first_of_duplicates(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde", item_id="1")
        output = []
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            room_choice="1",
            single_command="play Nights by Frank Ocean",
            auto_confirm=True,
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=output.append,
        )
        self.assertEqual(app.run(), 0)
        self.assertTrue(any("Auto-picked:" in line for line in output))
        self.assertIn("clear_queue", speaker.calls)

    def test_auto_confirm_skips_prompt(self):
        speaker = FakeSpeaker()
        item = SimpleNamespace(title="Nights", artist="Frank Ocean", album="Blonde", item_id="1")
        output = []
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            room_choice="1",
            single_command="play Nights by Frank Ocean",
            auto_confirm=True,
            music_service_factory=lambda room: FakeService([item]),
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=output.append,
        )
        self.assertEqual(app.run(), 0)
        self.assertIn("clear_queue", speaker.calls)

    def test_json_status(self):
        speaker = FakeSpeaker()
        speaker.calls = []
        speaker.current = {"current_transport_state": "PLAYING", "title": "Nights",
                           "artist": "Frank Ocean", "album": "Blonde"}
        speaker.get_current_transport_info = lambda: {"current_transport_state": "PLAYING"}
        speaker.get_current_track_info = lambda: {"title": "Nights", "artist": "Frank Ocean", "album": "Blonde"}
        output = []
        app = Splay(
            discover_fn=lambda timeout: [speaker],
            room_choice="1",
            single_command="status",
            json_output=True,
            input_fn=lambda prompt="": (_ for _ in ()).throw(AssertionError("no input")),
            output_fn=output.append,
        )
        self.assertEqual(app.run(), 0)
        import json as jsonlib
        payload = jsonlib.loads(output[-1])
        self.assertEqual(payload["room"], "Office")
        self.assertEqual(payload["state"], "Playing")
        self.assertEqual(payload["title"], "Nights")
        self.assertEqual(payload["volume"], 32)


if __name__ == "__main__":
    unittest.main()
