"""FastAPI 入口：换表批计划与静态路由表压缩两个接口。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .compress import CompressError, compress
from .models import MAX_BATCH_SIZE, MAX_DEVICES, CompressRequest, PlanRequest
from .planner import PlanError, solve

app = FastAPI(
    title="园区路由器静态路由整表切换排程与压缩服务",
    description=(
        "两个能力：其一，根据新旧 IPv4 静态路由表与保通要求排出批数最少的整表"
        "切换计划；其二，提交全网 IPv4 静态路由表、目标路由器与可直连下一跳"
        "名单，返回条目数最少且行为等价的完整替换表。两者都基于路由等价类校验，"
        "不逐地址穷举。"
    ),
    version="1.1.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/constraints")
def constraints() -> dict:
    return {
        "max_devices": MAX_DEVICES,
        "max_batch_size": MAX_BATCH_SIZE,
        "switch_granularity": "per-device whole-table atomic switch",
        "verification": "longest-prefix-match over routing equivalence classes (no per-address enumeration)",
        "compress": {
            "target_devices": 1,
            "objective": "minimum route entry count",
            "preserves": [
                "addresses unmatched by the original table stay unmatched",
                "all other addresses still leave via their original external exit",
                "replacement paths never return to the target router",
            ],
        },
    }


@app.post("/plan")
def plan(req: PlanRequest) -> JSONResponse:
    try:
        result = solve(req)
    except PlanError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(exc)},
            media_type="application/json; charset=utf-8",
        )
    return JSONResponse(content=result, media_type="application/json; charset=utf-8")


@app.post("/compress")
def compress_routes(req: CompressRequest) -> JSONResponse:
    """只改目标设备，返回条目数最少、外部出口行为等价的完整替换表。"""
    try:
        result = compress(req)
    except CompressError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(exc)},
            media_type="application/json; charset=utf-8",
        )
    return JSONResponse(content=result, media_type="application/json; charset=utf-8")
