"""纯粹的抖音省份地点搜索计划与翻页状态机。"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re


# 使用抖音搜索框已经接受的短名称。顺序是用户可见的稳定查询顺序，不能依赖
# 字典排序或外部行政区接口在运行时生成。
PROVINCE_CITIES: dict[str, tuple[str, ...]] = {
    "河北": ("石家庄", "唐山", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水", "秦皇岛"),
    "山西": ("太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城", "忻州", "临汾", "吕梁"),
    "辽宁": ("沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛"),
    "吉林": ("长春", "吉林", "四平", "辽源", "通化", "白山", "松原", "白城", "延边"),
    "黑龙江": ("哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯", "七台河", "牡丹江", "黑河", "绥化", "大兴安岭"),
    "江苏": ("南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安", "盐城", "扬州", "镇江", "泰州", "宿迁"),
    "浙江": ("杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", "舟山", "台州", "丽水"),
    "安徽": ("合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州", "阜阳", "宿州", "六安", "亳州", "池州", "宣城"),
    "福建": ("福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德"),
    "江西": ("南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春", "抚州", "上饶"),
    "山东": ("济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁", "泰安", "威海", "日照", "临沂", "德州", "聊城", "滨州", "菏泽"),
    "河南": ("郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店"),
    "湖北": ("武汉", "黄石", "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州", "黄冈", "咸宁", "随州", "恩施"),
    "湖南": ("长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州", "永州", "怀化", "娄底", "湘西"),
    "广东": ("广州", "深圳", "佛山", "东莞", "珠海", "惠州", "中山", "江门", "肇庆", "汕头", "潮州", "揭阳", "汕尾", "韶关", "清远", "河源", "梅州", "阳江", "茂名", "湛江", "云浮"),
    "海南": ("海口", "三亚", "三沙", "儋州"),
    "四川": ("成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江", "乐山", "南充", "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳", "阿坝", "甘孜", "凉山"),
    "贵州": ("贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁", "黔西南", "黔东南", "黔南"),
    "云南": ("昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧", "楚雄", "红河", "文山", "西双版纳", "大理", "德宏", "怒江", "迪庆"),
    "陕西": ("西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛"),
    "甘肃": ("兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉", "庆阳", "定西", "陇南", "临夏", "甘南"),
    "青海": ("西宁", "海东", "海北", "黄南", "海南", "果洛", "玉树", "海西"),
    "内蒙古": ("呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔", "巴彦淖尔", "乌兰察布", "兴安", "锡林郭勒", "阿拉善"),
    "广西": ("南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林", "百色", "贺州", "河池", "来宾", "崇左"),
    "西藏": ("拉萨", "日喀则", "昌都", "林芝", "山南", "那曲", "阿里"),
    "宁夏": ("银川", "石嘴山", "吴忠", "固原", "中卫"),
    "新疆": ("乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "昌吉", "博尔塔拉", "巴音郭楞", "阿克苏", "克孜勒苏", "喀什", "和田", "伊犁", "塔城", "阿勒泰"),
}

MUNICIPALITIES = frozenset(("北京", "天津", "上海", "重庆"))

_REGION_SUFFIXES: dict[str, tuple[str, ...]] = {
    "内蒙古": ("内蒙古", "内蒙古自治区"),
    "广西": ("广西", "广西壮族自治区"),
    "西藏": ("西藏", "西藏自治区"),
    "宁夏": ("宁夏", "宁夏回族自治区"),
    "新疆": ("新疆", "新疆维吾尔自治区"),
}


@dataclass(frozen=True)
class LocationSearchPlan:
    schema_version: int
    original_keyword: str
    search_kind: str
    province: str
    merchant_term: str
    subqueries: tuple[str, ...]
    current_index: int = 0
    current_load_count: int = 0
    completed_indices: tuple[int, ...] = ()
    last_error_code: str = ""

    @property
    def current_keyword(self) -> str:
        return self.subqueries[self.current_index]

    @property
    def exhausted(self) -> bool:
        return len(self.completed_indices) == len(self.subqueries)


def build_location_search_plan(keyword: object) -> LocationSearchPlan:
    """把开头的大陆省份词拆成确定顺序的城市搜索计划。"""

    normalized = _normalize_keyword(keyword)
    city, city_merchant = _leading_city(normalized, include_bare=False)
    if city and city_merchant:
        return LocationSearchPlan(1, normalized, "city", "", city_merchant, (normalized,))

    province, merchant = _leading_region(normalized)
    if province and merchant:
        subqueries = (normalized,) + tuple(
            f"{city}{'市' if city == province else ''}{merchant}"
            for city in PROVINCE_CITIES[province]
        )
        return LocationSearchPlan(1, normalized, "province", province, merchant, subqueries)

    city, city_merchant = _leading_city(normalized, include_bare=True)
    search_kind = "city" if city and city_merchant else "plain"
    return LocationSearchPlan(1, normalized, search_kind, "", city_merchant, (normalized,))


def advance_after_page(
    plan: LocationSearchPlan, *, has_more: bool, eligible_total: int
) -> LocationSearchPlan:
    """记录当前平台页的结果；仅在平台明确耗尽时才前进到下一个子词。"""

    if plan.exhausted:
        return replace(plan, last_error_code="")
    if eligible_total >= 100:
        return replace(
            plan,
            completed_indices=tuple(range(len(plan.subqueries))),
            last_error_code="",
        )
    if has_more:
        return replace(
            plan,
            current_load_count=plan.current_load_count + 1,
            last_error_code="",
        )

    completed = tuple(sorted(set(plan.completed_indices + (plan.current_index,))))
    if len(completed) == len(plan.subqueries):
        return replace(plan, completed_indices=completed, last_error_code="")
    return replace(
        plan,
        current_index=plan.current_index + 1,
        current_load_count=0,
        completed_indices=completed,
        last_error_code="",
    )


def record_plan_error(plan: LocationSearchPlan, error_code: object) -> LocationSearchPlan:
    """保存可重试的平台错误，绝不把当前子词标记为已耗尽。"""

    return replace(plan, last_error_code=_normalize_keyword(error_code))


def plan_progress_text(
    plan: LocationSearchPlan, *, filter_label: str, eligible_total: int
) -> str:
    """返回给界面展示的当前计划进度，不把无新增视为终态。"""

    label = _normalize_keyword(filter_label) or "有效"
    if plan.search_kind != "province":
        return f"当前{plan.current_keyword} · 已找到{label}地址 {eligible_total} 个"

    city_total = len(plan.subqueries) - 1
    completed_cities = sum(index > 0 for index in plan.completed_indices)
    if plan.exhausted:
        return f"{plan.province}已检查 {city_total}/{city_total} 个城市 · 已找到{label}地址 {eligible_total} 个"
    current_city = plan.province if plan.current_index == 0 else plan.current_keyword.removesuffix(plan.merchant_term)
    return (
        f"{plan.province}已检查 {completed_cities}/{city_total} 个城市 · 当前{current_city} "
        f"· 已找到{label}地址 {eligible_total} 个"
    )


def _normalize_keyword(value: object) -> str:
    return re.sub(r"[\s\u3000]+", "", "" if value is None else str(value))


def _leading_region(keyword: str) -> tuple[str, str]:
    aliases: list[tuple[str, str]] = []
    for province in PROVINCE_CITIES:
        if province in _REGION_SUFFIXES:
            aliases.extend((alias, province) for alias in _REGION_SUFFIXES[province])
        else:
            aliases.extend(((f"{province}省", province), (province, province)))
    for alias, province in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if keyword.startswith(alias) and keyword[len(alias) :]:
            return province, keyword[len(alias) :]
    return "", ""


def _leading_city(keyword: str, *, include_bare: bool) -> tuple[str, str]:
    city_names = set(MUNICIPALITIES)
    city_names.update(city for cities in PROVINCE_CITIES.values() for city in cities)
    aliases: list[tuple[str, str]] = []
    for city in city_names:
        aliases.append((f"{city}市", city))
        if include_bare:
            aliases.append((city, city))
    for alias, city in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if keyword.startswith(alias) and keyword[len(alias) :]:
            return city, keyword[len(alias) :]
    return "", ""
