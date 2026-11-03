import json
import re
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from helpers import TempEnv

from study import db, plan, srs
from study.app import app
from study.config import get_settings

ALICE = {"X-authentik-username": "alice"}
TZ = "Europe/Zurich"
TODAY = date(2026, 11, 3)


class MakePlanTests(unittest.TestCase):
    def test_default_limit_without_exam(self):
        p = plan.make_plan(1, new=50, introduced=0, own_limit=None, default=20, exam=None, today=TODAY)
        self.assertEqual((p.limit, p.left, p.pace, p.custom), (20, 20, 0, False))
        self.assertEqual(p.finish, TODAY + timedelta(days=2))  # 20 + 20 + 10

    def test_exam_sets_minimum_pace_and_keeps_review_days(self):
        exam = TODAY + timedelta(days=13)
        p = plan.make_plan(1, new=100, introduced=0, own_limit=None, default=20, exam=exam, today=TODAY)
        self.assertEqual((p.days_left, p.pace, p.limit), (13, 10, 20))  # 10 Tage, 3 frei
        self.assertEqual(p.target, TODAY + timedelta(days=9))
        slow = plan.make_plan(1, new=100, introduced=0, own_limit=0, default=20, exam=exam, today=TODAY)
        self.assertEqual((slow.limit, slow.left, slow.finish), (10, 10, TODAY + timedelta(days=9)))

    def test_cards_learned_today_count_towards_the_day(self):
        exam = TODAY + timedelta(days=8)
        p = plan.make_plan(1, new=40, introduced=10, own_limit=0, default=20, exam=exam, today=TODAY)
        self.assertEqual((p.pace, p.left), (10, 0))  # 50 in 5 Tagen, heute schon 10

    def test_close_exam_takes_everything_now_but_capped(self):
        p = plan.make_plan(1, new=100, introduced=0, own_limit=0, default=20, exam=TODAY + timedelta(days=2),
                           today=TODAY)
        self.assertEqual((p.pace, p.left, p.target), (100, 100, TODAY))
        huge = plan.make_plan(1, new=10_000, introduced=0, own_limit=0, default=20,
                              exam=TODAY + timedelta(days=1), today=TODAY)
        self.assertEqual(huge.limit, plan.MAX_NEW_PER_DAY)

    def test_exam_today_or_past_falls_back_to_limit(self):
        for days in (0, -5):
            p = plan.make_plan(1, new=30, introduced=0, own_limit=None, default=20,
                               exam=TODAY + timedelta(days=days), today=TODAY)
            self.assertEqual((p.pace, p.limit, p.exam_ahead), (0, 20, False))

    def test_zero_limit_without_exam_means_no_new_cards(self):
        p = plan.make_plan(1, new=30, introduced=0, own_limit=0, default=20, exam=None, today=TODAY)
        self.assertEqual((p.left, p.finish), (0, None))

    def test_order_nearest_exam_first(self):
        a = plan.make_plan(1, 5, 0, None, 20, None, TODAY)
        b = plan.make_plan(2, 5, 0, None, 20, TODAY + timedelta(days=30), TODAY)
        c = plan.make_plan(3, 5, 0, None, 20, TODAY + timedelta(days=4), TODAY)
        d = plan.make_plan(4, 0, 0, None, 20, TODAY + timedelta(days=1), TODAY)  # nichts Neues
        self.assertEqual([p.deck_id for p in plan.new_order({p.deck_id: p for p in (a, b, c, d)})], [3, 2, 1])

    def test_day_labels(self):
        self.assertEqual(plan.day_label(TODAY, TODAY), "Heute")
        self.assertEqual(plan.day_label(TODAY + timedelta(days=1), TODAY), "Morgen")
        self.assertEqual(plan.day_label(date(2026, 11, 5), TODAY), "Do 05.11.")


