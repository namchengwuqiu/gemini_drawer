"""宿主数据目录绑定和旧配置迁移；失败时由宿主保留原配置。"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping

import json

from .managers import DataManager, KeyManager
from ..utils.config_file import _atomic_write_text


_DATA_FILES = (
    "keys.json", "data.json", "banana_prompts.json",
    "video_retry_guard.json", "autonomous_quota.json",
)
_LEGACY_API_FIELDS = (
    "enable_google", "api_url", "enable_lmarena", "lmarena_api_url",
    "lmarena_api_key", "lmarena_model_name",
)


def initialize_storage(data_dir: Path, plugin_dir: Path, keys: KeyManager, data: DataManager) -> None:
    """只导入当前 Bot 的旧公共目录；已有目标文件不覆盖，原文件保留。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    # ctx.paths.data_dir = <当前 Bot>/data/plugins/<plugin_id>。
    # 旧版目录也必须取自当前 Bot，不能按共享插件源码推算其他实例的数据。
    legacy_dir = data_dir.parent.parent / "gemini_drawer"
    source_root = plugin_dir.parent.parent.resolve()
    owns_source_data = data_dir.parent.parent.resolve() in (source_root, source_root / "data")
    marker = data_dir / ".legacy-storage-migrated.json"
    for name in _DATA_FILES:
        target = data_dir / name
        if marker.exists() or target.exists():
            continue
        sources = [legacy_dir / name]
        # 源码目录可能被多个 Bot 共用；源码内遗留文件只属于原始部署实例。
        if owns_source_data:
            sources.append(source_root / "gemini_drawer" / name)
            sources.append(plugin_dir / "data" / name)
            if name == "keys.json":
                sources.append(plugin_dir / name)
            elif name == "data.json":
                sources.extend(plugin_dir / filename for filename in (name, "data.js", "data.json.bak"))
        for source in sources:
            if not source.is_file() or source.resolve() == target.resolve():
                continue
            text = source.read_text(encoding="utf-8")
            try:
                if not isinstance(json.loads(text), dict):
                    raise ValueError
            except ValueError:
                raise ValueError(f"旧数据 {source.name} 无效，已停止迁移，请修复或从备份恢复") from None
            _atomic_write_text(target, text)
            break
    # 原地重新绑定，命令、页面等模块导入的管理器引用仍然有效。
    keys.__init__(data_dir / "keys.json")
    data.__init__(data_dir / "data.json")
    keys.plugin_dir = data.plugin_dir = plugin_dir
    if keys._load_error is not None or data._load_error is not None:
        raise ValueError("渠道或 Key 数据无效，已停止配置升级，请修复或从备份恢复")
    if not marker.exists():
        _atomic_write_text(marker, '{"version": 1}\n')


def _channel(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if isinstance(value, str):
        # 历史格式为 URL:Key；保留普通 URL（包括带端口的 URL）。
        url, separator, tail = value.rpartition(":")
        is_endpoint = tail.split("?", 1)[0] in {"generateContent", "streamGenerateContent"}
        if separator and not is_endpoint and "/" not in tail and (tail.startswith(("sk-", "AIza")) or len(tail) >= 10):
            return {"url": url, "key": tail, "enabled": True}
        return {"url": value, "enabled": True}
    raise ValueError("旧渠道格式无效，已停止配置升级")


def migrate_legacy_config(
    raw: Mapping[str, Any], keys: KeyManager, data: DataManager, config_path: Path,
) -> bool:
    """在宿主裁剪前保存旧渠道、Key、提示词；不直接重写 TOML。

    新目录已存在的同名渠道/提示词和 Key 状态优先。部分写入失败可重复执行，
    所有异常向宿主传播，防止尚未持久化的字段被清理。
    """
    api = raw.get("api", {})
    if not isinstance(api, Mapping):
        raise ValueError("旧 API 配置格式无效")
    present = "channels" in raw or "prompts" in raw or any(name in api for name in _LEGACY_API_FIELDS)
    if not present:
        return False
    if keys.data_dir is None or data.data_dir is None:
        raise ValueError("迁移旧配置前必须由宿主分配插件数据目录")
    old_channels = raw.get("channels", {})
    old_prompts = raw.get("prompts", {})
    if not isinstance(old_channels, Mapping) or not isinstance(old_prompts, Mapping):
        raise ValueError("旧渠道或提示词格式无效，已停止配置升级")

    channels = {name: _channel(value) for name, value in old_channels.items()}
    google_url = str(api.get("api_url") or "").strip()
    if google_url:
        channels.setdefault("google", {"url": google_url, "enabled": api.get("enable_google", True), "stream": False})
    lmarena_url = str(api.get("lmarena_api_url") or "").strip()
    if lmarena_url and lmarena_url != "http://xxx:666/v1/chat/completions":
        channel = {"url": lmarena_url, "enabled": api.get("enable_lmarena", False), "stream": True}
        if api.get("lmarena_model_name"):
            channel["model"] = api["lmarena_model_name"]
        if api.get("lmarena_api_key"):
            channel["key"] = api["lmarena_api_key"]
        channels.setdefault("lmarena", channel)

    # 先构造完整结果，再进行任何持久化；已有目标设置优先，包括显式关闭状态。
    with keys.lock, data.lock:
        current_keys = deepcopy(keys.get_all_keys())
        previous_data = deepcopy(data._load_data())
        current_data = deepcopy(previous_data)
        if keys._load_error is not None or data._load_error is not None:
            raise ValueError("渠道或 Key 数据无效，已停止配置升级")
        known_keys = {entry["value"] for entry in current_keys}
        for name, channel in channels.items():
            if name in current_data["channels"]:
                continue
            key = channel.pop("key", None)
            if key is not None and not isinstance(key, str):
                raise ValueError("旧渠道 Key 格式无效，已停止配置升级")
            if key and key not in known_keys:
                current_keys.append({"value": key, "type": name, "status": "active", "error_count": 0,
                                     "max_errors": 5, "last_used": None})
                known_keys.add(key)
            current_data["channels"][name] = channel
        for name, prompt in old_prompts.items():
            current_data["prompts"].setdefault(name, deepcopy(prompt))

        # 备份原始字节；仅首次迁移创建，重试不覆盖。备份包含凭据，限制文件权限。
        if config_path.is_file():
            backup = config_path.with_name("config.toml.pre-migration.bak")
            if not backup.exists():
                backup.touch(mode=0o600, exist_ok=False)
                try:
                    backup.write_bytes(config_path.read_bytes())
                except BaseException:
                    backup.unlink()
                    raise
        key_config = {**keys.config, "keys": current_keys}
        if key_config != keys.config:
            keys.save_config(key_config)
            keys.config = key_config
        if current_data != previous_data:
            data.data = current_data
            try:
                data.save_data()
            except BaseException:
                data.data = previous_data
                raise
    return True
