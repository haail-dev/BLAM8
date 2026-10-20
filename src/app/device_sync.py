"""Durable, account-scoped workspaces and replay protection across devices.

PostgreSQL serializes claims; no lock depends on the web process. Events are
only refresh hints. Clients read durable state on reconnect and periodically.
An interrupted operation stays unresolved rather than risking a second write.
"""

import hashlib
import json
from uuid import UUID, uuid5, NAMESPACE_URL
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from app import db, events
from app.auth import (
    TenantCtx,
    require_tenant,
    AuthRedirect,
    OnboardingRedirect,
    SubscriptionRequired,
)
from app.config import settings
from app.security import require_csrf, require_csrf_header, rate_limit

router = APIRouter(
    prefix="/device-sync", dependencies=[Depends(rate_limit("device_sync", 240, 60))]
)


def identity(ctx):
    if not settings.device_sync_enabled:
        raise HTTPException(404, "Device sync is disabled.")
    if not ctx.user_id or ctx.viewing_as:
        raise HTTPException(403, "Leave View as before syncing your work.")
    return {"p_tenant": ctx.tenant_id, "p_user": ctx.user_id}


def read(ctx, channel):
    identity(ctx)
    rows = (
        db.db()
        .table("device_sync")
        .select("revision,payload")
        .eq("tenant_id", ctx.tenant_id)
        .eq("user_id", ctx.user_id)
        .eq("channel", channel)
        .execute()
        .data
    )
    return rows[0] if rows else {"revision": 0, "payload": {}}


def rpc(name, args):
    try:
        return db.db().rpc(name, args).execute().data
    except Exception as exc:
        if any(
            code in str(exc) for code in ("SYNC_STALE", "SYNC_RUNNING", "SYNC_REUSED")
        ):
            raise HTTPException(
                409,
                "This work changed or is running on another device. Refresh before trying again.",
            ) from None
        raise HTTPException(
            503, "Sync could not be confirmed. Reconnect before trying again."
        ) from None


def claim(ctx, channel, operation, revision, fingerprint):
    return rpc(
        "claim_device_sync",
        {
            **identity(ctx),
            "p_channel": channel,
            "p_id": str(operation),
            "p_revision": revision,
            "p_fingerprint": fingerprint,
        },
    )


def finish(ctx, operation, patch, response):
    result = rpc(
        "finish_device_sync",
        {
            **identity(ctx),
            "p_id": str(operation),
            "p_patch": patch,
            "p_response": response,
        },
    )
    events.publish(ctx.tenant_id, {"type": "device_sync", "user_id": ctx.user_id})
    return result


def path_allowed(path):
    """Only local setup routes can ever drive another device's navigation."""
    parts = urlsplit(path)
    return (
        not parts.scheme
        and not parts.netloc
        and not parts.fragment
        and "\\" not in path
        and (
            parts.path
            in (
                "/onboarding/interview",
                "/onboarding/industry",
                "/onboarding/mailbox",
                "/onboarding/voice",
                "/onboarding/knowledge",
                "/onboarding/data",
            )
            or parts.path.startswith("/onboarding/knowledge/review/")
            or parts.path
            in (
                "/app/",
                "/app",
                "/app/diary/settings",
                "/app/diary/intake",
                "/app/diary/dependants",
                "/app/crm/settings",
                "/app/invoices/settings",
            )
        )
    )


