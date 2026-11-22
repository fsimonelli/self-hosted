# Agent Notes

## Media Library Hard Links

When the user asks to "do the same" for a TV show, they usually mean:

- Find the completed download under `/srv/media/downloads`.
- Find or create the show folder under `/srv/media/shows`.
- Preserve the library layout already used for that show:
  - If the show already uses `Season N` folders, create matching missing season folders.
  - If the show is flat, match the existing flat layout unless Sonarr/Jellyfin expects season folders.
- Create hard links with `ln`, not copies or symlinks.
- Link only media files unless the user explicitly asks for extras:
  - usually `.mkv` or `.mp4`
  - skip README/text files
  - skip Jellyfin `.trickplay` directories/files unless specifically requested
- Confirm source and destination are on the same device before hard-linking when practical:
  - `stat -c '%d %n' "$src" "$dst"`
- Verify after linking:
  - count files per season
  - spot-check matching inode numbers and link count with `stat -c '%h %i %n'`

Typical pattern:

```sh
src="/srv/media/downloads/SHOW_DOWNLOAD_FOLDER"
dst="/srv/media/shows/Show Name"

for season_dir in "$src"/Season\ {2..10}; do
  season="$(basename "$season_dir")"
  mkdir -p "$dst/$season"
  find "$season_dir" -maxdepth 1 -type f -name "*.mkv" -exec ln -n "{}" "$dst/$season/" \;
done
```

If the source is a flat absolute-numbered pack, do not assume Sonarr can import it directly. Split hard links into `Season N` folders and rename links to include `SxxEyy` based on the authoritative episode order. Check the series path casing in Sonarr; Linux paths are case-sensitive.

