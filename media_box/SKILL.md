# Media Box — MCP Server Instructions

You have access to the `media-box` MCP tools for managing media across Jellyfin, qBittorrent, Jackett, and TVMaze. This document defines how to handle user media requests end-to-end.

> **IMPORTANT — Use `media-box` MCP tools for everything.**
> All interaction with Jellyfin, qBittorrent, Jackett, and TVMaze **must** go through the MCP tools listed below. Do **not** use `curl`, `wget`, direct API calls, or any other method to contact these services. Do **not** use shell commands like `mv`, `cp`, `rsync`, or `rm` to move or copy media files — always use `mover_movie` / `mover_tv`. The tools handle authentication, path resolution, error handling, and output formatting. If a tool fails, report the error to the user — do not attempt to work around it by hitting the APIs directly or using shell commands.

## Available Tools

```
jellyfin_search(query, type?)            — search Jellyfin library (type: "movie", "series", "episode")
jellyfin_libraries()                     — list media libraries
jellyfin_episodes(series_id, season?)    — list episodes for a series
jellyfin_refresh(library_id?)            — refresh library/libraries

qbt_list(filter?, tag?, category?, state?)  — list torrents. state "Completed" includes both finished and seeding torrents.
qbt_info(query)                          — detailed torrent info: progress, speed, ETA, save path, files. query matches by hash prefix or name substring.
qbt_wait(query, timeout?)               — BLOCKING: waits for torrent to complete/error/timeout (default 1800s / 30 min). query matches by hash prefix or name substring.
qbt_delete(hashes, delete_files?)        — delete one or more torrents by their full info hash.

jackett_search(query, category?, limit?, sort?)  — search for torrents (category: "movies", "tv"; sort: "seeders", "size")
jackett_add(ref, category?, tag?)        — add a search result to qBittorrent (ref format: "search_id:number")

tvmaze_search(query)                     — search for TV shows
tvmaze_show(show_id)                     — show details
tvmaze_episodes(show_id, season?)        — list episodes
tvmaze_seasons(show_id)                  — list seasons
tvmaze_lookup(imdb?, tvdb?)              — lookup by external ID

mover_list(path?)                        — list files in the temp download location
mover_movie(source, dest_name, force?, torrent_hash?)  — move a movie file to the library
mover_tv(source, dest_name, show, season, force?, torrent_hash?)  — move a single TV episode file
mover_tv_batch(moves, show, season, force?, torrent_hash?)  — move multiple TV episodes in one call. moves is a list of {"source": "...", "dest_name": "..."}. Use this for season packs instead of calling mover_tv repeatedly.
```

### Jackett search → add flow

`jackett_search` returns a numbered table and a **search ID** (e.g. `a3f2c1`). To download result #3 from that search:

```
jackett_add(ref="a3f2c1:3", category="mm-tv")
```

This resolves the magnet/link (handling redirects automatically), downloads to the configured temp location, and adds to qBittorrent. No need to handle magnet links or URLs directly.

---

## Modes: Auto vs Manual

Every media request operates in one of two modes. **Ask the user which mode they want** if their intent is not clear.

### Auto Mode
The user wants hands-off, end-to-end execution. The LLM makes all decisions autonomously:
- Picks the best torrent automatically (highest seeders, reasonable size, 1080p preferred)
- Moves files without confirmation
- Only stops to ask the user if something goes wrong (no results, ambiguous match, error)

**Trigger phrases:** "just get it", "auto", "grab me", "download X", or any request that implies they don't want to be involved in the details.

### Manual Mode
The user wants to be in the loop at every decision point:
- Present torrent options and let the user pick
- Show the move plan and wait for approval
- Confirm before proceeding at each step

**Trigger phrases:** "find me options for", "what's available", "manual", or any request that implies they want to review choices.

> When in doubt, **default to auto mode** — most users just want the content. If they wanted to pick, they'd ask.

---

## Workflow: Handling a User Media Request

When a user asks for a movie or TV show, follow these steps in order. In **manual mode**, confirm with the user before downloading or moving files. In **auto mode**, make the best choice and proceed.

### Step 1 — Identify What They Want

Determine from the request:
- **Mode**: auto or manual (see Modes section above)
- **Title** of the movie or show
- **Type**: movie or TV show (ask if ambiguous)
- **Scope** (TV only): entire series, a specific season, or specific episodes

### Step 2 — Check If It Already Exists in Jellyfin

```
jellyfin_search(query="Breaking Bad", type="series")
```