def review_message(messages, answer):
    """Persist the same bounded decision cards as Concierge, before acknowledgement.

    Only the guide's item-scoped receipt settles an earlier card. A reply's prose
    and a model-inferred decision never become choice history.
    """
    decision = answer.get("review_decision")
    if isinstance(decision, dict) and decision.get("state") in (
        "prepared",
        "editing",
        "preview",
    ):
        for prior in reversed(messages):
            card = prior.get("knowledge_card")
            if not isinstance(card, dict):
                continue
            focus = card.get("focus")
            if not isinstance(focus, dict):
                continue
            if (
                all(
                    card.get(key) == decision.get(key)
                    for key in ("batch_id", "version", "focus_hash")
                )
                and focus.get("kind") == decision.get("kind")
                and focus.get("id") == decision.get("id")
            ):
                card.update(selected=decision["action"], state=decision["state"])
                break
    message = {"role": "assistant", "content": answer["reply"]}
    if all(
        answer.get(key) for key in ("focus_hash", "batch_id", "version")
    ) and isinstance(answer.get("focus"), dict):
        message["knowledge_card"] = {
            "batch_id": answer["batch_id"],
            "version": answer["version"],
            "focus_hash": answer["focus_hash"],
            "focus": {key: answer["focus"].get(key) for key in ("kind", "id")},
            "actions": answer.get("actions", []),
            "save": bool(answer.get("save_ready")),
        }
    return message


def valid_checkpoint(patch):
    """Reject malformed private drafts before they can poison a later route."""
    forms = patch.get("form", {})
    if not isinstance(forms, dict):
        return False
    for saved in forms.values():
        if (
            not isinstance(saved, dict)
            or not isinstance(saved.get("action"), str)
            or not isinstance(saved.get("version"), str)
            or not isinstance(saved.get("values"), dict)
        ):
            return False
        if any(
            not isinstance(name, str)
            or not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)
            for name, values in saved["values"].items()
        ):
            return False
    guide = patch.get("guide", {})
    return isinstance(guide, dict) and (
        not guide
        or (
            isinstance(guide.get("review"), str)
            and isinstance(guide.get("version"), str)
            and isinstance(guide.get("data"), dict)
        )
    )


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: UUID
    revision: int = Field(ge=0)
    patch: dict = Field(max_length=4)


@router.get("/{channel}")
def get_state(
    channel: str, response: Response, ctx: TenantCtx = Depends(require_tenant)
):
    if channel not in ("flow", "assistant"):
        raise HTTPException(404)
    response.headers["Cache-Control"] = "no-store"
    return read(ctx, channel)


@router.post("/{channel}", dependencies=[Depends(require_csrf_header)])
def change_state(channel: str, body: Change, ctx: TenantCtx = Depends(require_tenant)):
    identity(ctx)
    allowed = (
        {"path", "form", "guide", "messages"}
        if channel == "flow"
        else {"messages"}
        if channel == "assistant"
        else set()
    )
    if (
        not body.patch
        or set(body.patch) - allowed
        or len(json.dumps(body.patch)) > 700000
    ):
        raise HTTPException(422, "Invalid sync state.")
    if "path" in body.patch and (
        not isinstance(body.patch["path"], str) or not path_allowed(body.patch["path"])
    ):
        raise HTTPException(422, "Invalid setup location.")
    # Completion is written only by the actual onboarding response.
    if "complete" in body.patch:
        raise HTTPException(422, "Complete setup using its Continue action.")
    if "messages" in body.patch:
        messages = body.patch["messages"]
        if (
            not isinstance(messages, list)
            or len(messages) > 80
            or any(
                not isinstance(m, dict)
                or m.get("role") not in ("user", "assistant")
                or not isinstance(m.get("content"), str)
                or len(m["content"]) > 60000
                for m in messages
            )
        ):
            raise HTTPException(422, "Invalid transcript.")
    if not valid_checkpoint(body.patch):
        raise HTTPException(422, "Invalid setup checkpoint.")
    fingerprint = hashlib.sha256(
        json.dumps(body.patch, sort_keys=True).encode()
    ).hexdigest()
    current = claim(ctx, channel, body.operation, body.revision, fingerprint)
    if "replay" in current:
        return read(ctx, channel)
    result = finish(ctx, body.operation, body.patch, {"ok": True})
    return result


