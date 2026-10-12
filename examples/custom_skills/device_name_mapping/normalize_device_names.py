"""测试用已有脚本：字段名故意与平台 EntityDataset 不同。"""


def run(payload):
    """读取 devices[].id/label，返回 normalized_devices，而非标准 outputs。"""

    normalized_devices = []
    for device in payload["devices"]:
        original_name = str(device["label"])
        normalized_name = original_name
        for marker, preferred_name in (
            ("HP-01", "主液压泵"),
            ("M-02", "主驱动电机"),
            ("PS-03", "液压压力传感器"),
        ):
            if marker in original_name:
                normalized_name = preferred_name
                break
        normalized_devices.append(
            {"id": device["id"], "label": normalized_name}
        )
    return {"normalized_devices": normalized_devices}
