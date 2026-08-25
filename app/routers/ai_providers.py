import datetime

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, AIProviderConfig, AuditLogEntry
from ..auth import require_admin
from .. import llm_gateway

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PROVIDER_TYPES = [
    ("anthropic", "Anthropic (Claude)"),
    ("ollama", "Ollama (free, local)"),
    ("azure_openai", "Azure OpenAI Service"),
    ("openai_compatible", "OpenAI-compatible endpoint"),
]


def _mask_key(key: str | None) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 4:
        return "••••"
    return "•" * 8 + key[-4:]


@router.get("/admin/ai-providers", response_class=HTMLResponse)
def list_ai_providers(
    request: Request,
    test_result_id: int = 0,
    test_result_status: str = "",
    test_result_message: str = "",
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    configs = db.query(AIProviderConfig).order_by(AIProviderConfig.created_at.desc()).all()
    return templates.TemplateResponse(
        "admin_ai_providers.html",
        {"request": request, "teacher": admin, "configs": configs, "provider_types": PROVIDER_TYPES,
         "mask_key": _mask_key, "test_result_id": test_result_id,
         "test_result_status": test_result_status, "test_result_message": test_result_message},
    )


@router.post("/admin/ai-providers/{config_id}/test")
def test_ai_provider(
    config_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Checks reachability/credentials for one saved config, on demand - built directly from
    a real problem: a connection failure being discovered only when a real teacher tried
    to generate something for real, with no way to check it ahead of time. Result is
    stashed in-session (well, just as query params on the redirect) so it survives the
    redirect back to the list page without needing new UI state.
    """
    configs = db.query(AIProviderConfig).order_by(AIProviderConfig.created_at.desc()).all()
    target = db.get(AIProviderConfig, config_id)
    if not target:
        return RedirectResponse(url="/admin/ai-providers", status_code=303)

    success, message = llm_gateway.test_connection({
        "provider_type": target.provider_type, "api_key": target.api_key,
        "base_url": target.base_url, "model_name": target.model_name, "api_version": target.api_version,
    })
    from urllib.parse import quote
    status = "ok" if success else "fail"
    return RedirectResponse(
        url=f"/admin/ai-providers?test_result_id={config_id}&test_result_status={status}&test_result_message={quote(message)}",
        status_code=303,
    )


@router.post("/admin/ai-providers/create")
def create_ai_provider(
    provider_type: str = Form(...),
    label: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
    model_name: str = Form(""),
    api_version: str = Form(""),
    make_active: str = Form(""),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    if provider_type not in dict(PROVIDER_TYPES):
        return RedirectResponse(url="/admin/ai-providers", status_code=303)

    config = AIProviderConfig(
        provider_type=provider_type,
        label=label.strip() or dict(PROVIDER_TYPES)[provider_type],
        api_key=api_key.strip() or None,
        base_url=base_url.strip() or None,
        model_name=model_name.strip() or None,
        api_version=api_version.strip() or None,
        created_by_teacher_id=admin.id,
    )
    db.add(config)
    db.flush()

    if make_active:
        db.query(AIProviderConfig).update({"is_active": False})
        config.is_active = True

    db.add(AuditLogEntry(
        actor=admin.email, action="ai_provider_created",
        detail=f"Added '{config.label}' ({provider_type}), made active: {bool(make_active)}.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/ai-providers", status_code=303)


@router.post("/admin/ai-providers/{config_id}/activate")
def activate_ai_provider(
    config_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """Only one config is ever is_active at a time - activating this one deactivates every other."""
    target = db.get(AIProviderConfig, config_id)
    if target:
        db.query(AIProviderConfig).update({"is_active": False})
        target.is_active = True
        target.updated_at = datetime.datetime.utcnow()
        db.add(AuditLogEntry(
            actor=admin.email, action="ai_provider_activated",
            detail=f"'{target.label}' ({target.provider_type}) made the active provider for marking.",
        ))
        db.commit()
    return RedirectResponse(url="/admin/ai-providers", status_code=303)


@router.post("/admin/ai-providers/{config_id}/activate-for-spec-parsing")
def activate_ai_provider_for_spec_parsing(
    config_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """Same idea as the old SPEC_PARSING_LLM_PROVIDER env var - an optional SEPARATE
    provider just for spec parsing, independent of the everyday active one. config_id=0
    clears it, so spec parsing falls back to using the everyday active provider."""
    db.query(AIProviderConfig).update({"is_active_for_spec_parsing": False})
    label = "the everyday active provider (cleared override)"
    if config_id:
        target = db.get(AIProviderConfig, config_id)
        if target:
            target.is_active_for_spec_parsing = True
            target.updated_at = datetime.datetime.utcnow()
            label = f"'{target.label}' ({target.provider_type})"
    db.add(AuditLogEntry(
        actor=admin.email, action="ai_provider_spec_parsing_override_changed",
        detail=f"Spec parsing now uses: {label}.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/ai-providers", status_code=303)


@router.post("/admin/ai-providers/{config_id}/activate-for-draft-generation")
def activate_ai_provider_for_draft_generation(
    config_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    An optional override for scheme-of-work/lesson DRAFT generation specifically.
    Unlike spec parsing, this category does NOT fall back to the everyday active
    provider if nothing is set here - it defaults straight to Ollama (see
    llm_gateway.py's _resolve_config), so a paid provider activated for marking never
    silently starts charging for draft generation too. config_id=0 clears an explicit
    override, returning this category to its Ollama default.
    """
    db.query(AIProviderConfig).update({"is_active_for_draft_generation": False})
    label = "Ollama (its default)"
    if config_id:
        target = db.get(AIProviderConfig, config_id)
        if target:
            target.is_active_for_draft_generation = True
            target.updated_at = datetime.datetime.utcnow()
            label = f"'{target.label}' ({target.provider_type})"
    db.add(AuditLogEntry(
        actor=admin.email, action="ai_provider_draft_generation_override_changed",
        detail=f"Scheme-of-work/lesson draft generation now uses: {label}.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/ai-providers", status_code=303)


@router.post("/admin/ai-providers/{config_id}/delete")
def delete_ai_provider(
    config_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    target = db.get(AIProviderConfig, config_id)
    if target:
        db.add(AuditLogEntry(
            actor=admin.email, action="ai_provider_deleted",
            detail=f"Deleted '{target.label}' ({target.provider_type}).",
        ))
        db.delete(target)
        db.commit()
    return RedirectResponse(url="/admin/ai-providers", status_code=303)
