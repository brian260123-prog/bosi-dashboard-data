from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "cache" / "python_packages"))
import pymysql


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


local_env = Path(os.environ.get("DASHBOARD_ENV_FILE", "/Users/yuke/.codex/secrets/shower-dashboard.env"))
config = read_env(local_env) if local_env.exists() else {}
config.update({key: value for key, value in os.environ.items() if key.startswith("DB_")})
connection = pymysql.connect(
    host=config["DB_HOST"], port=int(config.get("DB_PORT", "3306")),
    user=config["DB_USER"], password=config["DB_PASSWORD"], database=config["DB_NAME"],
    charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=10, read_timeout=30, write_timeout=10,
    ssl={"check_hostname": False} if config.get("DB_SSL", "true").lower() == "true" else None,
)

business_specs = [
    {
        "key": "drawing", "name": "出图单", "quantityLabel": "房体数量",
        "table": "eb_drawing_forms", "quantity": "COALESCE(f.room_number, 0)",
        "statuses": {"-20": "已退回", "-10": "取消", "00": "待提交", "01": "已提交", "10": "待受理", "11": "已受理", "20": "出图完毕", "30": "已确认"},
        "completed": {"20", "30"},
    },
    {
        "key": "order", "name": "订货单", "quantityLabel": "订货数量",
        "table": "eb_order_forms", "quantity": "COALESCE(f.number, 0)",
        "statuses": {"-10": "取消", "00": "待提交", "01": "已提交", "10": "待受理", "11": "已受理", "20": "待付款", "30": "生产中", "40": "已发货", "50": "已完成", "70": "已报价", "71": "已付款"},
        "completed": {"50"},
    },
    {
        "key": "afterSale", "name": "售后单", "quantityLabel": "售后件数",
        "table": "eb_after_sale_forms", "quantity": "1",
        "statuses": {"-20": "已退回", "-10": "取消", "00": "待提交", "01": "已提交", "10": "待受理", "11": "已受理", "20": "生产中", "30": "已发货", "40": "完成"},
        "completed": {"40"},
    },
]


def aggregate(rows, spec):
    months = defaultdict(lambda: {
        "applicationTotal": 0, "effectiveTotal": 0, "completedTotal": 0,
        "pendingTotal": 0, "exceptionTotal": 0, "draftTotal": 0, "quantityTotal": 0,
        "statuses": defaultdict(int),
        "dealers": defaultdict(lambda: {"orders": 0, "effective": 0, "completed": 0, "quantity": 0}),
    })
    for row in rows:
        month, status = str(row["month"]), str(row["status"])
        count, quantity = int(row["order_count"] or 0), int(row["quantity_count"] or 0)
        dealer = str(row["distributor_name"] or f"经销商 {row['distributor_id']}")
        item, dealer_item = months[month], months[month]["dealers"][dealer]
        item["applicationTotal"] += count
        item["quantityTotal"] += quantity
        item["statuses"][status] += count
        dealer_item["orders"] += count
        dealer_item["quantity"] += quantity
        if status not in {"-20", "-10", "00"}:
            item["effectiveTotal"] += count
            dealer_item["effective"] += count
        if status in spec["completed"]:
            item["completedTotal"] += count
            dealer_item["completed"] += count
        elif status not in {"-20", "-10", "00"}:
            item["pendingTotal"] += count
        if status in {"-20", "-10"}: item["exceptionTotal"] += count
        if status == "00": item["draftTotal"] += count

    output = []
    for month in sorted(months):
        item = months[month]
        status_codes = list(spec["statuses"])
        for code in item["statuses"]:
            if code not in status_codes: status_codes.append(code)
        output.append({
            "month": month,
            "applicationTotal": item["applicationTotal"], "effectiveTotal": item["effectiveTotal"],
            "completedTotal": item["completedTotal"], "pendingTotal": item["pendingTotal"],
            "exceptionTotal": item["exceptionTotal"], "draftTotal": item["draftTotal"],
            "quantityTotal": item["quantityTotal"],
            "completionRate": round(item["completedTotal"] / item["effectiveTotal"], 4) if item["effectiveTotal"] else 0,
            "statuses": [{"code": code, "name": spec["statuses"].get(code, f"未知状态 {code}"), "count": item["statuses"].get(code, 0)} for code in status_codes],
            "dealers": [{"name": name, **values} for name, values in sorted(item["dealers"].items(), key=lambda pair: (-pair[1]["orders"], pair[0]))],
        })
    return output


businesses = []
with connection:
    with connection.cursor() as cursor:
        for spec in business_specs:
            cursor.execute(f"""
            SELECT DATE_FORMAT(f.created_at, '%Y-%m') AS month, d.id AS distributor_id,
                   d.name AS distributor_name, f.status, COUNT(*) AS order_count,
                   SUM({spec['quantity']}) AS quantity_count
            FROM {spec['table']} f
            JOIN (SELECT uid, MAX(distributor_id) AS distributor_id FROM eb_distributor_staffs GROUP BY uid) staff ON staff.uid=f.uid
            JOIN eb_distributors d ON d.id=staff.distributor_id
            WHERE f.created_at IS NOT NULL
            GROUP BY DATE_FORMAT(f.created_at, '%Y-%m'), d.id, d.name, f.status
            ORDER BY month, distributor_name
            """)
            businesses.append({
                "key": spec["key"], "name": spec["name"], "quantityLabel": spec["quantityLabel"],
                "months": aggregate(cursor.fetchall(), spec),
            })

payload = {
    "generatedAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    "timezone": "Asia/Shanghai", "monthField": "created_at", "source": "淋浴房小程序业务系统",
    "businesses": businesses,
    "notes": ["订货单状态70为已报价、71为已付款；订货完成量仍只按50已完成统计。", "售后单无独立数量字段，按一张售后单计一件。"],
}
target = Path(os.environ.get("DASHBOARD_OUTPUT", str(PROJECT / "app" / "data" / "dashboard.json")))
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"businesses": [{"key": b["key"], "months": len(b["months"]), "latest": b["months"][-1]["month"] if b["months"] else None} for b in businesses], "output": str(target)}, ensure_ascii=False))
