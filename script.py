#!/usr/bin/env python3
import argparse
import collections
import datetime
import logging
import os
import re
import subprocess
import traceback
from pathlib import Path

from ytmusicapi.continuations import get_continuations
from ytmusicapi.navigation import nav
from ytmusicapi.parsers.watch import parse_watch_playlist
from ytmusicapi import YTMusic


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger: logging.Logger = logging.getLogger(__name__)


Playlist = collections.namedtuple(
    "Playlist",
    [
        "url",
        "name",
        "description",
        "tracks",
    ],
)

Track = collections.namedtuple(
    "Track",
    [
        "id",
        "url",
        "duration_ms",
        "name",
        "album",
        "artists",
    ],
)

Album = collections.namedtuple("Album", ["url", "name"])
Artist = collections.namedtuple("Artist", ["url", "name"])


class InvalidPlaylistError(Exception):
    pass


class YouTubeMusic:
    WATCH_PLAYLIST_PREFIXES = ("RD",)

    def __init__(self, auth_path=None, watch_limit=100):
        self._client = YTMusic(auth=auth_path)
        self._watch_limit = watch_limit

    def get_playlist(self, playlist_id):
        if not playlist_id:
            raise InvalidPlaylistError

        if self._is_watch_playlist_id(playlist_id):
            return self._get_watch_playlist(playlist_id)

        try:
            data = self._client.get_playlist(playlist_id, limit=None)
        except Exception as exc:
            raise InvalidPlaylistError(str(exc)) from exc

        if not data or not data.get("title"):
            raise InvalidPlaylistError

        name = self._sanitize_playlist_name(data["title"])
        if not name:
            raise Exception(f"Empty playlist name: {playlist_id}")

        return Playlist(
            url=URL.ytmusic_playlist(playlist_id),
            name=name,
            description=data.get("description") or "",
            tracks=[self._track_from_item(item) for item in data.get("tracks", [])],
        )

    def _get_watch_playlist(self, playlist_id):
        video_id = self._video_id_from_watch_playlist_id(playlist_id)
        try:
            data = self._safe_get_watch_playlist(
                videoId=video_id,
                playlistId=playlist_id,
                limit=self._watch_limit,
            )
        except Exception as exc:
            raise InvalidPlaylistError(str(exc)) from exc

        tracks = self._unique_tracks(
            [self._track_from_item(item) for item in data.get("tracks", [])]
        )
        if not tracks:
            raise InvalidPlaylistError("watch playlist has no tracks")

        seed_name = tracks[0].name
        name = self._sanitize_playlist_name(f"Radio - {seed_name}")
        return Playlist(
            url=URL.ytmusic_watch_playlist(playlist_id, tracks[0].id),
            name=name,
            description=f"YouTube Music watch playlist: {playlist_id}",
            tracks=tracks,
        )

    def _safe_get_watch_playlist(self, videoId=None, playlistId=None, limit=100):
        body = {
            "enablePersistentPlaylistPanel": True,
            "isAudioOnly": True,
            "tunerSettingValue": "AUTOMIX_SETTING_NORMAL",
        }
        if videoId:
            body["videoId"] = videoId
            body["watchEndpointMusicSupportedConfigs"] = {
                "watchEndpointMusicConfig": {
                    "hasPersistentPlaylistPanel": True,
                    "musicVideoType": "MUSIC_VIDEO_TYPE_ATV",
                }
            }
        if playlistId:
            body["playlistId"] = playlistId

        endpoint = "next"
        response = self._client._send_request(endpoint, body)
        watch_next_renderer = nav(
            response,
            [
                "contents",
                "singleColumnMusicWatchNextResultsRenderer",
                "tabbedRenderer",
                "watchNextTabbedResultsRenderer",
            ],
        )
        results = nav(
            watch_next_renderer,
            [
                "tabs",
                0,
                "tabRenderer",
                "content",
                "musicQueueRenderer",
                "content",
                "playlistPanelRenderer",
            ],
            True,
        )
        if not results:
            raise InvalidPlaylistError("watch playlist has no content")

        tracks = parse_watch_playlist(results["contents"])
        if "continuations" in results and len(tracks) < limit:
            request_func = lambda additionalParams: self._client._send_request(
                endpoint,
                body,
                additionalParams,
            )
            parse_func = lambda contents: parse_watch_playlist(contents)
            tracks.extend(
                get_continuations(
                    results,
                    "playlistPanelContinuation",
                    limit - len(tracks),
                    request_func,
                    parse_func,
                    "Radio",
                )
            )

        return {"tracks": tracks, "playlistId": playlistId}

    @classmethod
    def _track_from_item(cls, item):
        video_id = item.get("videoId")
        title = item.get("title") or "<MISSING>"
        duration_ms = cls._duration_ms_from_item(item)

        album = item.get("album") or {}
        album_name = album.get("name") or "<MISSING>"

        artists = []
        for artist in item.get("artists") or []:
            artist_name = artist.get("name") or "<MISSING>"
            artists.append(
                Artist(
                    url=URL.ytmusic_browse(artist.get("id")),
                    name=artist_name,
                )
            )

        if not artists:
            logger.warning(f"Empty track artists: {URL.ytmusic_video(video_id)}")

        return Track(
            id=video_id,
            url=URL.ytmusic_video(video_id),
            duration_ms=duration_ms,
            name=title,
            album=Album(
                url=URL.ytmusic_browse(album.get("id")),
                name=album_name,
            ),
            artists=artists,
        )

    @classmethod
    def _sanitize_playlist_name(cls, name):
        return (
            name.replace("/", " ")
            .replace("\\", " ")
            .replace(":", " -")
            .replace("|", "-")
            .replace("?", "")
            .strip(" .")
        )

    @classmethod
    def _duration_ms_from_item(cls, item):
        duration_seconds = item.get("duration_seconds")
        if duration_seconds is not None:
            return int(duration_seconds * 1000)

        length = item.get("length")
        if not length:
            return 0

        try:
            seconds = 0
            for part in length.split(":"):
                seconds = seconds * 60 + int(part)
            return seconds * 1000
        except ValueError:
            logger.warning(f"Unable to parse track length: {length}")
            return 0

    @classmethod
    def _is_watch_playlist_id(cls, playlist_id):
        return playlist_id.startswith(cls.WATCH_PLAYLIST_PREFIXES)

    @classmethod
    def _video_id_from_watch_playlist_id(cls, playlist_id):
        if playlist_id.startswith("RDAMVM"):
            return playlist_id.removeprefix("RDAMVM")
        if playlist_id.startswith("RD"):
            return playlist_id.removeprefix("RD")
        return None

    @classmethod
    def _unique_tracks(cls, tracks):
        seen = set()
        unique = []
        for track in tracks:
            key = track.id or Formatter._plain_line_from_track(track).lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(track)
        return unique


