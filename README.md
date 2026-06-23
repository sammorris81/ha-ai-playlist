# AI Playlist

AI-powered playlist generation for Home Assistant + Music Assistant.

Uses any AI Task provider (Anthropic, OpenAI, Google, Ollama, etc.) to generate tracks based on text prompts, enqueue them on Music Assistant speakers, and automatically refill as you listen.

## AI Assisted 

Full disclosure, although this was originally a set of scripts, automations and pyscript I wrote myself, in it's current form it's probably 80% AI coded using Claude Code.  

## Prerequisites

- **Home Assistant 2025.11+** (AI Task requires 2025.8; config-flow reconfigure uses APIs added in 2025.11)
- **[Music Assistant](https://music-assistant.io/)** integration installed and configured
- **An AI Task entity** — configure an AI integration (e.g., Anthropic, OpenAI, Ollama) and enable its AI Task sub-entry

## Installation

1. Open HACS in Home Assistant
2. Click the three-dot menu (top right) → **Custom repositories**
3. Add repository URL: `https://github.com/rjbutler/ha-ai-playlist`
4. Category: **Integration**
5. Click **Add**, then find "AI Playlist" in HACS and click **Download**
6. Restart Home Assistant

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for "AI Playlist"
3. Select your AI Task entity (e.g., `ai_task.claude_ai_task_sonnet`)

## Configuration

Open the integration's options (three-dot menu → **Configure**) to manage:

### Playlists

Named prompt + settings combinations. Each playlist has:

| Setting | Default | Description |
|---|---|---|
| Name | — | Display name (used in service calls) |
| Prompt | — | Instructions for the AI (e.g., "Upbeat jazz fusion, heavy on the Rhodes piano") |
| Track count | 10 | Tracks generated per AI call |
| History depth | 50 | Tracks remembered to avoid repeats |
| Refill threshold | 2 | Remaining tracks before auto-refill triggers |
| Exclude live | off | Filter out live recordings.  How well this works depends on the AI task model, some are better than others.  They still sometimes slip through. |

You can also **import playlists from YAML** — paste a YAML list into the import form. See [examples/](examples/) for ready-to-import files.

#### Import YAML format

```yaml
- name: Classic Rock Foundations        # required — display name
  prompt: >                             # required — instructions for the AI
    Generate a playlist of classic rock songs from artists such as
    The Beatles, Led Zeppelin, The Who. Include similar artists.
  tags: [genre, rock]                   # optional — for collection filtering
  track_count: 10                       # optional (default: 10)
  history_depth: 50                     # optional (default: 50)
  refill_threshold: 2                   # optional (default: 2)
  exclude_live: true                    # optional (default: false)
```

Only `name` and `prompt` are required — everything else uses defaults if omitted. The format matches the internal storage structure, so you can export playlists from `.storage/ai_playlist.playlists` and convert them directly.

### Playlist Collections

A collection is a named group of playlists filtered by tags, exposed as a Home Assistant `select` entity. Use collections to organize playlists into categories for dashboards, physical controls, or automations.

**Why use collections?** If you have dozens of playlists, you probably don't want a single massive dropdown. Collections let you group playlists by tag (e.g., "Genre", "Mood", "Classical") so each dropdown shows only the relevant subset.

**How they work:** Create a collection in the options flow by giving it a name and comma-separated tags. The integration creates a `select.ai_playlist_*` entity whose options are all playlists matching those tags. When a user picks a playlist from the dropdown, it stages that selection — call `ai_playlist.play` (without a `playlist` argument) to start playback.

**Important:** Changing the dropdown selection only stages the playlist. It does not automatically start playback. You need a separate trigger (button press, automation, script) that calls `ai_playlist.play`.

| Setting | Description |
|---|---|
| Name | Display name (becomes the entity name) |
| Tags | Comma-separated tags — the dropdown shows playlists matching ALL tags |

### System Prompt

The instructions sent to the AI for track generation. The default prompt requests JSON output (`[{"artist": "...", "title": "...", "album": "..."}]`), plus uniqueness and diversity rules. Customize it from the options flow. If a custom prompt produces plain text instead of JSON, the parser will fall back to line-based `Artist - Title | Album` parsing automatically.

## Services

### `ai_playlist.play`

Start an AI-generated playlist on a media player.

| Field | Required | Description |
|---|---|---|
| `entity_id` | yes | Media player to play on |
| `playlist` | no | Name of a configured playlist |
| `prompt` | no | Ad-hoc prompt (used if no playlist specified) |
| `track_count` | no | Override tracks per generation |
| `clear_queue` | no | Clear existing queue first (default: true) |
| `collection` | no | Collection name for state tracking |
| `ai_entity` | no | Override the configured AI Task entity for this session. Applies to the initial generation and all refills. Not persisted across HA restarts. |

If neither `playlist` nor `prompt` is provided, plays the currently selected playlist (set via `ai_playlist.select`).

### `ai_playlist.stop`

Stop managing a media player. Caches unplayed tracks for next session.

| Field | Required | Description |
|---|---|---|
| `entity_id` | yes | Media player to stop managing |

### `ai_playlist.select`

Set the "up next" playlist for a media player without starting playback.

| Field | Required | Description |
|---|---|---|
| `entity_id` | yes | Media player |
| `playlist` | yes | Playlist name to select |
| `collection` | no | Collection name |

### `ai_playlist.clear_history`

Reset track history for a playlist (allows previously played tracks to be generated again).

| Field | Required | Description |
|---|---|---|
| `playlist` | yes | Playlist name |

### `ai_playlist.list_playlists`

Returns all configured playlists. Supports response data.

| Field | Required | Description |
|---|---|---|
| `tag` | no | Filter by single tag |
| `tags` | no | Filter by multiple tags (AND logic) |

### `ai_playlist.generate`

Run the AI generation pipeline and return the track list as service response data. Does not enqueue anything on a media player. Reads playlist history for dedup but does not write to it. One of `playlist` or `prompt` is required.

| Field | Required | Description |
|---|---|---|
| `playlist` | no | Name of a configured playlist |
| `prompt` | no | Ad-hoc prompt (used if no playlist specified) |
| `track_count` | no | Override tracks per generation |
| `ai_entity` | no | Override the configured AI Task entity for this call |

Response:

```yaml
tracks:
  - artist: "Miles Davis"
    title: "So What"
    album: "Kind of Blue"
```

## Examples

### Play a playlist on a speaker

The simplest use — play a named playlist on a specific media player:

```yaml
# Script: Morning music
sequence:
  - action: ai_playlist.play
    data:
      entity_id: media_player.kitchen_speaker
      playlist: "Baroque Instrumental"
      clear_queue: true
```

### Dashboard: collection dropdown + play button

Use a collection select entity as a dashboard dropdown, paired with a script that plays whatever is selected:

```yaml
# Script: Play selected playlist
sequence:
  - variables:
      playlist_name: "{{ states('select.ai_playlist_genre_playlists') }}"
  - action: ai_playlist.play
    data:
      entity_id: media_player.office_speaker
      playlist: "{{ playlist_name }}"
      collection: "Genre Playlists"
      clear_queue: true
```

Add the select entity and a button to your dashboard:

```yaml
# Dashboard card (minimal example)
type: entities
entities:
  - entity: select.ai_playlist_genre_playlists
  - type: button
    name: Play
    tap_action:
      action: perform-action
      perform_action: script.play_selected_playlist
```

### Random pick from a collection

Pick a random playlist from a collection and play it — useful for physical buttons or "surprise me" automations:

```yaml
# Script: Play random playlist from a collection
sequence:
  - variables:
      options: "{{ state_attr('select.ai_playlist_genre_playlists', 'options') | list }}"
      random_pick: "{{ options | random }}"
  - action: ai_playlist.select
    data:
      entity_id: media_player.office_speaker
      playlist: "{{ random_pick }}"
      collection: "Genre Playlists"
  - action: ai_playlist.play
    data:
      entity_id: media_player.office_speaker
      clear_queue: true
```

## How It Works

1. **Generate** — Sends your prompt + exclusion list to the AI Task entity
2. **Parse** — Extracts structured tracks from the AI response (JSON array preferred, with automatic fallback to `Artist - Title | Album` line parsing)
3. **Filter** — Removes duplicates against history and current queue
4. **Enqueue** — Sends tracks to Music Assistant via `play_media`
5. **Monitor** — Watches queue depth and auto-refills when tracks are running low
6. **Detach** — Stops managing if shuffle/repeat is enabled or queue is cleared externally

Sessions survive HA restarts — the integration resurrects active coordinators on startup.

## Data Sent to the AI Provider

Every generation and refill sends the following to the AI Task entity you've configured:

- The system prompt (default or your customized version)
- Your playlist prompt (or the ad-hoc prompt passed to `ai_playlist.play` / `ai_playlist.generate`)
- The track history for that playlist (artist + title strings, up to `history_depth`) and the currently-enqueued tracks, used as an exclusion list to prevent repeats

No media-player state, account info, or other Home Assistant data is sent. Choose your AI provider accordingly — local options like Ollama keep everything on your network.

## License

MIT
