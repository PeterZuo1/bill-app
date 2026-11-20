# -*- coding: utf-8 -*-
"""基于 bill_data 真实账单的导入/去重回归测试。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="billtest_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/")
sys.path.insert(0, str(PROJ))

from app.database import Base, engine, SessionLocal  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import BillFile, Transaction, MergeGroup, DedupCandidate  # noqa: E402
from app.services import import_service, aggregator  # noqa: E402

WECHAT = PROJ / "bill_data" / "微信支付账单流水文件(20261012-20261112)_20261112214220.xlsx"
ALIPAY = PROJ / "bill_data" / "支付宝交易明细(20261012-20261112)" / "支付宝交易明细(20261012-20261112).csv"
CARD = PROJ / "bill_data" / "2026年11月信用卡账单.pdf"


def _reset_db():
    """重建测试表（显式删除顺序 + 关闭外键，绕过 transaction <-> merge_group 循环依赖）。"""
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in (
            DedupCandidate.__table__, Transaction.__table__, MergeGroup.__table__,
            BillFile.__table__, app.models.ImportTemplate.__table__,
        ):
            table.drop(conn, checkfirst=True)
        Base.metadata.create_all(conn)
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


class ImportRealBillsTest(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _import(self, source: str, path: Path, force: bool = False) -> dict:
        return import_service.upload_and_import(
            self.db, source, path.read_bytes(), path.name, force=force)

    def _sum(self, file_id: int, bill_type: str | None = None):
        rows = self.db.query(Transaction).filter(Transaction.file_id == file_id)
        if bill_type:
            rows = rows.filter(Transaction.bill_type == bill_type)
        rows = rows.all()
        return len(rows), sum((r.amount for r in rows), Decimal("0"))

    def test_wechat_import_matches_statement(self):
        r = self._import("wechat", WECHAT)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["rows_imported"], 32)

        n, income = self._sum(r["file_id"], "income")
        self.assertEqual((n, income), (5, Decimal("454.00")))
        n, consume = self._sum(r["file_id"], "consume")
        n_t, transfer = self._sum(r["file_id"], "transfer")
        # 账单「支出 27 笔 808.72」= 消费 26 笔 + 转账 1 笔
        self.assertEqual(n + n_t, 27)
        self.assertEqual(consume + transfer, Decimal("808.72"))

    def test_alipay_neutral_transactions_excluded(self):
        r = self._import("alipay", ALIPAY)
        self.assertEqual(r["rows_imported"], 160)

        n_consume, consume = self._sum(r["file_id"], "consume")
        n_refund, refund = self._sum(r["file_id"], "refund")
        n_transfer, transfer = self._sum(r["file_id"], "transfer")
        self.assertEqual((n_consume, consume), (91, Decimal("3535.94")))
        self.assertEqual((n_refund, refund), (3, Decimal("-26.59")))
        # 「不计收支」69 笔：3 笔退款 + 66 笔中性流水，不再计入消费
        self.assertEqual((n_transfer, transfer), (66, Decimal("298.24")))

        nov = aggregator.get_monthly(self.db, "2026-11")
        self.assertEqual(nov["expense"], Decimal("1753.18"))
        oct_ = aggregator.get_monthly(self.db, "2026-10")
        self.assertEqual(oct_["expense"], Decimal("1756.17"))

    def test_card_pdf_imports_all_rows(self):
        r = self._import("card", CARD)
        # 账单明细 89 行 + 还款 1 行
        self.assertEqual(r["rows_imported"], 90)

        n_consume, consume = self._sum(r["file_id"], "consume")
        n_refund, refund = self._sum(r["file_id"], "refund")
        n_transfer, _ = self._sum(r["file_id"], "transfer")
        self.assertEqual((n_consume, consume), (84, Decimal("3071.88")))
        self.assertEqual((n_refund, refund), (5, Decimal("-76.10")))
        self.assertEqual(n_transfer, 1)  # 掌上生活还款 -> transfer，聚合时过滤

    def test_duplicate_file_skipped(self):
        r1 = self._import("wechat", WECHAT)
        r2 = self._import("wechat", WECHAT)
        self.assertEqual(r1["status"], "ok")
        self.assertEqual(r2["status"], "duplicate")
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r2["rows_imported"], 0)
        self.assertEqual(self.db.query(BillFile).count(), 1)
        self.assertEqual(self.db.query(Transaction).count(), 32)

        r3 = self._import("wechat", WECHAT, force=True)
        self.assertEqual(r3["status"], "ok")
        self.assertEqual(self.db.query(BillFile).count(), 2)

    def test_cross_source_auto_merge_matches_prefixed_merchant(self):
        self._import("wechat", WECHAT)
        self._import("card", CARD)

        groups = self.db.query(MergeGroup).filter(MergeGroup.auto_merged.is_(True)).all()
        merged_pairs = set()
        for g in groups:
            members = self.db.query(Transaction).filter(
                Transaction.merged_group_id == g.id).all()
            sources = {m.source for m in members}
            amounts = {abs(m.amount) for m in members}
            if len(sources) == 2:
                merged_pairs.add((tuple(sorted(sources)), tuple(sorted(amounts))))
        # 微信「肯德基 19.90」与信用卡「财付通-肯德基 19.90」应自动合并
        self.assertIn((("card", "wechat"), (Decimal("19.90"),)), merged_pairs)

    def test_candidate_not_duplicated(self):
        self._import("wechat", WECHAT)
        self._import("alipay", ALIPAY)
        pairs = self.db.query(DedupCandidate).filter(
            DedupCandidate.status == "pending").all()
        keys = [tuple(sorted((c.tx_a_id, c.tx_b_id))) for c in pairs]
        self.assertEqual(len(keys), len(set(keys)), "同一对交易不应重复入队")


class DedupRuleTest(unittest.TestCase):
    """按新规则验证：60 秒时间窗、只配同类型、只配信用卡支付。"""

    WX_HEADER = "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态\n"
    ALI_HEADER = "交易时间,交易分类,交易对方,商品说明,收/支,金额,收/付款方式,交易状态\n"
    CARD_HEADER = "交易日期,记账日期,交易摘要,人民币金额,卡号末四位,交易地金额\n"

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _load(self, source: str, text: str, name: str) -> dict:
        return import_service.upload_and_import(
            self.db, source, text.encode("utf-8-sig"), name, force=True)

    def _groups(self):
        return self.db.query(MergeGroup).all()

    def test_same_file_only_merges_within_60_seconds(self):
        text = self.WX_HEADER + (
            "2026-11-01 12:00:00,商户消费,测试商户,测试商品,支出,10.00,零钱,支付成功\n"
            "2026-11-01 12:00:30,商户消费,测试商户,测试商品,支出,10.00,零钱,支付成功\n"
            "2026-11-01 12:05:00,商户消费,测试商户,测试商品,支出,10.00,零钱,支付成功\n"
        )
        r = self._load("wechat", text, "wx.csv")
        self.assertEqual(r["rows_imported"], 3)
        self.assertEqual(self.db.query(MergeGroup).count(), 1)
        primary = self.db.query(Transaction).filter(Transaction.is_primary.is_(True)).count()
        self.assertEqual(primary, 2)  # 前两笔合并，第三笔（5 分钟后）保留

    def test_card_identical_rows_are_not_merged(self):
        text = self.CARD_HEADER + (
            "11/03,11/04,抖音支付-金拱门,13.90,2782,13.90\n"
            "11/03,11/04,抖音支付-金拱门,13.90,2782,13.90\n"
        )
        r = self._load("card", text, "card.csv")
        self.assertEqual(r["rows_imported"], 2)
        self.assertEqual(self.db.query(MergeGroup).count(), 0)
        self.assertEqual(self.db.query(Transaction).filter(
            Transaction.is_primary.is_(True)).count(), 2)

    def test_consume_matches_consume_and_refund_matches_refund(self):
        self._load("card", self.CARD_HEADER + (
            "11/02,11/03,测试商户,12.27,2782,12.27\n"
            "11/02,11/03,测试商户,-12.27,2782,-12.27\n"
        ), "card.csv")
        self._load("alipay", self.ALI_HEADER + (
            "2026-11-02 10:00:00,日用百货,测试商户,测试商户,支出,12.27,招商银行信用卡(2782),交易成功\n"
            "2026-11-02 10:01:00,日用百货,测试商户,退款-测试商户,支出,12.27,招商银行信用卡(2782),退款成功\n"
        ), "ali.csv")
        groups = self._groups()
        self.assertEqual(len(groups), 2)
        for g in groups:
            types = {m.bill_type for m in self.db.query(Transaction).filter(
                Transaction.merged_group_id == g.id)}
            self.assertEqual(len(types), 1, "付款不能和退款合并")

    def test_time_gap_and_non_card_payment_block_merge(self):
        self._load("card", self.CARD_HEADER + "11/04 12:00:00,11/04,测试商户,8.88,2782,8.88\n", "card.csv")
        self._load("alipay", self.ALI_HEADER + (
            "2026-11-04 12:05:00,日用百货,测试商户,测试商户,支出,8.88,招商银行信用卡(2782),交易成功\n"
            "2026-11-04 12:00:00,日用百货,另一个商户,另一个商户,支出,8.88,花呗,交易成功\n"
        ), "ali.csv")
        self.assertEqual(self.db.query(MergeGroup).count(), 0)
        self.assertEqual(self.db.query(DedupCandidate).count(), 0)


class UploadApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def setUp(self):
        _reset_db()

    def test_upload_then_duplicate_and_invalid_detect(self):
        with open(WECHAT, "rb") as f:
            r = self.client.post(
                "/api/bills/upload",
                files={"file": (WECHAT.name, f, "application/vnd.ms-excel")},
                data={"source": "wechat"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(r.json()["rows_imported"], 32)

        with open(WECHAT, "rb") as f:
            r2 = self.client.post(
                "/api/bills/upload",
                files={"file": (WECHAT.name, f, "application/vnd.ms-excel")},
                data={"source": "wechat"})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r2.json()["duplicate"])

        r3 = self.client.post(
            "/api/templates/detect",
            files={"file": ("broken.pdf", b"not a pdf", "application/pdf")},
            data={"source": "card"})
        self.assertEqual(r3.status_code, 400, r3.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
