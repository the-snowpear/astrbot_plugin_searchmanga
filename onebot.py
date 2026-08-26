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


async def get_msg(event: AstrMessageEvent, message_id: str | int) -> Any:
    params_list: list[dict[str, Any]] = [{"message_id": message_id}, {"id": message_id}]
    if isinstance(message_id, str) and message_id.isdigit():
        params_list.extend([{"message_id": int(message_id)}, {"id": int(message_id)}])
    elif isinstance(message_id, int):
        params_list.extend([{"message_id": str(message_id)}, {"id": str(message_id)}])

    last_error: Exception | None = None
    for params in params_list:
        try:
            return await call_onebot_action(event, "get_msg", **params)
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return None


def extract_images_from_onebot_message(message_data: Any) -> list[str]:
    images: list[str] = []
    if not message_data:
        return images

    if isinstance(message_data, dict):
        # 1. 如果包含嵌套 message 字段（如 get_msg 的返回体），优先递归提取
        msg = message_data.get("message") or message_data.get("messages")
        if msg:
            return extract_images_from_onebot_message(msg)
        inner_data = message_data.get("data")
        if isinstance(inner_data, dict):
            inner_msg = inner_data.get("message") or inner_data.get("messages")
            if inner_msg:
                return extract_images_from_onebot_message(inner_msg)

        # 2. 解析当前字典是否为 image 消息段
        seg_type = message_data.get("type")
        if seg_type == "image":
            if isinstance(inner_data, dict):
                url = inner_data.get("url") or inner_data.get("file") or inner_data.get("path")
                if url and isinstance(url, str):
                    images.append(url.strip())
            elif isinstance(message_data.get("file"), str):
                images.append(message_data["file"].strip())
            elif isinstance(message_data.get("url"), str):
                images.append(message_data["url"].strip())
        elif isinstance(inner_data, dict) and (inner_data.get("url") or inner_data.get("file")):
            url = inner_data.get("url") or inner_data.get("file") or inner_data.get("path")
            if url and isinstance(url, str):
                images.append(url.strip())
        return images

    if isinstance(message_data, list):
        for item in message_data:
            images.extend(extract_images_from_onebot_message(item))
        return images

    if isinstance(message_data, Comp.Image):
        url = getattr(message_data, "url", None) or getattr(message_data, "file", None) or getattr(message_data, "path", None)
        if url and isinstance(url, str):
            images.append(url.strip())

    return images
