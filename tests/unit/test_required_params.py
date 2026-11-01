import pytest

from ecomops.listing.checkers import DEFAULT_REQUIRED, REQUIRED_PARAMS, check, extract_structured

EXPECTED = {
    "HOME_APPLIANCE_HEATER": ("额定功率", "容量", "能效等级", "3C"),
    "PERSONAL_SHAMPOO": ("净含量", "保质期", "备案号"),
    "DAILY_GENERAL": ("品名", "规格"),
    "DIGITAL_TABLET": ("品名", "屏幕尺寸", "存储容量", "3C"),
    "DIGITAL_PHONE": ("品名", "屏幕尺寸", "存储容量", "3C"),
    "DIGITAL_COMPUTER": ("品名", "处理器", "内存", "存储容量"),
    "BOOKS": ("书名", "ISBN", "出版社"),
    "FRESH_FRUIT": ("品名", "产地", "净含量", "保质期"),
    "DAIRY_MILK": ("品名", "净含量", "保质期", "生产许可证编号"),
    "APPAREL": ("品名", "面料成分", "尺码", "执行标准"),
    "TRAVEL_HOTEL": ("酒店名称", "地址", "入住时间", "退订规则"),
}


def missing(category, fields):
    text = "；".join(f"{f}：样例值" for f in fields)
    result = check("required_params", {"text": text, "certificates": []}, category, extract_structured(text))
    return [v["field"] for v in result["blocking_violations"]]


def test_table_matches_the_documented_contract_for_every_catalog_category():
    assert REQUIRED_PARAMS == EXPECTED and DEFAULT_REQUIRED == ("品名",)


@pytest.mark.parametrize("category,fields", EXPECTED.items())
def test_complete_sheet_passes_and_each_missing_field_is_reported(category, fields):
    assert missing(category, fields) == []
    for field in fields:
        assert missing(category, [f for f in fields if f != field]) == [field]
    assert missing(category, []) == list(fields)


def test_unknown_category_only_requires_product_name():
    assert missing("SOMETHING_ELSE", []) == ["品名"]
    assert missing("SOMETHING_ELSE", ["品名"]) == []
