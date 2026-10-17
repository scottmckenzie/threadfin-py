from __future__ import annotations

import unittest

from threadfin.failover import Decision, Failover

OFF = Decision(0, False, False)


class FailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [0.0]
        self.tracker = Failover(clock=lambda: self.now[0])

    def hit(
        self,
        at: float,
        *,
        backups: int = 2,
        client: str = "c",
        stream: str = "s",
        requests: int = 3,
        seconds: int = 5,
    ) -> Decision:
        self.now[0] = at
        return self.tracker.request(client, stream, backups=backups, requests=requests, seconds=seconds)

    def test_third_request_within_window_switches(self) -> None:
        self.assertEqual(self.hit(0), OFF)
        self.assertEqual(self.hit(3), OFF)
        self.assertEqual(self.hit(4), Decision(1, True, False))

    def test_spread_out_requests_never_switch(self) -> None:
        for at in (0, 6, 12, 18):
            self.assertEqual(self.hit(at), OFF)

    def test_window_slides(self) -> None:
        self.hit(0)
        self.hit(3)
        self.assertEqual(self.hit(6), OFF)
        self.assertEqual(self.hit(7), Decision(1, True, False))

    def test_moves_on_to_next_backup(self) -> None:
        results = [self.hit(at) for at in range(6)]
        self.assertEqual(results[2], Decision(1, True, False))
        self.assertEqual(results[5], Decision(2, True, False))

    def test_stays_on_last_backup(self) -> None:
        results = [self.hit(at) for at in range(9)]
        self.assertEqual(results[8], Decision(2, False, True))
        later = [self.hit(at) for at in (9, 10, 11)]
        self.assertEqual(later[2], Decision(2, False, False))
        self.assertTrue(all(not d.stuck for d in later))

    def test_no_backups_stays_on_main(self) -> None:
        self.hit(0, backups=0)
        self.hit(1, backups=0)
        self.assertEqual(self.hit(2, backups=0), Decision(0, False, True))

    def test_quiet_gap_resets_to_main(self) -> None:
        results = [self.hit(at) for at in (0, 1, 2)]
        self.assertEqual(results[2].step, 1)
        self.assertEqual(self.hit(7.5), OFF)

    def test_clients_are_independent(self) -> None:
        self.hit(0, client="a")
        self.hit(1, client="a")
        self.assertEqual(self.hit(2, client="a").step, 1)
        self.assertEqual(self.hit(2, client="b").step, 0)

    def test_streams_are_independent(self) -> None:
        self.hit(0, stream="s1")
        self.hit(1, stream="s1")
        self.assertEqual(self.hit(2, stream="s1").step, 1)
        self.assertEqual(self.hit(2, stream="s2").step, 0)

    def test_backups_shrink_after_rebuild(self) -> None:
        for at in range(6):
            last = self.hit(at, backups=2)
        self.assertEqual(last.step, 2)
        self.assertEqual(self.hit(6, backups=1), Decision(1, False, False))

    def test_off_and_bad_values(self) -> None:
        for requests in (0, 1):
            self.setUp()
            for at in (0, 0.1, 0.2, 0.3):
                self.assertEqual(self.hit(at, requests=requests), OFF)
        self.setUp()
        self.assertEqual(self.hit(0, seconds=0), OFF)
        self.assertEqual(self.hit(0.1, seconds=0), OFF)
        self.assertEqual(self.hit(0.2, seconds=0), Decision(1, True, False))
        self.setUp()
        self.hit(0, seconds=0)
        self.hit(0.1, seconds=0)
        self.assertEqual(self.hit(1.5, seconds=0), OFF)

    def test_idle_state_is_pruned(self) -> None:
        for i in range(1000):
            self.hit(0, client=f"c{i}")
        self.assertEqual(len(self.tracker._state), 1000)
        self.hit(10, client="late")
        self.assertEqual(len(self.tracker._state), 1)


if __name__ == "__main__":
    unittest.main()
