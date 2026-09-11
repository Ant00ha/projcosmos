"""
Расчётное ядро для задачи «Проектирование устойчивой спутниковой группировки».
Использует geometry.py из папки «Расчетный модуль».
Python 3.10+, NumPy.
"""
from __future__ import annotations
import json
import math
from collections import deque, Counter
from pathlib import Path

import numpy as np

from geometry import load, snapshot, validate, positions, R


# ------------------------------------------------------------------ граф
def build_graph(snap: dict, scenario: dict) -> tuple[dict, dict]:
    """
    Строит граф контактов для одного отсчёта.
    Возвращает (adj, role):
      adj  : {node_id: [соседи]}
      role : {node_id: 'sat' | 'client' | 'gateway'}
    """
    adj: dict[str, list[str]] = {}
    role: dict[str, str] = {}

    for sat in snap["satellites"]:
        adj[sat["id"]] = []
        role[sat["id"]] = "sat"

    for g in scenario["ground_sites"]:
        adj[g["id"]] = []
        role[g["id"]] = g["role"]

    for e in snap["edges"]:
        a, b = e[0], e[1]
        adj[a].append(b)
        adj[b].append(a)

    for k in adj:
        adj[k].sort()

    return adj, role


def bfs_route(
    adj: dict,
    role: dict,
    client_id: str,
    gateway_ids: set[str],
    sat_active: dict[str, bool],
) -> list[str] | None:
    """
    BFS по числу переходов. Промежуточные узлы — только активные спутники.
    Старт — client, финиш — любой доступный gateway.
    """
    if client_id not in adj:
        return None

    visited = {client_id}
    parent: dict[str, str | None] = {client_id: None}
    q = deque([client_id])

    while q:
        node = q.popleft()

        if node in gateway_ids and node != client_id:
            path: list[str] = []
            cur: str | None = node
            while cur is not None:
                path.append(cur)
                cur = parent[cur]
            return path[::-1]

        for nb in adj[node]:
            if nb in visited:
                continue
            if role[nb] == "sat" and not sat_active.get(nb, False):
                continue
            if role[nb] in ("client", "gateway") and nb not in gateway_ids:
                continue
            visited.add(nb)
            parent[nb] = node
            q.append(nb)

    return None


# ------------------------------------------------------ причина отсутствия
def route_failure_reason(
    snap: dict,
    scenario: dict,
    client_id: str,
    gateway_ids: set[str],
    sat_active: dict[str, bool],
) -> str:
    """
    Классификация причины отсутствия маршрута:
      no_visible_satellite  — у client нет видимого активного спутника
      no_gateway_contact    — у шлюза нет видимого активного спутника
      gateway_outage        — шлюз в периоде недоступности
      isl_disconnected      — спутники видны, но ISL-сеть не связывает их
    """

    # 1. Видимые активные спутники у клиента
    client_sats: list[str] = []
    for e in snap["edges"]:
        a, b = e[0], e[1]
        if a == client_id and sat_active.get(b, False):
            client_sats.append(b)
        elif b == client_id and sat_active.get(a, False):
            client_sats.append(a)
    if not client_sats:
        return "no_visible_satellite"

    # 2. Шлюз в отказе?
    t = snap["t_s"]
    for f in scenario.get("gateway_outages", []):
        if f["gateway_id"] in gateway_ids and f["start_s"] <= t < f["end_s"]:
            return "gateway_outage"

    # 3. Видимые активные спутники у любого шлюза
    gateway_has_sat = False
    for e in snap["edges"]:
        a, b = e[0], e[1]
        if a in gateway_ids and sat_active.get(b, False):
            gateway_has_sat = True
            break
        if b in gateway_ids and sat_active.get(a, False):
            gateway_has_sat = True
            break
    if not gateway_has_sat:
        return "no_gateway_contact"

    # 4. Иначе — разрыв межспутниковой сети
    return "isl_disconnected"


