"""豆瓣将映插件。

RSSHub 提供即将上映条目；插件用宿主统一搜索与详情操作解析作品，用订阅操作
创建订阅。歧义项和缺少开播日期的条目只保留为待确认，不会替用户猜第一部。
"""

import contextlib
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mediaclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from mediaclaw_sdk import Context, net, plugin
from mediaclaw_sdk.events import PLUGIN_EVENTS, plugin_event
from mediaclaw_sdk.ops import HOST_OPS, OpsError
from mediaclaw_sdk.services import PLUGIN_DATA, PLUGIN_ROUTES

PLUGIN_ID = "douban-coming-notice"
REQUIRES = {
    "scheduled-tasks": "^1.1",
    "plugin-data": "^1.0",
    "plugin-routes": "^1.0",
    "plugin-events": "^1.0",
}
OPERATIONS = ("search.titles", "discover.get-title-details", "subscriptions.create")

_DOUBAN_ID_RE = re.compile(r"/subject/(\d+)")
_WISH_RE = re.compile(r"(?:想看人数|想看)[：:\s]*([\d,]+)")
_YEAR_RE = re.compile(r"[（(](\d{4})[)）]")
_SEASON_RE = re.compile(r"(?:第\s*([一二三四五六七八九十百\d]+)\s*季|[Ss](\d{1,2})(?!\d))")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


class Settings(BaseModel):
    rsshub: str = "https://rsshub.ddsrem.com"
    sort_by: Literal["hot", "time"] = "hot"
    count: int = Field(default=10, ge=1, le=100)
    wish_count_threshold: int = Field(default=5000, ge=0)
    advance_days: int = Field(default=7, ge=0, le=90)
    notify_before_air: bool = True
    notify_hours: int = Field(default=24, ge=0, le=720)
    enabled: bool = True


class Airing(BaseModel):
    model_config = ConfigDict(frozen=True)
    title: str
    air_date: str


AIRING = plugin_event("douban-coming-notice.airing", Airing, title="豆瓣开播提醒")


