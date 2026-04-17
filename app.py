from __future__ import annotations

import json
import math
import os
import re
import zipfile
import time 
from io import BytesIO
from typing import Any

from flask import Flask, jsonify, render_template, request
from google import genai
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

TEMP_EMPTY = {
    "dtu": None,
    "fpga": None,
    "pmc": None,
    "pwr12v": None,
    "aisg145v": None,
    "rfu0": None,
    "rfu1": None,
}

STATUS_PASS = "PASS"
STATUS_WARNING = "WARNING"
STATUS_FAIL = "FAIL"
STATUS_NA = "N/A"


def safe_float(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pstdev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    avg = mean(values)
    if avg is None:
        return 0.0
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def calc_stats(values: list[float | None]) -> dict[str, float | None]:
    valid = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return {
        "min": min(valid) if valid else None,
        "avg": mean(valid),
        "max": max(valid) if valid else None,
    }


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return safe_float(value)


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_display_id(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    match = re.search(r"(\d+)$", base)
    if not match:
        return base
    return match.group(1)[-3:].zfill(3)


def calc_return_loss(vswr: float | None) -> float | None:
    if vswr is None or vswr <= 1.0:
        return None
    gamma = (vswr - 1.0) / (vswr + 1.0)
    if gamma <= 0:
        return None
    return -20.0 * math.log10(gamma)


def calc_vswr_from_return_loss(rl_db: float | None) -> float | None:
    if rl_db is None or rl_db <= 0:
        return None
    gamma = 10 ** (-rl_db / 20.0)
    if gamma >= 1.0:
        return None
    return (1.0 + gamma) / (1.0 - gamma)


def resolve_column_index(header_cols: list[str], data_cols: list[str], wanted: str) -> int | None:
    wanted = wanted.lower()
    try:
        idx = next(i for i, token in enumerate(header_cols) if token.lower() == wanted)
    except StopIteration:
        return None

    first = header_cols[0].lower() if header_cols else ""
    if len(header_cols) == len(data_cols) + 1 and first in {"port", "idx", "index", "ch", "channel"}:
        idx -= 1

    if 0 <= idx < len(data_cols):
        return idx
    if 0 <= idx - 1 < len(data_cols):
        return idx - 1
    return None


def extract_power_monitor_port_map(text: str) -> dict[int, dict[str, float | None]] | None:
    lines = text.splitlines()
    found: dict[int, dict[str, float | None]] | None = None

    for i, line in enumerate(lines):
        if "system power monitor" not in line.lower():
            continue

        block = lines[i:i + 100]
        header_cols = None
        header_index = -1

        for j, row in enumerate(block[:20]):
            if all(key in row.lower() for key in ("dl_pwr", "ul_pwr", "vswr")):
                header_cols = row.strip().split()
                header_index = j
                break

        if not header_cols:
            continue

        port_map: dict[int, dict[str, float | None]] = {0: {}, 1: {}, 2: {}, 3: {}}
        got_any = False

        for row in block[header_index + 1:header_index + 20]:
            match = re.match(r"^\s*([0-3]):\s*(.*)$", row)
            if not match:
                continue

            port = int(match.group(1))
            cols = match.group(2).strip().split()

            entry = {
                "dl_pwr": None,
                "ul_pwr": None,
                "vswr": None,
            }

            for key in ("dl_pwr", "ul_pwr", "vswr"):
                idx = resolve_column_index(header_cols, cols, key)
                entry[key] = safe_float(cols[idx]) if idx is not None and idx < len(cols) else None

            if any(v is not None for v in entry.values()):
                got_any = True

            port_map[port] = entry

        if got_any:
            found = port_map

    return found


def extract_dl_pwr_port0(text: str) -> float | None:
    port_map = extract_power_monitor_port_map(text)
    if not port_map:
        return None
    return port_map.get(0, {}).get("dl_pwr")


def extract_ul_pwr_port0(text: str) -> float | None:
    port_map = extract_power_monitor_port_map(text)
    if not port_map:
        return None
    return port_map.get(0, {}).get("ul_pwr")


def extract_return_loss_ports_from_text(text: str) -> dict[str, float | None] | None:
    port_map = extract_power_monitor_port_map(text)
    if not port_map:
        return None

    result = {
        "p0": port_map.get(0, {}).get("vswr"),
        "p1": port_map.get(1, {}).get("vswr"),
        "p2": port_map.get(2, {}).get("vswr"),
        "p3": port_map.get(3, {}).get("vswr"),
    }

    return result if any(v is not None for v in result.values()) else None


def extract_temperature(text: str) -> dict[str, float | None]:
    if not text:
        return dict(TEMP_EMPTY)

    lines = text.splitlines()
    result = None

    for i, line in enumerate(lines):
        if "@rru# mon -s" not in line:
            continue

        block = "\n".join(lines[i:i + 25])

        dtu = re.search(r"dtu temp\s*=\s*(-?\d+(?:\.\d+)?)\(C\)", block, re.I)
        fpga = re.search(r"fpga temp\s*=\s*(-?\d+(?:\.\d+)?)\(C\)", block, re.I)
        pmc = re.search(r"pmc temp\s*=\s*(-?\d+(?:\.\d+)?)\(C\)", block, re.I)
        pwr12v = re.search(r"12V\s+PWR\s+temp\s*=\s*(-?\d+(?:\.\d+)?)\(C\)", block, re.I)
        aisg = re.search(r"14\.5V\s+AISG temp\s*=\s*(-?\d+(?:\.\d+)?)\(C\)", block, re.I)
        rfu = re.search(
            r"rfu temp\s*=\s*rfu0\s*(-?\d+(?:\.\d+)?)\(C\)\s*/\s*rfu1\s*(-?\d+(?:\.\d+)?)\(C\)",
            block,
            re.I,
        )

        result = {
            "dtu": safe_float(dtu.group(1)) if dtu else None,
            "fpga": safe_float(fpga.group(1)) if fpga else None,
            "pmc": safe_float(pmc.group(1)) if pmc else None,
            "pwr12v": safe_float(pwr12v.group(1)) if pwr12v else None,
            "aisg145v": safe_float(aisg.group(1)) if aisg else None,
            "rfu0": safe_float(rfu.group(1)) if rfu else None,
            "rfu1": safe_float(rfu.group(2)) if rfu else None,
        }

    return result if result is not None else dict(TEMP_EMPTY)


def extract_psu_in(text: str) -> float | None:
    match = re.search(r"PSU IN\s*=\s*(-?\d+(?:\.\d+)?)\(V\)", text, re.I)
    return safe_float(match.group(1)) if match else None


def extract_sfp_tx(text: str) -> float | None:
    match = re.search(r"tx power\s*=\s*[-+]?\d+(?:\.\d+)?mW\s*=\s*(-?\d+(?:\.\d+)?)dBm", text, re.I)
    return safe_float(match.group(1)) if match else None


def extract_sfp_rx(text: str) -> float | None:
    match = re.search(r"rx power\s*=\s*[-+]?\d+(?:\.\d+)?mW\s*=\s*(-?\d+(?:\.\d+)?)dBm", text, re.I)
    return safe_float(match.group(1)) if match else None


def calculate_histogram(values: list[float], bin_count: int) -> list[dict[str, float | int]]:
    if not values:
        return []

    vmin = min(values)
    vmax = max(values)

    if vmin == vmax:
        return [{"start": vmin - 0.05, "end": vmax + 0.05, "count": len(values)}]

    safe_bins = max(5, min(int(bin_count or 10), 30))
    width = (vmax - vmin) / safe_bins
    bins: list[dict[str, float | int]] = []

    for i in range(safe_bins):
        bins.append({
            "start": vmin + i * width,
            "end": vmin + (i + 1) * width,
            "count": 0,
        })

    for value in values:
        idx = int((value - vmin) / width)
        if idx >= safe_bins:
            idx = safe_bins - 1
        bins[idx]["count"] += 1

    return bins


def get_vswr_p0(item: dict[str, Any]) -> float | None:
    vswr = item.get("vswr")
    return vswr.get("p0") if isinstance(vswr, dict) else None


def get_temp_rfu0(item: dict[str, Any]) -> float | None:
    temp = item.get("temp")
    return temp.get("rfu0") if isinstance(temp, dict) else None


def sort_results(results: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    cloned = list(results)

    if mode == "dlAsc":
        return sorted(cloned, key=lambda x: (x.get("dlPwr") is None, x.get("dlPwr") or 0.0, str(x.get("displayId", "")).zfill(12)))

    if mode == "dlDesc":
        return sorted(cloned, key=lambda x: (x.get("dlPwr") is None, -(x.get("dlPwr") or 0.0), str(x.get("displayId", "")).zfill(12)))

    if mode == "vswr0Asc":
        return sorted(cloned, key=lambda x: (get_vswr_p0(x) is None, get_vswr_p0(x) or 0.0, str(x.get("displayId", "")).zfill(12)))

    if mode == "vswr0Desc":
        return sorted(cloned, key=lambda x: (get_vswr_p0(x) is None, -(get_vswr_p0(x) or 0.0), str(x.get("displayId", "")).zfill(12)))

    if mode == "rfu0Asc":
        return sorted(cloned, key=lambda x: (get_temp_rfu0(x) is None, get_temp_rfu0(x) or 0.0, str(x.get("displayId", "")).zfill(12)))

    if mode == "rfu0Desc":
        return sorted(cloned, key=lambda x: (get_temp_rfu0(x) is None, -(get_temp_rfu0(x) or 0.0), str(x.get("displayId", "")).zfill(12)))

    return sorted(cloned, key=lambda x: str(x.get("displayId", "")).zfill(12))


def build_temp_stats(sorted_results: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    return {
        "dtu": calc_stats([item["temp"].get("dtu") for item in sorted_results]),
        "fpga": calc_stats([item["temp"].get("fpga") for item in sorted_results]),
        "pmc": calc_stats([item["temp"].get("pmc") for item in sorted_results]),
        "psu": calc_stats([item["temp"].get("pwr12v") for item in sorted_results]),
        "aisg": calc_stats([item["temp"].get("aisg145v") for item in sorted_results]),
        "rfu0": calc_stats([item["temp"].get("rfu0") for item in sorted_results]),
        "rfu1": calc_stats([item["temp"].get("rfu1") for item in sorted_results]),
    }


def parse_zip_logs(file_bytes: bytes) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
        names = sorted([name for name in zf.namelist() if not name.endswith("/") and name.lower().endswith((".log", ".txt"))])

        if not names:
            raise ValueError("ZIP 내부에 .log 또는 .txt 파일이 없습니다.")

        for name in names:
            try:
                text = decode_bytes(zf.read(name))

                dl_pwr = extract_dl_pwr_port0(text)
                ul_pwr = extract_ul_pwr_port0(text)

                # 로그의 vswr 컬럼은 실제로 RL(dB) 값으로 판단
                rl = extract_return_loss_ports_from_text(text)

                # UI 호환을 위해 실제 VSWR ratio는 RL에서 역산
                vswr = {
                    "p0": calc_vswr_from_return_loss(rl.get("p0")) if rl else None,
                    "p1": calc_vswr_from_return_loss(rl.get("p1")) if rl else None,
                    "p2": calc_vswr_from_return_loss(rl.get("p2")) if rl else None,
                    "p3": calc_vswr_from_return_loss(rl.get("p3")) if rl else None,
                } if rl else None

                temp = extract_temperature(text)
                psu_in = extract_psu_in(text)
                sfp_tx = extract_sfp_tx(text)
                sfp_rx = extract_sfp_rx(text)

                results.append({
                    "name": name,
                    "displayId": extract_display_id(name),
                    "dlPwr": dl_pwr,
                    "ulPwr": ul_pwr,
                    "vswr": vswr,
                    "rl": rl,
                    "temp": temp,
                    "psuIn": psu_in,
                    "sfpTx": sfp_tx,
                    "sfpRx": sfp_rx,
                })

            except Exception:
                results.append({
                    "name": name,
                    "displayId": extract_display_id(name),
                    "dlPwr": None,
                    "ulPwr": None,
                    "vswr": None,
                    "rl": None,
                    "temp": dict(TEMP_EMPTY),
                    "psuIn": None,
                    "sfpTx": None,
                    "sfpRx": None,
                })

    return results


def build_analysis(
    raw_results: list[dict[str, Any]],
    sort_mode: str,
    hist_bin_count: int,
    target_value: float | None,
    tolerance_value: float | None,
) -> dict[str, Any]:
    sorted_results = sort_results(raw_results, sort_mode)

    dl_values = [item["dlPwr"] for item in sorted_results if item.get("dlPwr") is not None]
    ul_values = [item["ulPwr"] for item in sorted_results if item.get("ulPwr") is not None]

    vswr_p0 = [item["vswr"]["p0"] for item in sorted_results if item.get("vswr") and item["vswr"].get("p0") is not None]
    vswr_p1 = [item["vswr"]["p1"] for item in sorted_results if item.get("vswr") and item["vswr"].get("p1") is not None]
    vswr_p2 = [item["vswr"]["p2"] for item in sorted_results if item.get("vswr") and item["vswr"].get("p2") is not None]
    vswr_p3 = [item["vswr"]["p3"] for item in sorted_results if item.get("vswr") and item["vswr"].get("p3") is not None]

    dtu_temps = [item["temp"]["dtu"] for item in sorted_results if item["temp"].get("dtu") is not None]
    fpga_temps = [item["temp"]["fpga"] for item in sorted_results if item["temp"].get("fpga") is not None]
    rfu0_temps = [item["temp"]["rfu0"] for item in sorted_results if item["temp"].get("rfu0") is not None]
    rfu1_temps = [item["temp"]["rfu1"] for item in sorted_results if item["temp"].get("rfu1") is not None]

    heatmap_values = [
        temp
        for item in sorted_results
        for temp in (item["temp"].get("rfu0"), item["temp"].get("rfu1"))
        if temp is not None
    ]

    failed = []
    for item in sorted_results:
        temp = item["temp"]
        no_temp = all(temp.get(k) is None for k in ("dtu", "fpga", "pmc", "pwr12v", "aisg145v", "rfu0", "rfu1"))
        if item.get("dlPwr") is None and item.get("rl") is None and no_temp:
            failed.append(item)

    return {
        "rawResults": raw_results,
        "sortedResults": sorted_results,
        "failed": failed,
        "options": {
            "histBinCount": hist_bin_count,
            "targetValue": target_value,
            "toleranceValue": tolerance_value,
        },
        "dl": {
            "values": dl_values,
            "avg": mean(dl_values),
            "std": pstdev(dl_values),
            "min": min(dl_values) if dl_values else None,
            "max": max(dl_values) if dl_values else None,
            "range": (max(dl_values) - min(dl_values)) if dl_values else None,
            "histogram": calculate_histogram(dl_values, hist_bin_count),
        },
        "ul": {
            "values": ul_values,
            "avg": mean(ul_values),
            "std": pstdev(ul_values),
            "min": min(ul_values) if ul_values else None,
            "max": max(ul_values) if ul_values else None,
            "range": (max(ul_values) - min(ul_values)) if ul_values else None,
            "histogram": calculate_histogram(ul_values, hist_bin_count),
        },
        "vswr": {
            "p0Avg": mean(vswr_p0),
            "p1Avg": mean(vswr_p1),
            "p2Avg": mean(vswr_p2),
            "p3Avg": mean(vswr_p3),
        },
        "temp": {
            "dtuAvg": mean(dtu_temps),
            "fpgaAvg": mean(fpga_temps),
            "rfu0Avg": mean(rfu0_temps),
            "rfu1Avg": mean(rfu1_temps),
            "min": min(heatmap_values) if heatmap_values else None,
            "max": max(heatmap_values) if heatmap_values else None,
            "stats": build_temp_stats(sorted_results),
        },
    }


def eval_dl_pwr(value: float | None, fleet_avg: float | None, fleet_std: float | None) -> tuple[str, list[str]]:
    if value is None:
        return STATUS_NA, ["dl_pwr 값 없음"]

    notes: list[str] = []

    if value < 45.0 or value > 46.5:
        status = STATUS_FAIL
    elif 45.5 <= value <= 46.0:
        status = STATUS_PASS
    else:
        status = STATUS_WARNING

    if fleet_avg is not None and fleet_std is not None and fleet_std > 0:
        lo = fleet_avg - (2.0 * fleet_std)
        hi = fleet_avg + (2.0 * fleet_std)
        if value < lo or value > hi:
            notes.append(f"Fleet Mean±2σ 이탈 ({lo:.2f} ~ {hi:.2f})")

    return status, notes


def eval_return_loss(rl_values: list[float | None]) -> tuple[str, float | None]:
    valid = [v for v in rl_values if v is not None]
    if not valid:
        return STATUS_NA, None

    worst = min(valid)
    if worst < 15.0:
        return STATUS_FAIL, worst
    if 15.0 <= worst <= 20.0:
        return STATUS_WARNING, worst
    return STATUS_PASS, worst


def eval_ul_pwr(value: float | None, fleet_avg: float | None) -> tuple[str, str | None]:
    if value is None or fleet_avg is None:
        return STATUS_NA, None

    delta = abs(value - fleet_avg)
    if delta > 5.0:
        return STATUS_FAIL, f"fleet avg 대비 {delta:.2f} dB"
    if 3.0 <= delta <= 5.0:
        return STATUS_WARNING, f"fleet avg 대비 {delta:.2f} dB"
    return STATUS_PASS, f"fleet avg 대비 {delta:.2f} dB"


def eval_dtu_temp(value: float | None) -> str:
    if value is None:
        return STATUS_NA
    if value > 60.0:
        return STATUS_FAIL
    if 50.0 <= value <= 60.0:
        return STATUS_WARNING
    return STATUS_PASS


def eval_rfu_temp(rfu0: float | None, rfu1: float | None) -> tuple[str, float | None]:
    valid = [v for v in (rfu0, rfu1) if v is not None]
    if not valid:
        return STATUS_NA, None

    worst = max(valid)
    if worst > 65.0:
        return STATUS_FAIL, worst
    if 55.0 <= worst <= 65.0:
        return STATUS_WARNING, worst
    return STATUS_PASS, worst


def eval_psu_in(value: float | None) -> str:
    if value is None:
        return STATUS_NA
    if -57.0 <= value < -42.0:
        return STATUS_PASS
    if -42.0 <= value <= -40.0:
        return STATUS_WARNING
    return STATUS_FAIL


def eval_sfp_tx(value: float | None) -> str:
    if value is None:
        return STATUS_NA
    if value < -2.0 or value > 5.0:
        return STATUS_FAIL
    if -2.0 <= value < 0.0:
        return STATUS_WARNING
    return STATUS_PASS


def eval_sfp_rx(value: float | None) -> str:
    if value is None:
        return STATUS_NA
    if value < -10.0:
        return STATUS_FAIL
    if -10.0 <= value <= -8.0:
        return STATUS_WARNING
    return STATUS_PASS


def build_status_by_item(item: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    dl_status, dl_notes = eval_dl_pwr(item.get("dlPwr"), analysis["dl"]["avg"], analysis["dl"]["std"])
    rl_status, rl_worst = eval_return_loss([
        item.get("rl", {}).get("p0") if item.get("rl") else None,
        item.get("rl", {}).get("p1") if item.get("rl") else None,
        item.get("rl", {}).get("p2") if item.get("rl") else None,
        item.get("rl", {}).get("p3") if item.get("rl") else None,
    ])
    ul_status, ul_note = eval_ul_pwr(item.get("ulPwr"), analysis["ul"]["avg"])
    dtu_status = eval_dtu_temp(item.get("temp", {}).get("dtu"))
    rfu_status, rfu_worst = eval_rfu_temp(item.get("temp", {}).get("rfu0"), item.get("temp", {}).get("rfu1"))
    psu_status = eval_psu_in(item.get("psuIn"))
    sfp_tx_status = eval_sfp_tx(item.get("sfpTx"))
    sfp_rx_status = eval_sfp_rx(item.get("sfpRx"))

    return {
        "dl_pwr": {
            "status": dl_status,
            "value": item.get("dlPwr"),
            "notes": dl_notes,
        },
        "return_loss": {
            "status": rl_status,
            "worst": rl_worst,
        },
        "ul_pwr": {
            "status": ul_status,
            "value": item.get("ulPwr"),
            "notes": [ul_note] if ul_note else [],
        },
        "dtu_temp": {
            "status": dtu_status,
            "value": item.get("temp", {}).get("dtu"),
        },
        "rfu_temp": {
            "status": rfu_status,
            "worst": rfu_worst,
            "rfu0": item.get("temp", {}).get("rfu0"),
            "rfu1": item.get("temp", {}).get("rfu1"),
        },
        "psu_in": {
            "status": psu_status,
            "value": item.get("psuIn"),
        },
        "sfp_tx": {
            "status": sfp_tx_status,
            "value": item.get("sfpTx"),
        },
        "sfp_rx": {
            "status": sfp_rx_status,
            "value": item.get("sfpRx"),
        },
    }


def analyze_single_log_risk(item: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    status_by_item = build_status_by_item(item, analysis)

    issues: list[str] = []
    causes: list[str] = []
    checks: list[str] = []
    score = 0
    fail_count = 0
    warning_count = 0

    for _, detail in status_by_item.items():
        status = detail["status"]
        if status == STATUS_FAIL:
            fail_count += 1
            score += 40
        elif status == STATUS_WARNING:
            warning_count += 1
            score += 15

    if item.get("dlPwr") is None:
        issues.append("DL Power 추출 실패")
        checks.append("system power monitor / dl_pwr 컬럼 확인")
        score += 10

    if item.get("ulPwr") is None:
        issues.append("UL Power 추출 실패")
        checks.append("system power monitor / ul_pwr 컬럼 확인")
        score += 8

    if item.get("rl") is None:
        issues.append("Return Loss 추출 실패")
        checks.append("system power monitor 마지막 블록 vswr 컬럼 확인")
        score += 8

    temp = item.get("temp") or {}
    if all(temp.get(k) is None for k in ("dtu", "fpga", "pmc", "pwr12v", "aisg145v", "rfu0", "rfu1")):
        issues.append("온도 추출 실패")
        checks.append("mon -s 출력 형식 확인")
        score += 8

    dl_detail = status_by_item["dl_pwr"]
    if dl_detail["status"] == STATUS_FAIL:
        issues.append(f"DL Power FAIL ({item.get('dlPwr'):.2f} dBm)")
        causes.append("출력 보정값 오류, PA gain 편차, 출력 경로 문제 가능성")
        checks.append("DL calibration 값, PA path, coupler 경로 점검")
    elif dl_detail["status"] == STATUS_WARNING:
        issues.append(f"DL Power WARNING ({item.get('dlPwr'):.2f} dBm)")
        checks.append("Fleet 평균 대비 편차 재확인")
    for note in dl_detail.get("notes", []):
        issues.append(f"DL 참고: {note}")

    rl_detail = status_by_item["return_loss"]
    if rl_detail["status"] == STATUS_FAIL and rl_detail["worst"] is not None:
        issues.append(f"Return Loss FAIL ({rl_detail['worst']:.2f} dB)")
        causes.append("급전계/커넥터/안테나 매칭 불량 가능성")
        checks.append("포트별 케이블 체결, 안테나, 점퍼, 커넥터 점검")
    elif rl_detail["status"] == STATUS_WARNING and rl_detail["worst"] is not None:
        issues.append(f"Return Loss WARNING ({rl_detail['worst']:.2f} dB)")
        checks.append("반사손실 경계값 여부 재확인")

    ul_detail = status_by_item["ul_pwr"]
    if ul_detail["status"] == STATUS_FAIL:
        issues.append(f"UL Power FAIL ({item.get('ulPwr'):.2f} dBm)")
        causes.append("UL gain 편차, 보정 오프셋, 수신경로 이상 가능성")
        checks.append("UL gain/baseline 보정값 점검")
    elif ul_detail["status"] == STATUS_WARNING:
        issues.append(f"UL Power WARNING ({item.get('ulPwr'):.2f} dBm)")
        checks.append("Fleet 평균 편차 추세 확인")

    dtu_detail = status_by_item["dtu_temp"]
    if dtu_detail["status"] == STATUS_FAIL:
        issues.append(f"DTU Temp FAIL ({dtu_detail['value']:.1f} C)")
        causes.append("DTU 발열 과다 또는 냉각 경로 문제 가능성")
        checks.append("DTU 방열 구조, 써멀패드, 장비 주변온도 점검")
    elif dtu_detail["status"] == STATUS_WARNING:
        issues.append(f"DTU Temp WARNING ({dtu_detail['value']:.1f} C)")
        checks.append("DTU 온도 여유도 확인")

    rfu_detail = status_by_item["rfu_temp"]
    if rfu_detail["status"] == STATUS_FAIL and rfu_detail["worst"] is not None:
        issues.append(f"RFU Temp FAIL ({rfu_detail['worst']:.1f} C)")
        causes.append("RFU 발열 증가 또는 방열 불량 가능성")
        checks.append("RFU0/RFU1 방열, 체결, 주변온도, 트래픽 조건 확인")
    elif rfu_detail["status"] == STATUS_WARNING and rfu_detail["worst"] is not None:
        issues.append(f"RFU Temp WARNING ({rfu_detail['worst']:.1f} C)")
        checks.append("환경보정 기준과 함께 재확인")

    psu_detail = status_by_item["psu_in"]
    if psu_detail["status"] == STATUS_FAIL and psu_detail["value"] is not None:
        issues.append(f"PSU IN FAIL ({psu_detail['value']:.2f} V)")
        causes.append("입력 전원 범위 이탈 가능성")
        checks.append("전원 공급기, 케이블, 현장 입력전압 점검")
    elif psu_detail["status"] == STATUS_WARNING and psu_detail["value"] is not None:
        issues.append(f"PSU IN WARNING ({psu_detail['value']:.2f} V)")
        checks.append("입력 전압 저하 추세 확인")

    sfp_tx_detail = status_by_item["sfp_tx"]
    if sfp_tx_detail["status"] == STATUS_FAIL and sfp_tx_detail["value"] is not None:
        issues.append(f"SFP TX FAIL ({sfp_tx_detail['value']:.2f} dBm)")
        causes.append("광 송신 출력 저하 또는 모듈 이상 가능성")
        checks.append("SFP TX power, 광 모듈 교체, 패치코드 점검")
    elif sfp_tx_detail["status"] == STATUS_WARNING and sfp_tx_detail["value"] is not None:
        issues.append(f"SFP TX WARNING ({sfp_tx_detail['value']:.2f} dBm)")
        checks.append("광 송신 출력 저하 추세 확인")

    sfp_rx_detail = status_by_item["sfp_rx"]
    if sfp_rx_detail["status"] == STATUS_FAIL and sfp_rx_detail["value"] is not None:
        issues.append(f"SFP RX FAIL ({sfp_rx_detail['value']:.2f} dBm)")
        causes.append("광 수신 레벨 부족, 손실 증가, 광 Budget 부족 가능성")
        checks.append("광 감쇠, 패치코드, 커넥터 오염, 상대단 TX 레벨 확인")
    elif sfp_rx_detail["status"] == STATUS_WARNING and sfp_rx_detail["value"] is not None:
        issues.append(f"SFP RX WARNING ({sfp_rx_detail['value']:.2f} dBm)")
        checks.append("광 Budget 여유도 확인")

    if rl_detail["status"] in {STATUS_FAIL, STATUS_WARNING} and dl_detail["status"] in {STATUS_FAIL, STATUS_WARNING}:
        causes.append("반사 증가와 출력 편차가 동시에 존재하여 RF 경로 문제 가능성")
        checks.append("PA 출력과 안테나 경로를 분리하여 교차 점검")

    if rfu_detail["status"] in {STATUS_FAIL, STATUS_WARNING} and dl_detail["status"] in {STATUS_FAIL, STATUS_WARNING}:
        causes.append("고온 상태와 출력 편차가 동반되어 열 영향 가능성")
        checks.append("온도 조건별 출력 변동 재측정")

    overall_status = STATUS_FAIL if fail_count > 0 else STATUS_WARNING if warning_count > 0 else STATUS_PASS
    level = "HIGH" if overall_status == STATUS_FAIL else "MEDIUM" if warning_count >= 2 else "LOW" if warning_count == 1 else "NORMAL"

    return {
        "displayId": item.get("displayId"),
        "overallStatus": overall_status,
        "level": level,
        "score": score,
        "failCount": fail_count,
        "warningCount": warning_count,
        "statusByItem": status_by_item,
        "issues": list(dict.fromkeys(issues)),
        "causes": list(dict.fromkeys(causes)),
        "checks": list(dict.fromkeys(checks)),
    }


def build_per_log_ai(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    results = [analyze_single_log_risk(item, analysis) for item in analysis["sortedResults"]]
    results.sort(key=lambda x: (-x["score"], x["displayId"]))
    return results


def build_ai_summary(analysis: dict[str, Any], per_log_ai: list[dict[str, Any]]) -> dict[str, Any]:
    fail_logs = [x for x in per_log_ai if x["overallStatus"] == STATUS_FAIL]
    warning_logs = [x for x in per_log_ai if x["overallStatus"] == STATUS_WARNING]

    summary_lines = [
        f"전체 로그 {len(per_log_ai)}건 기준으로 PASS/WARNING/FAIL 판정을 수행했습니다.",
        f"FAIL {len(fail_logs)}건, WARNING {len(warning_logs)}건입니다.",
    ]
    risks: list[str] = []

    if fail_logs:
        top_ids = ", ".join(x["displayId"] for x in fail_logs[:5])
        risks.append(f"우선 점검 대상 FAIL 장비: {top_ids}")

    issue_counter: dict[str, int] = {}
    for row in per_log_ai:
        for issue in row["issues"]:
            issue_counter[issue] = issue_counter.get(issue, 0) + 1

    for issue, count in sorted(issue_counter.items(), key=lambda x: (-x[1], x[0]))[:5]:
        summary_lines.append(f"빈발 이슈: {issue} ({count}건)")

    overall = "정상" if not fail_logs and not warning_logs else "점검 필요" if fail_logs else "주의"
    return {"overall": overall, "summary": summary_lines, "risks": risks}


def fmt(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def get_worst_rl(item: dict[str, Any]) -> float | None:
    rl = item.get("rl")
    if not isinstance(rl, dict):
        return None
    valid = [v for v in [rl.get("p0"), rl.get("p1"), rl.get("p2"), rl.get("p3")] if v is not None]
    return min(valid) if valid else None


def build_openai_payload(analysis: dict[str, Any], per_log_ai: list[dict[str, Any]]) -> dict[str, Any]:
    item_map = {item["displayId"]: item for item in analysis["sortedResults"]}

    top_risks: list[dict[str, Any]] = []
    for row in per_log_ai[:8]:
        item = item_map.get(row["displayId"], {})
        top_risks.append({
            "displayId": row["displayId"],
            "overallStatus": row["overallStatus"],
            "level": row["level"],
            "score": row["score"],
            "failCount": row["failCount"],
            "warningCount": row["warningCount"],
            "issues": row["issues"][:6],
            "causes": row["causes"][:6],
            "checks": row["checks"][:6],
            "measured": {
                "dlPwr_p0": fmt(item.get("dlPwr"), 3),
                "ulPwr_p0": fmt(item.get("ulPwr"), 3),
                "worstReturnLoss": fmt(get_worst_rl(item), 2),
                "dtuTemp": fmt(item.get("temp", {}).get("dtu"), 1),
                "fpgaTemp": fmt(item.get("temp", {}).get("fpga"), 1),
                "rfu0Temp": fmt(item.get("temp", {}).get("rfu0"), 1),
                "rfu1Temp": fmt(item.get("temp", {}).get("rfu1"), 1),
                "psuIn": fmt(item.get("psuIn"), 2),
                "sfpTx": fmt(item.get("sfpTx"), 2),
                "sfpRx": fmt(item.get("sfpRx"), 2),
            },
        })

    return {
        "fleet": {
            "totalLogs": len(analysis["sortedResults"]),
            "failedLogs": len(analysis["failed"]),
            "dl": {
                "avg": fmt(analysis["dl"]["avg"], 3),
                "std": fmt(analysis["dl"]["std"], 3),
                "min": fmt(analysis["dl"]["min"], 3),
                "max": fmt(analysis["dl"]["max"], 3),
                "range": fmt(analysis["dl"]["range"], 3),
            },
            "ul": {
                "avg": fmt(analysis["ul"]["avg"], 3),
                "std": fmt(analysis["ul"]["std"], 3),
                "min": fmt(analysis["ul"]["min"], 3),
                "max": fmt(analysis["ul"]["max"], 3),
                "range": fmt(analysis["ul"]["range"], 3),
            },
            "vswrAvg": {
                "p0": fmt(analysis["vswr"]["p0Avg"], 3),
                "p1": fmt(analysis["vswr"]["p1Avg"], 3),
                "p2": fmt(analysis["vswr"]["p2Avg"], 3),
                "p3": fmt(analysis["vswr"]["p3Avg"], 3),
            },
            "tempAvg": {
                "dtu": fmt(analysis["temp"]["dtuAvg"], 2),
                "fpga": fmt(analysis["temp"]["fpgaAvg"], 2),
                "rfu0": fmt(analysis["temp"]["rfu0Avg"], 2),
                "rfu1": fmt(analysis["temp"]["rfu1Avg"], 2),
            },
        },
        "topRiskLogs": top_risks,
    }

def normalize_ai_error(exc: Exception) -> dict[str, Any]:
    msg = str(exc)

    # Gemini 무료 quota / rate limit
    if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        return {
            "status": "ERROR",
            "errorType": "QUOTA",
            "title": "Gemini 사용 한도 초과",
            "userMessage": (
                "현재 Gemini 무료 사용 한도를 초과했습니다. "
                "잠시 후 다시 시도하거나, 정렬 변경 시 AI 재호출을 줄이세요."
            ),
            "detail": msg,
        }

    # Gemini 일시 과부하
    if "503" in msg or "UNAVAILABLE" in msg or "high demand" in msg.lower():
        return {
            "status": "ERROR",
            "errorType": "TEMP_UNAVAILABLE",
            "title": "Gemini 일시 과부하",
            "userMessage": (
                "현재 Gemini 서비스 응답이 불안정합니다. "
                "잠시 후 다시 시도해 주세요."
            ),
            "detail": msg,
        }

    # 키 문제
    if "API key not valid" in msg or "authentication" in msg.lower() or "api_key" in msg.lower():
        return {
            "status": "ERROR",
            "errorType": "AUTH",
            "title": "Gemini 인증 오류",
            "userMessage": (
                "Gemini API 키가 없거나 올바르지 않습니다. "
                "서버 환경변수를 확인해야 합니다."
            ),
            "detail": msg,
        }

    return {
        "status": "ERROR",
        "errorType": "UNKNOWN",
        "title": "AI 분석 오류",
        "userMessage": (
            "AI 분석 중 알 수 없는 오류가 발생했습니다. "
            "잠시 후 다시 시도해 주세요."
        ),
        "detail": msg,
    }

def call_openai_solution(analysis: dict[str, Any], per_log_ai: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "UNAVAILABLE",
            "model": None,
            "content": None,
            "error": "GEMINI_API_KEY가 설정되지 않아 Gemini 모델 분석을 수행하지 않았습니다.",
        }

    payload = build_openai_payload(analysis, per_log_ai)

    # 과부하 시에는 Lite 계열이 더 유리함
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    developer_prompt = """
당신은 5G ORAN RU/RRU/DAS 장비 로그 분석 전문가다.
반드시 아래 원칙을 지켜라.

1. 제공된 수치와 규칙 기반 판정만 근거로 사용한다.
2. 근거 없는 추측은 금지한다.
3. 원인 추정은 "추정"이라고 명시한다.
4. 출력은 한국어로 작성한다.
5. 다음 섹션 순서를 반드시 지킨다.

[사실 요약]
- 3~6개 bullet

[추정 원인]
- 공통 원인과 장비별 대표 원인 분리
- 확실한 사실과 추정을 구분

[우선 점검 순서]
- 현장 점검 우선순위를 1, 2, 3... 형태로 제시

[권장 조치]
- 즉시 조치 / 재측정 / 추가 로그 확보 항목으로 구분

[추가 필요 정보]
- 현재 데이터만으로 확정할 수 없는 항목 제시

불필요한 미사여구는 금지한다.
""".strip()

    user_prompt = f"""
다음은 Python으로 로그를 추출하고 이상치/규칙 기반 판정을 수행한 결과다.
이 데이터를 바탕으로 Gemini 모델 기반 솔루션을 작성하라.

데이터(JSON):
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()

    client = genai.Client(api_key=api_key)

    last_error = None
    delays = [2, 5, 10]

    for attempt in range(len(delays) + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=f"{developer_prompt}\n\n{user_prompt}",
            )

            content = (response.text or "").strip() if getattr(response, "text", None) else ""

            if not content:
                return {
                    "status": "ERROR",
                    "model": model_name,
                    "content": None,
                    "error": "Gemini 응답은 왔지만 text가 비어 있습니다.",
                }

            return {
                "status": "READY",
                "model": model_name,
                "content": content,
                "error": None,
            }

        except Exception as exc:
            last_error = str(exc)

            # 503 / UNAVAILABLE / high demand 인 경우만 재시도
            retryable = (
                "503" in last_error
                or "UNAVAILABLE" in last_error
                or "high demand" in last_error.lower()
            )

            if retryable and attempt < len(delays):
                time.sleep(delays[attempt])
                continue


            if retryable:
                return {
                    "status": "ERROR",
                    "model": model_name,
                    "content": None,
                    "error": (
                        f"Gemini 호출 실패: {last_error}\n"
                        "일시적 과부하 상태입니다. 잠시 후 다시 시도하거나 "
                        "GEMINI_MODEL을 더 가벼운 Flash/Lite 계열로 바꾸세요."
                    ),
                }

            return {
                "status": "ERROR",
                "model": model_name,
                "content": None,
                "error": f"Gemini 호출 실패: {last_error}",
            }

    return {
        "status": "ERROR",
        "model": model_name,
        "content": None,
        "error": f"Gemini 호출 실패: {last_error}",
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze() -> Any:
    upload = request.files.get("zipFile")
    if upload is None or upload.filename is None or not upload.filename.lower().endswith(".zip"):
        return jsonify({"ok": False, "message": "ZIP 파일만 입력 가능합니다."}), 400

    try:
        hist_bin_count = int(request.form.get("histBinCount", "10"))
    except ValueError:
        hist_bin_count = 10

    sort_mode = request.form.get("sortMode", "name")
    target_value = parse_optional_float(request.form.get("targetValue"))
    tolerance_value = parse_optional_float(request.form.get("toleranceValue"))

    try:
        raw_results = parse_zip_logs(upload.read())
        analysis = build_analysis(raw_results, sort_mode, hist_bin_count, target_value, tolerance_value)
        per_log_ai = build_per_log_ai(analysis)
        ai_summary = build_ai_summary(analysis, per_log_ai)
        openai_solution = call_openai_solution(analysis, per_log_ai)

        temp_extracted_count = sum(
            1
            for item in analysis["sortedResults"]
            if any(item["temp"].get(key) is not None for key in ("dtu", "fpga", "pmc", "pwr12v", "aisg145v", "rfu0", "rfu1"))
        )

        return jsonify({
            "ok": True,
            "message": "분석 완료",
            "analysis": analysis,
            "aiSummary": ai_summary,
            "perLogAi": per_log_ai,
            "openAiSolution": openai_solution,
            "counts": {
                "total": len(analysis["sortedResults"]),
                "dlExtracted": sum(1 for item in analysis["sortedResults"] if item.get("dlPwr") is not None),
                "vswrExtracted": sum(1 for item in analysis["sortedResults"] if item.get("rl") is not None),
                "tempExtracted": temp_extracted_count,
                "failed": len(analysis["failed"]),
            },
        })

    except zipfile.BadZipFile:
        return jsonify({"ok": False, "message": "정상적인 ZIP 파일이 아닙니다."}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"오류 발생: {exc}"}), 500

@app.errorhandler(404)
def handle_404(exc):
    return jsonify({"ok": False, "message": "요청한 경로를 찾을 수 없습니다."}), 404


@app.errorhandler(413)
def handle_413(exc):
    return jsonify({"ok": False, "message": "업로드 파일 크기가 제한을 초과했습니다."}), 413


@app.errorhandler(500)
def handle_500(exc):
    return jsonify({"ok": False, "message": "서버 내부 오류가 발생했습니다."}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
