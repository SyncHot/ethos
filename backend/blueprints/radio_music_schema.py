"""
Radio & Music App — Unified Data Schema (Phase 2)
==================================================

This module documents the unified data structures and migration strategy for the Radio & Music app.

PHASE 2 GOAL: Consolidate fragmented data models (_musicQueue, _podQueue, _favorites, _subscriptions)
into unified, extensible systems with backwards compatibility.

BEFORE (Fragmented):
  - _musicQueue[] + _musicQueueIdx — music/audiobook tracks
  - _podQueue[] + _podQueueIdx — podcast episodes
  - _favorites[] — radio stations
  - _subscriptions[] — podcast subscriptions
  - history.json — play history with play_count
  - favorites.json — radio stations
  - subscriptions.json — podcast subscriptions
  - liked_songs.json — user's liked music tracks

AFTER (Unified):
  - _queue[] — all content types (music, radio, podcast, youtube)
  - _queueIdx — current playing item index
  - _favorites[] — all favorites (radio, podcasts, playlists)
  - history.json — unified history with play_count (single file)


=== 1. UNIFIED QUEUE ENTRY ===
Each item in the unified _queue[] has this structure:

  {
    id: "item-{source}-{hash}",      // REQUIRED: Unique ID for deduplication
                                      // Format: "item-{source}-{uniqueHash}"
                                      // Sources: local, radio, podcast, youtube

    // Core metadata
    title: "Track/Station/Episode",   // REQUIRED: Display name
    source: "local|radio|podcast|youtube",  // REQUIRED: Origin of content
    type: "music|stream|episode|video",    // REQUIRED: Content type
    url: "http://stream.url/...",    // REQUIRED: Play URL (direct stream or yt ID)
    
    // Optional display fields
    image: "http://image.url/...",   // Album art or station logo
    image_url: "...",                 // Alias for image (some sources use this)
    thumbnail: "...",                 // Alias for image (YouTube uses this)
    
    // Metadata
    meta: "Artist/Channel",           // Artist name or channel
    channel: "...",                   // Alias for meta (YouTube)
    artist: "...",                    // Alternative artist field
    album: "...",                     // Album name (music only)
    
    // Duration & timing
    duration: 300,                    // Seconds (0 if unknown/live)
    added_at: 1234567890,            // Timestamp: when added to queue
    played_at: 1234567891,           // Timestamp: when last played (in history)
    play_count: 5,                    // Total times played (in history)
    
    // Source-specific fields
    path: "/local/path/to/file.mp3", // Local file path
    path_meta: {...},                 // Cached metadata for local files
    uuid: "station-uuid",             // Radio station UUID (radio)
    tags: "jazz,blues",               // Radio station tags
    feed_url: "http://podcast.feed/", // Podcast feed URL
    episode_url: "http://episode.mp3", // Episode-specific URL
    episode_pubdate: 1234567890,     // When episode was published
    season: 1, episode: 5,            // Episode metadata
    
    // Playback state
    progress: 42,                      // Current playback position (seconds)
    finished: false,                   // Marked as finished/completed
    progress_url: "http://...",       // Save progress to this endpoint
    
    // Subscription-specific (podcasts)
    subscription_id: "sub-123",       // Link to subscription for podcasts
    
    // Playlist-specific
    playlist_id: "pl-123",            // Link to parent playlist
  }

  BACKWARDS COMPATIBILITY:
    - Old queue entries without 'id' field will auto-generate ID on load
    - Old entries without 'source' will be inferred from URL/type
    - 'name' field is alias for 'title' (normalized on load)
    - 'favicon' is alias for 'image' (radio stations)


=== 2. UNIFIED FAVORITES ENTRY ===
Each item in the unified _favorites[] has this structure:

  {
    id: "fav-{type}-{hash}",         // REQUIRED: Unique ID
                                      // Format: "fav-{type}-{uniqueHash}"
                                      // Types: station, subscription, playlist

    title: "Station/Podcast/Playlist", // REQUIRED: Display name
    source: "radio|podcast|playlist",  // REQUIRED: Origin type
    type: "station|subscription|playlist", // REQUIRED: Content type

    // URLs and endpoints
    url: "http://stream.url/...",    // Stream URL (radio) or feed URL (podcast)
    feed_url: "http://podcast.feed/", // Podcast feed URL
    
    // Display & metadata
    image: "http://image.url/...",   // Logo or cover art
    favicon: "...",                   // Alternative image field
    
    meta: "Artist/Channel",           // Associated metadata
    channel: "...",                   // Alternative meta field
    description: "...",               // Human-readable description
    
    // Radio-specific
    uuid: "station-uuid",             // Radio station UUID (radio)
    country: "PL",                    // Station country code
    language: "en",                   // Station language
    tags: "jazz,blues",               // Station tags/genres
    website: "http://...",            // Station website
    
    // Podcast-specific
    genre: "True Crime",              // Podcast genre
    artist_name: "Podcast Host",      // Podcast author
    explicit: false,                  // Content warning
    language: "en",                   // Podcast language
    
    // Playlist-specific (when saved)
    tracks_count: 42,                 // Number of tracks in playlist
    
    // Timestamps
    added_at: 1234567890,            // When added to favorites
    updated_at: 1234567890,          // Last modification
    
    // User state
    rating: 5,                        // Optional user rating (1-5 stars)
    notes: "Great jazz station",      // Optional user notes
  }

  BACKWARDS COMPATIBILITY:
    - Old 'name' field is alias for 'title'
    - Old separate subscriptions.json entries converted with source='podcast'
    - Old favorites.json entries converted with source='radio'


=== 3. UNIFIED HISTORY ENTRY ===
Each item in history.json has this structure (same as queue entry):

  {
    id: "item-{source}-{hash}",
    title: "...",
    source: "local|radio|podcast|youtube",
    type: "music|stream|episode|video",
    url: "...",
    
    played_at: 1234567890,           // REQUIRED: When played (for sorting)
    play_count: 5,                    // How many times played
    progress: 0,                      // Last playback position
    finished: false,                  // Whether fully listened/watched
    duration: 300,                    // Duration in seconds
    
    // Other metadata fields (same as queue entry)
    ...
  }

  QUERY RULES:
    - /history → all items sorted by played_at DESC
    - /history?sort=plays → all items sorted by play_count DESC
    - /most-played (deprecated, redirects to /history?sort=plays)


=== 4. BACKEND FILE LAYOUT ===

DATA FILES (in /opt/ethos/data/):
  - history.json          — unified history (all sources)
  - favorites.json        — unified favorites (radio + podcasts + playlists)
  - liked_songs.json      — user's liked music tracks (unchanged, references in queue.id)
  - playback_state.json   — current playback state (uses unified _queue format)
  - playlists.json        — user's playlists (tracks use unified entry format)
  - subscriptions.json    — DEPRECATED, migrate to favorites.json on first load
  - old_subscriptions.json — backup of original subscriptions.json (auto-created)

MIGRATION PROCESS:
  On first load with Phase 2 code:
    1. Load old subscriptions.json if it exists
    2. Convert each subscription entry: {... → {source='podcast', type='subscription', ...}
    3. Merge converted entries into favorites.json (skip duplicates by feed_url)
    4. Backup original subscriptions.json as old_subscriptions.json
    5. Delete subscriptions.json
    6. Load old history and fix any type mismatches
    7. Add play_count field if missing


=== 5. MIGRATION HELPER FUNCTIONS (Backend) ===

_migrate_old_subscriptions():
  """Convert old subscriptions.json entries to unified favorites format."""
  - Called automatically on load
  - Returns (migrated_count, errors)
  - Backups original file before deletion
  - Idempotent (safe to call multiple times)

_migrate_old_queue_state():
  """Convert saved playback state with old queue format."""
  - Converts old _musicQueue/_podQueue to unified _queue
  - Preserves queue order and current play index
  - Called during /playback-state GET

_normalize_entry(item, source_hint=None):
  """Normalize a queue/history/favorites entry to unified format."""
  - Auto-generates ID if missing
  - Infers source from URL patterns if not provided
  - Normalizes field aliases (name→title, favicon→image, etc.)
  - Ensures required fields are present


=== 6. FRONTEND WRAPPER FUNCTIONS ===

For backwards compatibility, provide getter functions:

  _getMusicQueue():
    """Return music items from unified _queue (local + youtube)."""
    return _queue.filter(q => ['local', 'youtube'].includes(q.source))

  _getPodcastQueue():
    """Return podcast items from unified _queue."""
    return _queue.filter(q => q.source === 'podcast')

  _getRadioFavorites():
    """Return radio stations from unified _favorites."""
    return _favorites.filter(f => f.source === 'radio')

  _getPodcastSubscriptions():
    """Return podcast subscriptions from unified _favorites."""
    return _favorites.filter(f => f.source === 'podcast' && f.type === 'subscription')


=== 7. ENDPOINT CHANGES ===

NEW ENDPOINTS:
  GET  /radio-music/queue/unified       — Get unified queue
  POST /radio-music/queue/add           — Add to unified queue
  POST /radio-music/queue/remove        — Remove from unified queue
  
  GET  /radio-music/favorites/unified   — Get all favorites (radio + podcasts)
  POST /radio-music/favorite/add        — Add to unified favorites
  POST /radio-music/favorite/remove     — Remove from unified favorites
  
  GET  /radio-music/history?sort=plays — Return history sorted by play_count
  POST /radio-music/history/clear       — Clear history

UPDATED ENDPOINTS:
  GET  /radio-music/history             → Returns unified history
  POST /radio-music/history             → Adds to unified history
  POST /radio-music/subscribe           → Adds to unified favorites
  POST /radio-music/unsubscribe         → Removes from unified favorites
  GET  /radio-music/most-played         → Still works (redirects to history?sort=plays)
  
DEPRECATED ENDPOINTS:
  (These still work but use new unified format internally)
  GET  /radio-music/favorites           → Returns radio items from unified favorites
  GET  /radio-music/subscriptions       → Returns podcast items from unified favorites


=== 8. TRANSITION PLAN ===

PHASE 2A (Current): Data Layer Unification
  1. Create schema documentation (this file) ✓
  2. Implement migration helpers in backend
  3. Update frontend to use unified _queue and _favorites
  4. Ensure all existing queues/favorites load without errors
  5. Test backwards compatibility

PHASE 2B: Backend Endpoints
  1. Update /history endpoint to return unified format
  2. Consolidate /most-played into /history?sort=plays
  3. Update /favorites and /subscribe endpoints
  4. Implement /favorites/unified endpoint

PHASE 2C: Frontend UI Optimization
  1. Audit all playback functions to check source type
  2. Update queue rendering for mixed sources
  3. Update favorites UI for mixed types
  4. Test all playback modes (local, radio, podcast, YouTube)

PHASE 3: UI Redesign
  1. Redesign queue panel to show mixed sources clearly
  2. Redesign favorites panel with tabs/filters
  3. Add unified history with search/filter


=== 9. TESTING CHECKLIST ===

[ ] Existing user queues load without errors
[ ] Existing favorites/subscriptions migrate correctly
[ ] Playback works for all source types (local, radio, podcast, youtube)
[ ] Queue navigation (next/prev) respects source types
[ ] Queue filtering by source works (_getMusicQueue, _getPodcastQueue)
[ ] Favorites/subscriptions CRUD operations work
[ ] History entries have correct play_count and sorting
[ ] Cross-device resume works with unified format
[ ] Service worker caching respects new entry format
[ ] Playlist operations work with new unified entries
[ ] No data loss during migration
"""

# Migration helper functions will be implemented in radio_music_playlist.py