def _cn_to_int(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        head, tail = value.split("十", 1)
        return (10 * _CN_NUM.get(head, 1) if head else 10) + _CN_NUM.get(tail, 0)
    return _CN_NUM.get(value, 1)


def _parse_rss(xml_text: str) -> list[dict[str, Any]]:
    """解析 RSSHub；兼容「想看人数：」「想看：」、中文季号和 Sxx。"""

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    rows: list[dict[str, Any]] = []
    for node in root.iter("item"):
        original = (node.findtext("title") or "").strip()
        if not original:
            continue
        link = node.findtext("link") or node.findtext("guid") or ""
        description = node.findtext("description") or ""
        douban_match = _DOUBAN_ID_RE.search(link) or _DOUBAN_ID_RE.search(description)
        wish_match = _WISH_RE.search(description)
        year_match = _YEAR_RE.search(original)
        season_match = _SEASON_RE.search(original)
        wish_count = 0
        if wish_match:
            with contextlib.suppress(ValueError):
                wish_count = int(wish_match.group(1).replace(",", ""))
        season = 1
        if season_match:
            season = _cn_to_int(season_match.group(1) or season_match.group(2) or "1")
        base_title = _SEASON_RE.sub("", original).strip()
        published = node.findtext("pubDate") or node.findtext("published") or ""
        try:
            published_at = parsedate_to_datetime(published).isoformat() if published else None
        except (TypeError, ValueError):
            published_at = None
        rows.append(
            {
                "title": base_title or original,
                "original_title": original,
                "douban_id": douban_match.group(1) if douban_match else None,
                "wish_count": wish_count,
                "year": int(year_match.group(1)) if year_match else None,
                "season": season,
                "published_at": published_at,
            }
        )
    return rows


def _days_until(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return (date.fromisoformat(value[:10]) - date.today()).days
    except ValueError:
        return None


def _choose_candidate(
    candidates: list[dict[str, Any]], item: dict[str, Any]
) -> dict[str, Any] | None:
    """先按豆瓣 ID 收敛，再按精确片名与年份收敛；仍有多个结果就交给用户确认。"""

    douban_id = item.get("douban_id")
    if douban_id:
        exact = [
            row for row in candidates
            if row.get("provider") == "douban" and str(row.get("external_id")) == str(douban_id)
        ]
        if len(exact) == 1:
            return exact[0]
    title = str(item.get("title") or "").casefold()
    year = item.get("year")
    exact = [
        row for row in candidates
        if str(row.get("title") or row.get("original_title") or "").casefold() == title
        and (not year or not row.get("release_year") or int(row["release_year"]) == int(year))
    ]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _candidate_refs(details: Any) -> list[str]:
    if isinstance(details, list):
        return [
            str(row.get("title_ref"))
            for row in details
            if isinstance(row, dict) and row.get("title_ref")
        ]
    if isinstance(details, dict):
        rows = details.get("candidates") or details.get("titles") or []
        return [
            str(row.get("title_ref"))
            for row in rows
            if isinstance(row, dict) and row.get("title_ref")
        ]
    return []


async def _refresh(
    ops: Any, store: Any, events: Any, settings: Settings, logger: Any
) -> dict[str, Any]:
    url = f"{settings.rsshub.rstrip('/')}/douban/tv/coming?sort={settings.sort_by}"
    async with httpx.AsyncClient(transport=net.http_transport(PLUGIN_ID), timeout=20) as client:
        response = await client.get(url)
        response.raise_for_status()
        items = _parse_rss(response.text)

    history = await store.get("history", default=[])
    pending = await store.get("pending", default={})
    notified = await store.get("notified", default=[])
    if not isinstance(history, list):
        history = []
    if not isinstance(pending, dict):
        pending = {}
    if not isinstance(notified, list):
        notified = []
    notified_set = set(map(str, notified))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for item in items[: settings.count]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        wish_count = int(item.get("wish_count") or 0)
        if wish_count and wish_count < settings.wish_count_threshold:
            continue
        found = await ops.call(
            "search.titles",
            {"query": title, "provider": "all", "save_history": False},
        )
        candidates = found.get("titles") or []
        chosen = _choose_candidate(candidates, item)
        unique = f"douban:{item.get('douban_id') or title}:s{item.get('season') or 1}"
        previous = next((row for row in history if row.get("unique") == unique), {})
        row = {
            **previous,
            **item,
            "unique": unique,
            "title": chosen.get("title") if chosen else title,
            "poster_url": chosen.get("poster_url") if chosen else None,
            "genres": chosen.get("genres") or [] if chosen else [],
            "subscribed": bool(previous.get("subscribed")),
            "status": "pending" if not chosen else "resolved",
            "candidates": [candidate.get("title_ref") for candidate in candidates],
            "time": now,
        }
        if not chosen:
            pending[unique] = {
                "title": title,
                "reason": "匹配到多个作品" if len(candidates) > 1 else "没有找到可靠作品",
                "candidates": candidates,
                "time": now,
            }
            history = [old for old in history if old.get("unique") != unique] + [row]
            continue

        title_ref = str(chosen["title_ref"])
        detail = await ops.call("discover.get-title-details", {"title_ref": title_ref})
        air_date = str((detail.get("metadata") or {}).get("released") or "")[:10] or None
        row["air_date"] = air_date
        days = _days_until(air_date)
        row["library_status"] = chosen.get("library_status")
        if days is None:
            row["status"] = "waiting-date"
            pending[unique] = {
                "title": row["title"],
                "reason": "详情页还没有开播日期",
                "candidates": [chosen],
                "time": now,
            }
        elif (
            0 <= days <= settings.advance_days
            and not row["subscribed"]
            and not chosen.get("library_status")
        ):
            try:
                created = await ops.call(
                    "subscriptions.create",
                    {
                        "title_ref": title_ref,
                        # 从豆瓣匹配到 TMDB 后仍保留原始豆瓣身份；宿主只接受豆瓣
                        # title_ref 作为 source_title_ref，不能把 TMDB 引用回传。
                        **(
                            {"source_title_ref": f"douban:{item['douban_id']}"}
                            if item.get("douban_id")
                            else {}
                        ),
                        "selected_seasons": [int(item.get("season") or 1)],
                        "follow_future": False,
                    },
                )
                row["subscribed"] = bool(created.get("subscription") or created.get("created"))
                row["status"] = "subscribed" if row["subscribed"] else "subscription-existing"
                pending.pop(unique, None)
            except OpsError as exc:
                if exc.code != "SUBSCRIPTION_TARGET_AMBIGUOUS":
                    raise
                row["status"] = "pending"
                pending[unique] = {
                    "title": row["title"],
                    "reason": "豆瓣作品对应多个媒体条目，等待确认",
                    "candidates": _candidate_refs(exc.details),
                    "time": now,
                }

        notify_key = f"{item.get('douban_id') or title}:{air_date or 'unknown'}"
        if (
            settings.notify_before_air
            and days is not None
            and 0 <= days * 24 <= settings.notify_hours
            and notify_key not in notified_set
        ):
            await events.publish(AIRING, Airing(title=row["title"], air_date=air_date or "-"))
            notified.append(notify_key)
            notified_set.add(notify_key)
            row["notify_sent"] = True

        history = [old for old in history if old.get("unique") != unique] + [row]

    summary = {
        "last_time": now,
        "item_count": len(items[: settings.count]),
        "history_count": len(history),
        "pending_count": len(pending),
    }
    await store.set("history", history[-200:])
    await store.set("pending", pending)
    await store.set("notified", notified[-500:])
    await store.set("summary", summary)
    logger.info("豆瓣将映刷新完成：历史 %d 条，待确认 %d 条", len(history), len(pending))
    return {"summary": summary, "history": list(reversed(history[-30:])), "pending": pending}


@plugin(
    PLUGIN_ID,
    title="豆瓣将映",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES, PLUGIN_EVENTS),
    requires=REQUIRES,
    permissions=OPERATIONS,
    reloadable=True,
)
async def apply(ctx: Context) -> None:
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)
    events = await ctx.use(PLUGIN_EVENTS).register(ctx, AIRING)

    async def settings() -> Settings:
        raw = await store.get("settings", default={})
        if not isinstance(raw, dict):
            return Settings()
        try:
            return Settings.model_validate(raw)
        except ValueError:
            return Settings()

    async def refresh() -> None:
        current = await settings()
        if current.enabled:
            await _refresh(ops, store, events, current, ctx.logger)

    ctx.contribute(
        SCHEDULED_TASKS,
        f"{PLUGIN_ID}.refresh",
        TaskDefinition(
            key=f"{PLUGIN_ID}.refresh",
            title="豆瓣将映刷新",
            handler=refresh,
            default_trigger_type=TriggerType.CRON,
            default_cron="0 8 * * *",
            description="抓取豆瓣即将播出剧集，临近开播时自动订阅并发送提醒。",
        ),
    )

    router = APIRouter()

    @router.get("/report", operation_id=f"plugins.{PLUGIN_ID}.report")
    async def report() -> dict[str, Any]:
        history = await store.get("history", default=[])
        pending = await store.get("pending", default={})
        summary = await store.get("summary", default={})
        return {
            "settings": (await settings()).model_dump(),
            "history": list(reversed(history[-30:])) if isinstance(history, list) else [],
            "pending": pending if isinstance(pending, dict) else {},
            "summary": summary if isinstance(summary, dict) else {},
        }

    @router.put("/settings", operation_id=f"plugins.{PLUGIN_ID}.settings-save")
    async def save(payload: Settings) -> dict[str, Any]:
        await store.set("settings", payload.model_dump())
        return {"settings": payload.model_dump()}

    @router.post("/refresh", operation_id=f"plugins.{PLUGIN_ID}.refresh-now")
    async def refresh_now(payload: Settings) -> dict[str, Any]:
        await store.set("settings", payload.model_dump())
        try:
            return await _refresh(ops, store, events, payload, ctx.logger)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"抓取 RSS 失败：{exc}") from exc

    @router.delete("/pending/{key}", operation_id=f"plugins.{PLUGIN_ID}.pending-delete")
    async def delete_pending(key: str) -> dict[str, Any]:
        pending = await store.get("pending", default={})
        if not isinstance(pending, dict) or key not in pending:
            raise HTTPException(status_code=404, detail="待确认项不存在")
        pending.pop(key)
        await store.set("pending", pending)
        return {"deleted": True}

    routes.mount(ctx, router)
