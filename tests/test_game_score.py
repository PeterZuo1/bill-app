# -*- coding: utf-8 -*-
"""斗地主记分规则测试。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="gametest_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/")
sys.path.insert(0, PROJ)

from app.database import Base, engine, SessionLocal  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import GameSession, GameRound  # noqa: E402
from app.services import game_service  # noqa: E402


class GameServiceTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(engine)
        self.db = SessionLocal()
        self.db.query(GameRound).delete()
        self.db.query(GameSession).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _setup(self, names=("张三", "李四", "王五")):
        return game_service.setup_players(self.db, list(names))

    def test_initial_state_not_configured(self):
        s = game_service.get_state(self.db)
        self.assertFalse(s["configured"])
        self.assertEqual(s["totals"], [0, 0, 0])
        self.assertEqual(s["rounds"], [])
        self.assertIsNone(s["leader"])

    def test_setup_players_validation(self):
        with self.assertRaises(ValueError):
            self._setup(("张三", "张三", "王五"))
        with self.assertRaises(ValueError):
            self._setup(("张三", "", "王五"))
        s = self._setup()
        self.assertEqual([s.player1, s.player2, s.player3], ["张三", "李四", "王五"])
        # 改名不清空历史
        game_service.add_round(self.db, 0, 1, 1, True)
        game_service.setup_players(self.db, ["甲", "乙", "丙"])
        state = game_service.get_state(self.db)
        self.assertEqual(state["players"], ["甲", "乙", "丙"])
        self.assertEqual(state["round_count"], 1)

    def test_landlord_win_scores(self):
        self._setup()
        r = game_service.add_round(self.db, 0, 2, 3, True)
        self.assertEqual([r.score1, r.score2, r.score3], [12, -6, -6])
        self.assertEqual(sum([r.score1, r.score2, r.score3]), 0)

    def test_landlord_lose_scores(self):
        self._setup()
        r = game_service.add_round(self.db, 0, 1, 2, False)
        self.assertEqual([r.score1, r.score2, r.score3], [-4, 2, 2])

    def test_spring_doubles_scores(self):
        self._setup()
        r = game_service.add_round(self.db, 0, 1, 2, True, spring=True)
        self.assertEqual([r.score1, r.score2, r.score3], [8, -4, -4])

    def test_totals_and_leader(self):
        self._setup()
        game_service.add_round(self.db, 0, 2, 3, True)     # [12, -6, -6]
        game_service.add_round(self.db, 1, 1, 2, False)    # [2, -4, 2]
        s = game_service.get_state(self.db)
        self.assertEqual(s["totals"], [14, -10, -4])
        self.assertEqual(s["leader"], 0)
        self.assertEqual(s["round_count"], 2)

    def test_delete_round_renumbers(self):
        self._setup()
        r1 = game_service.add_round(self.db, 0, 1, 1, True)    # [2, -1, -1]
        r2 = game_service.add_round(self.db, 1, 1, 2, True)    # [-2, 4, -2]
        r3 = game_service.add_round(self.db, 2, 2, 1, False)   # [2, 2, -4]
        game_service.delete_round(self.db, r2.id)
        s = game_service.get_state(self.db)
        self.assertEqual([r["round_no"] for r in s["rounds"]], [1, 2])
        self.assertEqual([r["id"] for r in s["rounds"]], [r1.id, r3.id])
        self.assertEqual(s["totals"], [4, 1, -5])

    def test_clear_keeps_players(self):
        self._setup()
        game_service.add_round(self.db, 0, 1, 1, True)
        game_service.clear_rounds(self.db)
        s = game_service.get_state(self.db)
        self.assertEqual(s["players"], ["张三", "李四", "王五"])
        self.assertEqual(s["round_count"], 0)
        self.assertEqual(s["totals"], [0, 0, 0])


class GameApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def setUp(self):
        Base.metadata.create_all(engine)
        db = SessionLocal()
        db.query(GameRound).delete()
        db.query(GameSession).delete()
        db.commit()
        db.close()

    def test_full_api_flow(self):
        r = self.client.get("/api/game/state")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["configured"])

        r = self.client.put("/api/game/players", json={"players": ["张三", "李四", "王五"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["configured"])

        r = self.client.post("/api/game/rounds", json={
            "landlord": 0, "base_score": 2, "multiplier": 3,
            "landlord_win": True, "spring": False})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["totals"], [12, -6, -6])
        round_id = body["rounds"][0]["id"]

        r = self.client.delete(f"/api/game/rounds/{round_id}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["round_count"], 0)

        r = self.client.post("/api/game/clear")
        self.assertEqual(r.status_code, 200, r.text)

    def test_invalid_payload(self):
        self.client.put("/api/game/players", json={"players": ["张三", "李四", "王五"]})
        r = self.client.post("/api/game/rounds", json={
            "landlord": 3, "base_score": 1, "multiplier": 1, "landlord_win": True})
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
