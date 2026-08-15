from fastapi import APIRouter, HTTPException, status

from app.core.eval_service import run_eval_suite
from app.core.eval_store import eval_case_store
from app.schemas.eval import EvalCase, EvalSuiteResult


router = APIRouter(prefix="/api/eval", tags=["eval"])


@router.get("/cases", response_model=list[EvalCase])
def list_eval_cases(environment: str | None = None) -> list[EvalCase]:
    return eval_case_store.list_cases(environment=environment)


@router.get("/cases/{case_id}", response_model=EvalCase)
def get_eval_case(case_id: str) -> EvalCase:
    case = eval_case_store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Eval case not found.")
    return case


@router.post("/run", response_model=EvalSuiteResult)
async def run_eval_suite_endpoint(environment: str | None = None, suite_name: str = "Golden UAT Suite") -> EvalSuiteResult:
    return await run_eval_suite(environment=environment, suite_name=suite_name)