class Formatter:
    TRACK_NO = "No."
    TITLE = "Title"
    ARTISTS = "Artist(s)"
    ALBUM = "Album"
    LENGTH = "Length"
    ADDED = "Added"
    REMOVED = "Removed"

    ARTIST_SEPARATOR = ", "
    LINK_REGEX = r"\[(.+?)\]\(.+?\)"

    @classmethod
    def plain(cls, playlist_id, playlist):
        lines = [cls._plain_line_from_track(track) for track in playlist.tracks]
        header = [playlist.name, playlist.description, ""]
        return "\n".join(header + lines)

    @classmethod
    def pretty(cls, playlist_id, playlist):
        columns = [
            cls.TRACK_NO,
            cls.TITLE,
            cls.ARTISTS,
            cls.ALBUM,
            cls.LENGTH,
        ]

        vertical_separators = ["|"] * (len(columns) + 1)
        line_template = " {} ".join(vertical_separators)
        divider_line = "---".join(vertical_separators)
        lines = cls._markdown_header_lines(
            playlist_name=playlist.name,
            playlist_url=playlist.url,
            playlist_id=playlist_id,
            playlist_description=playlist.description,
            is_cumulative=False,
        )
        lines += [
            line_template.format(*columns),
            divider_line,
        ]

        for i, track in enumerate(playlist.tracks):
            lines.append(
                line_template.format(
                    i + 1,
                    cls._link(track.name, track.url),
                    cls.ARTIST_SEPARATOR.join(
                        [cls._link(artist.name, artist.url) for artist in track.artists]
                    ),
                    cls._link(track.album.name, track.album.url),
                    cls._format_duration(track.duration_ms),
                )
            )

        return "\n".join(lines)

    @classmethod
    def cumulative(cls, now, prev_content, playlist_id, playlist):
        today = now.strftime("%Y-%m-%d")
        columns = [
            cls.TITLE,
            cls.ARTISTS,
            cls.ALBUM,
            cls.LENGTH,
            cls.ADDED,
            cls.REMOVED,
        ]

        vertical_separators = ["|"] * (len(columns) + 1)
        line_template = " {} ".join(vertical_separators)
        divider_line = "---".join(vertical_separators)
        header = cls._markdown_header_lines(
            playlist_name=playlist.name,
            playlist_url=playlist.url,
            playlist_id=playlist_id,
            playlist_description=playlist.description,
            is_cumulative=True,
        )
        header += [
            line_template.format(*columns),
            divider_line,
        ]

        rows = cls._rows_from_prev_content(today, prev_content, divider_line)
        current_keys = []
        for track in playlist.tracks:
            key = cls._plain_line_from_track(track).lower()
            current_keys.append(key)
            row = rows.get(key, {column: None for column in columns})
            rows[key] = row
            row[cls.TITLE] = cls._link(track.name, track.url)
            row[cls.ARTISTS] = cls.ARTIST_SEPARATOR.join(
                [cls._link(artist.name, artist.url) for artist in track.artists]
            )
            row[cls.ALBUM] = cls._link(track.album.name, track.album.url)
            row[cls.LENGTH] = cls._format_duration(track.duration_ms)

            if not row[cls.ADDED]:
                row[cls.ADDED] = today

            row[cls.REMOVED] = ""

        ordered_keys = current_keys + sorted(key for key in rows if key not in current_keys)
        lines = []
        for key in ordered_keys:
            row = rows[key]
            lines.append(line_template.format(*[row[column] for column in columns]))

        return "\n".join(header + lines)

    @classmethod
    def _markdown_header_lines(
        cls,
        playlist_name,
        playlist_url,
        playlist_id,
        playlist_description,
        is_cumulative,
    ):
        if is_cumulative:
            pretty = cls._link("pretty", URL.pretty(playlist_name))
            cumulative = "cumulative"
        else:
            pretty = "pretty"
            cumulative = cls._link("cumulative", URL.cumulative(playlist_name))

        return [
            f"{pretty} - {cumulative} - {cls._link('plain', URL.plain(playlist_id))} ({cls._link('githistory', URL.plain_history(playlist_id))})",
            "",
            f"### {cls._link(playlist_name, playlist_url)}",
            "",
            f"> {playlist_description}",
            "",
        ]

    @classmethod
    def _rows_from_prev_content(cls, today, prev_content, divider_line):
        rows = {}
        if not prev_content:
            return rows

        prev_lines = prev_content.splitlines()
        try:
            index = prev_lines.index(divider_line)
        except ValueError:
            return rows

        for i in range(index + 1, len(prev_lines)):
            prev_line = prev_lines[i]

            try:
                title, artists, album, length, added, removed = prev_line[2:-2].split(" | ")
            except Exception:
                continue

            key = cls._plain_line_from_names(
                track_name=cls._unlink(title),
                artist_names=[artist for artist in re.findall(cls.LINK_REGEX, artists)],
                album_name=cls._unlink(album),
            ).lower()

            row = {
                cls.TITLE: title,
                cls.ARTISTS: artists,
                cls.ALBUM: album,
                cls.LENGTH: length,
                cls.ADDED: added,
                cls.REMOVED: removed,
            }
            rows[key] = row

            if not row[cls.REMOVED]:
                row[cls.REMOVED] = today

        return rows

    @classmethod
    def _plain_line_from_track(cls, track):
        return cls._plain_line_from_names(
            track_name=track.name,
            artist_names=[artist.name for artist in track.artists],
            album_name=track.album.name,
        )

    @classmethod
    def _plain_line_from_names(cls, track_name, artist_names, album_name):
        return f"{track_name} -- {cls.ARTIST_SEPARATOR.join(artist_names)} -- {album_name}"

    @classmethod
    def _link(cls, text, url):
        if not url:
            return text
        return f"[{text}]({url})"

    @classmethod
    def _unlink(cls, link):
        match = re.match(cls.LINK_REGEX, link)
        return match and match.group(1) or ""

    @classmethod
    def _format_duration(cls, duration_ms):
        try:
            seconds = int(duration_ms // 1000)
            timedelta = str(datetime.timedelta(seconds=seconds))

            index = 0
            while timedelta[index] in [":", "0"]:
                index += 1

            return timedelta[index:]
        except Exception:
            return "00:00"


class URL:
    BASE = "/playlists"
    HISTORY_BASE = (
        "https://github.githistory.xyz/vitokorn/ytmusic-playlist-archive/"
        "blob/master/playlists"
    )

    @classmethod
    def plain_history(cls, playlist_id):
        return cls.HISTORY_BASE + f"/plain/{playlist_id}"

    @classmethod
    def plain(cls, playlist_id):
        return cls.BASE + f"/plain/{playlist_id}"

    @classmethod
    def pretty(cls, playlist_name):
        sanitized = playlist_name.replace(" ", "%20")
        return cls.BASE + f"/pretty/{sanitized}.md"

    @classmethod
    def cumulative(cls, playlist_name):
        sanitized = playlist_name.replace(" ", "%20")
        return cls.BASE + f"/cumulative/{sanitized}.md"

    @classmethod
    def ytmusic_playlist(cls, playlist_id):
        return f"https://music.youtube.com/playlist?list={playlist_id}"

    @classmethod
    def ytmusic_watch_playlist(cls, playlist_id, video_id):
        if not video_id:
            return cls.ytmusic_playlist(playlist_id)
        return f"https://music.youtube.com/watch?v={video_id}&list={playlist_id}"

    @classmethod
    def ytmusic_video(cls, video_id):
        if not video_id:
            return None
        return f"https://music.youtube.com/watch?v={video_id}"

    @classmethod
    def ytmusic_browse(cls, browse_id):
        if not browse_id:
            return None
        return f"https://music.youtube.com/browse/{browse_id}"


def update_files(now, playlists_dir, auth_path=None, watch_limit=100, debug=False):
    plain_dir = playlists_dir / "plain"
    pretty_dir = playlists_dir / "pretty"
    cumulative_dir = playlists_dir / "cumulative"

    for directory in [plain_dir, pretty_dir, cumulative_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    ytmusic = YouTubeMusic(auth_path=auth_path, watch_limit=watch_limit)
    playlist_ids = sorted(
        path.name
        for path in plain_dir.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )

    readme_lines = []
    for playlist_id in playlist_ids:
        plain_path = plain_dir / playlist_id
        try:
            playlist = ytmusic.get_playlist(playlist_id)
        except InvalidPlaylistError as exc:
            logger.warning(f"Skipping invalid playlist {playlist_id}: {exc}")
            if debug:
                traceback.print_exception(exc)
            continue

        readme_lines.append(f"- [{playlist.name}]({URL.pretty(playlist.name)})")

        pretty_path = pretty_dir / f"{playlist.name}.md"
        cumulative_path = cumulative_dir / f"{playlist.name}.md"

        for path, func, include_history in [
            (plain_path, Formatter.plain, False),
            (pretty_path, Formatter.pretty, False),
            (cumulative_path, Formatter.cumulative, True),
        ]:
            try:
                prev_content = path.read_text()
            except Exception:
                prev_content = None

            if include_history:
                args = [now, prev_content, playlist_id, playlist]
            else:
                args = [playlist_id, playlist]

            content = func(*args)
            if content == prev_content:
                logger.info(f"No changes to file: {path}")
            else:
                logger.info(f"Writing updates to file: {path}")
                path.write_text(content)

        cleanup_stale_outputs(
            playlist_id=playlist_id,
            keep_paths={pretty_path, cumulative_path},
            output_dirs=[pretty_dir, cumulative_dir],
        )

    write_readme(playlists_dir, readme_lines)


def cleanup_stale_outputs(playlist_id, keep_paths, output_dirs):
    plain_link = URL.plain(playlist_id)
    for output_dir in output_dirs:
        for path in output_dir.glob("*.md"):
            if path in keep_paths:
                continue
            try:
                content = path.read_text()
            except Exception:
                continue
            if plain_link not in content:
                continue
            logger.info(f"Removing stale generated file: {path}")
            path.unlink()


def write_readme(playlists_dir, readme_lines):
    readme_path = playlists_dir.parent / "README.md"
    marker = "The list below is autogenerated."
    try:
        existing = readme_path.read_text()
    except FileNotFoundError:
        existing = ""

    if marker in existing:
        prefix = existing.split(marker, 1)[0] + marker
    else:
        prefix = "\n".join(
            [
                "# YouTube Music Playlist Archive",
                "",
                "Add YouTube Music playlist IDs as files in `playlists/plain`.",
                "",
                marker,
            ]
        )

    lines = [prefix.rstrip(), "", "## Playlists", ""]
    lines += sorted(readme_lines, key=lambda line: line.lower())
    readme_path.write_text("\n".join(lines) + "\n")


def run(args):
    logger.info(f"- Running: {args}")
    result = subprocess.run(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    logger.info(f"- Exited with: {result.returncode}")
    return result


def push_updates(now):
    diff = run(["git", "status", "-s"])
    has_changes = bool(diff.stdout)

    if not has_changes:
        logger.info("No changes, not pushing")
        return

    logger.info("Staging changes")
    add = run(["git", "add", "-A"])
    if add.returncode != 0:
        raise Exception("Failed to stage changes")

    logger.info("Committing changes")
    build = os.getenv("BUILD_NUMBER")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    message = f"[ytmusic archive] Build #{build} ({now_str})"
    commit = run(["git", "commit", "-m", message])
    if commit.returncode != 0:
        raise Exception("Failed to commit changes")

    logger.info("Pushing changes")
    push = run(["git", "push"])
    if push.returncode != 0:
        logger.warning(f"Push returned code {push.returncode}")
        raise Exception("Failed to push changes")


def main():
    parser = argparse.ArgumentParser(description="Snapshot YouTube Music playlists")
    parser.add_argument(
        "--playlists-dir",
        default=Path(__file__).resolve().parent / "playlists",
        type=Path,
        help="Directory containing plain/pretty/cumulative playlist folders",
    )
    parser.add_argument(
        "--auth",
        default=os.getenv("YTMUSIC_AUTH_PATH"),
        help="Optional ytmusicapi auth file path. Defaults to YTMUSIC_AUTH_PATH.",
    )
    parser.add_argument(
        "--watch-limit",
        default=100,
        type=int,
        help="Minimum number of tracks to fetch for watch/radio playlist IDs.",
    )
    parser.add_argument(
        "--debug",
        help="Print tracebacks for skipped playlists",
        action="store_true",
    )
    parser.add_argument(
        "--push",
        help="Commit and push updated playlists",
        action="store_true",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    update_files(
        now,
        args.playlists_dir,
        auth_path=args.auth,
        watch_limit=args.watch_limit,
        debug=args.debug,
    )

    if args.push:
        push_updates(now)

    logger.info("Done")


if __name__ == "__main__":
    main()
