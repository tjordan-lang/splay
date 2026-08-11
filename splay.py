#!/usr/bin/env python3
"""A small interactive Sonos remote for TIDAL."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import readline  # noqa: F401  # Enables macOS terminal history for input().
import shlex
import unicodedata
from dataclasses import dataclass, replace as dataclasses_replace
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from soco.discovery import discover as _soco_discover
    from soco.music_services import MusicService
    from soco.plugins.sharelink import ShareLinkPlugin
except ImportError:  # Keep parsing/ranking testable before dependencies are installed.
    _soco_discover = None
    MusicService = None
    ShareLinkPlugin = None

try:
    import tidalapi as _tidalapi
except ImportError:
    _tidalapi = None


class SplayError(Exception):
    """An expected, user-facing error."""


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    uri: str = ""
    item: object = None
    source: str = "search"
    source_url: str = ""
    item_id: str = ""
    kind: str = "track"

    @property
    def version(self) -> str:
        markers = version_markers(f"{self.title} {self.album}")
        return ", ".join(marker.title() for marker in markers) or "Original"


VERSION_MARKERS = (
    "remix",
    "live",
    "sped up",
    "slowed",
    "cover",
    "karaoke",
    "acoustic",
    "instrumental",
    "demo",
    "radio edit",
)


def version_markers(value: str) -> list[str]:
    normalized = normalize(value)
    return [marker for marker in VERSION_MARKERS if normalize(marker) in normalized]


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def parse_query(query: str) -> tuple[str, str]:
    match = re.match(r"^\s*(.+?)\s+by\s+(.+?)\s*$", query, re.IGNORECASE)
    return (match.group(1), match.group(2)) if match else (query.strip(), "")


def parse_command(line: str) -> tuple[str, list[str]]:
    try:
        parts = shlex.split(line)
    except ValueError as error:
        raise SplayError(f"Could not parse command: {error}") from error
    return (parts[0].casefold(), parts[1:]) if parts else ("", [])


def _value(item: object, name: str, default: str = "") -> object:
    if isinstance(item, dict):
        value = item.get(name, default)
    else:
        try:
            value = getattr(item, name)
        except AttributeError:
            metadata = getattr(item, "metadata", {})
            value = metadata.get(name, default) if isinstance(metadata, dict) else default
    return default if value is None else value


def _track_from_item(item: object, source: str = "search", source_url: str = "") -> Track | None:
    title = str(_value(item, "title")).strip()
    if not title:
        return None
    artist = str(_value(item, "artist") or _value(item, "album_artist")).strip()
    album = str(_value(item, "album")).strip()
    item_id = str(_value(item, "item_id") or _value(item, "id")).strip()
    return Track(
        title=title,
        artist=artist,
        album=album,
        uri=str(_value(item, "uri")).strip(),
        item=item,
        source=source,
        source_url=source_url,
        item_id=item_id,
    )


def rank_tracks(items: list[object], query: str) -> list[Track]:
    """Return the short list of plausible, best-matching TIDAL tracks."""
    wanted_title, wanted_artist = parse_query(query)
    title_key = normalize(wanted_title)
    artist_key = normalize(wanted_artist)
    query_key = normalize(query)
    requested_markers = set(version_markers(query))
    scored: list[tuple[float, Track]] = []

    for item in items:
        track = _track_from_item(item)
        can_play = _value(item, "can_play", True)
        if isinstance(can_play, str):
            can_play = can_play.casefold() != "false"
        if track is None or not can_play:
            continue
        title_key_candidate = normalize(track.title)
        artist_key_candidate = normalize(track.artist)
        if title_key:
            score = 100 * difflib.SequenceMatcher(None, title_key, title_key_candidate).ratio()
            if title_key == title_key_candidate:
                score += 100
        else:
            score = 0
        if artist_key:
            score += 60 * difflib.SequenceMatcher(
                None, artist_key, artist_key_candidate
            ).ratio()
            if artist_key == artist_key_candidate:
                score += 60
        else:
            score += 35 * difflib.SequenceMatcher(
                None, query_key, normalize(f"{track.title} {track.artist}")
            ).ratio()
        unwanted = set(version_markers(f"{track.title} {track.album}")) - requested_markers
        score -= 80 * len(unwanted)
        scored.append((score, track))

    if not scored:
        return []
    scored.sort(key=lambda result: result[0], reverse=True)
    top_score = scored[0][0]
    plausible = [track for score, track in scored if score >= max(75, top_score - 15)]
    return plausible[:5] or [scored[0][1]]


def tidal_track_url(value: str) -> tuple[str, str] | None:
    parsed = urlparse(value)
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host != "tidal.com" and not host.endswith(".tidal.com"):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        index = next(index for index, part in enumerate(parts) if part.casefold() == "track")
        track_id = parts[index + 1]
    except (StopIteration, IndexError):
        return None
    if not re.fullmatch(r"[\w-]+", track_id):
        return None
    return f"https://tidal.com/browse/track/{track_id}", track_id


def tidal_album_url(value: str) -> tuple[str, str] | None:
    parsed = urlparse(value)
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host != "tidal.com" and not host.endswith(".tidal.com"):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        index = next(index for index, part in enumerate(parts) if part.casefold() == "album")
        album_id = parts[index + 1]
    except (StopIteration, IndexError):
        return None
    if not re.fullmatch(r"[\w-]+", album_id):
        return None
    return f"https://tidal.com/browse/album/{album_id}", album_id


def _media_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    for key in ("getMediaMetadataResult", "mediaMetadata"):
        if key in value:
            return _media_metadata(value[key])
    return value


class TidalSearch:
    """Search TIDAL through tidalapi. One-time browser login, then session saved."""

    def __init__(self, session_file: Path, login_fn=None, session_factory=None):
        self.session_file = session_file
        self.login_fn = login_fn or print
        self.session_factory = session_factory or _tidalapi.Session
        self.session = None

    def _ensure(self):
        if self.session is not None and self.session.check_login():
            return self.session
        session = self.session_factory()
        self.session_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.session_file.parent.chmod(0o700)
        if self.session_file.is_file():
            self.session_file.chmod(0o600)
            try:
                if session.load_session_from_file(self.session_file) and session.check_login():
                    self.session = session
                    return session
            except Exception:
                session = self.session_factory()
        session.login_oauth_simple(fn_print=self.login_fn)
        session.save_session_to_file(self.session_file)
        self.session_file.chmod(0o600)
        self.session = session
        return session

    def search(self, query: str, limit: int = 25) -> list:
        results = self._ensure().search(query, models=[_tidalapi.Track], limit=limit)
        return list((results or {}).get("tracks") or [])

    def search_albums(self, query: str, limit: int = 25) -> list:
        results = self._ensure().search(query, models=[_tidalapi.Album], limit=limit)
        return list((results or {}).get("albums") or [])

    def track(self, track_id: str) -> object:
        return self._ensure().track(int(track_id))

    def album(self, album_id: str) -> object:
        return self._ensure().album(int(album_id))


def _track_from_tidalapi(item: object) -> Track | None:
    track_id = str(_value(item, "id") or "").strip()
    title = str(_value(item, "name") or _value(item, "title")).strip()
    if not title or not track_id:
        return None
    artist = _value(item, "artist")
    if isinstance(artist, dict):
        artist = str(artist.get("name") or "")
    else:
        artist = str(getattr(artist, "name", artist or "") or "")
    album = _value(item, "album")
    if isinstance(album, dict):
        album = str(album.get("name") or album.get("title") or "")
    else:
        album = str(getattr(album, "name", album or "") or "")
    version = str(_value(item, "version") or "").strip()
    if version:
        title = f"{title} ({version})"
    return Track(
        title=title,
        artist=artist.strip(),
        album=album.strip(),
        source="search",
        source_url=f"https://tidal.com/browse/track/{track_id}",
        item_id=track_id,
    )


def _album_from_tidalapi(item: object) -> Track | None:
    album_id = str(_value(item, "id") or "").strip()
    title = str(_value(item, "name") or "").strip()
    if not title or not album_id:
        return None
    artist = _value(item, "artist")
    if isinstance(artist, dict):
        artist = str(artist.get("name") or "")
    else:
        artist = str(getattr(artist, "name", artist or "") or "")
    return Track(
        title=title,
        artist=artist.strip(),
        album=title,
        kind="album",
        source="search",
        source_url=f"https://tidal.com/browse/album/{album_id}",
        item_id=album_id,
    )


def _tidal_session_from_env(env=None):
    """Create a TIDAL session using the user's developer credentials."""
    env = os.environ if env is None else env
    client_id = str(env.get("TIDAL_CLIENT_ID", "")).strip()
    client_secret = str(env.get("TIDAL_CLIENT_SECRET", "")).strip()
    if not client_id or not client_secret:
        raise SplayError(
            "Set TIDAL_CLIENT_ID and TIDAL_CLIENT_SECRET. "
            "Create them at https://developer.tidal.com/dashboard."
        )
    config = _tidalapi.Config()
    config.client_id = client_id
    config.client_secret = client_secret
    return _tidalapi.Session(config)


