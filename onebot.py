import asyncio
from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent

from .parsing import unwrap_onebot_payload


def iter_reply_components(event: AstrMessageEvent) -> list[Any]:
    chain = getattr(getattr(event, "message_obj", None), "message", None) or []
    reply_cls = getattr(Comp, "Reply", None)
    return [
        seg
        for seg in chain
        if (isinstance(reply_cls, type) and isinstance(seg, reply_cls))
        or (isinstance(seg, dict) and seg.get("type") == "reply")
    ]


def collect_forward_ids_from_components(components: Any) -> list[str]:
    ids: list[str] = []
    if not components:
        return ids
    if isinstance(components, dict):
        seg_type = components.get("type")
        data = components.get("data") if isinstance(components.get("data"), dict) else {}
        if seg_type == "forward":
            fid = data.get("id") or data.get("message_id") or components.get("id")
            if fid:
                ids.append(str(fid))
        for key in ("content", "message", "messages", "nodes", "nodeList"):
            if key in components:
                ids.extend(collect_forward_ids_from_components(components[key]))
            if key in data:
                ids.extend(collect_forward_ids_from_components(data[key]))
        return ids

    if not isinstance(components, list):
        return ids

    forward_cls = getattr(Comp, "Forward", None)
    nodes_cls = getattr(Comp, "Nodes", None)
    node_cls = getattr(Comp, "Node", None)

    for comp in components:
        if isinstance(forward_cls, type) and isinstance(comp, forward_cls):
            forward_id = getattr(comp, "id", "")
            if forward_id:
                ids.append(str(forward_id))
        elif isinstance(nodes_cls, type) and isinstance(comp, nodes_cls):
            for node in getattr(comp, "nodes", []) or []:
                ids.extend(collect_forward_ids_from_components(getattr(node, "content", []) or []))
        elif isinstance(node_cls, type) and isinstance(comp, node_cls):
            ids.extend(collect_forward_ids_from_components(getattr(comp, "content", []) or []))
        elif isinstance(comp, dict):
            ids.extend(collect_forward_ids_from_components(comp))
        elif hasattr(comp, "nodes"):
            for node in getattr(comp, "nodes", []) or []:
                ids.extend(collect_forward_ids_from_components(getattr(node, "content", []) or []))
        elif hasattr(comp, "content"):
            ids.extend(collect_forward_ids_from_components(getattr(comp, "content", []) or []))
    return ids


def extract_forward_ids_from_onebot_message(payload: Any) -> list[str]:
    ids: list[str] = []
    data = unwrap_onebot_payload(payload)
    if data:
        ids.extend(collect_forward_ids_from_components(data))
    elif payload:
        ids.extend(collect_forward_ids_from_components(payload))
    seen: set[str] = set()
    unique: list[str] = []
    for fid in ids:
        if fid not in seen:
            seen.add(fid)
            unique.append(fid)
    return unique


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
        msg = message_data.get("message") or message_data.get("messages")
        if msg:
            return extract_images_from_onebot_message(msg)
        inner_data = message_data.get("data")
        if isinstance(inner_data, dict):
            inner_msg = inner_data.get("message") or inner_data.get("messages")
            if inner_msg:
                return extract_images_from_onebot_message(inner_msg)

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


@dataclass(frozen=True)
class ArbiterContext:
    message_id: int
    msg_time: int
    self_id: int


class EmojiLikeArbiter:
    """基于 QQ 表情贴纸的多 Bot 仲裁器（可选启用，MIT 参考实现）。"""

    _EMOJI_ID = 289
    _EMOJI_TYPE = "1"
    _WAIT_SEC = 1.0
    _FEEDBACK_EMOJI_ID = 124
    _FEEDBACK_EMOJI_TYPE = "1"
    _FEEDBACK_WAIT_SEC = 0.7
    _TIME_SLICE = 60

    async def compete(self, event: AstrMessageEvent, ctx: ArbiterContext) -> bool:
        mid = ctx.message_id
        if await self._fetch_users(event, mid, self._EMOJI_ID, self._EMOJI_TYPE):
            return False
        try:
            await call_onebot_action(
                event,
                "set_msg_emoji_like",
                message_id=mid,
                emoji_id=self._EMOJI_ID,
                emoji_type=self._EMOJI_TYPE,
                set=True,
            )
        except Exception:
            # 协议端不支持该接口时不阻塞单 Bot 使用
            return True

        await asyncio.sleep(self._WAIT_SEC)
        users = await self._fetch_users(event, mid, self._EMOJI_ID, self._EMOJI_TYPE)
        if not users:
            return True
        order = self._decide_order(users, ctx.msg_time)
        if not order:
            return False
        if len(order) == 1:
            return order[0] == ctx.self_id

        for candidate in order:
            if candidate == ctx.self_id:
                try:
                    await call_onebot_action(
                        event,
                        "set_msg_emoji_like",
                        message_id=mid,
                        emoji_id=self._FEEDBACK_EMOJI_ID,
                        emoji_type=self._FEEDBACK_EMOJI_TYPE,
                        set=True,
                    )
                except Exception:
                    pass
            await asyncio.sleep(self._FEEDBACK_WAIT_SEC)
            winners = await self._fetch_users(
                event, mid, self._FEEDBACK_EMOJI_ID, self._FEEDBACK_EMOJI_TYPE
            )
            if winners:
                return ctx.self_id in winners and candidate == ctx.self_id
        return False

    async def _fetch_users(
        self,
        event: AstrMessageEvent,
        message_id: int,
        emoji_id: int,
        emoji_type: str,
    ) -> list[int]:
        try:
            resp = await call_onebot_action(
                event,
                "fetch_emoji_like",
                message_id=message_id,
                emojiId=str(emoji_id),
                emojiType=emoji_type,
            )
        except Exception:
            return []
        payload = unwrap_onebot_payload(resp) if isinstance(resp, dict) else {}
        emoji_list = payload.get("emojiLikesList") if isinstance(payload, dict) else None
        if not isinstance(emoji_list, list):
            return []
        users: list[int] = []
        for item in emoji_list:
            if isinstance(item, dict) and item.get("tinyId") is not None:
                try:
                    users.append(int(item["tinyId"]))
                except (TypeError, ValueError):
                    pass
        return users

    def _decide_order(self, users: list[int], msg_time: int) -> list[int]:
        participants = sorted(set(users))
        if not participants:
            return []
        slice_id = msg_time // self._TIME_SLICE
        start = slice_id % len(participants)
        return [
            participants[(start + i) % len(participants)]
            for i in range(len(participants))
        ]