class SyncRoute(APIRoute):
    """Replay a submitted turn/Continue without executing its handler twice.

    Existing clients remain supported while the deployment flag is disabled.
    The new client supplies a revision and operation id for guarded writes.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def synced(request: Request):
            if request.method != "POST":
                return await handler(request)
            operation = request.headers.get("x-blam8-operation")
            form = None
            if (
                settings.device_sync_enabled
                and request.method == "POST"
                and "application/json" not in request.headers.get("content-type", "")
            ):
                form = await request.form()
                operation = operation or form.get("_sync_operation")
            if not settings.device_sync_enabled or not operation:
                return await handler(request)
            try:
                operation = UUID(str(operation))
                revision = int(
                    request.headers.get("x-blam8-revision")
                    or form.get("_sync_revision")
                )
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(422, "Invalid sync operation.") from None
            ctx = await require_tenant(request, Response())
            identity(ctx)
            if form is not None:
                await require_csrf(request)
            else:
                await require_csrf_header(request)
            channel = (
                "flow"
                if request.url.path.startswith("/onboarding/")
                or request.url.path == "/concierge/onboard"
                or request.headers.get("x-blam8-channel") == "flow"
                or (form is not None and form.get("_sync_channel") == "flow")
                else "assistant"
            )
            raw = (
                await request.body()
                if form is None
                else str(
                    sorted(
                        (k, str(v))
                        for k, v in form.multi_items()
                        if not k.startswith("_sync_")
                    )
                ).encode()
            )
            digest = hashlib.sha256(
                (request.url.path + "?" + request.url.query).encode() + b"\n" + raw
            ).hexdigest()
            # Arrival greetings are a shared operation too: opening a second
            # device must not run the same automatic interview again.
            automatic = request.headers.get("x-blam8-automatic") == "1"
            arrival = None
            if (
                automatic
                and form is None
                and (
                    request.url.path in ("/concierge/onboard", "/concierge/help")
                    or request.url.path.endswith("/guide")
                )
            ):
                body = json.loads(raw)
                arrival = (
                    "onboard:" + str(body.get("step"))
                    if request.url.path == "/concierge/onboard"
                    else "help:" + str(body.get("path"))
                    if request.url.path == "/concierge/help"
                    else "guide:"
                    + request.url.path
                    + ":"
                    + str((body.get("form") or {}).get("version"))
                )
                if request.url.path.endswith("/guide"):
                    # A read-only arrival must reflect the restored review,
                    # rather than replay the first note for its entire version.
                    guide_identity = {"form": body.get("form") or {}}
                    if str(body.get("question") or "").strip():
                        guide_identity.update(question=body.get("question"), focus_hash=body.get("focus_hash"))
                    arrival += ":" + hashlib.sha256(
                        json.dumps(guide_identity, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                key = arrival
                operation = uuid5(NAMESPACE_URL, f"{ctx.tenant_id}:{ctx.user_id}:{key}")
                digest = hashlib.sha256(key.encode()).hexdigest()
            current = await run_in_threadpool(
                claim, ctx, channel, operation, revision, digest
            )
            if "replay" in current:
                stored = current["replay"]
                headers = {
                    **stored.get("headers", {}),
                    "X-Blam8-Revision": str(
                        (await run_in_threadpool(read, ctx, channel))["revision"]
                    ),
                }
                return Response(
                    stored.get("body", ""),
                    status_code=stored.get("status", 409),
                    headers=headers,
                )
            try:
                result = await handler(request)
            except RequestValidationError as exc:
                result = Response(
                    json.dumps({"detail": exc.errors()}, default=str),
                    status_code=422,
                    media_type="application/json",
                )
            except (AuthRedirect, OnboardingRedirect, SubscriptionRequired) as exc:
                location = (
                    "/login"
                    if isinstance(exc, AuthRedirect)
                    else "/onboarding"
                    if isinstance(exc, OnboardingRedirect)
                    else "/billing/plans"
                )
                result = Response(status_code=303, headers={"location": location})
            except HTTPException as exc:
                result = Response(
                    json.dumps({"detail": exc.detail}),
                    status_code=exc.status_code,
                    media_type="application/json",
                )
            except Exception:
                # Do not release a request whose side effects are unknown.
                raise
            patch = {}
            if result.status_code < 400:
                if (
                    channel == "flow"
                    and request.url.path.startswith("/onboarding/knowledge")
                    and not request.url.path.endswith("/guide")
                ):
                    patch["refresh"] = str(operation)
                location = result.headers.get("location", "")
                if channel == "flow" and location and path_allowed(location):
                    patch = {"path": urlsplit(location).path, "form": {}, "guide": {}}
                    if not location.startswith("/onboarding/"):
                        from app import setup_handoff

                        nxt = await run_in_threadpool(
                            lambda: setup_handoff.first_open(
                                setup_handoff.load_steps(
                                    ctx.tenant_id, client=ctx.client
                                )
                            )
                        )
                        patch.update(
                            path=nxt["href"] if nxt else "/app/", complete=nxt is None
                        )
                elif (
                    channel == "flow"
                    and request.url.path.startswith(
                        ("/app/", "/concierge/setup-cards/")
                    )
                    and current["payload"].get("complete") is not True
                ):
                    from app import setup_handoff

                    steps = await run_in_threadpool(
                        lambda: setup_handoff.load_steps(
                            ctx.tenant_id, client=ctx.client
                        )
                    )
                    active = next(
                        (
                            s
                            for s in steps
                            if s["href"] == current["payload"].get("path")
                        ),
                        None,
                    )
                    if active and active["done"]:
                        nxt = setup_handoff.first_open(steps)
                        patch = {
                            "path": nxt["href"] if nxt else "/app/",
                            "complete": nxt is None,
                            "form": {},
                            "guide": {},
                        }
                if request.url.path == "/app/setup/hide":
                    patch = {
                        "path": urlsplit(location).path
                        if path_allowed(location)
                        else "/app/",
                        "complete": True,
                        "form": {},
                        "guide": {},
                    }
                if "application/json" in result.headers.get(
                    "content-type", ""
                ) and hasattr(result, "body"):
                    answer = json.loads(result.body)
                    body = json.loads(raw) if form is None else {}
                    if answer.get("reply") and (
                        request.url.path in ("/concierge/help", "/concierge/onboard")
                        or request.url.path.endswith("/guide")
                    ):
                        messages = list(current["payload"].get("messages", []))
                        question = body.get("question", "")
                        if (
                            not automatic
                            and question
                            and (
                                not messages
                                or messages[-1].get("role") != "user"
                                or messages[-1].get("content") != question
                            )
                        ):
                            messages.append({"role": "user", "content": question})
                        message = review_message(messages, answer)
                        for key in ("links", "trail"):
                            if answer.get(key):
                                message[key] = answer[key]
                        messages.append(message)
                        patch["messages"] = messages[-80:]
                        if arrival:
                            patch["arrivals"] = list(
                                dict.fromkeys(
                                    current["payload"].get("arrivals", []) + [arrival]
                                )
                            )[-80:]
                        if request.url.path.endswith("/guide"):
                            patch["guide"] = {
                                "review": request.url.path.split("/")[-2],
                                "data": answer,
                                "version": (body.get("form") or {}).get("version"),
                            }
                            forms = current["payload"].get("form", {})
                            for saved in forms.values():
                                if saved.get("action", "").endswith(
                                    "/" + patch["guide"]["review"]
                                ):
                                    for name, value in answer.get(
                                        "patches", {}
                                    ).items():
                                        saved["values"][name] = [value]
                            patch["form"] = forms
            stored = {
                "status": result.status_code,
                "headers": {
                    k: v
                    for k, v in result.headers.items()
                    if k in ("location", "content-type")
                },
                "body": result.body.decode() if hasattr(result, "body") else "",
            }
            # Streaming handlers are deliberately excluded by the client.
            await run_in_threadpool(finish, ctx, operation, patch, stored)
            result.headers["X-Blam8-Revision"] = str(current["revision"] + 1)
            return result

        return synced
