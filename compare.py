"""
Сравнение двух результатов расчёта (result_*.json).
Python 3.10+.
"""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path


# ------------------------------------------------------------- загрузка
def load_result(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != "cosmo-A-result-1.0":
        raise ValueError(f"Неверный schema_version в {path}: "
                         f"{data.get('schema_version')!r}")
    for key in ("effective_scenario", "routes", "summary"):
        if key not in data:
            raise ValueError(f"Нет обязательного поля {key!r} в {path}")
    return data


# --------------------------------------------------- diff по параметрам
def scenario_diff(a: dict, b: dict) -> dict:
    """Что отличается в сценариях (environment + design + failures + outages)."""
    sa, sb = a["effective_scenario"], b["effective_scenario"]
    changes: dict[str, dict] = {}

    # environment
    for k in ("altitude_km", "inclination_deg", "earth_angle0_deg",
              "horizon_s", "step_s", "min_elevation_deg",
              "isl_range_km", "target_availability"):
        va, vb = sa["environment"].get(k), sb["environment"].get(k)
        if va != vb:
            changes[f"environment.{k}"] = {"from": va, "to": vb}

    # launch_stage
    la, lb = sa["design"].get("launch_stage"), sb["design"].get("launch_stage")
    if la != lb:
        changes["design.launch_stage"] = {"from": la, "to": lb}

    # planes (по id)
    pa = {p["id"]: p for p in sa["design"]["planes"]}
    pb = {p["id"]: p for p in sb["design"]["planes"]}
    for pid in sorted(set(pa) | set(pb)):
        if pid not in pa:
            changes[f"planes[{pid}]"] = {"from": None, "to": pb[pid]}
        elif pid not in pb:
            changes[f"planes[{pid}]"] = {"from": pa[pid], "to": None}
        else:
            for k in ("raan_deg", "phase_deg"):
                if pa[pid].get(k) != pb[pid].get(k):
                    changes[f"planes[{pid}].{k}"] = {
                        "from": pa[pid].get(k), "to": pb[pid].get(k)
                    }

    # satellites — считаем по количеству и составу
    sat_a = {s["id"]: s for s in sa["design"]["satellites"]}
    sat_b = {s["id"]: s for s in sb["design"]["satellites"]}
    if set(sat_a) != set(sat_b):
        changes["satellites.ids"] = {
            "added": sorted(set(sat_b) - set(sat_a)),
            "removed": sorted(set(sat_a) - set(sat_b)),
        }
    else:
        for sid in sat_a:
            for k in ("plane_id", "slot_deg", "launch_batch"):
                if sat_a[sid].get(k) != sat_b[sid].get(k):
                    changes[f"satellites[{sid}].{k}"] = {
                        "from": sat_a[sid].get(k), "to": sat_b[sid].get(k)
                    }

    # ground_sites — id/role/координаты
    gs_a = {g["id"]: g for g in sa["ground_sites"]}
    gs_b = {g["id"]: g for g in sb["ground_sites"]}
    if set(gs_a) != set(gs_b):
        changes["ground_sites.ids"] = {
            "added": sorted(set(gs_b) - set(gs_a)),
            "removed": sorted(set(gs_a) - set(gs_b)),
        }
    else:
        for gid in gs_a:
            for k in ("role", "lat_deg", "lon_deg"):
                if gs_a[gid].get(k) != gs_b[gid].get(k):
                    changes[f"ground_sites[{gid}].{k}"] = {
                        "from": gs_a[gid].get(k), "to": gs_b[gid].get(k)
                    }

    # failures / gateway_outages — сравниваем как множества кортежей
    fa = {(f["satellite_id"], f["start_s"], f["end_s"]) for f in sa.get("failures", [])}
    fb = {(f["satellite_id"], f["start_s"], f["end_s"]) for f in sb.get("failures", [])}
    if fa != fb:
        changes["failures"] = {
            "added": sorted(fb - fa),
            "removed": sorted(fa - fb),
        }

    goa = {(f["gateway_id"], f["start_s"], f["end_s"]) for f in sa.get("gateway_outages", [])}
    gob = {(f["gateway_id"], f["start_s"], f["end_s"]) for f in sb.get("gateway_outages", [])}
    if goa != gob:
        changes["gateway_outages"] = {
            "added": sorted(gob - goa),
            "removed": sorted(goa - gob),
        }

    return changes


# ------------------------------------------------------ diff по метрикам
def metrics_diff(a: dict, b: dict) -> dict:
    """
    Сравнение summary по каждому client.
    Возвращает {client_id: {metric: {a, b, delta, pct_delta}}}
    """
    ma, mb = a["summary"], b["summary"]
    out: dict[str, dict] = {}

    for cid in sorted(set(ma) | set(mb)):
        ra, rb = ma.get(cid, {}), mb.get(cid, {})
        cm: dict[str, dict] = {}

        for k in ("visibility_ratio", "route_ratio"):
            va, vb = ra.get(k), rb.get(k)
            if va is None or vb is None:
                continue
            cm[k] = {
                "a": va, "b": vb,
                "delta": vb - va,
                "pct_delta": (vb - va) * 100,
            }

        for k in ("max_gap_s", "max_gap_at_start_s", "max_gap_at_end_s"):
            va, vb = ra.get(k), rb.get(k)
            if va is None or vb is None:
                continue
            cm[k] = {
                "a": va, "b": vb,
                "delta": vb - va,
            }

        for k in ("transitions_min", "transitions_max", "transitions_avg"):
            va, vb = ra.get(k), rb.get(k)
            if va is None or vb is None:
                continue
            cm[k] = {"a": va, "b": vb, "delta": vb - va}

        # target_met
        ta, tb = ra.get("target_met"), rb.get("target_met")
        cm["target_met"] = {"a": ta, "b": tb}

        # reasons — счётчики
        cm["reasons"] = {
            "a": ra.get("reasons", {}),
            "b": rb.get("reasons", {}),
        }

        out[cid] = cm

    return out


# ------------------------------------------------------ diff по маршрутам
# def routes_diff(a: dict, b: dict) -> dict:
#     """
#     Побайтовое сравнение маршрутов по (t_s, client_id).
#     Возвращает:
#       same, changed, appeared, disappeared — счётчики
#       examples — несколько примеров изменений
#       transitions_delta — распределение изменения длины маршрута
#     """
#     ra = {(r["t_s"], r["client_id"]): r for r in a["routes"]}
#     rb = {(r["t_s"], r["client_id"]): r for r in b["routes"]}

#     keys = sorted(set(ra) | set(rb))
#     same = changed = appeared = disappeared = 0
#     examples: list[dict] = []
#     trans_delta = Counter()

#     for key in keys:
#         pa = ra.get(key, {}).get("path")
#         pb = rb.get(key, {}).get("path")

#         if pa is None and pb is None:
#             continue
#         if pa is None:
#             appeared += 1
#             continue
#         if pb is None:
#             disappeared += 1
#             continue

#         if pa == pb:
#             same += 1
#         else:
#             changed += 1
#             # изменение числа переходов
#             la = len(pa) - 1 if pa else 0
#             lb = len(pb) - 1 if pb else 0
#             trans_delta[lb - la] += 1

#             if len(examples) < 10:
#                 examples.append({
#                     "t_s": key[0],
#                     "client_id": key[1],
#                     "path_a": pa,
#                     "path_b": pb,
#                     "transitions_delta": lb - la,
#                 })

#     return {
#         "same": same,
#         "changed": changed,
#         "appeared": appeared,
#         "disappeared": disappeared,
#         "transitions_delta": dict(trans_delta),
#         "examples": examples,
#     }
def routes_diff(a: dict, b: dict) -> dict:
    """
    Побайтовое сравнение маршрутов по (t_s, client_id).
    Считает:
      same        — маршрут не изменился (включая оба пустых)
      changed     — маршрут есть в обоих, но отличается
      appeared    — у A пусто, у B появился маршрут
      disappeared — у A был маршрут, у B пусто
    """
    ra = {(r["t_s"], r["client_id"]): r for r in a["routes"]}
    rb = {(r["t_s"], r["client_id"]): r for r in b["routes"]}

    keys = sorted(set(ra) | set(rb))
    same = changed = appeared = disappeared = 0
    examples: list[dict] = []
    trans_delta: Counter = Counter()

    for key in keys:
        pa = ra.get(key, {}).get("path", []) or []
        pb = rb.get(key, {}).get("path", []) or []
        has_a = bool(pa)
        has_b = bool(pb)

        if has_a == has_b and pa == pb:
            same += 1
        elif not has_a and has_b:
            appeared += 1
            if len(examples) < 10:
                examples.append({
                    "t_s": key[0], "client_id": key[1],
                    "kind": "appeared",
                    "path_a": pa, "path_b": pb,
                })
        elif has_a and not has_b:
            disappeared += 1
            if len(examples) < 10:
                examples.append({
                    "t_s": key[0], "client_id": key[1],
                    "kind": "disappeared",
                    "path_a": pa, "path_b": pb,
                })
        else:
            changed += 1
            la = len(pa) - 1 if pa else 0
            lb = len(pb) - 1 if pb else 0
            trans_delta[lb - la] += 1
            if len(examples) < 10:
                examples.append({
                    "t_s": key[0], "client_id": key[1],
                    "kind": "changed",
                    "path_a": pa, "path_b": pb,
                    "transitions_delta": lb - la,
                })

    return {
        "same": same,
        "changed": changed,
        "appeared": appeared,
        "disappeared": disappeared,
        "transitions_delta": dict(trans_delta),
        "examples": examples,
    }


# ---------------------------------------------------------- рекомендация
def make_recommendation(a: dict, b: dict) -> list[str]:
    """
    Простые правила: какой вариант лучше по доле маршрутов / перерывам / цели.
    Возвращает список текстовых выводов.
    """
    lines: list[str] = []
    ma, mb = a["summary"], b["summary"]

    # 1. Цель 90%
    ta = all(v.get("target_met") for v in ma.values())
    tb = all(v.get("target_met") for v in mb.values())
    if ta and not tb:
        lines.append("Вариант A достигает цели 90% по всем пунктам, вариант B — нет.")
    elif tb and not ta:
        lines.append("Вариант B достигает цели 90% по всем пунктам, вариант A — нет.")
    elif not ta and not tb:
        lines.append("Ни один вариант не достигает цели 90% по всем пунктам.")
    else:
        lines.append("Оба варианта достигают цели 90% по всем пунктам.")

    # 2. Средняя доля маршрутов
    ra = sum(v.get("route_ratio", 0) for v in ma.values()) / max(len(ma), 1)
    rb = sum(v.get("route_ratio", 0) for v in mb.values()) / max(len(mb), 1)
    if rb - ra > 0.01:
        lines.append(f"Вариант B лучше по сквозной доступности: "
                     f"{ra*100:.1f}% → {rb*100:.1f}% (+{(rb-ra)*100:.1f} п.п.).")
    elif ra - rb > 0.01:
        lines.append(f"Вариант A лучше по сквозной доступности: "
                     f"{ra*100:.1f}% → {rb*100:.1f}% ({(rb-ra)*100:.1f} п.п.).")
    else:
        lines.append(f"Сквозная доступность практически не изменилась "
                     f"({ra*100:.1f}% vs {rb*100:.1f}%).")

    # 3. Сумма причин isl_disconnected
    isl_a = sum(v.get("reasons", {}).get("isl_disconnected", 0) for v in ma.values())
    isl_b = sum(v.get("reasons", {}).get("isl_disconnected", 0) for v in mb.values())
    if isl_b - isl_a > 10:
        lines.append(f"В варианте B заметно чаще рвётся ISL-сеть "
                     f"({isl_a} → {isl_b} отсчётов).")
    elif isl_a - isl_b > 10:
        lines.append(f"В варианте A заметно чаще рвётся ISL-сеть "
                     f"({isl_a} → {isl_b} отсчётов).")

    # 4. Максимальный перерыв
    ga = max((v.get("max_gap_s", 0) for v in ma.values()), default=0)
    gb = max((v.get("max_gap_s", 0) for v in mb.values()), default=0)
    if gb < ga:
        lines.append(f"Максимальный перерыв сократился: {ga} с → {gb} с.")
    elif ga < gb:
        lines.append(f"Максимальный перерыв вырос: {ga} с → {gb} с.")

    return lines


# --------------------------------------------------------------- печать
def print_report(a: dict, b: dict, name_a: str, name_b: str) -> None:
    print(f"\n{'='*70}")
    print(f"СРАВНЕНИЕ: A = {name_a}  vs  B = {name_b}")
    print(f"{'='*70}")

    # 1. Параметры
    print("\n--- Изменения в параметрах сценария ---")
    changes = scenario_diff(a, b)
    if not changes:
        print("  (сценарии идентичны)")
    else:
        for k, v in changes.items():
            print(f"  {k}: {v}")

    # 2. Метрики
    print("\n--- Метрики по наземным пунктам ---")
    mdiff = metrics_diff(a, b)
    for cid, m in mdiff.items():
        print(f"\n  {cid}:")
        if "visibility_ratio" in m:
            v = m["visibility_ratio"]
            print(f"    видимость    : {v['a']*100:5.1f}% → {v['b']*100:5.1f}%  "
                  f"({v['pct_delta']:+.1f} п.п.)")
        if "route_ratio" in m:
            v = m["route_ratio"]
            print(f"    путь до шлюза: {v['a']*100:5.1f}% → {v['b']*100:5.1f}%  "
                  f"({v['pct_delta']:+.1f} п.п.)")
        if "max_gap_s" in m:
            v = m["max_gap_s"]
            print(f"    макс. перерыв: {v['a']:>6} с → {v['b']:>6} с  ({v['delta']:+})")
        if "transitions_avg" in m:
            v = m["transitions_avg"]
            print(f"    переходов ср.: {v['a']:.2f} → {v['b']:.2f}  ({v['delta']:+.2f})")
        tm = m.get("target_met", {})
        if tm:
            print(f"    цель 90%     : {'да' if tm['a'] else 'НЕТ'} → "
                  f"{'да' if tm['b'] else 'НЕТ'}")

    # 3. Причины
    print("\n--- Причины отсутствия маршрута (суммарно по всем пунктам) ---")
    reasons_a = Counter()
    reasons_b = Counter()
    for v in a["summary"].values():
        reasons_a.update(v.get("reasons", {}))
    for v in b["summary"].values():
        reasons_b.update(v.get("reasons", {}))
    all_keys = sorted(set(reasons_a) | set(reasons_b))
    if not all_keys:
        print("  (маршруты есть всегда)")
    else:
        for k in all_keys:
            print(f"  {k:<22}: {reasons_a.get(k, 0):>5} → {reasons_b.get(k, 0):>5}")

    # 4. Маршруты
    print("\n--- Изменения маршрутов ---")
    rd = routes_diff(a, b)
    print(f"  без изменений : {rd['same']}")
    print(f"  изменились    : {rd['changed']}")
    print(f"  появились     : {rd['appeared']}")
    print(f"  исчезли       : {rd['disappeared']}")
    if rd["transitions_delta"]:
        print("  изменение числа переходов (B − A):")
        for d, n in sorted(rd["transitions_delta"].items()):
            print(f"    {d:+d}: {n} моментов")
    if rd["examples"]:
        print("  примеры изменений:")
        for ex in rd["examples"][:5]:
            print(f"    t={ex['t_s']:>6} {ex['client_id']}: "
                  f"{ex['path_a']} → {ex['path_b']}")

    # 5. Рекомендация
    print("\n--- Рекомендация ---")
    for line in make_recommendation(a, b):
        print(f"  • {line}")
    print()


# ------------------------------------------------------------------ CLI
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Использование: python compare.py result_a.json result_b.json")
        sys.exit(1)

    path_a, path_b = sys.argv[1], sys.argv[2]
    a = load_result(path_a)
    b = load_result(path_b)
    name_a = Path(path_a).stem
    name_b = Path(path_b).stem
    print_report(a, b, name_a, name_b)