- If results come back, **tell the user it's already in their library** and show what's there.
- For TV shows, also check which episodes exist:
  ```
  jellyfin_episodes(series_id="<id>", season=3)
  ```
- If the user wants episodes that are already present, let them know — no download needed.
- If some episodes are missing, note exactly which ones are needed and proceed.

### Step 3 — Get Metadata from TVMaze (TV Shows Only)

For TV shows, always fetch metadata so you know the correct season/episode structure:

```
tvmaze_search(query="Breaking Bad")
```

Pick the correct match (highest score, correct network/year), then:

```
tvmaze_seasons(show_id=169)
tvmaze_episodes(show_id=169, season=3)
```

This tells you:
- How many seasons exist
- Episode names and airdates
- Whether episodes have actually aired yet (don't try to download future episodes)

**Manual mode:** Present this information to the user and ask what they want.

**Auto mode:** Use the metadata internally to validate the request and proceed.

### Step 4 — Search for Torrents on Jackett

> **LIMIT: Maximum 2 Jackett searches per request.** If the first search returns no good results, try ONE alternative query. If that also fails, tell the user no results were found. Do NOT keep trying variations — 2 searches max.

**For movies:**
```
jackett_search(query="The Matrix 1999", category="movies")
```

**For a full TV season:**
```
jackett_search(query="Breaking Bad S03", category="tv")
```

**For a specific episode:**
```
jackett_search(query="Breaking Bad S03E07", category="tv")
```

Pick the best option based on:
1. **Seeders** — more is better
2. **Size** — reasonable for the content
3. **Quality** — prefer 1080p WEB-DL or BluRay
4. **Completeness** — for season packs, prefer complete packs

### Step 5 — Download the Torrent

```
jackett_add(ref="<search-id>:<number>", category="mm-tv", tag="breaking-bad-s03")
```

- Use `category="mm-tv"` for TV shows, `category="mm-movie"` for movies
- **Always use `tag`** with a short, unique, lowercase label for the request

### Step 6 — Monitor Download Progress

```
qbt_wait(query="<hash>")
```

`qbt_wait` blocks until the torrent completes, errors, or times out.

> **CRITICAL — Always use `qbt_wait` to wait for downloads. Do NOT manually check in a loop.**

### Step 7 — Move Files to Final Destination

1. **List the downloaded files:**
   ```
   mover_list()
   mover_list(path="<torrent-folder>")
   ```

2. **Move movies:**
   ```
   mover_movie(source="torrent-folder/matrix.mkv", dest_name="The Matrix (1999).mkv", torrent_hash="<hash>")
   ```

3. **Move TV episodes** — use `mover_tv_batch` for season packs (one tool call for all episodes):
   ```
   mover_tv_batch(
     moves=[
       {"source": "torrent-folder/bb.s03e01.mkv", "dest_name": "Breaking Bad - S03E01 - No Mas.mkv"},
       {"source": "torrent-folder/bb.s03e02.mkv", "dest_name": "Breaking Bad - S03E02 - Caballo Sin Nombre.mkv"},
       ...
     ],
     show="Breaking Bad",
     season=3,
     torrent_hash="<hash>"
   )
   ```
   For a single episode, use `mover_tv` instead.

### Step 8 — Trigger Jellyfin Library Refresh

```
jellyfin_refresh()
```

### Step 9 — Verify

```
jellyfin_search(query="Breaking Bad", type="series")
```

---

## Important Rules

1. **Only use the MCP tools** — never use `curl`, `wget`, direct API calls, `mv`, `cp`, `rsync`, `rm`, or any other method.
2. **Always check Jellyfin first** — don't download what the user already has.
3. **In manual mode, confirm before downloading** — show the user what you found and let them pick.
4. **In manual mode, confirm before moving files** — show the planned file moves and destinations.
5. **Use TVMaze for episode titles** — Jellyfin expects episode titles in filenames for TV shows.
6. **Don't download unaired episodes** — check airdates from TVMaze.
7. **Prefer season packs** over individual episodes when the user wants a full season.
8. **Keep the user informed** — in manual mode, at every step. In auto mode, provide brief status updates.
9. **Handle errors gracefully** — if a service is down or a search returns nothing, tell the user and suggest alternatives.
10. **Only use tools listed above** — do not invent or guess tool names.
11. **Never use `sleep` or manual loops** — always use `qbt_wait`. If it times out, call `qbt_wait` again.
12. **Maximum 2 Jackett searches per request** — if two searches return no usable results, stop and ask the user.
