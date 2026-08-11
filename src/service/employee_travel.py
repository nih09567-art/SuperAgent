from __future__ import annotations

import json
import math
import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional


TRAVEL_FIELDS = (
    "employee_name",
    "origin",
    "destination",
    "start_date",
    "end_date",
    "purpose",
    "project_or_cost_center",
    "transport_preference",
    "accommodation_standard",
)

TRAVEL_QUESTIONS = {
    "employee_name": "请问是哪位员工出差？",
    "origin": "请问从哪里出发？",
    "destination": "请问目的地是哪里？",
    "start_date": "请问哪天出发？请使用 YYYY-MM-DD 格式。",
    "end_date": "请问哪天返回？请使用 YYYY-MM-DD 格式。",
    "purpose": "请问本次差旅的事由是什么？",
    "project_or_cost_center": "请问关联哪个项目或成本中心？",
    "transport_preference": "请问交通偏好是什么（如高铁、飞机）？",
    "accommodation_standard": "请问住宿标准是多少（如 500 元/晚）？",
}

TRAVEL_LABELS = {
    "employee_name": "员工",
    "origin": "出发地",
    "destination": "目的地",
    "start_date": "出发日期",
    "end_date": "返回日期",
    "purpose": "事由",
    "project_or_cost_center": "项目或成本中心",
    "transport_preference": "交通偏好",
    "accommodation_standard": "住宿标准",
}

CITY_COORDINATES = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "天津": (39.3434, 117.3616),
    "重庆": (29.4316, 106.9123),
    "石家庄": (38.0428, 114.5149),
    "太原": (37.8706, 112.5489),
    "呼和浩特": (40.8426, 111.7492),
    "沈阳": (41.8057, 123.4315),
    "大连": (38.9140, 121.6147),
    "长春": (43.8171, 125.3235),
    "哈尔滨": (45.8038, 126.5349),
    "南京": (32.0603, 118.7969),
    "苏州": (31.2989, 120.5853),
    "无锡": (31.4912, 120.3119),
    "常州": (31.8107, 119.9741),
    "南通": (31.9802, 120.8943),
    "扬州": (32.3942, 119.4129),
    "徐州": (34.2044, 117.2858),
    "杭州": (30.2741, 120.1551),
    "宁波": (29.8683, 121.5440),
    "温州": (27.9938, 120.6994),
    "金华": (29.0792, 119.6474),
    "绍兴": (30.0302, 120.5802),
    "合肥": (31.8206, 117.2272),
    "黄山": (29.7147, 118.3376),
    "福州": (26.0745, 119.2965),
    "厦门": (24.4798, 118.0894),
    "泉州": (24.8741, 118.6757),
    "南昌": (28.6820, 115.8579),
    "赣州": (25.8311, 114.9350),
    "济南": (36.6512, 117.1201),
    "青岛": (36.0671, 120.3826),
    "烟台": (37.4638, 121.4479),
    "郑州": (34.7466, 113.6254),
    "洛阳": (34.6197, 112.4540),
    "武汉": (30.5928, 114.3055),
    "宜昌": (30.6919, 111.2865),
    "长沙": (28.2282, 112.9388),
    "株洲": (27.8274, 113.1339),
    "湘潭": (27.8297, 112.9441),
    "张家界": (29.1171, 110.4792),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "珠海": (22.2707, 113.5767),
    "佛山": (23.0215, 113.1214),
    "东莞": (23.0207, 113.7518),
    "惠州": (23.1115, 114.4152),
    "中山": (22.5176, 113.3928),
    "南宁": (22.8170, 108.3669),
    "桂林": (25.2736, 110.2900),
    "海口": (20.0440, 110.1999),
    "三亚": (18.2528, 109.5119),
    "成都": (30.5728, 104.0668),
    "贵阳": (26.6470, 106.6302),
    "昆明": (25.0389, 102.7183),
    "拉萨": (29.6520, 91.1721),
    "西安": (34.3416, 108.9398),
    "兰州": (36.0611, 103.8343),
    "西宁": (36.6171, 101.7782),
    "银川": (38.4872, 106.2309),
    "乌鲁木齐": (43.8256, 87.6168),
    "香港": (22.3193, 114.1694),
    "澳门": (22.1987, 113.5439),
    "台北": (25.0330, 121.5654),
}


def _clean_value(value: Any) -> str:
    return str(value or "").strip().strip("，。；;：:")


