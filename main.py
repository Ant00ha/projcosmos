"""
FastAPI-сервер для проектирования спутниковой группировки.
Использует solve_01.py как ядро расчёта.
Python 3.10+.
"""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --- локальные модули ---
import solve_01
import compare as compare_mod

HERE = Path(__file__).parent
DATA_DIR = HERE / "Данные"
RUNS_DIR = HERE / "runs"
STATIC_DIR = HERE / "static"

RUNS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="cosmo-A satellite constellation designer", version="0.1.0")


# ------------------------------------------------------------- модели
class RunRequest(BaseModel):
    scenario_id: str
    name: str | None = None


class EditRequest(BaseModel):
    patch: dict[str, Any]


class CompareRequest(BaseModel):
    a: str
    b: str


# ------------------------------------------------------------- утилиты
def _scenario_path(scenario_id: str) -> Path:
    for base in (DATA_DIR, RUNS_DIR):
        p = base / f"{scenario_id}.json"
        if p.exists():
            return p
    raise HTTPException(404, f"Сценарий {scenario_id!r} не найден")


def _run_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def _load_run(run_id: str) -> dict:
    p = _run_path(run_id)
    if not p.exists():
        raise HTTPException(404, f"Прогон {run_id!r} не найден")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_run(result: dict, name: str | None) -> str:
    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    payload = {
        "run_id": run_id,
        "name": name or run_id,
        "created_at": time.time(),
        "schema_version": result["schema_version"],
        "effective_scenario": result["effective_scenario"],
        "summary": result["summary"],
        "routes": result["routes"],
    }
    _run_path(run_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return run_id


# ------------------------------------------------------------- API: сценарии

@app.get("/api/result/{run_id}/timeline")
def get_timeline(run_id: str) -> dict:
    """
    Лёгкая выжимка для таймлайна и диаграммы Гантта:
      step_s, horizon_s, n_steps,
      по каждому client — массив [t_индекс] с наличием маршрута и причины.
    """
    data = _load_run(run_id)
    scenario = data["effective_scenario"]
    e = scenario["environment"]
    step = e["step_s"]
    horizon = e["horizon_s"]
    n_steps = horizon // step

    clients = [g["id"] for g in scenario["ground_sites"] if g["role"] == "client"]

    # routes в формате: [(t_s, client_id, has_path, reason), ...]
    routes_by_client: dict[str, list[dict]] = {cid: [] for cid in clients}
    for r in data.get("routes", []):
        routes_by_client[r["client_id"]].append({
            "t_s": r["t_s"],
            "has_path": bool(r.get("path")),
            "reason": r.get("reason"),
        })

    # отсортируем по t
    for cid in clients:
        routes_by_client[cid].sort(key=lambda x: x["t_s"])

    return {
        "run_id": run_id,
        "step_s": step,
        "horizon_s": horizon,
        "n_steps": n_steps,
        "clients": clients,
        "timeline": routes_by_client,
    }

@app.delete("/api/result/{run_id}")
def delete_run(run_id: str) -> dict:
    """Удаляет один прогон."""
    p = _run_path(run_id)
    if not p.exists():
        raise HTTPException(404, f"Прогон {run_id!r} не найден")
    p.unlink()
    return {"deleted": run_id}


@app.post("/api/runs/clear")
def clear_runs() -> dict:
    """Удаляет все прогоны. Возвращает число удалённых."""
    count = 0
    for p in RUNS_DIR.glob("run_*.json"):
        p.unlink()
        count += 1
    return {"deleted_count": count}

@app.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    out: list[dict] = []
    # читаем и Данные/, и runs/ — чтобы видеть отредактированные
    for base in (DATA_DIR, RUNS_DIR):
        for p in sorted(base.glob("*.json")):
            if base == RUNS_DIR and p.name.startswith("run_"):
                continue  # это результаты расчёта, не сценарии
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if "design" not in data or "ground_sites" not in data:
                    continue  # это не сценарий
                out.append({
                    "id": p.stem,
                    "title": data.get("meta", {}).get("title", p.stem),
                    "schema_version": data.get("schema_version"),
                    "launch_stage": data.get("design", {}).get("launch_stage"),
                    "isl_range_km": data.get("environment", {}).get("isl_range_km"),
                    "horizon_s": data.get("environment", {}).get("horizon_s"),
                    "step_s": data.get("environment", {}).get("step_s"),
                    "source": "Данные" if base == DATA_DIR else "runs",
                })
            except Exception as e:
                out.append({"id": p.stem, "error": str(e)})
    # убираем дубли по id (если один и тот же есть в двух местах — оставляем из Данные)
    seen = {}
    for s in out:
        if s["id"] not in seen:
            seen[s["id"]] = s
    return sorted(seen.values(), key=lambda s: (s.get("source") != "Данные", s["id"]))


@app.get("/api/scenarios/{scenario_id}")
def get_scenario(scenario_id: str) -> dict:
    return json.loads(_scenario_path(scenario_id).read_text(encoding="utf-8"))


@app.post("/api/scenarios/upload")
async def upload_scenario(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"Некорректный JSON: {e}")

    # валидация через geometry.validate (уже подключён в solve_01)
    try:
        solve_01.validate(data)
    except Exception as e:
        raise HTTPException(400, f"Ошибка валидации: {e}")

    sid = data.get("meta", {}).get("id") or f"upload_{uuid.uuid4().hex[:6]}"
    target = RUNS_DIR / f"{sid}.json"
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return {"id": sid, "path": str(target)}


# ------------------------------------------------------------- API: запуск
@app.post("/api/run")
def run_scenario(req: RunRequest) -> dict:
    path = _scenario_path(req.scenario_id)
    try:
        result = solve_01.run(str(path))
    except Exception as e:
        raise HTTPException(500, f"Ошибка расчёта: {e}")
    run_id = _save_run(result, req.name)
    return {
        "run_id": run_id,
        "name": req.name or run_id,
        "summary": result["summary"],
        "routes_count": len(result["routes"]),
    }


@app.post("/api/run/{scenario_id}/edit")
def edit_scenario(scenario_id: str, req: EditRequest) -> dict:
    path = _scenario_path(scenario_id)
    scenario = json.loads(path.read_text(encoding="utf-8"))

    def deep_update(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep_update(dst[k], v)
            else:
                dst[k] = v

    deep_update(scenario, req.patch)
    new_id = f"{scenario_id}_edited_{uuid.uuid4().hex[:4]}"
    scenario.setdefault("meta", {})["id"] = new_id
    (RUNS_DIR / f"{new_id}.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"new_scenario_id": new_id, "scenario": scenario}


# ------------------------------------------------------------- API: результаты
@app.get("/api/runs")
def list_runs() -> list[dict]:
    out: list[dict] = []
    for p in sorted(RUNS_DIR.glob("run_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "run_id": data.get("run_id"),
                "name": data.get("name"),
                "created_at": data.get("created_at"),
                "scenario_id": data.get("effective_scenario", {}).get("meta", {}).get("id"),
                "summary": data.get("summary"),
            })
        except Exception:
            continue
    return out


@app.get("/api/result/{run_id}")
def get_result(run_id: str, include_routes: bool = False) -> dict:
    data = _load_run(run_id)
    if not include_routes:
        data.pop("routes", None)
    return data


@app.get("/api/result/{run_id}/snapshot")
def get_snapshot(run_id: str, t: int) -> dict:
    data = _load_run(run_id)
    scenario = data["effective_scenario"]
    # snapshot уже доступен через solve_01
    snap = solve_01.snapshot(scenario, t)
    routes = [r for r in data.get("routes", []) if r["t_s"] == t]
    return {"snapshot": snap, "routes": routes}

def _eci_to_geodetic(xyz, t_s: float, earth_angle0_deg: float):
    """
    Преобразует ECI-координаты (км) в геодезические (lat_deg, lon_deg)
    для отрисовки на карте. Учитывает вращение Земли.
    """
    import math
    R = 6371.0
    T = 86164.09054
    theta = math.radians(earth_angle0_deg) + 2 * math.pi * t_s / T
    c, s = math.cos(theta), math.sin(theta)

    out = []
    for (x, y, z) in xyz:
        # переход в земную СК
        xe = c * x + s * y
        ye = -s * x + c * y
        ze = z
        r = math.sqrt(xe*xe + ye*ye + ze*ze)
        lat = math.degrees(math.asin(ze / r))
        lon = math.degrees(math.atan2(ye, xe))
        out.append((lat, lon))
    return out


@app.get("/api/result/{run_id}/map")
def get_map(run_id: str, t: int) -> dict:
    """
    Данные для карты на момент t:
      satellites: id, lat, lon, active, launch_batch, failed
      ground_sites: id, name, role, lat, lon
      edges: [[id_a, id_b, kind], ...]  kind: 'isl' | 'ground'
      routes: [{client_id, path}, ...]
    """
    data = _load_run(run_id)
    scenario = data["effective_scenario"]
    e = scenario["environment"]

    # геометрия на момент t через solve_01.snapshot
    snap = solve_01.snapshot(scenario, t)

    # координаты спутников в ECI, переводим в lat/lon
    import numpy as np
    from pathlib import Path
    # получаем ECI отдельно — через positions, если она доступна
    ids, inertial, fixed = solve_01.positions(scenario, t)
    latlon = _eci_to_geodetic(inertial, t, e["earth_angle0_deg"])

    # кто в отказе прямо сейчас
    failed = set()
    for f in scenario.get("failures", []):
        if f["start_s"] <= t < f["end_s"]:
            failed.add(f["satellite_id"])

    sats = []
    for k, sid in enumerate(ids):
        sat_meta = next((s for s in scenario["design"]["satellites"]
                         if s["id"] == sid), {})
        sats.append({
            "id": sid,
            "lat": latlon[k][0],
            "lon": latlon[k][1],
            "active": snap["satellites"][k]["active"],
            "plane_id": sat_meta.get("plane_id"),
            "launch_batch": sat_meta.get("launch_batch"),
            "failed": sid in failed,
        })

    # ground_sites
    gsites = [{
        "id": g["id"],
        "name": g["name"],
        "role": g["role"],
        "lat": g["lat_deg"],
        "lon": g["lon_deg"],
    } for g in scenario["ground_sites"]]

    # рёбра: определяем тип по id
    edges = []
    for ed in snap["edges"]:
        a, b = ed[0], ed[1]
        kind = "isl" if (a.startswith("S") and b.startswith("S")) else "ground"
        edges.append([a, b, kind])

    # маршруты
    routes = []
    for r in data.get("routes", []):
        if r["t_s"] == t:
            routes.append({
                "client_id": r["client_id"],
                "path": r["path"],
            })

    return {
        "t_s": t,
        "satellites": sats,
        "ground_sites": gsites,
        "edges": edges,
        "routes": routes,
    }

@app.get("/api/result/{run_id}/export")
def export_result(run_id: str) -> FileResponse:
    p = _run_path(run_id)
    if not p.exists():
        raise HTTPException(404, f"Прогон {run_id!r} не найден")
    return FileResponse(p, media_type="application/json",
                        filename=f"{run_id}.json")


# ------------------------------------------------------------- API: сравнение
@app.post("/api/compare")
def compare_runs(req: CompareRequest) -> dict:
    a = _load_run(req.a)
    b = _load_run(req.b)
    return {
        "a": req.a,
        "b": req.b,
        "scenario_diff": compare_mod.scenario_diff(a, b),
        "metrics_diff": compare_mod.metrics_diff(a, b),
        "routes_diff": compare_mod.routes_diff(a, b),
        "recommendation": compare_mod.make_recommendation(a, b),
    }


# ------------------------------------------------------------- статика
@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True),
              name="static")