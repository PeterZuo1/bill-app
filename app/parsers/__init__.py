"""解析器注册表：按 source 返回对应渠道解析器。"""
from app.parsers.wechat import WechatParser
from app.parsers.alipay import AlipayParser
from app.parsers.creditcard import CreditCardParser
from app.parsers.base import BaseParser

_PARSERS: dict[str, BaseParser] = {
    WechatParser.source: WechatParser(),
    AlipayParser.source: AlipayParser(),
    CreditCardParser.source: CreditCardParser(),
}


def get_parser(source: str) -> BaseParser:
    parser = _PARSERS.get(source)
    if parser is None:
        raise ValueError(f"不支持的渠道 source={source}")
    return parser


def supported_sources() -> list[str]:
    return list(_PARSERS.keys())
