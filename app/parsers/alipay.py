"""支付宝账单解析器。

支付宝账单典型列：交易创建时间 / 交易对方 / 商品名称 / 收/支 /
交易金额 / 故付方式 / 当前状态 / 交易号 / 备注。
"""
from decimal import Decimal
from typing import Any

from app.parsers.base import BaseParser, norm_direction

_TRANSFER_KW = ("转账", "转给", "转出")
_REFUND_KW = ("退款", "退货退款")


class AlipayParser(BaseParser):
    source = "alipay"
    default_col_map = {
        "tx_date": "交易创建时间",
        "amount": "交易金额",
        "direction": "收/支",
        "merchant": "商品名称",
        "counterparty": "交易对方",
        "description": "备注",
        "payment_method": "收/付款方式",
    }

    def classify(self, amount: Decimal, view: dict[str, Any]) -> str:
        direction = norm_direction(view.get("direction"))
        merchant = str(view.get("merchant") or "")
        counterparty = str(view.get("counterparty") or "")
        desc = str(view.get("description") or "")
        blob = f"{merchant}|{counterparty}|{desc}"

        if any(k in blob for k in _REFUND_KW):
            return "refund"
        if direction == "transfer":
            # 不计收支（还款/提现/理财申赎等）：不计入收支
            return "transfer"
        if direction == "income":
            return "income"
        if direction == "expense":
            if any(k in blob for k in _TRANSFER_KW):
                return "transfer"
            return "consume"
        # 无方向列时的兜底：负金额=退款
        if amount < 0:
            return "refund"
        return "consume"
