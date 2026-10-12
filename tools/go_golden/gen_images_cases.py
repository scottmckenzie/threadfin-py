"""Writes tests/golden/images/*.input.json. Run run_images_golden.sh afterwards.

``files`` are names in the cache folder; ``md5:<url>`` stands for the cache name of that URL.
"""
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

SOURCES = [
    "http://img.example/logo.png",
    "http://img.example/logo.png?size=big",
    "https://cdn.example/a/b/Channel.PNG",
    "http://img.example/noext",
    "http://img.example/dir.v2/noext",
    "http://img.example/logo.jpg#frag",
    "http://img.example/page.html",
    "http://img.example/script.php?id=1",
    "logo.png",
    "ftp://img.example/logo.gif",
    "\r\nhttp://img.example/trimmed.png\r\n",
    "",
    "http://img.example/space name.svg",
    "http://img.example/weird%20.webp",
]
CACHED = [f"md5:{u}" for u in ("http://img.example/logo.png", "https://cdn.example/a/b/Channel.PNG",
                                 "http://img.example/page.html", "http://img.example/trimmed.png",
                                 "http://img.example/space name.svg")]


def case(name, **kw):
    body = {"caching": True, "cacheURL": "http://192.168.1.2:34400/images/", "files": CACHED,
            "httpDomain": "", "port": "34400", "forceHttps": False, "httpsPort": 443, "httpsDomain": "",
            "sources": SOURCES, **kw}
    (out / f"{name}.input.json").write_text(json.dumps(body, indent=1) + "\n")


case("caching_off", caching=False)
case("plain")
case("empty_cache", files=[])
case("http_domain", httpDomain="tv.example")
case("http_domain_with_port", httpDomain="tv.example:8080")
case("force_https", forceHttps=True, httpsDomain="tv.example", httpsPort=8443)
case("force_https_default_port", forceHttps=True, httpsDomain="tv.example")
case("both_domains", forceHttps=True, httpsDomain="secure.example", httpDomain="tv.example")
