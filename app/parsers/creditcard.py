"""信用卡/银行卡账单解析器。

信用卡账单典型列（各银行略有差异，以招行为例）：交易日期 /
信用卡卡号 / 交易摘要 / 交易金额(人民币) / 交易币种 / 折合人民币 /
交易地点 / 收单机构。

储蓄卡（借记卡）流水常带"收/支"方向列，本解析器同样支持：
有方向列时按方向判定收入/支出，无方向列时（典型信用卡账单）
按金额正负 + 关键词判定，保证两类银行账单都能正确分类。
"""
from decimal import Decimal
from typing import Any

from app.parsers.base import BaseParser, norm_direction

_FEE_KW = ("手续费", "分期")
_REFUND_KW = ("退款",)
_REPAY_KW = ("还款", "自动扣款", "溢缴款")


class CreditCardParser(BaseParser):
    source = "card"
    default_col_map = {
        "tx_date": "交易日期",
        "amount": "交易金额(人民币)",
        "merchant": "交易摘要",
        "entry_date": "记账日期",
        "currency": "交易币种",
    }

    def classify(self, amount: Decimal, view: dict[str, Any]) -> str:
        direction = norm_direction(view.get("direction"))
        merchant = str(view.get("merchant") or "")
        desc = str(view.get("description") or "")
        blob = f"{merchant}|{desc}"

        # 退款关键词优先（无论方向）
        if any(k in blob for k in _REFUND_KW):
            return "refund"
        # 信用卡还款属于账户间资金挪移，不计入收支
        if any(k in blob for k in _REPAY_KW):
            return "transfer"
        # 有方向列（储蓄卡流水）：按收支方向判定
        if direction == "income":
            return "income"
        if direction == "expense":
            return "refund" if amount < 0 else "consume"
        # 无方向列（典型信用卡账单）：负金额=退款，手续费=fee，其余消费
        if amount < 0:
            return "refund"
        if any(k in blob for k in _FEE_KW):
            return "fee"
        return "consume"
