"""Persistence layer for AI Playlist integration.

Manages playlist configurations (via HA Store) and per-playlist
track history + cache (via JSON files).
"""
import asyncio
import json
import logging
import os
import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_HISTORY_DEPTH,
    DEFAULT_REFILL_THRESHOLD,
    DEFAULT_TRACK_COUNT,
    STORAGE_KEY_PLAYLISTS,
    STORAGE_KEY_SESSIONS,
    STORAGE_VERSION,
)
from .track_processing import normalize_track

_LOGGER = logging.getLogger(__name__)


class PlaylistStore:
    """Manages playlist configs and track history persistence."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_PLAYLISTS)
        self._sessions_store = Store(hass, STORAGE_VERSION, STORAGE_KEY_SESSIONS)
        self._playlists: dict[str, dict] = {}
        self._active_sessions: dict[str, dict] = {}  # entity_id -> {playlist_name, collection_name}
        self._known_players: list[str] = []
        self._history_dir = os.path.join(hass.config.path(".storage"), "ai_playlist", "history")
        self._history_locks: dict[str, asyncio.Lock] = {}

    def _history_lock(self, playlist_name: str) -> asyncio.Lock:
        """Return (creating if needed) the asyncio.Lock for a playlist's history file."""
        slug = self._playlist_slug(playlist_name)
        lock = self._history_locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self._history_locks[slug] = lock
        return lock

    async def async_load(self) -> None:
        """Load playlist configs and session state from HA storage."""
        data = await self._store.async_load()
        if data and "playlists" in data:
            self._playlists = data["playlists"]
        else:
            self._playlists = {}

        sessions_data = await self._sessions_store.async_load()
        if sessions_data:
            self._active_sessions = sessions_data.get("sessions", {})
            self._known_players = sessions_data.get("known_players", [])

    async def _async_save(self) -> None:
        """Save playlist configs to HA storage."""
        await self._store.async_save({"playlists": self._playlists})

    async def _save_sessions(self) -> None:
        """Save session state to HA storage."""
        await self._sessions_store.async_save({
            "sessions": self._active_sessions,
            "known_players": self._known_players,
        })

    # --- Playlist config CRUD ---

    def get_playlist(self, name: str) -> dict | None:
        """Get a playlist config by name."""
        slug = self._playlist_slug(name)
        return self._playlists.get(slug)

    def get_all_playlists(self) -> dict[str, dict]:
        """Get all playlist configs."""
        return dict(self._playlists)

    async def async_save_playlist(self, name: str, config: dict) -> None:
        """Save or update a playlist config.

        Raises ValueError if `name` slugifies to a slug already used by a
        different playlist (e.g. "Rock & Roll" vs "Rock Roll" both → "rock_roll").
        """
        slug = self._playlist_slug(name)
        existing = self._playlists.get(slug)
        if existing and existing.get("name") != name:
            raise ValueError(
                f"Playlist name '{name}' collides with existing playlist "
                f"'{existing.get('name')}' (both produce slug '{slug}'). "
                f"Pick a more distinct name."
            )
        self._playlists[slug] = {
            "name": name,
            "prompt": config.get("prompt", ""),
            "track_count": config.get("track_count", DEFAULT_TRACK_COUNT),
            "history_depth": config.get("history_depth", DEFAULT_HISTORY_DEPTH),
            "refill_threshold": config.get("refill_threshold", DEFAULT_REFILL_THRESHOLD),
            "exclude_live": config.get("exclude_live", False),
            "tags": config.get("tags", []),
        }
        await self._async_save()

    def get_playlists_by_tag(self, tag: str) -> list[dict]:
        """Get all playlists matching a tag (case-insensitive)."""
        tag_lower = tag.lower()
        return [
            p for p in self._playlists.values()
            if tag_lower in [t.lower() for t in p.get("tags", [])]
        ]

    def get_playlists_by_tags(self, tags: list[str]) -> list[dict]:
        """Return playlists matching ALL specified tags (AND logic, case-insensitive)."""
        if not tags:
            return list(self._playlists.values())
        result = []
        for config in self._playlists.values():
            playlist_tags = [t.lower() for t in config.get("tags", [])]
            if all(t.lower() in playlist_tags for t in tags):
                result.append(config)
        return result

    async def async_delete_playlist(self, name: str) -> None:
        """Delete a playlist config."""
        slug = self._playlist_slug(name)
        self._playlists.pop(slug, None)
        await self._async_save()

    async def async_import_playlists(self, yaml_text: str) -> tuple[int, int]:
        """Import playlists from YAML text. Returns (imported, skipped)."""
        import yaml

        try:
            items = yaml.safe_load(yaml_text)
        except yaml.YAMLError as err:
            _LOGGER.warning("YAML parse error during playlist import: %s", err)
            return 0, 0

        if not isinstance(items, list):
            _LOGGER.warning(
                "Playlist import expected a YAML list, got %s", type(items).__name__
            )
            return 0, 0

        imported = 0
        skipped = 0
        for item in items:
            if not isinstance(item, dict) or "name" not in item or "prompt" not in item:
                skipped += 1
                continue
            try:
                await self.async_save_playlist(item["name"], item)
            except ValueError as err:
                _LOGGER.warning("Skipping playlist import: %s", err)
                skipped += 1
                continue
            imported += 1

        return imported, skipped

    # --- Track history ---

    def _history_path(self, playlist_name: str) -> str:
        slug = self._playlist_slug(playlist_name)
        return os.path.join(self._history_dir, f"{slug}.json")

    def _load_history_data(self, playlist_name: str) -> dict:
        path = self._history_path(playlist_name)
        if not os.path.isfile(path):
            return {"tracks": [], "unplayed_cache": []}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("tracks", [])
            data.setdefault("unplayed_cache", [])
            return data
        except (json.JSONDecodeError, OSError):
            return {"tracks": [], "unplayed_cache": []}

    def _save_history_data(self, playlist_name: str, data: dict) -> None:
        os.makedirs(self._history_dir, exist_ok=True)
        path = self._history_path(playlist_name)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    def get_history(self, playlist_name: str) -> list[str]:
        """Get track history for a playlist."""
        return self._load_history_data(playlist_name)["tracks"]

    def add_to_history(
        self, playlist_name: str, track: str, max_tracks: int | None = None
    ) -> None:
        """Add a track to history. Dedupes and enforces FIFO limit."""
        if max_tracks is None:
            config = self.get_playlist(playlist_name)
            max_tracks = config.get("history_depth", DEFAULT_HISTORY_DEPTH) if config else DEFAULT_HISTORY_DEPTH

        data = self._load_history_data(playlist_name)
        tracks = data["tracks"]

        # Remove existing normalized match (keeps most recent at end)
        new_norm = normalize_track(track)
        if new_norm:
            tracks = [t for t in tracks if normalize_track(t) != new_norm]

        # FIFO eviction
        if len(tracks) >= max_tracks:
            tracks = tracks[len(tracks) - max_tracks + 1 :]

        tracks.append(track)
        data["tracks"] = tracks
        self._save_history_data(playlist_name, data)

    def clear_history(self, playlist_name: str) -> None:
        """Clear track history (preserves cache)."""
        data = self._load_history_data(playlist_name)
        data["tracks"] = []
        self._save_history_data(playlist_name, data)

    # --- Unplayed cache ---

    def save_cache(self, playlist_name: str, tracks: list[str]) -> None:
        """Save unplayed tracks to cache."""
        data = self._load_history_data(playlist_name)
        data["unplayed_cache"] = tracks
        self._save_history_data(playlist_name, data)

    def get_cache(self, playlist_name: str) -> list[str]:
        """Get cached tracks, filtered against history. Clears cache after read."""
        data = self._load_history_data(playlist_name)
        cached = data.get("unplayed_cache", [])
        if not cached:
            return []

        # Filter against history
        from .track_processing import tracks_match
        history = data.get("tracks", [])
        filtered = [
            t for t in cached
            if not any(tracks_match(t, h) for h in history)
        ]

        # Clear cache after retrieval
        data["unplayed_cache"] = []
        self._save_history_data(playlist_name, data)

        return filtered

    # --- Session persistence ---

    async def set_active_session(
        self, entity_id: str, playlist_name: str, collection_name: str | None = None
    ) -> None:
        """Record an active coordinator session for resurrection after restart."""
        self._active_sessions[entity_id] = {
            "playlist_name": playlist_name,
            "collection_name": collection_name,
        }
        if entity_id not in self._known_players:
            self._known_players.append(entity_id)
        await self._save_sessions()

    async def clear_active_session(self, entity_id: str) -> None:
        """Remove a session record (coordinator stopped normally)."""
        self._active_sessions.pop(entity_id, None)
        await self._save_sessions()

    def get_active_sessions(self) -> dict[str, dict]:
        """Return all persisted active sessions."""
        return dict(self._active_sessions)

    def get_known_players(self) -> list[str]:
        """Return all entity IDs that have ever had a coordinator session."""
        return list(self._known_players)

    def get_cache_peek(self, playlist_name: str) -> list[str]:
        """Get cached tracks without clearing them (non-destructive)."""
        return self._load_history_data(playlist_name).get("unplayed_cache", [])

    # --- Async wrappers (lock-protected, executor-dispatched) ---
    #
    # The history file holds both `tracks` and `unplayed_cache`. Without
    # serialization, concurrent add_to_history / save_cache calls (e.g. a
    # state-change event recording history while a coordinator detach saves
    # the cache) can interleave and lose data. Production callers must use
    # these async wrappers; the sync methods are retained for tests.

    async def async_add_to_history(
        self, playlist_name: str, track: str, max_tracks: int | None = None
    ) -> None:
        async with self._history_lock(playlist_name):
            await self._hass.async_add_executor_job(
                self.add_to_history, playlist_name, track, max_tracks
            )

    async def async_clear_history(self, playlist_name: str) -> None:
        async with self._history_lock(playlist_name):
            await self._hass.async_add_executor_job(
                self.clear_history, playlist_name
            )

    async def async_save_cache(
        self, playlist_name: str, tracks: list[str]
    ) -> None:
        async with self._history_lock(playlist_name):
            await self._hass.async_add_executor_job(
                self.save_cache, playlist_name, tracks
            )

    async def async_get_cache(self, playlist_name: str) -> list[str]:
        async with self._history_lock(playlist_name):
            return await self._hass.async_add_executor_job(
                self.get_cache, playlist_name
            )

    # --- Helpers ---

    @staticmethod
    def _playlist_slug(name: str) -> str:
        """Convert playlist name to a filename-safe slug."""
        if not name:
            return ""
        slug = name.lower()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"\s+", "_", slug)
        slug = slug.strip("_")
        return slug if slug else "_unnamed"
