"""路由：账单上传 / 模板管理 / 清空当月。"""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.config import UPLOAD_MAX_MB
from app.database import get_db
from app.models import ImportTemplate
from app.schemas import TemplateOut, TemplateDetectOut, UploadResult
from app.services import import_service, bill_ops

router = APIRouter(prefix="/api", tags=["bills"])


@router.post("/bills/upload", response_model=UploadResult)
async def upload_bill(
    file: UploadFile = File(...),
    source: str = Form(...),
    force: bool = Form(False),
    db: Session = Depends(get_db),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传文件为空，请重新选择账单文件")
    if len(data) > UPLOAD_MAX_MB * 1024 * 1024:
        raise HTTPException(413, "文件过大，超过限制")
    try:
        result = import_service.upload_and_import(
            db, source, data, file.filename or "upload", force=force)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"解析失败：{e}")
    return UploadResult(**result)


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)):
    return db.query(ImportTemplate).order_by(ImportTemplate.source).all()


@router.post("/templates/detect", response_model=TemplateDetectOut)
async def detect_template(
    file: UploadFile = File(...),
    source: str = Form(...),
    db: Session = Depends(get_db),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传文件为空，请重新选择账单文件")
    try:
        col_map = import_service.detect_template(source, data, file.filename or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"无法识别该文件：{e}")
    return TemplateDetectOut(source=source, suggested_col_map=col_map)


@router.post("/bills/clear-month")
def clear_month(month: str = Query(..., pattern="^\\d{4}-\\d{2}$"), db: Session = Depends(get_db)):
    """清空指定月份全部账单数据（交易+合并组+候选+账单文件）。"""
    return bill_ops.clear_month(db, month)