def _normalize_date(value: str) -> str:
    text = _clean_value(value)
    if not text:
        return ""
    for pattern in (
        r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$",
        r"^(\d{1,2})[-/.月](\d{1,2})日?$",
    ):
        match = re.match(pattern, text)
        if not match:
            continue
        values = [int(item) for item in match.groups()]
        if len(values) == 2:
            values.insert(0, datetime.now().year)
        try:
            return date(*values).isoformat()
        except ValueError:
            return ""
    return ""


def _extract_dates(text: str) -> list[str]:
    candidates = re.findall(
        r"(?:\d{4}[-/.年])?\d{1,2}(?:[-/.月])\d{1,2}日?",
        text,
    )
    dates: list[str] = []
    for candidate in candidates:
        normalized = _normalize_date(candidate)
        if normalized and normalized not in dates:
            dates.append(normalized)
    return dates


def _city_coordinates(value: Any) -> Optional[tuple[float, float]]:
    normalized = re.sub(r"[\s市区县]+$", "", _clean_value(value))
    if normalized in CITY_COORDINATES:
        return CITY_COORDINATES[normalized]
    return next(
        (coordinates for city, coordinates in CITY_COORDINATES.items() if city in normalized),
        None,
    )


def _route_distance_km(origin: Any, destination: Any) -> Optional[int]:
    start = _city_coordinates(origin)
    end = _city_coordinates(destination)
    if not start or not end:
        return None
    latitude_1, longitude_1 = map(math.radians, start)
    latitude_2, longitude_2 = map(math.radians, end)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = longitude_2 - longitude_1
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2) ** 2
    )
    return round(6371 * 2 * math.asin(math.sqrt(haversine)))


def _transport_cost(transport: str, distance_km: Optional[int]) -> float:
    if distance_km is None:
        if "飞机" in transport:
            return 1600.0
        if "高铁" in transport or "动车" in transport:
            return 800.0
        if "火车" in transport:
            return 500.0
        if any(item in transport for item in ("自驾", "汽车", "大巴")):
            return 300.0
        return 800.0

    if "飞机" in transport:
        bands = (
            (800, 1000.0),
            (1500, 1400.0),
            (2400, 1800.0),
            (float("inf"), 2200.0),
        )
    elif "高铁" in transport or "动车" in transport:
        bands = (
            (800, 600.0),
            (1200, 800.0),
            (1800, 1200.0),
            (2400, 1600.0),
            (float("inf"), 2000.0),
        )
    elif "火车" in transport:
        bands = (
            (800, 400.0),
            (1200, 500.0),
            (1800, 700.0),
            (2400, 900.0),
            (float("inf"), 1200.0),
        )
    elif any(item in transport for item in ("自驾", "汽车", "大巴")):
        bands = (
            (500, 300.0),
            (1000, 600.0),
            (1800, 900.0),
            (2400, 1200.0),
            (float("inf"), 1500.0),
        )
    else:
        bands = (
            (800, 600.0),
            (1200, 800.0),
            (1800, 1200.0),
            (2400, 1600.0),
            (float("inf"), 2000.0),
        )
    return next(cost for maximum, cost in bands if distance_km <= maximum)


