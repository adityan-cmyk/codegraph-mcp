from datetime import UTC, datetime
from uuid import uuid4

from app.core.eval_store import eval_case_store
from app.rag.assembler.context_assembler import assemble_incident_context
from app.schemas.context import ContextBounds
from app.schemas.eval import EvalCase, EvalResult, EvalSuiteResult
from app.schemas.incident import IncidentSession, ResolutionPackage


def create_eval_case_from_resolution(session: IncidentSession, package: ResolutionPackage) -> EvalCase:
    case = EvalCase(
        case_id=uuid4().hex,
        fingerprint=package.fingerprint,
        expected_root_cause=package.root_cause,
        expected_patch=package.patch,
        environment=session.environment,
        created_at=datetime.now(UTC),
        tags=["auto-generated", session.state.value],
    )
    eval_case_store.save(case)
    return case


async def run_eval_case(case: EvalCase, bounds: ContextBounds) -> EvalResult:
    from app.agents.orchestrator import run_incident_workflow

    try:
        context = assemble_incident_context(case.fingerprint, bounds)
        analysis = await run_incident_workflow(case.fingerprint, bounds)
        actual_root_cause = analysis.root_cause
        actual_patch = analysis.patch

        status: str = "pass"
        if actual_root_cause != case.expected_root_cause or actual_patch != case.expected_patch:
            status = "fail"

        return EvalResult(
            case_id=case.case_id,
            status=status,
            actual_root_cause=actual_root_cause,
            actual_patch=actual_patch,
            confidence_scores=[item.model_dump() for item in analysis.confidence],
        )
    except Exception as exc:
        return EvalResult(
            case_id=case.case_id,
            status="error",
            error_detail=str(exc),
        )


async def run_eval_suite(
    environment: str | None = None,
    suite_name: str = "Golden UAT Suite",
) -> EvalSuiteResult:
    cases = eval_case_store.list_cases(environment=environment)
    bounds = ContextBounds()
    started_at = datetime.now(UTC)

    results: list[EvalResult] = []
    for case in cases:
        result = await run_eval_case(case, bounds)
        results.append(result)

    passed = sum(1 for result in results if result.status == "pass")
    failed = sum(1 for result in results if result.status == "fail")
    errors = sum(1 for result in results if result.status == "error")

    return EvalSuiteResult(
        suite_name=suite_name,
        total_cases=len(cases),
        passed=passed,
        failed=failed,
        errors=errors,
        results=results,
        started_at=started_at,
        completed_at=datetime.now(UTC),
    )
