from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent


def iter_reply_components(event: AstrMessageEvent) -> list[Comp.Reply]:
    chain = getattr(event.message_obj, "message", None) or []
    return [seg for seg in chain if isinstance(seg, Comp.Reply)]


def collect_forward_ids_from_components(components: list[Any]) -> list[str]:
    ids: list[str] = []
    for comp in components or []:
        if isinstance(comp, Comp.Forward):
            forward_id = getattr(comp, "id", "")
            if forward_id:
                ids.append(str(forward_id))
        elif isinstance(comp, Comp.Nodes):
            for node in getattr(comp, "nodes", []) or []:
                ids.extend(collect_forward_ids_from_components(getattr(node, "content", []) or []))
        elif isinstance(comp, Comp.Node):
            ids.extend(collect_forward_ids_from_components(getattr(comp, "content", []) or []))
    return ids


async def call_onebot_action(event: AstrMessageEvent, action: str, **params: Any) -> Any:
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if callable(call_action):
        return await call_action(action, **params)
    api = getattr(bot, "api", None)
    call_action = getattr(api, "call_action", None)
    if callable(call_action):
        return await call_action(action, **params)
    raise RuntimeError("当前平台无法调用 OneBot API")


async def get_forward_msg(event: AstrMessageEvent, forward_id: str) -> Any:
    params_list: list[dict[str, Any]] = [{"message_id": forward_id}, {"id": forward_id}]
    if forward_id.isdigit():
        params_list.extend([{"message_id": int(forward_id)}, {"id": int(forward_id)}])
    last_error: Exception | None = None
    for params in params_list:
        try:
            return await call_onebot_action(event, "get_forward_msg", **params)
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return None