def extract_travel_fields(message: str) -> dict[str, str]:
    text = _clean_value(message)
    fields: dict[str, str] = {}

    employee_patterns = (
        r"帮\s*([\u4e00-\u9fa5A-Za-z·]{2,12}?)\s*(?:办|申请|安排)(?:一?次)?(?:差旅|出差)",
        r"(?:员工|申请人)\s*[：:]?\s*([\u4e00-\u9fa5A-Za-z·]{2,12})",
        r"([\u4e00-\u9fa5A-Za-z·]{2,12})\s*的(?:差旅|出差)",
    )
    for pattern in employee_patterns:
        match = re.search(pattern, text)
        if match:
            fields["employee_name"] = _clean_value(match.group(1))
            break

    route = re.search(
        r"从\s*([^，,。;；\s]{1,20})\s*(?:到|去|前往)\s*([^，,。;；\s]{1,20})",
        text,
    )
    if route:
        fields["origin"] = _clean_value(route.group(1))
        fields["destination"] = _clean_value(route.group(2))
    else:
        origin = re.search(r"(?:出发地|从)\s*(?:是|为|改为)?\s*[：:]?\s*([^，,。;；\s]{1,20})", text)
        destination = re.search(r"(?:目的地|前往|去)\s*(?:是|为|改为)?\s*[：:]?\s*([^，,。;；\s]{1,20})", text)
        if origin:
            fields["origin"] = _clean_value(origin.group(1))
        if destination:
            fields["destination"] = _clean_value(destination.group(1))

    start_label = re.search(
        r"(?:出发日期|开始日期|出发时间)\s*(?:是|为|改为)?\s*[：:]?\s*((?:\d{4}[-/.年])?\d{1,2}(?:[-/.月])\d{1,2}日?)",
        text,
    )
    end_label = re.search(
        r"(?:返回日期|结束日期|返程日期|返回时间)\s*(?:是|为|改为)?\s*[：:]?\s*((?:\d{4}[-/.年])?\d{1,2}(?:[-/.月])\d{1,2}日?)",
        text,
    )
    if start_label:
        fields["start_date"] = _normalize_date(start_label.group(1))
    if end_label:
        fields["end_date"] = _normalize_date(end_label.group(1))
    dates = _extract_dates(text)
    if dates and "start_date" not in fields and "end_date" not in fields:
        fields["start_date"] = dates[0]
    if len(dates) > 1 and "end_date" not in fields:
        fields["end_date"] = dates[1]

    labelled_patterns = {
        "purpose": r"(?:事由|原因|目的)\s*(?:是|为|改为)?\s*[：:]?\s*([^，,。;；]+)",
        "project_or_cost_center": r"(?:项目|成本中心)\s*(?:是|为|改为)?\s*[：:]?\s*([^，,。;；]+)",
        "transport_preference": r"(?:交通偏好|交通方式|交通)\s*(?:是|为|改为)?\s*[：:]?\s*([^，,。;；]+)",
        "accommodation_standard": r"(?:住宿标准|住宿)\s*(?:是|为|改为)?\s*[：:]?\s*([^，,。;；]+)",
    }
    for field, pattern in labelled_patterns.items():
        match = re.search(pattern, text)
        if match:
            fields[field] = _clean_value(match.group(1))

    if "transport_preference" not in fields:
        transport = re.search(r"(高铁|动车|火车|飞机|自驾|汽车|大巴)", text)
        if transport:
            fields["transport_preference"] = transport.group(1)
    if "accommodation_standard" not in fields:
        accommodation = re.search(r"(\d+(?:\.\d+)?\s*元?\s*/?\s*晚)", text)
        if accommodation:
            fields["accommodation_standard"] = _clean_value(accommodation.group(1))
    return fields


def estimate_budget(draft: dict[str, Any]) -> dict[str, Any]:
    start = date.fromisoformat(str(draft["start_date"]))
    end = date.fromisoformat(str(draft["end_date"]))
    if end < start:
        raise ValueError("返回日期不能早于出发日期")

    trip_days = (end - start).days + 1
    nights = max((end - start).days, 0)
    transport = str(draft.get("transport_preference") or "")
    distance_km = _route_distance_km(draft.get("origin"), draft.get("destination"))
    used_fallback_distance = distance_km is None
    pricing_distance_km = 1400 if used_fallback_distance else distance_km
    transport_cost = _transport_cost(transport, pricing_distance_km)

    standard = str(draft.get("accommodation_standard") or "")
    standard_match = re.search(r"\d+(?:\.\d+)?", standard)
    nightly_rate = float(standard_match.group()) if standard_match else 400.0
    accommodation_cost = nightly_rate * nights
    allowance_cost = 100.0 * trip_days
    total = transport_cost + accommodation_cost + allowance_cost
    route_note = (
        "城市未命中离线坐标，交通费按跨区域约 1400 公里及交通偏好估算"
        if used_fallback_distance
        else f"交通费按路线约 {distance_km} 公里及交通偏好估算"
    )
    return {
        "currency": "CNY",
        "transport": round(transport_cost, 2),
        "accommodation": round(accommodation_cost, 2),
        "allowance": round(allowance_cost, 2),
        "total": round(total, 2),
        "trip_days": trip_days,
        "nights": nights,
        "note": f"{route_note}，补贴按 100 元/天预估。",
    }


