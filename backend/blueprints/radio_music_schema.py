"""
Radio & Music App — Unified Data Schema (Phase 2)

This module documents the unified data structures and migration strategy.

PHASE 2 GOAL: Consolidate fragmented data models into unified, extensible systems
with backwards compatibility.

UNIFIED DATA STRUCTURES:

1. UNIFIED QUEUE ENTRY
   _queue[] array where each item has:
   - id: "item-{source}-{hash}" (unique ID)
   - title: display name
   - source: "local|radio|podcast|youtube"
   - type: "music|stream|episode|video"
   - url: play URL
   - image: album art or logo
   - duration: in seconds
   - play_count: number of times played

2. UNIFIED FAVORITES
   _favorites[] where each item has:
   - id: "fav-{type}-{hash}"
   - title: station/podcast/playlist name
   - source: "radio|podcast|playlist"
   - type: "station|subscription|playlist"
   - url: stream or feed URL
   - image: logo/artwork
   - added_at: timestamp

3. UNIFIED HISTORY
   history.json single file with:
   - Each entry: {id, title, source, url, play_count, last_played}
   - Sorted by play_count descending for most-played

MIGRATION STRATEGY:
- Old _musicQueue + _podQueue → new _queue
- Old _favorites + _subscriptions → new _favorites
- Old separate history files → single history.json
- Backwards compat: wrapper functions (_getMusicQueue, _getPodcastQueue)

BACKWARDS COMPATIBILITY:
- _getMusicQueue() returns _queue filtered by source (local, youtube)
- _getPodcastQueue() returns _queue filtered by source (podcast)
- _getRadioFavorites() returns _favorites filtered by source (radio)
- _getPodcastSubscriptions() returns _favorites filtered by source (podcast)

For implementation details, see radio_music_playlist.py and radio_music.js
"""

# No code in this module - documentation only
