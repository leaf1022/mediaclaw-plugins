"""媒体库封面生成插件。

媒体库数据和实际渲染都由宿主负责。插件只保存选择、触发生成并记录最近结果，
因此不需要直接读取宿主数据库，也不会覆盖用户手动上传的封面。
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mediaclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from mediaclaw_sdk import Context, plugin
from mediaclaw_sdk.ops import HOST_OPS
from mediaclaw_sdk.services import PLUGIN_DATA, PLUGIN_ROUTES

PLUGIN_ID = "media-cover-generator"
REQUIRES = {
    "scheduled-tasks": "^1.1",
    "plugin-data": "^1.0",
    "plugin-routes": "^1.0",
}
OPERATIONS = ("library.list", "library.cover.generate")
STYLES = {
    "static_1": "经典拼贴",
    "static_2": "斜切层叠",
    "static_3": "多图矩阵",
    "static_4": "玻璃标题",
}
HISTORY_LIMIT = 50


class Settings(BaseModel):
    """页面和定时任务共用的最小设置。"""

    library_id: int | None = Field(default=None, ge=1)
    include_libraries: list[int] = Field(default_factory=list)
    style: Literal["static_1", "static_2", "static_3", "static_4"] = "static_1"
    enabled: bool = True


def _settings(raw: Any) -> Settings:
    if not isinstance(raw, dict):
        return Settings()
    try:
        return Settings.model_validate(raw)
    except ValueError:
        return Settings()


def _library_count(library: dict[str, Any]) -> int:
    stats = library.get("stats")
    return int((stats if isinstance(stats, dict) else {}).get("item_count") or 0)


async def _generate(
    ops: Any,
    store: Any,
    settings: Settings,
    *,
    library_id: int | None = None,
    style: str | None = None,
) -> dict[str, Any]:
    """生成一个或全部目标媒体库，并按 library_id + style 更新历史。"""

    listed = await ops.call("library.list", {})
    libraries = listed.get("libraries") or listed.get("items") or []
    selected_ids = set(settings.include_libraries)
    if settings.library_id:
        selected_ids.add(settings.library_id)
    if library_id:
        selected_ids = {library_id}

    targets = [
        row
        for row in libraries
        if isinstance(row, dict)
        and (not selected_ids or int(row.get("id") or 0) in selected_ids)
    ]
    selected_style = style if style in STYLES else settings.style
    history = await store.get("history", default=[])
    if not isinstance(history, list):
        history = []
    history = [row for row in history if isinstance(row, dict)]

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for library in targets:
        current_id = int(library.get("id") or 0)
        name = str(library.get("name") or f"媒体库 {current_id}")
        if not current_id:
            continue
        try:
            result = await ops.call(
                "library.cover.generate",
                {
                    "library_id": current_id,
                    "style": selected_style,
                    "replace_custom": False,
                },
            )
        except Exception as exc:
            skipped.append({"library_id": current_id, "title": name, "reason": str(exc)})
            continue
        if not result.get("applied"):
            skipped.append(
                {
                    "library_id": current_id,
                    "title": name,
                    "reason": result.get("reason") or "宿主没有生成封面",
                }
            )
            continue
        row = {
            "unique": f"{current_id}:{selected_style}",
            "library_id": current_id,
            "title": name,
            "style": selected_style,
            "style_label": STYLES[selected_style],
            "cover_url": result.get("cover_url"),
            "reason": result.get("reason"),
            "item_count": _library_count(library),
            "time": now,
        }
        existing = next((item for item in history if item.get("unique") == row["unique"]), None)
        if existing is None:
            history.append(row)
        else:
            existing.update(row)
        generated.append(row)

    summary = {
        "last_time": now,
        "style": selected_style,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
    }
    await store.set("history", history[-HISTORY_LIMIT:])
    await store.set("summary", summary)
    return {"generated": generated, "skipped": skipped, "summary": summary}


@plugin(
    PLUGIN_ID,
    title="媒体库封面生成",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES),
    requires=REQUIRES,
    permissions=OPERATIONS,
    reloadable=True,
)
async def apply(ctx: Context) -> None:
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)

    async def refresh() -> None:
        current = _settings(await store.get("settings", default={}))
        if not current.enabled:
            return
        result = await _generate(ops, store, current)
        ctx.logger.info(
            "媒体库封面刷新完成：生成 %d，跳过 %d",
            len(result["generated"]),
            len(result["skipped"]),
        )

    ctx.contribute(
        SCHEDULED_TASKS,
        f"{PLUGIN_ID}.refresh",
        TaskDefinition(
            key=f"{PLUGIN_ID}.refresh",
            title="媒体库封面刷新",
            handler=refresh,
            default_trigger_type=TriggerType.CRON,
            default_cron="0 6 * * *",
            description="按插件页面保存的媒体库和风格刷新封面，不覆盖手动上传的封面。",
        ),
    )

    router = APIRouter()

    @router.get("/report", operation_id=f"plugins.{PLUGIN_ID}.report")
    async def report() -> dict[str, Any]:
        listed = await ops.call("library.list", {})
        libraries = listed.get("libraries") or listed.get("items") or []
        history = await store.get("history", default=[])
        summary = await store.get("summary", default={})
        return {
            "settings": _settings(await store.get("settings", default={})).model_dump(),
            "libraries": [
                {
                    "id": int(row.get("id") or 0),
                    "name": str(row.get("name") or ""),
                    "kind": row.get("kind"),
                    "item_count": _library_count(row),
                    "has_custom_cover": bool(row.get("custom_cover")),
                }
                for row in libraries
                if isinstance(row, dict) and row.get("id")
            ],
            "history": list(reversed(history[-12:])) if isinstance(history, list) else [],
            "summary": summary if isinstance(summary, dict) else {},
            "styles": [{"id": key, "label": label} for key, label in STYLES.items()],
        }

    @router.put("/settings", operation_id=f"plugins.{PLUGIN_ID}.settings-save")
    async def save(payload: Settings) -> dict[str, Any]:
        await store.set("settings", payload.model_dump())
        return {"settings": payload.model_dump()}

    @router.post("/generate", operation_id=f"plugins.{PLUGIN_ID}.generate")
    async def generate(payload: Settings) -> dict[str, Any]:
        await store.set("settings", payload.model_dump())
        return await _generate(ops, store, payload)

    @router.delete("/history/{key}", operation_id=f"plugins.{PLUGIN_ID}.history-delete")
    async def delete_history(key: str) -> dict[str, Any]:
        history = await store.get("history", default=[])
        if not isinstance(history, list):
            history = []
        kept = [row for row in history if isinstance(row, dict) and row.get("unique") != key]
        if len(kept) == len(history):
            raise HTTPException(status_code=404, detail="记录不存在")
        await store.set("history", kept)
        return {"deleted": True}

    routes.mount(ctx, router)
