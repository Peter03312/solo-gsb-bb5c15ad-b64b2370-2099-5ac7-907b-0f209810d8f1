"""FastAPI 入口：共用产线食品换线过敏原放行服务。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import services
from .allergens import catalog
from .config import DEFAULT_LINE, HOST, PORT
from .database import connect, init_db
from .models import BatchCorrection, BatchCreate, MaterialCorrection, MaterialCreate


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 一键启动时自动建库建表（幂等）
    init_db()
    yield


app = FastAPI(
    title="食品换线过敏原放行服务",
    description=(
        "按产线顺序登记生产批次、原料批号过敏原、成品声明过敏原与清洁核验，"
        "返回每批实际可能含有的过敏原、未声明项与放行/扣留结论；"
        "支持更正后按产线顺序级联重算并保留历史裁决版本。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(services.ApiError)
async def api_error_handler(_: Request, exc: services.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {
        "service": "allergen-line-release",
        "docs": "/docs",
        "health": "/health",
        "default_line": DEFAULT_LINE,
    }


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    conn = connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/allergens", tags=["catalog"])
def allergens() -> list[dict]:
    """过敏原标识目录（可接受的代码与名称）。"""
    return catalog()


# ---------------------------------------------------------------- 原料

@app.post("/materials", tags=["materials"], status_code=201)
def create_material(payload: MaterialCreate) -> dict:
    return services.create_material(payload.model_dump())


@app.get("/materials", tags=["materials"])
def list_materials() -> list[dict]:
    return services.list_materials()


@app.get("/materials/{lot_no}", tags=["materials"])
def get_material(lot_no: str) -> dict:
    return services.get_material(lot_no)


@app.put("/materials/{lot_no}", tags=["materials"])
def correct_material(lot_no: str, payload: MaterialCorrection) -> dict:
    """更正原料批号主数据（名称/过敏原），跨产线重算所有使用该批号的批次。"""
    return services.correct_material(lot_no, payload.model_dump(exclude_unset=True))


@app.get("/materials/{lot_no}/corrections", tags=["materials"])
def material_corrections(lot_no: str) -> list[dict]:
    """某原料批号的主数据更正审计。"""
    return services.list_material_corrections(lot_no)


# ---------------------------------------------------------------- 批次

@app.post("/batches", tags=["batches"], status_code=201)
def register_batch(payload: BatchCreate) -> dict:
    """登记生产批次（自动排在该产线末尾并立即裁决）。"""
    return services.register_batch(payload.model_dump())


@app.get("/batches", tags=["batches"])
def list_batches(line_code: str | None = Query(default=None)) -> list[dict]:
    return services.list_batches(line_code)


@app.get("/batches/{batch_id}", tags=["batches"])
def get_batch(batch_id: int) -> dict:
    return services.get_batch(batch_id)


@app.put("/batches/{batch_id}", tags=["batches"])
def correct_batch(batch_id: int, payload: BatchCorrection) -> dict:
    """更正批次的原料批号/声明/清洁，按产线顺序重算该批及后续批次。"""
    return services.correct_batch(batch_id, payload.model_dump(exclude_unset=True))


@app.get("/batches/{batch_id}/history", tags=["batches"])
def batch_history(batch_id: int) -> dict:
    """查询该批全部历史裁决版本（旧版本保留，永不覆盖）。"""
    return services.get_history(batch_id)


@app.get("/batches/{batch_id}/corrections", tags=["batches"])
def batch_corrections(batch_id: int) -> list[dict]:
    """该批的批次级更正审计记录。"""
    return services.list_corrections(batch_id)


# ---------------------------------------------------------------- 产线

@app.get("/lines", tags=["lines"])
def list_lines() -> list[dict]:
    return services.list_lines()


@app.get("/lines/{line_code}", tags=["lines"])
def get_line(line_code: str) -> dict:
    """产线当前状态：末批裁决与留存在产线上的过敏原。"""
    return services.get_line(line_code)


def main() -> None:
    import uvicorn

    init_db()
    uvicorn.run("app.main:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
