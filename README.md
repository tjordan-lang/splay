# Splay

Splay is a small interactive TIDAL remote for Sonos speakers on macOS. It discovers Sonos rooms on your local network, searches TIDAL, and controls playback from the terminal.

## Requirements

- macOS and Python 3.10 or newer
- Sonos speakers on the same local network
- TIDAL linked to your Sonos system
- A TIDAL Developer app with your own Client ID and Client Secret

Create an app in the [TIDAL Developer Dashboard](https://developer.tidal.com/dashboard), then copy its Client ID and Client Secret.

## Install

```sh
git clone https://github.com/tjordan-lang/splay.git
cd splay
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Set your TIDAL developer credentials in the shell where you run Splay:

```sh
export TIDAL_CLIENT_ID="your-client-id"
export TIDAL_CLIENT_SECRET="your-client-secret"
```

Do not commit these values or put them in a tracked file.

## Run

```sh
splay
```

The first TIDAL search prints a login URL. Open it, sign in to TIDAL, and authorize your own developer app. Splay stores the resulting session locally at `~/.splay/tidal-session.json` with user-only permissions.

Commands:

- `play <song and artist>`
- `play album <album name>`
- `play <TIDAL track or album URL>`
- `add [next] <song or album>`
- `pause`, `resume`, `stop`, `next`, `previous`
- `volume <0-100|up|down>`, `mute`, `unmute`
- `status`, `queue`, `room`, `rooms`, `help`, `exit`

When a search has multiple plausible results, interactive mode presents a chooser. Albums are queued as a single Sonos album container.

## Scriptable mode

```sh
splay --room Office -c "status" --json
splay --room Office -c "play Nights by Frank Ocean" -y
splay -c "rooms" --json
splay --room 2
```

- `--room NAME|NUMBER` skips the room picker.
- `-c CMD` runs one command and exits.
- `-y` automatically uses the top search result.
- `--json` prints status, rooms, or queue output as JSON.

## Development

```sh
python -m unittest -v
```

The unit tests use fake Sonos and TIDAL clients and do not control real speakers.

## Privacy and third-party services

Splay has no telemetry and does not operate a hosted service. Credentials and OAuth tokens remain on the computer running Splay. Network requests go to the local Sonos system and TIDAL.

Splay is an independent project and is not affiliated with or endorsed by TIDAL or Sonos. TIDAL metadata is provided by TIDAL, and playback is performed by the user's Sonos system through its linked TIDAL service. Use of those services remains subject to their respective terms. The `tidalapi` dependency is an unofficial TIDAL API client.
