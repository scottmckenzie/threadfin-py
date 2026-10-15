"""XMLTV helpers: programme counting (the channel-scan half is covered through tests/test_xepg.py)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from threadfin import xmltv
from threadfin.xepg import XEPGChannel
from threadfin.xmltv import DUMMY_FILE, make_program_counter, programme_counts

GUIDE = """<?xml version="1.0"?><tv>
<channel id="a"><display-name>A</display-name></channel>
<programme channel="a" start="20261015000000 +0000"><title>1</title></programme>
<programme channel="a" start="20261015010000 +0000"><title>2</title></programme>
<programme channel="b" start="20261015000000 +0000"><title>3</title></programme>
<programme start="20261015000000 +0000"><title>no channel</title></programme>
</tv>"""


def channel(file: str, mapping: str) -> XEPGChannel:
    return XEPGChannel(x_xmltv_file=file, x_mapping=mapping)


class ProgrammeCounts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        (self.folder / "g.xml").write_text(GUIDE, "utf-8")
        (self.folder / "h.xml").write_text(GUIDE.replace('channel="b"', 'channel="a"'), "utf-8")

    def reads(self) -> mock._patch:
        return mock.patch.object(xmltv, "iter_top_level", side_effect=xmltv.iter_top_level)

    def test_programme_counts(self) -> None:
        self.assertEqual(programme_counts(self.folder / "g.xml"), {"a": 2, "b": 1, "": 1})

    def test_counts_per_channel(self) -> None:
        count = make_program_counter(self.folder)
        self.assertEqual(count(channel("g.xml", "a")), 2)
        self.assertEqual(count(channel("g.xml", "b")), 1)
        self.assertEqual(count(channel("g.xml", "zzz")), 0)
        self.assertEqual(count(channel("h.xml", "a")), 3)  # files are counted separately
        self.assertEqual(count(channel("", "a")), 0)
        self.assertEqual(count(channel(DUMMY_FILE, "PPV")), 3)
        self.assertEqual(count(channel(DUMMY_FILE, "60_Minutes")), 4 * 24)

    def test_each_file_is_read_once(self) -> None:
        count = make_program_counter(self.folder)
        with self.reads() as spy:
            for _ in range(20):
                count(channel("g.xml", "a"))
                count(channel("g.xml", "b"))
                count(channel("h.xml", "a"))
                count(channel(DUMMY_FILE, "PPV"))  # the dummy guide is never read from disk
        self.assertEqual(sorted(Path(c.args[0]).name for c in spy.call_args_list), ["g.xml", "h.xml"])

    def test_unreadable_file_counts_zero_and_is_read_once(self) -> None:
        (self.folder / "bad.xml").write_text("<tv><programme channel='a'>", "utf-8")
        count = make_program_counter(self.folder)
        with self.reads() as spy:
            self.assertEqual(count(channel("bad.xml", "a")), 0)  # invalid, even after a valid programme
            self.assertEqual(count(channel("bad.xml", "a")), 0)
            self.assertEqual(count(channel("missing.xml", "a")), 0)
            self.assertEqual(count(channel("missing.xml", "a")), 0)
        self.assertEqual(spy.call_count, 2)

    def test_a_new_counter_reads_the_file_again(self) -> None:
        self.assertEqual(make_program_counter(self.folder)(channel("g.xml", "b")), 1)
        (self.folder / "g.xml").write_text(GUIDE.replace('channel="a"', 'channel="b"'), "utf-8")
        self.assertEqual(make_program_counter(self.folder)(channel("g.xml", "b")), 3)


if __name__ == "__main__":
    unittest.main()