class EmployeeTravelService:
    def __init__(self, storage_path: Path):
        self.storage_path = Path(storage_path)
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        if not self.storage_path.exists():
            return []
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(f"{self.storage_path.suffix}.tmp")
        temporary.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.storage_path)

    def create(
        self,
        *,
        user_id: str,
        draft: dict[str, Any],
        plan_id: str,
    ) -> tuple[dict[str, Any], bool]:
        normalized = {field: _clean_value(draft.get(field)) for field in TRAVEL_FIELDS}
        missing = [field for field in TRAVEL_FIELDS if not normalized[field]]
        if missing:
            raise ValueError(f"缺少字段：{TRAVEL_LABELS[missing[0]]}")
        normalized["start_date"] = _normalize_date(normalized["start_date"])
        normalized["end_date"] = _normalize_date(normalized["end_date"])
        if not normalized["start_date"] or not normalized["end_date"]:
            raise ValueError("日期格式无效，请使用 YYYY-MM-DD")
        budget = estimate_budget(normalized)

        with self._lock:
            records = self._load()
            for record in records:
                if record.get("user_id") == user_id and record.get("plan_id") == plan_id:
                    return dict(record), False
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            record = {
                "request_id": f"TR-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
                "user_id": user_id,
                "plan_id": plan_id,
                **normalized,
                "budget": budget,
                "status": "已创建",
                "created_at": now,
                "updated_at": now,
            }
            records.append(record)
            self._save(records)
        return dict(record), True

    def query(
        self,
        *,
        user_id: str,
        employee_name: str = "",
        travel_date: str = "",
        destination: str = "",
        status: str = "",
        request_id: str = "",
    ) -> list[dict[str, Any]]:
        target_date = _normalize_date(travel_date) if travel_date else ""
        with self._lock:
            records = [dict(item) for item in self._load()]
        results = []
        for record in records:
            if record.get("user_id") != user_id:
                continue
            if request_id and request_id.upper() != str(record.get("request_id", "")).upper():
                continue
            if employee_name and employee_name not in str(record.get("employee_name", "")):
                continue
            if destination and destination not in str(record.get("destination", "")):
                continue
            if status and status != str(record.get("status", "")):
                continue
            if target_date and not (
                str(record.get("start_date", "")) <= target_date <= str(record.get("end_date", ""))
            ):
                continue
            results.append(record)
        return sorted(results, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def update(
        self,
        *,
        user_id: str,
        request_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {field: _clean_value(value) for field, value in changes.items() if field in TRAVEL_FIELDS and _clean_value(value)}
        if not allowed:
            raise ValueError("没有识别到需要修改的差旅字段")
        with self._lock:
            records = self._load()
            for record in records:
                if record.get("user_id") != user_id or str(record.get("request_id", "")).upper() != request_id.upper():
                    continue
                if record.get("status") == "已撤销":
                    raise ValueError("已撤销的差旅申请不能修改")
                if "start_date" in allowed:
                    allowed["start_date"] = _normalize_date(allowed["start_date"])
                if "end_date" in allowed:
                    allowed["end_date"] = _normalize_date(allowed["end_date"])
                if ("start_date" in allowed and not allowed["start_date"]) or ("end_date" in allowed and not allowed["end_date"]):
                    raise ValueError("日期格式无效，请使用 YYYY-MM-DD")
                record.update(allowed)
                record["budget"] = estimate_budget(record)
                record["status"] = "已修改"
                record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                self._save(records)
                return dict(record)
        raise KeyError("差旅申请不存在")

    def cancel(self, *, user_id: str, request_id: str) -> dict[str, Any]:
        with self._lock:
            records = self._load()
            for record in records:
                if record.get("user_id") != user_id or str(record.get("request_id", "")).upper() != request_id.upper():
                    continue
                record["status"] = "已撤销"
                record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                self._save(records)
                return dict(record)
        raise KeyError("差旅申请不存在")


def _next_missing_field(draft: dict[str, Any]) -> Optional[str]:
    return next((field for field in TRAVEL_FIELDS if not _clean_value(draft.get(field))), None)


def _extract_request_id(message: str) -> str:
    match = re.search(
        r"(?<![A-Z0-9])TR-\d{8}-[A-Z0-9]{6}(?![A-Z0-9])",
        message.upper(),
    )
    return match.group(0) if match else ""


def _query_filters(message: str) -> dict[str, str]:
    fields = extract_travel_fields(message)
    matched_status = next(
        (item for item in ("已创建", "已修改", "已撤销") if item in message),
        "",
    )
    filters = {
        "employee_name": fields.get("employee_name", ""),
        "destination": fields.get("destination", ""),
        "travel_date": (_extract_dates(message) or [""])[0],
        "status": matched_status,
        "request_id": _extract_request_id(message),
    }
    employee = re.search(
        r"(?:查询|查看|查一下|查)\s*([\u4e00-\u9fa5A-Za-z·]{2,12}?)\s*(?:的)?(?:差旅|出差)",
        message,
    )
    if employee and not employee.group(1).startswith(("去", "到", "前往")):
        filters["employee_name"] = employee.group(1)
    destination = re.search(
        r"(?:去|到|前往|目的地(?:是|为)?)\s*([\u4e00-\u9fa5A-Za-z]{2,12}?)(?:的)?(?:差旅|出差|申请|记录|$)",
        message,
    )
    if destination:
        filters["destination"] = destination.group(1)
        filters["employee_name"] = ""
    if matched_status and matched_status in filters["employee_name"]:
        filters["employee_name"] = ""
    return filters


def handle_travel_turn(
    service: EmployeeTravelService,
    *,
    user_id: str,
    conversation_id: str,
    message: str,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    current = dict(state or {})
    draft = dict(current.get("draft") or {})
    mode = str(current.get("mode") or "")
    text = _clean_value(message)

    # An explicit record command always starts a new operation, even if the
    # previous turn left an unfinished create draft in the browser history.
    request_id = _extract_request_id(text)
    if any(word in text for word in ("撤销", "取消")) and request_id:
        record = service.cancel(user_id=user_id, request_id=request_id)
        return {"kind": "records", "message": "差旅申请已撤销。", "state": None, "records": [record]}

    if any(word in text for word in ("修改", "变更")) and request_id:
        changes = extract_travel_fields(text.replace(request_id, ""))
        record = service.update(user_id=user_id, request_id=request_id, changes=changes)
        return {"kind": "records", "message": "差旅申请已修改。", "state": None, "records": [record]}

    if any(word in text for word in ("查询", "查一下", "查看", "有哪些", "记录")):
        records = service.query(user_id=user_id, **_query_filters(text))
        return {
            "kind": "records",
            "message": f"共找到 {len(records)} 条差旅申请。" if records else "没有找到符合条件的差旅申请。",
            "state": None,
            "records": records,
        }

    if mode in {"collecting", "pending_confirmation"}:
        extracted = extract_travel_fields(text)
        awaiting = str(current.get("awaiting_field") or "")
        if awaiting in {"start_date", "end_date"}:
            direct_date = _normalize_date(text)
            if direct_date:
                extracted.pop("start_date", None)
                extracted.pop("end_date", None)
                extracted[awaiting] = direct_date
        if awaiting and awaiting not in extracted and text:
            value = _normalize_date(text) if awaiting in {"start_date", "end_date"} else text
            if value:
                extracted[awaiting] = value
        draft.update({key: value for key, value in extracted.items() if value})
        missing = _next_missing_field(draft)
        if missing:
            next_state = {"mode": "collecting", "draft": draft, "awaiting_field": missing}
            return {
                "kind": "clarification",
                "message": TRAVEL_QUESTIONS[missing],
                "state": next_state,
                "missing_field": missing,
            }
        try:
            budget = estimate_budget(draft)
        except (KeyError, ValueError) as exc:
            bad_field = "end_date"
            next_state = {"mode": "collecting", "draft": draft, "awaiting_field": bad_field}
            return {"kind": "clarification", "message": f"{exc}，请重新提供返回日期。", "state": next_state, "missing_field": bad_field}
        plan_id = str(current.get("plan_id") or f"travel:{conversation_id}:{uuid.uuid4().hex}")
        next_state = {"mode": "pending_confirmation", "draft": draft, "budget": budget, "plan_id": plan_id}
        return {
            "kind": "plan",
            "message": "差旅计划已生成。确认信息无误后，请点击“确认执行”创建申请。",
            "state": next_state,
            "draft": draft,
            "budget": budget,
            "plan_id": plan_id,
        }

    extracted = extract_travel_fields(text)
    draft.update(extracted)
    missing = _next_missing_field(draft)
    if missing:
        next_state = {"mode": "collecting", "draft": draft, "awaiting_field": missing}
        return {
            "kind": "clarification",
            "message": TRAVEL_QUESTIONS[missing],
            "state": next_state,
            "missing_field": missing,
        }
    budget = estimate_budget(draft)
    plan_id = f"travel:{conversation_id}:{uuid.uuid4().hex}"
    next_state = {"mode": "pending_confirmation", "draft": draft, "budget": budget, "plan_id": plan_id}
    return {
        "kind": "plan",
        "message": "差旅计划已生成。确认信息无误后，请点击“确认执行”创建申请。",
        "state": next_state,
        "draft": draft,
        "budget": budget,
        "plan_id": plan_id,
    }