class StatsTests(TempEnv):
    def setUp(self):
        super().setUp()
        db.init(get_settings().db_path)
        self.conn = db.connect(get_settings().db_path)
        self.conn.execute("INSERT INTO users (name, created) VALUES ('alice', 'x')")
        self.conn.execute("INSERT INTO decks (user_id, name, created) VALUES (1, 'A', 'x')")
        self.now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def card(self, due: datetime | None, suspended=0) -> int:
        state = srs.new_card()
        return self.conn.execute(
            "INSERT INTO cards (user_id, deck_id, type, front, back, fsrs, due, reps, suspended, created, updated) "
            "VALUES (1, 1, 'basic', 'F', 'B', ?, ?, ?, ?, 'x', 'x')",
            (srs.to_json(state), (due or self.now).isoformat(timespec="seconds"), 1 if due else 0, suspended),
        ).lastrowid

    def review(self, card_id: int, at: datetime, rating=3, was_new=0, state=None):
        prev = None
        if state is not None:
            prev = json.dumps({"fsrs": json.dumps({"state": state}), "due": "x", "reps": 1, "lapses": 0})
        self.conn.execute(
            "INSERT INTO reviews (card_id, user_id, rating, was_new, reviewed_at, duration_ms, prev) "
            "VALUES (?, 1, ?, ?, ?, 60000, ?)",
            (card_id, rating, was_new, at.isoformat(timespec="seconds"), prev),
        )

    def test_forecast_buckets_overdue_into_today_in_local_time(self):
        self.card(self.now - timedelta(days=3))  # überfällig
        self.card(self.now + timedelta(hours=2))
        # 23:30 UTC am 4.11. ist in Zürich schon der 5.11.
        self.card(datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc))
        self.card(self.now + timedelta(days=9))  # nach der Woche
        self.card(self.now + timedelta(days=1), suspended=1)
        self.card(None)  # neu
        days = dict(plan.forecast(self.conn, 1, TZ, now=self.now))
        self.assertEqual(len(days), 7)
        self.assertEqual(days[date(2026, 11, 3)], 2)
        self.assertEqual(days[date(2026, 11, 5)], 1)
        self.assertEqual(sum(days.values()), 3)
        stats = plan.stats(self.conn, 1, TZ, now=self.now)
        self.assertEqual([d.labeled for d in stats.forecast], [True] + [False] * 6)  # heute ist auch der höchste
        self.assertEqual(stats.forecast[2].height, 50)

    def test_retention_counts_only_review_state(self):
        card = self.card(self.now)
        for i in range(9):
            self.review(card, self.now - timedelta(days=1, minutes=i), rating=3, state=plan.REVIEW_STATE)
        self.review(card, self.now - timedelta(days=1), rating=1, state=plan.REVIEW_STATE)
        self.review(card, self.now - timedelta(days=1), rating=1, state=1)  # Lernschritt
        self.review(card, self.now - timedelta(days=1), rating=1, was_new=1)  # erste Begegnung, alt
        self.review(card, self.now - timedelta(days=2), rating=2)  # alt ohne Vorzustand, zählt
        self.review(card, self.now - timedelta(days=40), rating=1, state=plan.REVIEW_STATE)  # zu alt
        self.assertEqual(plan.retention(self.conn, 1, now=self.now), (10, 11))
        self.assertEqual(plan.stats(self.conn, 1, TZ, now=self.now).rate, 91)

    def test_streak_and_today(self):
        card = self.card(self.now)
        for days in (1, 2, 3, 5):
            self.review(card, self.now - timedelta(days=days))
        self.assertEqual(plan.streak(self.conn, 1, TZ, now=self.now), 3)  # bis gestern
        self.review(card, self.now - timedelta(hours=1))
        self.assertEqual(plan.streak(self.conn, 1, TZ, now=self.now), 4)
        self.assertEqual(plan.today_done(self.conn, 1, TZ, now=self.now), (1, 1))


class PlanAppTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, follow_redirects=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def deck(self, name, cards):
        res = self.client.post("/decks", data={"name": name}, headers=ALICE)
        deck_id = int(re.search(r"/decks/(\d+)", res.headers["location"]).group(1))
        for i in range(cards):
            self.client.post(f"/decks/{deck_id}/cards", data={"front": f"{name} {i}", "back": "A"}, headers=ALICE)
        return deck_id

    def test_limits_per_deck_and_nearest_exam_first(self):
        far = self.deck("Fern", 5)
        near = self.deck("Nah", 30)
        self.client.post(f"/decks/{far}/plan", data={"exam_date": "", "new_per_day": "2"}, headers=ALICE)
        exam = (plan.local_today(get_settings().tz) + timedelta(days=6)).isoformat()
        res = self.client.post(f"/decks/{near}/plan", data={"exam_date": exam, "new_per_day": "0"}, headers=ALICE)
        self.assertIn("msg=plan-saved", res.headers["location"])
        # Nah: 30 Karten in 3 Tagen (6 minus 3 zum Wiederholen) = 10 pro Tag
        page = self.client.get("/", headers=ALICE).text
        self.assertIn("12</strong><span>neu</span>", page)
        self.assertIn("Prüfung in 6 Tagen", page)
        self.assertIn("Fällige Wiederholungen", page)
        self.assertIn("Nah 0", self.client.get("/learn", headers=ALICE).text)
        deck_page = self.client.get(f"/decks/{near}", headers=ALICE).text
        self.assertIn("10 neue pro Tag", deck_page)
        self.assertIn(f'value="{exam}"', deck_page)
        # Fern: nach zwei neuen ist für heute Schluss
        conn = db.connect(get_settings().db_path)
        for card in conn.execute("SELECT id FROM cards WHERE deck_id = ? ORDER BY id LIMIT 2", (far,)).fetchall():
            self.client.post(f"/learn/{card['id']}", data={"rating": 4, "reps": 0}, headers=ALICE)
        conn.close()
        learn = self.client.get(f"/learn?deck={far}", headers=ALICE).text
        self.assertIn("Tageslimit für neue Karten in diesem Kurs (2)", learn)

    def test_invalid_plan_is_rejected(self):
        deck_id = self.deck("Kurs", 1)
        for data in ({"exam_date": "3.11.2026"}, {"exam_date": "1900-01-01"}, {"new_per_day": "501"},
                     {"new_per_day": "-1"}, {"new_per_day": "x"}):
            res = self.client.post(f"/decks/{deck_id}/plan", data=data, headers=ALICE)
            self.assertIn("msg=plan-invalid", res.headers["location"])
        conn = db.connect(get_settings().db_path)
        row = conn.execute("SELECT exam_date, new_per_day FROM decks").fetchone()
        self.assertEqual((row["exam_date"], row["new_per_day"]), (None, None))
        conn.close()

    def test_empty_deck_shows_plan_without_stats(self):
        deck_id = self.deck("Leer", 0)
        page = self.client.get(f"/decks/{deck_id}", headers=ALICE).text
        self.assertIn('id="planung"', page)
        self.assertNotIn('id="statistik"', page)


if __name__ == "__main__":
    unittest.main()
