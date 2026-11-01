import pytest

from ecomops.listing.checkers import violating_terms

WORDS = ("最", "第一", "国家级", "100%", "永久", "根治")


@pytest.mark.parametrize("text", [
    "不是最好的", "没有第一", "不宣称国家级", "不保证100%有效", "并非永久有效", "不能根治疾病", "本品不属于国家级产品", "无最高级用语",
])
def test_negated_mentions_are_not_violations(text):
    assert violating_terms(text, WORDS) == []


@pytest.mark.parametrize("text,expected", [
    ("全网最低价", ["最"]),
    ("不骗你，全网最低价", ["最"]),                 # 否定词和违规词之间隔着标点/别的词：仍是宣称
    ("不仅是最好的，更是行业第一", ["最", "第一"]),    # "不仅是"不是对宣称的否定
    ("国家级认证，永久有效", ["国家级", "永久"]),
    ("不是最好的，但是全网第一", ["第一"]),           # 同一句里一处否定、一处真宣称：真宣称不能被前一处掩盖
])
def test_real_claims_still_flagged(text, expected):
    assert violating_terms(text, WORDS) == expected
