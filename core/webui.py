"""插件管理页面的数据接口。只操作现有 JSON，不进入 TOML 配置流程。"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

import json

from .managers import DataManager, KeyManager
from .protocols import IMAGE_PROTOCOLS, VIDEO_PROTOCOLS, normalize_protocol


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _key_id(key: Dict[str, Any], index: int) -> str:
    # 同一个密钥可能属于多个渠道，甚至有历史重复条目；每一条都必须单独呈现。
    # 数组发生增删后旧位置会失效，版本校验会要求重新打开，避免改错 Key。
    return _digest([key.get("value", ""), key.get("type", ""), index])


def _version(kind: str, value: Any) -> str:
    if kind == "keys":
        # 使用统计、自动禁用状态在绘图时会改变，不让这些字段阻断配置编辑。
        value = {field: value.get(field) for field in ("value", "type", "max_errors")}
    return _digest(value)


def _masked(value: str) -> str:
    return value[:4] + "••••" + value[-4:] if len(value) > 12 else "••••••••"


def _key_channel(key: Dict[str, Any]) -> str:
    return key.get("type") or ("bailili" if key["value"].startswith("sk-") else "google")


def _channel(info: Any) -> Dict[str, Any]:
    if isinstance(info, dict):
        return deepcopy(info)
    if isinstance(info, str):
        # 兼容旧的 URL:Key，同时避免把 URL 里的端口误当密钥。
        url, _, tail = info.rpartition(":")
        if tail.startswith("sk-") or (len(tail) >= 10 and "/" not in tail and tail not in {"generateContent", "streamGenerateContent"}):
            return {"url": url, "key": tail}
        return {"url": info}
    raise ValueError("渠道记录格式不正确，请检查 data.json。")


def _read(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path.name}，请检查文件权限与 JSON 格式；未保存任何修改。") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 的根节点必须是对象。")
    return value


class DrawerWebUI:
    def __init__(self, data: DataManager, keys: KeyManager):
        self.data = data
        self.keys = keys

    def _snapshot(self) -> tuple[Dict[str, Any], Dict[str, Any]]:
        data = _read(self.data.data_file, {"channels": {}, "prompts": {}})
        keys = _read(self.keys.keys_file, {"keys": [], "current_index": 0})
        if any(not isinstance(data.get(field, {}), dict) for field in ("channels", "prompts")):
            raise ValueError("data.json 的 channels 和 prompts 必须是对象。")
        if not isinstance(keys.get("keys", []), list) or any(
            not isinstance(key, dict) or not isinstance(key.get("value"), str)
            for key in keys.get("keys", [])
        ):
            raise ValueError("keys.json 的 keys 必须是 Key 对象数组。")
        return data, keys

    @staticmethod
    def _collection(kind: str, data: Dict[str, Any], keys: Dict[str, Any]) -> Dict[str, Any]:
        if kind == "keys":
            return {_key_id(key, index): key for index, key in enumerate(keys.get("keys", []))}
        if kind in {"channels", "prompts"}:
            return data.get(kind, {})
        raise ValueError("不支持的数据类型。")

    def list_records(self, kind: str, search: str = "", page: int = 1,
                     channel: str = "", unassigned: bool = False) -> Dict[str, Any]:
        if type(page) is not int or page < 1:
            raise ValueError("页码必须是正整数。")
        with self.data.lock, self.keys.lock:
            data, keys = self._snapshot()
            if kind == "banana":
                banana, entries = self._banana_entries()
                rows = []
                for name, entry in entries.items():
                    if search.casefold() not in (name + " " + json.dumps(entry, ensure_ascii=False)).casefold():
                        continue
                    rows.append({"id": name, "name": entry.get("source_title") or name,
                                 "section_title": entry.get("section_title") or "未分类",
                                 "preview": entry["content"][:160], "length": len(entry["content"]),
                                 "restricted": bool(entry.get("restricted", False))})
                result = self._page(rows, page, data, keys)
                result["banana"] = {"synced_at": str(banana.get("synced_at") or "尚未同步"),
                                    "remote_last_updated": str(banana.get("remote_last_updated") or "未知"),
                                    "readonly": True}
                return result
            key_rows = [
                {"id": record_id, "version": _version("keys", raw), "name": _masked(raw["value"]),
                 "channel": _key_channel(raw), "status": raw.get("status", "active"),
                 "error_count": raw.get("error_count", 0), "max_errors": raw.get("max_errors", 5),
                 "last_used": raw.get("last_used")}
                for record_id, raw in self._collection("keys", data, keys).items()
            ]
            grouped_keys: Dict[str, list] = {}
            for row in key_rows:
                grouped_keys.setdefault(row["channel"], []).append(row)
            if kind == "keys":
                rows = [row for row in key_rows
                        if (not channel or row["channel"] == channel)
                        and (not unassigned or row["channel"] not in data.get("channels", {}))
                        and search.casefold() in json.dumps(row, ensure_ascii=False).casefold()]
                return self._page(rows, page, data, keys)
            rows = []
            for name, raw in self._collection(kind, data, keys).items():
                row = {"id": name, "version": _version(kind, raw)}
                if kind == "channels":
                    info = _channel(raw)
                    row.update(name=name, model=info.get("model", ""), enabled=info.get("enabled", True),
                               is_video=info.get("is_video", False), protocol=info.get("protocol", "auto"))
                    url = str(info.get("url", ""))
                    parsed = urlsplit(url)
                    row["url"] = f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}{parsed.path}" if parsed.scheme else ""
                    channel_keys = grouped_keys.get(name, [])
                    row.update(keys=channel_keys[:5], key_count=len(channel_keys),
                               key_pages=max(1, (len(channel_keys) + 19) // 20),
                               has_inline_key=bool(info.get("key")))
                elif kind == "prompts":
                    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                    row.update(name=name, preview=text[:160], length=len(text))
                # 搜索提示词全文；Key 搜索只匹配列表可见信息，不传回明文。
                haystack = (name + " " + str(raw) if kind == "prompts" else
                            json.dumps({**row, "keys": grouped_keys.get(name, [])}, ensure_ascii=False))
                if search.casefold() in haystack.casefold():
                    rows.append(row)
            result = self._page(rows, page, data, keys)
            if kind == "channels":
                orphan_keys = [row for row in key_rows if row["channel"] not in data.get("channels", {})
                               and search.casefold() in json.dumps(row, ensure_ascii=False).casefold()]
                result["unassigned"] = self._page(orphan_keys, 1, data, keys)
            return result

    def _page(self, rows: list, page: int, data: Dict[str, Any], keys: Dict[str, Any]) -> Dict[str, Any]:
        total = len(rows)
        page = min(page, max(1, (total + 19) // 20))
        return {"rows": rows[(page - 1) * 20:page * 20], "total": total, "page": page,
                    "pages": max(1, (total + 19) // 20),
                    "counts": {"channels": len(data.get("channels", {})), "prompts": len(data.get("prompts", {})),
                               "keys": len(keys.get("keys", [])),
                               "banana": len(self.data.get_banana_prompt_entries(show_restricted=True))}}

    def _banana_entries(self) -> tuple[Dict[str, Any], Dict[str, Any]]:
        banana = _read(self.data.banana_file, {"schema_version": 1, "prompts": {}})
        raw_entries = banana.get("prompts", {})
        if not isinstance(raw_entries, dict):
            raise ValueError("banana_prompts.json 的 prompts 必须是对象。")
        entries = {}
        for name, raw in raw_entries.items():
            entry = {"content": raw} if isinstance(raw, str) else raw
            if isinstance(entry, dict) and isinstance(entry.get("content"), str) and entry["content"].strip():
                entries[name] = entry
        return banana, entries

    def detail(self, kind: str, record_id: str) -> Dict[str, Any]:
        with self.data.lock, self.keys.lock:
            if kind == "banana":
                _, entries = self._banana_entries()
                if record_id not in entries:
                    raise ValueError("词条已更新或不存在，请刷新词库列表。")
                entry = entries[record_id]
                result = {"id": record_id, "readonly": True,
                          "payload": {"text": entry["content"], "source_title": entry.get("source_title") or record_id,
                                      "section_title": entry.get("section_title") or "未分类",
                                      "prompt_id": str(entry.get("prompt_id") or ""),
                                      "restricted": bool(entry.get("restricted", False))}}
                if len(json.dumps(result, ensure_ascii=False).encode()) > 400000:
                    raise ValueError("该词条超过页面查看大小限制，请直接查看 banana_prompts.json。")
                return result
            data, keys = self._snapshot()
            collection = self._collection(kind, data, keys)
            if record_id not in collection:
                raise ValueError("记录已删除，请刷新列表。")
            raw = collection[record_id]
            if kind == "channels":
                payload = _channel(raw)
                has_key = bool(payload.pop("key", ""))
                payload["key"] = ""
            elif kind == "keys":
                payload = {"type": _key_channel(raw), "status": raw.get("status", "active"),
                           "max_errors": raw.get("max_errors", 5), "value": ""}
                has_key = True
            else:
                if not isinstance(raw, str):
                    raise ValueError("该提示词不是文本，请先检查 data.json 中的记录格式。")
                payload = {"text": raw}
                has_key = False
            result = {"id": record_id, "version": _version(kind, raw), "payload": payload,
                      "has_key": has_key}
            if len(json.dumps(result, ensure_ascii=False).encode()) > 50000:
                raise ValueError("该记录超过页面单次编辑大小限制，请直接编辑 JSON 文件。")
            return result

    def change(self, kind: str, operation: str, record_id: str, version: str,
               payload: Dict[str, Any]) -> Dict[str, Any]:
        if kind == "banana":
            raise ValueError("大香蕉词库只读，不能新增、修改或删除词条；请使用更新词库按钮同步。")
        if operation not in {"save", "delete", "reset"}:
            raise ValueError("不支持的操作。")
        if not isinstance(payload, dict):
            raise ValueError("记录内容必须是对象。")
        with self.data.lock, self.keys.lock:
            data, keys = self._snapshot()
            collection = self._collection(kind, data, keys)
            old = collection.get(record_id)
            exists = record_id in collection
            if version:
                if not exists or _version(kind, old) != version:
                    raise ValueError("记录已被其他操作修改或删除，请重新打开记录后再保存；你的输入仍保留在页面。")
            elif operation != "save" or exists:
                raise ValueError("该记录已存在，请刷新列表后编辑。")

            if operation == "delete":
                del collection[record_id]
            elif operation == "reset":
                if kind != "keys":
                    raise ValueError("只有 Key 支持重置。")
                collection[record_id] = {**old, "status": "active", "error_count": 0}
            elif kind == "prompts":
                if not exists:
                    self._name(record_id)
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("请输入提示词内容。")
                collection[record_id] = text
            elif kind == "channels":
                if not exists:
                    self._name(record_id)
                collection[record_id] = self._validate_channel(old, payload)
            else:
                new_key = self._validate_key(old, payload, data)
                identity_changed = old is None or (old.get("value"), old.get("type")) != (new_key["value"], new_key["type"])
                if identity_changed and any(
                    (key["value"], key.get("type")) == (new_key["value"], new_key["type"])
                    for key_id, key in collection.items() if key_id != record_id
                ):
                    raise ValueError("该 Key 已存在，请编辑现有记录。")
                # 编辑 Key 时保留原数组顺序，不改变轮换优先级。
                ordered = []
                saved_index = len(keys.get("keys", []))
                for index, key in enumerate(keys.get("keys", [])):
                    if exists and _key_id(key, index) == record_id:
                        ordered.append(new_key)
                        saved_index = index
                    else:
                        ordered.append(key)
                if not exists:
                    ordered.append(new_key)
                keys["keys"] = ordered
                record_id = _key_id(new_key, saved_index)

            if kind == "keys":
                if operation != "save":
                    keys["keys"] = list(collection.values())
                self.keys.config = keys
                self.keys.save_config(keys)
            else:
                data[kind] = collection
                self.data.data = data
                self.data.save_data()
            return {"id": record_id, "message": "已删除" if operation == "delete" else "已重置" if operation == "reset" else "已保存"}

    @staticmethod
    def _name(name: str) -> None:
        if not name.strip() or name != name.strip() or len(name) > 120 or any(char.isspace() for char in name):
            raise ValueError("名称需要 1–120 个字符，不能包含空白字符。")

    @staticmethod
    def _validate_channel(old: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        info = _channel(old) if old is not None else {}
        patch = deepcopy(payload)
        clear_key = patch.pop("clear_key", False)
        key = patch.pop("key", "")
        if type(clear_key) is not bool or not isinstance(key, str):
            raise ValueError("内联 Key 必须是文本，清除 Key 必须是开关值。")
        info.update(patch)
        if clear_key:
            info.pop("key", None)
        elif key:
            info["key"] = key.strip()
        url = info.get("url", "")
        if not isinstance(url, str):
            raise ValueError("API 地址必须是文本。")
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("请输入完整的 http:// 或 https:// API 地址。")
        info["url"] = url.strip()
        for field in ("enabled", "is_video", "stream"):
            if field in info and type(info[field]) is not bool:
                raise ValueError(f"{field} 必须是开关值。")
        for field in ("model", "protocol", "response_format"):
            if field in info and not isinstance(info[field], str):
                raise ValueError(f"{field} 必须是文本。")
            if field in info and not info[field].strip():
                info.pop(field)
        if "protocol" in info:
            protocol = normalize_protocol(info["protocol"])
            choices = VIDEO_PROTOCOLS if info.get("is_video", False) else IMAGE_PROTOCOLS
            if protocol not in choices:
                raise ValueError("所选协议与图片/视频渠道类型不匹配。")
            info["protocol"] = protocol
        if info.get("response_format") not in {None, "url", "b64_json"}:
            raise ValueError("返回格式只支持 url 或 b64_json。")
        return info

    @staticmethod
    def _validate_key(old: Any, payload: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if set(payload) - {"value", "type", "status", "max_errors"}:
            raise ValueError("Key 使用统计只能通过重置操作修改。")
        info = deepcopy(old) if old is not None else {"status": "active", "error_count": 0, "last_used": None, "max_errors": 5}
        patch = deepcopy(payload)
        if not patch.get("value"):
            patch.pop("value", None)
        info.update(patch)
        if not isinstance(info.get("value"), str) or not info["value"].strip():
            raise ValueError("请输入 Key。")
        info["value"] = info["value"].strip()
        channel = info.get("type")
        if not isinstance(channel, str) or not channel.strip():
            raise ValueError("请输入 Key 所属渠道。")
        # 已删除渠道的存量 Key 允许编辑或删除；新归属必须是现有渠道。
        if channel not in data.get("channels", {}) and (old is None or channel != _key_channel(old)):
            raise ValueError("所属渠道不存在，请先添加渠道。")
        if info.get("status") not in {"active", "disabled"}:
            raise ValueError("Key 状态只能是 active 或 disabled。")
        limit = info.get("max_errors", 5)
        if type(limit) is not int or (limit != -1 and limit < 1):
            raise ValueError("错误上限应为正整数；-1 表示永不自动禁用。")
        return info