def default_tidal_search() -> TidalSearch | None:
    """Build the default TidalSearch if tidalapi is installed."""
    if _tidalapi is None:
        return None
    return TidalSearch(
        Path.home() / ".splay" / "tidal-session.json",
        session_factory=_tidal_session_from_env,
    )


class Splay:
    def __init__(
        self,
        discover_fn=None,
        music_service_factory=None,
        sharelink_factory=None,
        tidal_api=None,
        room_choice: str | None = None,
        single_command: str | None = None,
        auto_confirm: bool = False,
        json_output: bool = False,
        input_fn=None,
        output_fn=None,
        discovery_timeout: float = 3,
    ):
        self.discover_fn = discover_fn or self._discover
        self.music_service_factory = music_service_factory or self._music_service
        self.sharelink_factory = sharelink_factory or self._sharelink
        self.tidal_api = tidal_api
        self.room_choice = room_choice
        self.single_command = single_command
        self.auto_confirm = auto_confirm
        self.json_output = json_output
        self.input_fn = input_fn or input
        self.output_fn = output_fn or print
        self.discovery_timeout = discovery_timeout
        self.rooms: list[object] = []
        self.active_room: object | None = None

    @staticmethod
    def _discover(timeout: float):
        if _soco_discover is None:
            raise SplayError("SoCo is not installed. Run: python3 -m pip install -r requirements.txt")
        return _soco_discover(timeout=timeout) or set()

    @staticmethod
    def _music_service(room: object):
        if MusicService is None:
            raise SplayError("SoCo is not installed. Run: python3 -m pip install -r requirements.txt")
        return MusicService("TIDAL", device=room)

    @staticmethod
    def _sharelink(room: object):
        if ShareLinkPlugin is None:
            raise SplayError("SoCo is not installed. Run: python3 -m pip install -r requirements.txt")
        return ShareLinkPlugin(room)

    def _say(self, message: str = "") -> None:
        self.output_fn(message)

    @staticmethod
    def _room_name(room: object) -> str:
        try:
            name = str(room.player_name).strip()
        except Exception:
            name = ""
        return name or str(getattr(room, "ip_address", "Sonos room"))

    def refresh_rooms(self) -> list[object]:
        rooms = list(self.discover_fn(timeout=self.discovery_timeout) or [])
        self.rooms = sorted(rooms, key=lambda room: (self._room_name(room).casefold(), str(room)))
        self.group_map: dict[str, object | None] = {}
        for room in self.rooms:
            try:
                group = getattr(room, "group", None)
            except Exception:
                group = None
            self.group_map[str(room)] = group
        return self.rooms

    def _room_label(self, room: object) -> str:
        name = self._room_name(room)
        group = self.group_map.get(str(room))
        if group is None:
            return name
        others = sorted(
            self._room_name(member)
            for member in getattr(group, "members", []) or []
            if member is not room
        )
        if not others:
            return name
        return f"{name} (+{', '.join(others)})"

    def _transport_room(self) -> object:
        """The group coordinator for the active room, or the room itself."""
        group = self.group_map.get(str(self.active_room))
        coordinator = getattr(group, "coordinator", None)
        return coordinator or self.active_room

    def _show_rooms(self, header: str = "Available Sonos rooms:") -> None:
        self._say(header)
        if not self.rooms:
            self._say("No Sonos rooms found. Check that the speakers and Mac are on the same network.")
            return
        for index, room in enumerate(self.rooms, 1):
            self._say(f"{index}. {self._room_label(room)}")

    def choose_room(self) -> object:
        if not self.rooms:
            self.refresh_rooms()
        if not self.rooms:
            raise SplayError("No Sonos rooms found. Check that the speakers and Mac are on the same network.")
        self._show_rooms("Choose a Sonos room:")
        while True:
            choice = self.input_fn("> ").strip()
            if choice.casefold() in {"exit", "quit"}:
                raise EOFError
            if choice.isdigit() and 1 <= int(choice) <= len(self.rooms):
                self.active_room = self.rooms[int(choice) - 1]
                self._say(f"Using {self._room_label(self.active_room)}.")
                return self.active_room
            by_name = next(
                (room for room in self.rooms if self._room_name(room).casefold() == choice.casefold()),
                None,
            )
            if by_name is not None:
                self.active_room = by_name
                self._say(f"Using {self._room_label(self.active_room)}.")
                return by_name
            self._say("Enter a room number or name.")

    def _choose_room_value(self, value: str) -> object:
        """Select a room by number or name; used by --room."""
        if not self.rooms:
            raise SplayError("No Sonos rooms found. Check that the speakers and Mac are on the same network.")
        value = value.strip()
        if value.isdigit() and 1 <= int(value) <= len(self.rooms):
            self.active_room = self.rooms[int(value) - 1]
            return self.active_room
        by_name = next(
            (room for room in self.rooms if self._room_name(room).casefold() == value.casefold()),
            None,
        )
        if by_name is not None:
            self.active_room = by_name
            return by_name
        names = ", ".join(self._room_name(room) for room in self.rooms)
        raise SplayError(f"No Sonos room named “{value}”. Rooms: {names}")

    def run(self) -> int:
        try:
            self.refresh_rooms()
            if self.room_choice is not None:
                self._choose_room_value(self.room_choice)
            else:
                self.choose_room()
        except EOFError:
            self._say("Goodbye.")
            return 0
        except KeyboardInterrupt:
            self._say("^C")
            return 0
        except Exception as error:
            self._report(error)
            return 1

        if self.single_command is not None:
            try:
                self.handle_command(self.single_command)
            except KeyboardInterrupt:
                self._say("^C")
            except Exception as error:
                self._report(error)
                return 1
            return 0

        while True:
            try:
                line = self.input_fn("splay> ")
            except EOFError:
                self._say("Goodbye.")
                return 0
            except KeyboardInterrupt:
                self._say("^C")
                continue
            if not line.strip():
                continue
            try:
                if self.handle_command(line):
                    return 0
            except KeyboardInterrupt:
                self._say("^C")
            except Exception as error:
                self._report(error)

    def handle_command(self, line: str) -> bool:
        command, args = parse_command(line)
        if command in {"exit", "quit"}:
            self._say("Goodbye.")
            return True
        if command == "help":
            self._help()
        elif command == "play":
            if not args:
                raise SplayError("Usage: play <song and artist>")
            self._play_command(" ".join(args))
        elif command == "add":
            if not args:
                raise SplayError("Usage: add <song and artist>")
            as_next = args[0].casefold() == "next"
            rest = args[1:] if as_next else args
            if not rest:
                raise SplayError("Usage: add [next] <song and artist>")
            self._add_command(" ".join(rest), as_next=as_next)
        elif command == "pause":
            self._transport_room().pause()
            self._say("Paused.")
        elif command == "resume" and not args:
            self._transport_room().play()
            self._say("Playing.")
        elif command == "stop":
            self._transport_room().stop()
            self._say("Stopped.")
        elif command == "next":
            self._transport_room().next()
            self._say("Next track.")
        elif command == "previous":
            self._transport_room().previous()
            self._say("Previous track.")
        elif command == "volume":
            self._volume_command(args)
        elif command == "mute":
            self.active_room.mute = True
            self._say("Muted.")
        elif command == "unmute":
            self.active_room.mute = False
            self._say("Unmuted.")
        elif command == "status":
            self._status()
        elif command == "queue":
            self._queue()
        elif command == "rooms":
            self.refresh_rooms()
            if self.json_output:
                self._say(json.dumps([
                    {
                        "name": self._room_name(room),
                        "group": self._room_label(room),
                    }
                    for room in self.rooms
                ], ensure_ascii=False))
            else:
                self._show_rooms()
        elif command == "room":
            self.refresh_rooms()
            self.choose_room()
        elif command:
            self._say("Unknown command. Type help.")
        return False

    def _help(self) -> None:
        self._say("Commands:")
        self._say("  play <song and artist>   add [next] <song and artist>   pause   resume   stop")
        self._say("  next   previous   volume <0-100|up|down>   mute   unmute")
        self._say("  status   queue   room   rooms   help   exit")

    def _volume_command(self, args: list[str]) -> None:
        if len(args) != 1:
            raise SplayError("Usage: volume <0-100|up|down>")
        if args[0].casefold() == "up":
            volume = self.active_room.set_relative_volume(5)
        elif args[0].casefold() == "down":
            volume = self.active_room.set_relative_volume(-5)
        else:
            try:
                volume = int(args[0])
            except ValueError as error:
                raise SplayError("Volume must be 0-100, up, or down.") from error
            if not 0 <= volume <= 100:
                raise SplayError("Volume must be between 0 and 100.")
            self.active_room.volume = volume
        if volume is None:
            volume = self.active_room.volume
        self._say(f"Volume {volume}.")

    def _status(self) -> None:
        room = self._transport_room()
        transport = room.get_current_transport_info()
        track = room.get_current_track_info()
        state = str(transport.get("current_transport_state", "")).replace("_", " ").title()
        volume = room.volume
        muted = "Muted" if room.mute else "Unmuted"
        if self.json_output:
            self._say(json.dumps({
                "room": self._room_name(self.active_room),
                "group": self._room_label(self.active_room),
                "volume": volume,
                "muted": room.mute,
                "state": state,
                "title": str(track.get("title") or ""),
                "artist": str(track.get("artist") or ""),
                "album": str(track.get("album") or ""),
            }, ensure_ascii=False))
            return
        self._say(f"{self._room_label(self.active_room)} · Volume {volume} · {muted}")
        if track.get("title"):
            artist = f" — {track.get('artist')}" if track.get("artist") else ""
            self._say(f"{state}: “{track['title']}”{artist}")
            if track.get("album"):
                self._say(f"Album: {track['album']}")
        else:
            self._say(state or "Nothing playing.")

    def _queue(self) -> None:
        queue = list(self.active_room.get_queue(max_items=20))
        if not queue:
            self._say("Queue is empty.")
            return
        if self.json_output:
            self._say(json.dumps([
                {
                    "title": str(_value(item, "title", "Untitled")),
                    "artist": str(_value(item, "artist") or _value(item, "creator") or ""),
                }
                for item in queue
            ], ensure_ascii=False))
            return
        for index, item in enumerate(queue, 1):
            title = str(_value(item, "title", "Untitled"))
            artist = str(_value(item, "artist") or _value(item, "creator")).strip()
            suffix = f" — {artist}" if artist else ""
            self._say(f"{index}. {title}{suffix}")

    def _enqueue(self, room: object, track: Track, as_next: bool = False) -> int:
        if track.source_url:
            return self.sharelink_factory(room).add_share_link_to_queue(
                track.source_url, as_next=as_next
            )
        return room.add_to_queue(track.item, as_next=as_next)

    def _play_command(self, query: str) -> None:
        track = self._resolve(query)
        if track is None:
            return
        room = self._transport_room()
        if track.kind == "album":
            self._say(f"Album: “{track.title}” — {track.artist}")
        else:
            self._say(f"Track: “{track.title}” — {track.artist}")
            self._say(f"Album: {track.album or 'Unknown'}")
            self._say(f"Version: {track.version}")
        self._say(f"Room: {self._room_label(self.active_room)}")
        room.clear_queue()
        queue_number = self._enqueue(room, track)
        room.play_from_queue(max(0, int(queue_number) - 1))
        if track.kind == "album":
            self._say(f"Playing album: “{track.title}” — {track.artist}")
        else:
            self._say(f"Playing “{track.title}” — {track.artist}")
        try:
            self._say(f"{self._room_label(self.active_room)} · Volume {room.volume}")
        except Exception:
            self._say(f"{self._room_label(self.active_room)}")

    def _add_command(self, query: str, as_next: bool = False) -> None:
        track = self._resolve(query)
        if track is None:
            return
        room = self._transport_room()
        if track.kind == "album":
            self._say(f"Album: “{track.title}” — {track.artist}")
        else:
            self._say(f"Track: “{track.title}” — {track.artist}")
            self._say(f"Album: {track.album or 'Unknown'}")
            self._say(f"Version: {track.version}")
        self._say(f"Room: {self._room_label(self.active_room)}")
        self._enqueue(room, track, as_next=as_next)
        if track.kind == "album":
            self._say(
                f"Added album {'' if not as_next else 'next: '}“{track.title}” — {track.artist}."
            )
        else:
            self._say(f"Added {'' if not as_next else 'next: '}“{track.title}” — {track.artist}.")

    def _resolve(self, query: str) -> Track | None:
        track_url = tidal_track_url(query)
        album_url = tidal_album_url(query)
        if self.tidal_api is not None:
            return self._resolve_via_api(query, track_url, album_url)
        service = self.music_service_factory(self.active_room)
        if track_url:
            return self._resolve_url(service, *track_url)
        if album_url or query.casefold().startswith("album "):
            raise SplayError(
                "Album playback requires the TIDAL login. Run without it or use a track URL."
            )
        if query.startswith(("http://", "https://")):
            raise SplayError("Only TIDAL track URLs are supported.")
        result = self._with_auth_retry(
            service, lambda: service.search("tracks", term=query, count=25)
        )
        candidates = rank_tracks(list(result or []), query)
        if not candidates:
            self._say(f'No TIDAL track found for “{query}”.')
            return None
        return self._choose_or_auto(candidates)

    def _choose_or_auto(self, candidates: list[Track]) -> Track:
        """Dedupe; auto-pick the top result in scripted mode, else show a chooser."""
        seen: set[str] = set()
        unique: list[Track] = []
        for track in candidates:
            key = track.item_id or f"{track.title}\x00{track.artist}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(track)
        if self.auto_confirm:
            picked = unique[0]
            self._say(f"Auto-picked: “{picked.title}” — {picked.artist}")
            return picked
        if len(unique) == 1:
            return unique[0]
        self._say("Choose a track:")
        for index, track in enumerate(unique, 1):
            album = f" · {track.album}" if track.album else ""
            self._say(f"{index}. “{track.title}” — {track.artist}{album}")
        while True:
            choice = self.input_fn("> ").strip()
            if not choice:
                self._say("Cancelled.")
                return None
            if choice.isdigit() and 1 <= int(choice) <= len(unique):
                return unique[int(choice) - 1]
            self._say("Enter a track number, or press Return to cancel.")

    @staticmethod
    def _is_auth_error(error: Exception) -> bool:
        name = error.__class__.__name__.casefold()
        message = str(error).strip().casefold()
        return (
            "auth" in name
            or "authentication" in message
            or "logininvalid" in message
            or "not logged" in message
        )

    def _link_tidal(self, service: object) -> bool:
        """Run SoCo's one-time TIDAL device-link flow; return True once linked."""
        try:
            reg_url = str(service.begin_authentication())
            link_code = str(getattr(service, "link_code", ""))
        except Exception as error:
            self._report(error)
            return False
        self._say("splay needs its own TIDAL authorization (separate from the Sonos app):")
        self._say(f"1. Open: {reg_url}")
        self._say(f"2. Sign in to TIDAL and enter link code: {link_code}")
        self._say("3. Authorize, then press Return here.")
        try:
            self.input_fn("Press Return after authorizing... ")
        except (EOFError, KeyboardInterrupt):
            self._say("Cancelled.")
            return False
        try:
            service.complete_authentication()
        except Exception as error:
            self._report(error)
            return False
        self._say("TIDAL authorized for splay.")
        return True

    def _with_auth_retry(self, service: object, func) -> object:
        try:
            return func()
        except Exception as error:
            if not self._is_auth_error(error):
                raise
            if not self._link_tidal(service):
                raise SplayError(
                    "TIDAL authorization did not complete; try the command again."
                ) from error
            return func()

    def _resolve_via_api(
        self,
        query: str,
        track_url: tuple[str, str] | None,
        album_url: tuple[str, str] | None,
    ) -> Track | None:
        if track_url:
            _, track_id = track_url
            try:
                item = self.tidal_api.track(track_id)
            except SplayError:
                raise
            except Exception as error:
                raise SplayError(f"TIDAL error: {error}") from error
            track = _track_from_tidalapi(item)
            if track is None:
                raise SplayError(f"TIDAL has no track with id {track_id}.")
            return track
        if album_url:
            _, album_id = album_url
            try:
                item = self.tidal_api.album(album_id)
            except SplayError:
                raise
            except Exception as error:
                raise SplayError(f"TIDAL error: {error}") from error
            track = _album_from_tidalapi(item)
            if track is None:
                raise SplayError(f"TIDAL has no album with id {album_id}.")
            return track
        if query.startswith(("http://", "https://")):
            raise SplayError("Only TIDAL track or album URLs are supported.")
        if query.casefold().startswith("album "):
            album_query = query[6:].strip()
            if not album_query:
                raise SplayError("Usage: play album <album name>")
            try:
                items = self.tidal_api.search_albums(album_query)
            except SplayError:
                raise
            except Exception as error:
                raise SplayError(f"TIDAL error: {error}") from error
            candidates = rank_tracks([_album_from_tidalapi(item) for item in items], album_query)
            candidates = [
                dataclasses_replace(
                    track,
                    kind="album",
                    source_url=f"https://tidal.com/browse/album/{track.item_id}",
                )
                for track in candidates
            ]
            if not candidates:
                self._say(f'No TIDAL album found for “{album_query}”.')
                return None
            return self._choose_or_auto(candidates)
        try:
            items = self.tidal_api.search(query)
        except SplayError:
            raise
        except Exception as error:
            raise SplayError(f"TIDAL error: {error}") from error
        candidates = rank_tracks([_track_from_tidalapi(item) for item in items], query)
        candidates = [
            dataclasses_replace(
                track, source_url=f"https://tidal.com/browse/track/{track.item_id}"
            )
            for track in candidates
        ]
        if not candidates:
            self._say(f'No TIDAL track found for “{query}”.')
            return None
        return self._choose_or_auto(candidates)

    def _resolve_url(self, service: object, canonical_url: str, track_id: str) -> Track:
        last_error: Exception | None = None
        for item_id in (f"track:{track_id}", f"tidal:track:{track_id}", track_id):
            try:
                payload = _media_metadata(
                    self._with_auth_retry(
                        service, lambda: service.get_media_metadata(item_id)
                    )
                )
            except Exception as error:
                last_error = error
                continue
            title = str(payload.get("title", "")).strip()
            metadata = payload.get("trackMetadata") or {}
            artist = str(_value(metadata, "artist") or payload.get("artist", "")).strip()
            album = str(_value(metadata, "album") or payload.get("album", "")).strip()
            if title:
                return Track(
                    title=title,
                    artist=artist,
                    album=album,
                    source="url",
                    source_url=canonical_url,
                    item_id=str(payload.get("id", track_id)),
                )
        if last_error is not None:
            raise last_error
        raise SplayError("TIDAL did not return metadata for that track URL.")

    def _report(self, error: Exception) -> None:
        name = error.__class__.__name__.casefold()
        message = str(error).strip()
        detail = message.casefold()
        if (
            "auth" in name
            or "authentication" in detail
            or "logininvalid" in detail
            or "not logged" in detail
        ):
            self._say("TIDAL authorization failed. Run a play command to start the link flow.")
        elif "unknown music service" in message.casefold():
            self._say("TIDAL is not available on this Sonos system. Add TIDAL in the Sonos app.")
        elif isinstance(error, SplayError):
            self._say(message)
        else:
            self._say(f"Sonos error: {message or name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="splay",
        description="Interactive TIDAL remote for Sonos speakers.",
    )
    parser.add_argument("--room", metavar="NAME", help="Sonos room name or number (skips the picker)")
    parser.add_argument("-c", "--command", metavar="CMD", help="run one command, then exit")
    parser.add_argument("-y", "--yes", action="store_true", help="automatically use the top search result")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="print status/rooms/queue output as JSON")
    args = parser.parse_args(argv)
    return Splay(
        tidal_api=default_tidal_search(),
        room_choice=args.room,
        single_command=args.command,
        auto_confirm=args.yes,
        json_output=args.json_output,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
