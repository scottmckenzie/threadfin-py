"""Settings page layout: categories and texts (from Go's ``html/lang/en.json``).

Only settings that do something in this port are shown. The stream-buffer, VLC, SSDP, self-update
and temp-folder settings stay in settings.json (Go compatibility) but are not offered.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SettingField:
    key: str
    title: str
    description: str = ""
    kind: str = "str"  # bool, int, str, times, select
    placeholder: str = ""
    choices: tuple[str, ...] = ()
    needs_web_auth: bool = False  # only meaningful with web authentication on


SETTINGS_PAGE: tuple[tuple[str, tuple[SettingField, ...]], ...] = (
    ("General", (
        SettingField("tuner", "Number of Tuners", "Number of parallel connections that can be established "
                     "to the provider. After a change, remove Threadfin from the Plex / Emby / Jellyfin DVR "
                     "settings and set it up again.", "int"),
        SettingField("epgSource", "EPG Source", "PMS: use EPG data from Plex, Emby or Jellyfin. XEPG: use one "
                     "or more XMLTV files, with channel management and M3U / XMLTV export.", "select",
                     choices=("PMS", "XEPG")),
        SettingField("epgCategories", "EPG Categories", "Format key:value|key:value, e.g. "
                     "News:news|Sports:sports|Movies:movies"),
        SettingField("epgCategoriesColors", "EPG Categories Colors", "Format value:color|value:color, e.g. "
                     "news:tomato|sports:yellowgreen|movies:royalblue"),
        SettingField("dummy", "Enable Default Dummy Data", "Map inactive channels to the dummy data channel "
                     "below. Use this to keep Live Event channels active.", "bool"),
        SettingField("dummyChannel", "Dummy Data Channel", "The dummy channel used for inactive channels.",
                     "select", choices=("", "PPV", "30_Minutes", "60_Minutes", "90_Minutes", "120_Minutes",
                                        "180_Minutes", "240_Minutes", "360_Minutes")),
        SettingField("ignoreFilters", "Ignore Filters", "If checked, filtering is completely ignored.", "bool"),
        SettingField("api", "API Interface", "Allow commands through Threadfin's API (/api/).", "bool"),
    )),
    ("Updates and guide", (
        SettingField("update", "Schedule for updating (Playlist, XMLTV, Backup)", "Time in 24 hour format "
                     "(0800 = 8:00 am), several separated by commas. Leave empty for no scheduled updates.",
                     "times", "0000,1000,2000"),
        SettingField("files.update", "Update all files at startup", "Updates all playlists, tuners and XMLTV "
                     "files at startup.", "bool"),
        SettingField("cache.images", "Image Caching", "Channel logos and programme images are downloaded and "
                     "served by Threadfin (/images/), allowing faster rendering of the grid in the client. "
                     "Downloading runs in the background after each update; turning this off deletes the cache.",
                     "bool"),
        SettingField("xepg.replace.missing.images", "Replace missing program images", "If the poster in the "
                     "XMLTV program is missing, the channel logo is used.", "bool"),
        SettingField("xepg.replace.channel.title", "Replace PPV channels title/desc", "Use this if your "
                     "provider maps the PPV event name to the channel name.", "bool"),
        SettingField("enableNonAscii", "Enable Non-ASCII", "Allow non-ASCII characters in the M3U and EPG.",
                     "bool"),
        SettingField("user.agent", "User Agent", "Sent with every request to the providers. Only change it if "
                     "Threadfin is blocked.", placeholder="Threadfin"),
        SettingField("udpxy", "UDPxy address", "If set, multicast (udp://@) channel URLs are rewritten to go "
                     "through this UDPxy server.", placeholder="host:port"),
        SettingField("ffmpeg.path", "FFmpeg Binary Path", "The channel probe uses the ffprobe next to it "
                     "(or the one on the PATH).", placeholder="/usr/bin/ffmpeg"),
    )),
    ("Network", (
        SettingField("bindIpAddress", "Bind IP Address for WebUI/API", "Set the address to listen on, for "
                     "systems with several network interfaces."),
        SettingField("httpThreadfinDomain", "HTTP Threadfin Domain", "Use this domain instead of the address "
                     "clients used in M3U and EPG links (without http://)."),
        SettingField("forceHttps", "Force HTTPS", "Use https:// and the HTTPS domain below in M3U and EPG "
                     "links.", "bool"),
        SettingField("httpsPort", "HTTPS Port", "Default is 443.", "int"),
        SettingField("httpsThreadfinDomain", "HTTPS Threadfin Domain", "Domain for HTTPS links (without "
                     "https://)."),
    )),
    ("Backup", (
        SettingField("backup.path", "Location for automatic backups", "Threadfin makes a backup before every "
                     "scheduled update. It needs write permission for this folder.",
                     placeholder="/mnt/data/backup/threadfin/"),
        SettingField("backup.keep", "Number of backups to keep", "Older backups are deleted automatically.",
                     "int"),
    )),
    ("Backup channels", (
        SettingField("backup.failover.requests", "Failover requests", "A client that requests the same channel "
                     "this many times within the window below is sent to the channel's next backup. 0 turns it "
                     "off. Behind a reverse proxy, see FORWARDED_ALLOW_IPS in the README.", "int"),
        SettingField("backup.failover.seconds", "Failover window (seconds)", "The time in which repeated "
                     "requests for the same channel count towards the number above.", "int"),
    )),
    ("Authentication", (
        SettingField("authentication.web", "WEB Authentication", "Access to the web interface only with "
                     "credentials.", "bool"),
        SettingField("authentication.pms", "PMS Authentication", "Plex requests only with credentials. After "
                     "turning this on, remove Threadfin from the PMS DVR settings and set it up again.", "bool",
                     needs_web_auth=True),
        SettingField("authentication.m3u", "M3U Authentication", "Downloading threadfin.m3u only with "
                     "credentials.", "bool", needs_web_auth=True),
        SettingField("authentication.xml", "XML Authentication", "Downloading threadfin.xml only with "
                     "credentials.", "bool", needs_web_auth=True),
        SettingField("authentication.api", "API Authentication", "Access to the API only with credentials.",
                     "bool", needs_web_auth=True),
    )),
)  # fmt: skip

SETTING_FIELDS: dict[str, SettingField] = {f.key: f for _, fields in SETTINGS_PAGE for f in fields}

# Mapping-editor labels (Go ``mapping.*``)
CHANNEL_LABELS = {
    "x-active": "Active",
    "x-channelID": "Channel number",
    "x-name": "Channel Name",
    "x-description": "Channel Description",
    "x-update-channel-name": "Update Channel Name",
    "tvg-logo": "Logo URL",
    "x-update-channel-icon": "Update Channel Logo",
    "x-category": "EPG Category",
    "x-group-title": "Group Title (threadfin.m3u)",
    "x-xmltv-file": "XMLTV File",
    "x-mapping": "XMLTV Channel",
    "x-ppv-extra": "PPV Extra Title",
    "x-backup-channel-1": "Backup Channel 1",
    "x-backup-channel-2": "Backup Channel 2",
    "x-backup-channel-3": "Backup Channel 3",
    "x-hide-channel": "Hide channel",
}

USER_LEVELS = (
    ("authentication.web", "Web interface"),
    ("authentication.pms", "PMS (lineup.json)"),
    ("authentication.m3u", "M3U playlist"),
    ("authentication.xml", "XMLTV guide"),
    ("authentication.api", "API"),
)

__all__ = ["CHANNEL_LABELS", "SETTINGS_PAGE", "SETTING_FIELDS", "USER_LEVELS", "SettingField"]