# ------------------------------------------------------------------ метрики
def compute_metrics(
    scenario: dict,
    routes: list[dict],
    n_steps: int,
) -> dict:
    """
    По уже посчитанным маршрутам считает метрики по каждому client.
    """
    e = scenario["environment"]
    step = e["step_s"]
    clients = [g["id"] for g in scenario["ground_sites"] if g["role"] == "client"]

    by_client: dict[str, list[dict]] = {cid: [] for cid in clients}
    for r in routes:
        by_client[r["client_id"]].append(r)

    result: dict[str, dict] = {}

    for cid in clients:
        entries = sorted(by_client[cid], key=lambda r: r["t_s"])
        routed = 0
        gaps: list[int] = []
        current_gap = 0
        transitions: list[int] = []
        reasons = Counter()

        for r in entries:
            if r["path"]:
                routed += 1
                transitions.append(len(r["path"]) - 1)
                if current_gap > 0:
                    gaps.append(current_gap)
                    current_gap = 0
            else:
                current_gap += 1
                if r.get("reason"):
                    reasons[r["reason"]] += 1

        if current_gap > 0:
            gaps.append(current_gap)

        max_gap = max(gaps) * step if gaps else 0

        first_gap = 0
        for r in entries:
            if r["path"]:
                break
            first_gap += 1
        last_gap = 0
        for r in reversed(entries):
            if r["path"]:
                break
            last_gap += 1

        result[cid] = {
            "visibility_ratio": None,
            "route_ratio": routed / n_steps,
            "max_gap_s": max_gap,
            "max_gap_at_start_s": first_gap * step,
            "max_gap_at_end_s": last_gap * step,
            "transitions_min": min(transitions) if transitions else None,
            "transitions_max": max(transitions) if transitions else None,
            "transitions_avg": (sum(transitions) / len(transitions)) if transitions else None,
            "reasons": dict(reasons),
            "target_availability": e["target_availability"],
            "target_met": (routed / n_steps) >= e["target_availability"],
        }

    return result


def _visibility_ratio(snap: dict, client_id: str, sat_active: dict[str, bool]) -> bool:
    for e in snap["edges"]:
        a, b = e[0], e[1]
        if a == client_id and sat_active.get(b, False):
            return True
        if b == client_id and sat_active.get(a, False):
            return True
    return False


# ------------------------------------------------------------------ прогон
def run(scenario_path: str, out_path: str | None = None) -> dict:
    scenario = load(scenario_path)
    e = scenario["environment"]
    step = e["step_s"]
    horizon = e["horizon_s"]
    n_steps = horizon // step

    clients = [g["id"] for g in scenario["ground_sites"] if g["role"] == "client"]
    gateways = {g["id"] for g in scenario["ground_sites"] if g["role"] == "gateway"}

    routes: list[dict] = []
    visibility_counts: dict[str, int] = {cid: 0 for cid in clients}

    for k in range(n_steps):
        t = k * step
        snap = snapshot(scenario, t)
        sat_active = {s["id"]: s["active"] for s in snap["satellites"]}
        adj, role = build_graph(snap, scenario)

        for cid in clients:
            if _visibility_ratio(snap, cid, sat_active):
                visibility_counts[cid] += 1

            path = bfs_route(adj, role, cid, gateways, sat_active)
            if path is None:
                reason = route_failure_reason(snap, scenario, cid, gateways, sat_active)
            else:
                reason = None

            routes.append({
                "t_s": t,
                "client_id": cid,
                "path": path if path is not None else [],
                "reason": reason,
            })

    summary = compute_metrics(scenario, routes, n_steps)
    for cid in clients:
        summary[cid]["visibility_ratio"] = visibility_counts[cid] / n_steps

    result = {
        "schema_version": "cosmo-A-result-1.0",
        "effective_scenario": scenario,
        "routes": routes,
        "summary": summary,
    }

    if out_path:
        Path(out_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )

    return result


# ------------------------------------------------------------------ CLI
if __name__ == "__main__":
    scenarios = [
        "01_full_constellation",
        "02_first_launch",
        "03_satellite_outages",
        "04_link_range",
    ]
    data_dir = _HERE / "Данные"

    for name in scenarios:
        src = data_dir / f"{name}.json"
        if not src.exists():
            print(f"[skip] {src} не найден")
            continue

        out = run(str(src), str(_HERE / f"result_{name}.json"))
        print(f"\n=== {name} ===")
        for cid, m in out["summary"].items():
            r = m["reasons"]
            reason_txt = ", ".join(f"{k}={v}" for k, v in r.items()) if r else "—"
            print(
                f"  {cid}: видимость {m['visibility_ratio']*100:5.1f}%  "
                f"путь {m['route_ratio']*100:5.1f}%  "
                f"перерыв {m['max_gap_s']:>6} с  "
                f"цель {'да' if m['target_met'] else 'НЕТ':>3}  "
                f"[{reason_txt}]"
            )