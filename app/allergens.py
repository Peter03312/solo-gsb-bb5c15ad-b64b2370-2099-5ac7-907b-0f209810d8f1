"""过敏原标识目录与校验。

对外接受规范代码（大写英文）与常用中文名称，内部统一使用规范代码。
标识集合参考 GB 7718 / 食品过敏原管理常见的八大类及其常见子类。
"""
from __future__ import annotations

# 规范代码 -> (标准中文名, 可接受的其他名称)
ALLERGENS: dict[str, tuple[str, tuple[str, ...]]] = {
    "GLUTEN": ("含麸质谷物", ("gluten", "麸质", "麸质谷物", "小麦", "大麦", "黑麦", "燕麦")),
    "CRUSTACEAN": ("甲壳类动物", ("crustaceans", "甲壳类", "虾", "蟹", "龙虾")),
    "EGG": ("蛋类", ("egg", "eggs", "鸡蛋", "蛋", "蛋制品")),
    "FISH": ("鱼类", ("fish", "鱼", "鱼类制品")),
    "PEANUT": ("花生", ("peanuts", "花生类")),
    "SOY": ("大豆", ("soybeans", "soya", "黄豆", "豆类(大豆)")),
    "MILK": ("乳", ("milk", "dairy", "牛奶", "乳制品", "奶类")),
    "TREE_NUT": ("坚果", ("nuts", "tree nuts", "tree_nuts", "坚果类", "杏仁", "腰果", "榛子")),
    "CELERY": ("芹菜", ("celery", "芹菜类")),
    "MUSTARD": ("芥茉", ("mustard", "芥末", "芥类")),
    "SESAME": ("芝麻", ("sesame", "sesame seeds", "芝麻籽")),
    "SULPHITE": ("亚硫酸盐", ("sulphites", "sulfites", "sulphur dioxide", "二氧化硫", "亚硫酸盐(≥10mg/kg)")),
    "LUPIN": ("羽扇豆", ("lupin", "lupine", "羽扇豆类")),
    "MOLLUSC": ("软体动物", ("molluscs", "mollusks", "贝类", "软体类")),
}

# 别名（小写、去空格后） -> 规范代码
_ALIAS_TO_CODE: dict[str, str] = {}
for _code, (_name, _aliases) in ALLERGENS.items():
    _ALIAS_TO_CODE[_code.lower()] = _code
    _ALIAS_TO_CODE[_name.lower()] = _code
    for _alias in _aliases:
        _ALIAS_TO_CODE[_alias.lower()] = _code


class AllergenError(ValueError):
    """无效的过敏原标识。"""

    def __init__(self, value: str):
        self.value = value
        super().__init__(f"无效的过敏原标识: {value!r}")


def normalize_allergens(values: list[str] | None) -> list[str]:
    """把输入标识列表规范化为去重、排序后的规范代码列表。

    接受规范代码本身或中文/英文别名；无法识别时抛出 AllergenError。
    """
    if not values:
        return []
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise AllergenError(str(value))
        key = value.strip().upper().replace(" ", "_")
        # 先尝试直接命中规范代码
        if key in ALLERGENS:
            normalized.add(key)
            continue
        code = _ALIAS_TO_CODE.get(value.strip().lower())
        if code is None:
            raise AllergenError(value)
        normalized.add(code)
    return sorted(normalized)


def display_name(code: str) -> str:
    """规范代码 -> 中文名，未知代码原样返回。"""
    entry = ALLERGENS.get(code)
    return entry[0] if entry else code


def catalog() -> list[dict[str, str]]:
    """返回完整过敏原目录。"""
    return [
        {"code": code, "name": name, "aliases": list(aliases)}
        for code, (name, aliases) in ALLERGENS.items()
    ]
