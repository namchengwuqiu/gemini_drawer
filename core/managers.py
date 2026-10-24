"""
Gemini Drawer 数据管理模块

本模块提供插件的数据持久化和管理功能，包含两个核心管理器：

KeyManager (API Key 管理器):
    管理各渠道的 API Key，提供：
    - add_keys(): 为指定渠道添加新的 API Key
    - get_all_keys(): 获取所有渠道的 Key 列表及状态
    - record_key_usage(): 记录 Key 使用情况（成功/失败计数）
    - manual_reset_keys(): 手动重置 Key 的错误计数和禁用状态
    - reset_specific_key(): 重置指定渠道的特定 Key
    - _migrate_legacy_data(): 从旧版配置迁移数据

DataManager (配置数据管理器):
    管理提示词预设和渠道配置，提供：
    - get_prompts() / add_prompt() / delete_prompt(): 提示词 CRUD
    - get_channels() / add_channel() / delete_channel() / update_channel(): 渠道 CRUD
    - _migrate_from_toml(): 从 TOML 配置迁移到 JSON

数据存储：
    所有数据存储在 data/ 目录下的 JSON 文件中
    - keys.json: API Key 配置和使用统计
    - config.json: 提示词预设和渠道配置

模块级实例：
    - key_manager: KeyManager 的单例实例
    - data_manager: DataManager 的单例实例
"""
import json
from collections import Counter
from datetime import datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import List, Tuple, Optional, Dict, Any
from .host_bridge import get_plugin_logger
from ..utils import save_config_file

logger = get_plugin_logger("plugin.gemini_drawer.managers")


def _serialized(refresh: bool = False):
    """让页面线程和绘图/聊天指令共享锁；修改前读取最新文件。"""
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            with self.lock:
                if refresh:
                    if isinstance(self, KeyManager):
                        self.config = self._load_config()
                    else:
                        self.data = self._load_data()
                return method(self, *args, **kwargs)
        return wrapped
    return decorate


def _get_external_data_dir() -> Path:
    """获取插件外部数据目录，将数据存储在插件目录之外，
    避免 WebUI 更新插件时因删除整个插件目录导致数据丢失。
    
    支持两种部署方式:
    
    Docker 部署 (docker-compose.yml):
        /MaiMBot/              (WORKDIR)
        ├── plugins/gemini_drawer/  <- 挂载自 data/MaiMBot/plugins/
        └── data/                   <- 挂载自 data/MaiMBot/
            └── gemini_drawer/      <- 外部数据目录
    
    直接部署:
        data/MaiMBot/
        ├── plugins/gemini_drawer/  <- 插件代码
        └── gemini_drawer/          <- 外部数据目录
    """
    plugin_dir = Path(__file__).parent.parent  # .../plugins/gemini_drawer
    plugins_parent = plugin_dir.parent.parent  # Docker: /MaiMBot/  Host: data/MaiMBot/
    
    # Docker 环境: plugins/ 和 data/ 是同级目录，都在 /MaiMBot/ 下
    # 检测方式: 如果 parent 下有 data/ 子目录且它不是当前插件的 data/
    data_subdir = plugins_parent / "data"
    if data_subdir.is_dir() and data_subdir != plugin_dir / "data":
        external_dir = data_subdir / "gemini_drawer"
    else:
        # 直接部署: plugins/ 在 data/MaiMBot/ 下
        external_dir = plugins_parent / "gemini_drawer"
    
    external_dir.mkdir(parents=True, exist_ok=True)
    return external_dir


def _migrate_internal_file(internal_file: Path, external_file: Path) -> bool:
    """将插件内部数据文件迁移到外部目录
    
    如果外部文件不存在且内部文件存在，则复制数据。
    迁移完成后保留内部文件作为备份（不删除）。
    
    Returns:
        True 如果执行了迁移
    """
    if external_file.exists() or not internal_file.exists():
        return False
    
    try:
        import shutil
        shutil.copy2(internal_file, external_file)
        logger.info(f"已迁移数据文件: {internal_file.name} -> {external_file}")
        return True
    except Exception as e:
        logger.error(f"迁移数据文件失败 {internal_file.name}: {e}")
        return False


def _file_stamp(path: Path) -> Optional[Tuple[float, int]]:
    """返回 (mtime, size) 作为文件版本指纹；文件不存在返回 None。"""
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


class ConfigFileCorruptedError(RuntimeError):
    """数据文件损坏，拒绝在未恢复的状态下覆盖写入。"""


def _backup_corrupt_file(path: Path) -> Optional[Path]:
    """把损坏文件复制一份带时间戳的备份；原文件保留，便于人工恢复。"""
    try:
        import shutil
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}")
        if not backup.exists():
            shutil.copy2(path, backup)
            logger.warning(f"已备份损坏的数据文件: {path.name} -> {backup.name}")
        return backup
    except Exception as e:
        logger.error(f"备份损坏文件 {path.name} 失败: {e}")
        return None


def _normalize_keys_config(raw: Any) -> Dict[str, Any]:
    """校验并补全 keys.json 结构；结构非法时抛 ValueError。

    缺字段的合法旧格式在这里补默认值（明确的迁移规则）：
    status / error_count / max_errors / last_used 缺失时补默认，未知字段原样保留。
    """
    if not isinstance(raw, dict):
        raise ValueError("顶层必须是 JSON 对象")
    keys = raw.get("keys", [])
    if not isinstance(keys, list):
        raise ValueError("keys 字段必须是数组")
    normalized: List[Dict[str, Any]] = []
    for index, entry in enumerate(keys):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {index + 1} 个 Key 记录不是对象")
        value = entry.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"第 {index + 1} 个 Key 记录缺少有效的 value")
        entry.setdefault("status", "active")
        entry.setdefault("error_count", 0)
        entry.setdefault("max_errors", 5)
        entry.setdefault("last_used", None)
        normalized.append(entry)
    raw["keys"] = normalized
    raw.setdefault("current_index", 0)
    return raw


def _normalize_data_config(raw: Any) -> Dict[str, Any]:
    """校验 data.json 顶层结构；结构非法时抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("顶层必须是 JSON 对象")
    for field in ("prompts", "channels"):
        value = raw.get(field, {})
        if not isinstance(value, dict):
            raise ValueError(f"{field} 字段必须是对象")
        raw[field] = value
    return raw


class KeyManager:
    def __init__(self, keys_file_path: Path = None):
        self.lock = RLock()
        if keys_file_path is None:
            self.plugin_dir = Path(__file__).parent.parent
            self.data_dir = _get_external_data_dir()
            self.keys_file = self.data_dir / "keys.json"
            # 从插件内部旧路径迁移数据
            internal_keys = self.plugin_dir / "data" / "keys.json"
            _migrate_internal_file(internal_keys, self.keys_file)
        else:
            self.keys_file = keys_file_path
            self.plugin_dir = self.keys_file.parent.parent

        #: 最近一次读取失败的原因；非 None 时拒绝覆盖写入，直到文件恢复
        self._load_error: Optional[str] = None
        self.config = self._load_config()
        self._migrated = False

    def ensure_migrated(self) -> None:
        """执行一次性的历史数据迁移。

        迁移会写文件，因此不放在 __init__ 里——否则仅仅 import 本模块就会
        产生副作用，单元测试也无法安全导入。由 plugin.on_load() 调用。
        """
        if self._migrated:
            return
        self._migrated = True
        self._migrate_legacy_data()

    def _migrate_legacy_data(self):
        migrated = False
        old_keys_file = self.plugin_dir / "keys.json"
        if old_keys_file.exists() and old_keys_file != self.keys_file:
            try:
                with open(old_keys_file, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                    old_keys = old_data.get('keys', []) if isinstance(old_data, dict) else []
                    if not isinstance(old_keys, list):
                        old_keys = []
                    if old_keys:
                        current_keys = {k['value'] for k in self.config.get('keys', [])}
                        for k in old_keys:
                            if not isinstance(k, dict) or not isinstance(k.get('value'), str) or not k['value']:
                                continue
                            if k['value'] not in current_keys:
                                if 'type' not in k:
                                    k['type'] = 'bailili' if k['value'].startswith('sk-') else 'google'
                                self.config['keys'].append(k)
                                migrated = True
                old_keys_file.rename(old_keys_file.with_suffix('.json.bak'))
                logger.info("已迁移旧的 keys.json 数据")
            except Exception as e:
                logger.error(f"迁移旧 keys.json 失败: {e}")

        config_path = self.plugin_dir / "config.toml"
        if config_path.exists():
            try:
                import toml
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = toml.load(f)
                
                channels = config_data.get("channels", {})
                config_changed = False
                
                for name, info in channels.items():
                    key_to_migrate = None
                    if isinstance(info, str):
                        if ":" in info:
                            possible_url, possible_key = info.rsplit(":", 1)
                            is_key = True
                            if '/' in possible_key: is_key = False
                            elif possible_key in ['generateContent', 'streamGenerateContent']: is_key = False
                            elif len(possible_key) < 10 and not possible_key.startswith('sk-'):
                                if possible_url.lower() in ['http', 'https']: is_key = False
                            
                            if is_key:
                                url = possible_url
                                key = possible_key
                                key_to_migrate = key
                                channels[name] = {"url": url, "enabled": True}
                                config_changed = True
                            else:
                                channels[name] = {"url": info, "enabled": True}
                                config_changed = True
                                
                    elif isinstance(info, dict):
                        if "key" in info:
                            key_to_migrate = info.pop("key")
                            config_changed = True
                    
                    if key_to_migrate:
                        self.add_keys([key_to_migrate], name)
                        migrated = True
                        logger.info(f"已迁移渠道 {name} 的 Key")

                if config_changed:
                    save_config_file(config_path, config_data)
                    logger.info("已从 config.toml 移除 Key")

            except Exception as e:
                logger.error(f"迁移 config.toml 数据失败: {e}")

        if migrated:
            self.save_config(self.config)

    def _load_config(self) -> Dict[str, Any]:
        if not self.keys_file.exists():
            # 不在这里落盘：首次写 Key 时 save_config 会创建文件
            self._load_error = None
            return {"keys": [], "current_index": 0}
        try:
            with open(self.keys_file, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            config = _normalize_keys_config(raw)
        except (IOError, json.JSONDecodeError, ValueError) as e:
            # 读取失败时保留并备份原文件，阻止在未恢复状态下覆盖；
            # 不能用空默认值掩盖损坏（P1-3）。
            logger.error(f"读取密钥配置失败: {e}")
            self._load_error = str(e)
            _backup_corrupt_file(self.keys_file)
            return {"keys": [], "current_index": 0}
        self._load_error = None
        return config

    @_serialized()
    def save_config(self, config_data: Dict[str, Any]):
        """原子写入：并发绘图时多个 record_key_usage 同时落盘不会写出半截文件。

        Raises:
            ConfigFileCorruptedError: keys.json 损坏且未恢复时拒绝覆盖写入。
        """
        if self._load_error is not None:
            # 文件可能已被人工修复；重新读取一次以自动解除保护
            self._load_config()
        if self._load_error is not None:
            raise ConfigFileCorruptedError(
                f"keys.json 当前无法解析（{self._load_error}），已拒绝覆盖写入；"
                "请先修复或恢复该文件后重试。"
            )
        try:
            self.keys_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.keys_file.with_suffix(self.keys_file.suffix + ".tmp")
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
            tmp_file.replace(self.keys_file)
        except IOError as e:
            logger.error(f"保存密钥配置失败: {e}")
            raise

    @_serialized(refresh=True)
    def add_keys(self, new_keys: List[str], key_type: str) -> Tuple[int, int]:
        existing_keys = {key['value'] for key in self.config.get('keys', [])}
        added_count = 0
        duplicate_count = 0
        for key_value in new_keys:
            if key_value in existing_keys:
                duplicate_count += 1
            else:
                key_obj = {"value": key_value, "type": key_type, "status": "active", "error_count": 0, "last_used": None, "max_errors": 5}
                self.config['keys'].append(key_obj)
                existing_keys.add(key_value)
                added_count += 1
        self.save_config(self.config)
        return added_count, duplicate_count

    @_serialized()
    def get_all_keys(self) -> List[Dict[str, Any]]:
        # 每次调用时从文件重新加载，支持实时更新
        self.config = self._load_config()
        return self.config.get('keys', [])

    @_serialized(refresh=True)
    def record_key_usage(self, key_value: str, success: bool, force_disable: bool = False):
        keys = self.config.get('keys', [])
        for key_obj in keys:
            if key_obj['value'] == key_value:
                if success:
                    key_obj['error_count'] = 0
                else:
                    key_obj['error_count'] = key_obj.get('error_count', 0) + 1
                    max_errors = key_obj.get('max_errors', 5)
                    if max_errors != -1 and (force_disable or key_obj['error_count'] >= max_errors):
                        if key_obj['status'] == 'active':
                            key_obj['status'] = 'disabled'
                            reason = "配额耗尽" if force_disable else "错误次数过多"
                            logger.warning(f"API Key {key_value[:8]}... 已因“{reason}”被自动禁用。")
                # 记账同时更新最后使用时间（此前该字段从不更新，页面展示会误导）
                key_obj['last_used'] = datetime.now().isoformat(timespec="seconds")
                self.save_config(self.config)
                return

    @_serialized(refresh=True)
    def manual_reset_keys(self, key_type: Optional[str] = None) -> int:
        keys = self.config.get('keys', [])
        reset_count = 0
        for key_obj in keys:
            if key_type and key_obj.get('type') != key_type:
                continue
            if key_obj.get('status') == 'disabled':
                key_obj['status'] = 'active'
                key_obj['error_count'] = 0
                reset_count += 1
        if reset_count > 0:
            self.save_config(self.config)
        return reset_count

    @_serialized(refresh=True)
    def reset_specific_key(self, key_type: str, index: int) -> bool:
        keys = self.config.get('keys', [])
        target_keys = []
        for i, key_obj in enumerate(keys):
            if key_obj.get('type') == key_type:
                target_keys.append((i, key_obj))
        
        if index < 1 or index > len(target_keys):
            return False
        real_index, key_obj = target_keys[index - 1]
        key_obj['status'] = 'active'
        key_obj['error_count'] = 0
        self.save_config(self.config)
        return True

    @_serialized(refresh=True)
    def set_key_error_limit(self, key_type: str, index: int, limit: int) -> bool:
        keys = [key for key in self.config.get('keys', []) if key.get('type') == key_type]
        if index < 1 or index > len(keys):
            return False
        keys[index - 1]['max_errors'] = limit
        self.save_config(self.config)
        return True

    @_serialized(refresh=True)
    def delete_key(self, key_type: str, index: int) -> bool:
        """删除指定渠道的特定 Key"""
        keys = self.config.get('keys', [])
        target_keys = []
        for i, key_obj in enumerate(keys):
            if key_obj.get('type') == key_type:
                target_keys.append((i, key_obj))
        
        if index < 1 or index > len(target_keys):
            return False
        
        real_index, key_obj = target_keys[index - 1]
        del self.config['keys'][real_index]
        self.save_config(self.config)
        logger.info(f"已删除渠道 {key_type} 的第 {index} 个 Key: {key_obj['value'][:8]}...")
        return True

    @_serialized(refresh=True)
    def delete_keys_by_type(self, key_type: str) -> int:
        """删除指定渠道的所有 Key，返回删除数量"""
        keys = self.config.get('keys', [])
        original_count = len(keys)
        self.config['keys'] = [k for k in keys if k.get('type') != key_type]
        deleted_count = original_count - len(self.config['keys'])
        if deleted_count > 0:
            self.save_config(self.config)
            logger.info(f"已删除渠道 {key_type} 的所有 Key，共 {deleted_count} 个")
        return deleted_count

class DataManager:
    def __init__(self, data_file_path: Path = None):
        self.lock = RLock()
        # 文件版本指纹缓存：避免每次取渠道/提示词都重新解析大 JSON
        # （data.json 约 700KB，banana_prompts.json 约 2.6MB）
        self._data_cache: Optional[Dict[str, Any]] = None
        self._data_stamp: Optional[Tuple[float, int]] = None
        self._banana_cache: Optional[Dict[str, Any]] = None
        self._banana_stamp: Optional[Tuple[float, int]] = None
        #: 大香蕉词条计数的缓存（按文件指纹与过滤开关失效，P2-13）
        self._banana_count_cache: Optional[int] = None
        self._banana_count_stamp: Optional[Tuple[float, int]] = None
        self._banana_count_flag: Optional[bool] = None

        if data_file_path is None:
            self.plugin_dir = Path(__file__).parent.parent
            self.data_dir = _get_external_data_dir()
            self.data_file = self.data_dir / "data.json"
            self.banana_file = self.data_dir / "banana_prompts.json"
            # 从插件内部旧路径迁移数据
            internal_data = self.plugin_dir / "data" / "data.json"
            _migrate_internal_file(internal_data, self.data_file)
        else:
            self.data_file = data_file_path
            self.banana_file = self.data_file.parent / "banana_prompts.json"
            self.plugin_dir = self.data_file.parent.parent
            
        #: 最近一次读取失败的原因；非 None 时拒绝覆盖写入，直到文件恢复
        self._load_error: Optional[str] = None
        self.data = self._load_data()
        self._migrated = False

    def ensure_migrated(self) -> None:
        """执行一次性的历史数据迁移（写文件，故不放在 __init__）。"""
        if self._migrated:
            return
        self._migrated = True
        self._migrate_from_root()
        self._migrate_from_toml()

    def _migrate_from_root(self):
        """迁移根目录下的 data.json/data.js 到 data/data.json"""
        migrated = False
        
        # 检查可能的旧文件名称
        possible_files = ["data.json", "data.js", "data.json.bak"]
        
        for filename in possible_files:
            root_file = self.plugin_dir / filename
            if not root_file.exists():
                continue
                
            if root_file.resolve() == self.data_file.resolve():
                continue
                
            try:
                # 尝试读取旧文件
                content = {}
                with open(root_file, 'r', encoding='utf-8') as f:
                    content = json.load(f)
                
                if not content:
                    continue

                # 合并数据
                changed = False
                
                # 迁移 Prompts
                if "prompts" in content:
                    for name, prompt in content["prompts"].items():
                        if name not in self.data.get("prompts", {}):
                            self.add_prompt(name, prompt)
                            changed = True
                            
                # 迁移 Channels
                if "channels" in content:
                    for name, info in content["channels"].items():
                        if name not in self.data.get("channels", {}):
                            self.add_channel(name, info)
                            changed = True
                            
                if changed:
                    migrated = True
                    logger.info(f"已从 {filename} 迁移数据")
                
                # 备份旧文件
                backup_path = root_file.with_suffix(root_file.suffix + ".migrated")
                root_file.rename(backup_path)
                logger.info(f"已将 {filename} 重命名为 {backup_path.name}")
                
            except Exception as e:
                logger.error(f"迁移 {filename} 数据失败: {e}")

        if migrated:
            self.save_data()

    def _load_data(self) -> Dict[str, Any]:
        if not self.data_file.exists():
            return {"prompts": {}, "channels": {}}

        # 文件未变更则直接复用上次解析结果（返回同一对象，与 self.data 保持同一引用）
        stamp = _file_stamp(self.data_file)
        if stamp is not None and stamp == self._data_stamp and self._data_cache is not None:
            return self._data_cache

        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                data = _normalize_data_config(json.load(f))
            self._data_cache = data
            self._data_stamp = stamp
            self._load_error = None
            return data
        except Exception as e:
            # 读取失败时保留并备份原文件，阻止在未恢复状态下覆盖（P1-3 同类保护）
            logger.error(f"Failed to load data.json: {e}")
            self._load_error = str(e)
            _backup_corrupt_file(self.data_file)
            return {"prompts": {}, "channels": {}}

    @_serialized()
    def save_data(self):
        """原子写入，避免并发或中断时留下半截 data.json。

        Raises:
            ConfigFileCorruptedError: data.json 损坏且未恢复时拒绝覆盖写入。
        """
        if self._load_error is not None:
            # 文件可能已被人工修复；重新校验一次以自动解除保护（不改写 self.data）
            self._load_data()
        if self._load_error is not None:
            raise ConfigFileCorruptedError(
                f"data.json 当前无法解析（{self._load_error}），已拒绝覆盖写入；"
                "请先修复或恢复该文件后重试。"
            )
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.data_file.with_suffix(self.data_file.suffix + ".tmp")
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            tmp_file.replace(self.data_file)
            # 写入后刷新指纹，避免下次读取误判为外部改动而重复解析
            self._data_cache = self.data
            self._data_stamp = _file_stamp(self.data_file)
        except Exception as e:
            logger.error(f"Failed to save data.json: {e}")
            raise

    @_serialized()
    def load_banana_data(self) -> Dict[str, Any]:
        """读取大香蕉独立词库；失败时不影响本地 data.json。

        返回的字典按只读约定使用（命中缓存时会复用同一对象）。
        """
        if not self.banana_file.exists():
            return {"schema_version": 1, "prompts": {}}

        stamp = _file_stamp(self.banana_file)
        if stamp is not None and stamp == self._banana_stamp and self._banana_cache is not None:
            return self._banana_cache

        try:
            with open(self.banana_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("banana_prompts.json 顶层不是对象，已忽略")
                return {"schema_version": 1, "prompts": {}}
            prompts = data.get("prompts", {})
            if not isinstance(prompts, dict):
                logger.warning("banana_prompts.json prompts 字段不是对象，已忽略")
                data["prompts"] = {}
            self._banana_cache = data
            self._banana_stamp = stamp
            return data
        except Exception as e:
            logger.warning(f"读取 banana_prompts.json 失败，已忽略大香蕉扩展词库: {e}")
            return {"schema_version": 1, "prompts": {}}

    @_serialized()
    def save_banana_data(self, banana_data: Dict[str, Any]) -> None:
        """原子写入大香蕉独立词库，不触碰 data.json。"""
        try:
            self.banana_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.banana_file.with_suffix(self.banana_file.suffix + ".tmp")
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(banana_data, f, indent=2, ensure_ascii=False)
            tmp_file.replace(self.banana_file)
            self._banana_cache = banana_data
            self._banana_stamp = _file_stamp(self.banana_file)
            self._banana_count_cache = None
        except Exception as e:
            logger.error(f"保存 banana_prompts.json 失败: {e}")
            raise

    @_serialized()
    def count_banana_entries(self, show_restricted: bool = False) -> int:
        """统计大香蕉词条数量；按文件指纹与过滤开关缓存，避免分页时重复遍历（P2-13）。"""
        flag = bool(show_restricted)
        stamp = _file_stamp(self.banana_file)
        if (
            self._banana_count_cache is not None
            and self._banana_count_stamp == stamp
            and self._banana_count_flag == flag
        ):
            return self._banana_count_cache
        count = len(self.get_banana_prompt_entries(show_restricted=flag))
        self._banana_count_cache = count
        self._banana_count_stamp = stamp
        self._banana_count_flag = flag
        return count

    def get_banana_prompt_entries(self, show_restricted: bool = False) -> Dict[str, Dict[str, Any]]:
        """返回过滤后的大香蕉提示词条目，保留元数据用于搜索展示。"""
        banana_data = self.load_banana_data()
        raw_prompts = banana_data.get("prompts", {})
        if not isinstance(raw_prompts, dict):
            return {}

        filtered: Dict[str, Dict[str, Any]] = {}
        for name, entry in raw_prompts.items():
            if isinstance(entry, str):
                content = entry
                restricted = False
                normalized = {"content": content, "restricted": restricted}
            elif isinstance(entry, dict):
                content = entry.get("content", "")
                restricted = bool(entry.get("restricted", False))
                normalized = entry
            else:
                continue

            if restricted and not show_restricted:
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            filtered[str(name)] = normalized
        return filtered

    def get_banana_prompts(self, show_restricted: bool = False) -> Dict[str, str]:
        entries = self.get_banana_prompt_entries(show_restricted=show_restricted)
        prompts: Dict[str, str] = {}
        for name, entry in entries.items():
            content = entry.get("content", "")
            if isinstance(content, str) and content.strip():
                prompts[name] = content
        return prompts

    def get_effective_prompts(
        self,
        include_banana: bool = True,
        show_restricted: bool = False,
    ) -> Dict[str, str]:
        """返回本地词库 + 只读大香蕉词库的运行时合并视图。"""
        prompts, _ = self._effective_prompt_catalog(include_banana, show_restricted)
        return prompts

    def _effective_prompt_catalog(
        self, include_banana: bool, show_restricted: bool,
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """同时保留大香蕉原始键到合并后名称的映射，供短名称解析使用。"""
        merged = dict(self.get_prompts())
        banana_names: Dict[str, str] = {}
        if not include_banana:
            return merged, banana_names

        for name, prompt in self.get_banana_prompts(show_restricted=show_restricted).items():
            target_name = name
            if target_name in merged:
                suffix = "#banana"
                target_name = f"{name}{suffix}"
                counter = 2
                while target_name in merged:
                    target_name = f"{name}{suffix}{counter}"
                    counter += 1
            merged[target_name] = prompt
            banana_names[name] = target_name
        return merged, banana_names

    def get_banana_prompt_aliases(self, show_restricted: bool = False) -> Dict[str, str]:
        """返回原始键到可调用短名称的映射；同名词条全部追加阿拉伯数字。"""
        entries = self.get_banana_prompt_entries(show_restricted=True)
        titles = {
            name: str(entry.get("source_title") or name.rsplit("/", 1)[-1]).strip()
            for name, entry in entries.items()
        }
        counts = Counter(titles.values())
        # 先保留真实标题和完整名称，避免“举猫猫”的编号占用已有的“举猫猫1”。
        all_prompts, _ = self._effective_prompt_catalog(True, True)
        local_names = set(self.get_prompts())
        reserved = set(all_prompts) | set(titles.values())
        used = set()
        aliases: Dict[str, str] = {}
        # 使用完整键排序，远端调整列表顺序、切换过滤开关均不改变编号。
        for name in sorted(entries):
            title = titles[name]
            alias = title
            if counts[title] > 1 or title in local_names or (title in all_prompts and title != name):
                number = 1
                while f"{title}{number}" in reserved or f"{title}{number}" in used:
                    number += 1
                alias = f"{title}{number}"
            used.add(alias)
            if show_restricted or not entries[name].get("restricted", False):
                aliases[name] = alias
        return aliases

    def get_prompt_matches(
        self, name: str, include_banana: bool = True, show_restricted: bool = False,
    ) -> Dict[str, str]:
        """完整名称优先，其次解析短名称；未编号的重名标题返回所有候选。"""
        prompts, banana_names = self._effective_prompt_catalog(include_banana, show_restricted)
        if name in prompts:
            return {name: prompts[name]}
        if not include_banana:
            return {}
        aliases = self.get_banana_prompt_aliases(show_restricted)
        for original, alias in aliases.items():
            if name == alias:
                return {alias: prompts[banana_names[original]]}
        entries = self.get_banana_prompt_entries(show_restricted)
        return {
            alias: prompts[banana_names[original]]
            for original, alias in aliases.items()
            if name == str(entries[original].get("source_title") or original.rsplit("/", 1)[-1]).strip()
        }

    def _migrate_from_toml(self):
        config_path = self.plugin_dir / "config.toml"
        if not config_path.exists():
            return

        try:
            import toml
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            data_changed = False
            config_changed = False
            
            # Migrate Prompts
            if "prompts" in config_data:
                for name, prompt in config_data["prompts"].items():
                    if name not in self.data["prompts"]:
                        self.data["prompts"][name] = prompt
                        data_changed = True
                del config_data["prompts"]
                config_changed = True

            # Migrate Channels
            if "channels" in config_data:
                for name, info in config_data["channels"].items():
                    if name not in self.data["channels"]:
                        self.data["channels"][name] = info
                        data_changed = True
                del config_data["channels"]
                config_changed = True

            api_config = config_data.get("api")
            if isinstance(api_config, dict):
                channels = self.data.setdefault("channels", {})
                legacy_fields = [
                    "enable_google",
                    "api_url",
                    "enable_lmarena",
                    "lmarena_api_url",
                    "lmarena_api_key",
                    "lmarena_model_name",
                ]

                google_url = str(api_config.get("api_url") or "").strip()
                google_enabled = bool(api_config.get("enable_google", True))
                if google_url and "google" not in channels:
                    channels["google"] = {"url": google_url, "enabled": google_enabled, "stream": False}
                    data_changed = True

                lmarena_url = str(api_config.get("lmarena_api_url") or "").strip()
                lmarena_key = str(api_config.get("lmarena_api_key") or "").strip()
                lmarena_model = str(api_config.get("lmarena_model_name") or "").strip()
                lmarena_enabled = bool(api_config.get("enable_lmarena", False))
                is_placeholder_lmarena = lmarena_url in {"", "http://xxx:666/v1/chat/completions"}
                if lmarena_url and not is_placeholder_lmarena:
                    if "lmarena" not in channels:
                        channel_info: Dict[str, Any] = {
                            "url": lmarena_url,
                            "enabled": lmarena_enabled,
                            "stream": True,
                        }
                        if lmarena_model:
                            channel_info["model"] = lmarena_model
                        channels["lmarena"] = channel_info
                        data_changed = True
                    if lmarena_key:
                        added_count, _ = key_manager.add_keys([lmarena_key], "lmarena")
                        data_changed = data_changed or added_count > 0

                for field_name in legacy_fields:
                    if field_name in api_config:
                        del api_config[field_name]
                        config_changed = True

            if data_changed:
                self.save_data()
            if config_changed:
                save_config_file(config_path, config_data)
            if data_changed or config_changed:
                logger.info("Successfully migrated legacy config.toml data")

        except Exception as e:
            logger.error(f"Migration from TOML failed: {e}")

    @_serialized()
    def get_prompts(self) -> Dict[str, str]:
        # 与 get_channels 一致：从文件刷新（命中指纹缓存时无额外解析开销）
        self.data = self._load_data()
        return self.data.get("prompts", {})

    @_serialized(refresh=True)
    def add_prompt(self, name: str, prompt: str):
        if "prompts" not in self.data:
            self.data["prompts"] = {}
        self.data["prompts"][name] = prompt
        self.save_data()

    @_serialized(refresh=True)
    def delete_prompt(self, name: str) -> bool:
        if name in self.data.get("prompts", {}):
            del self.data["prompts"][name]
            self.save_data()
            return True
        return False

    @_serialized(refresh=True)
    def update_prompt(self, name: str, prompt: str) -> bool:
        """修改已存在的提示词"""
        if name in self.data.get("prompts", {}):
            self.data["prompts"][name] = prompt
            self.save_data()
            return True
        return False

    @_serialized()
    def get_channels(self) -> Dict[str, Any]:
        # 每次调用时从文件重新加载，支持实时更新（命中指纹缓存时无额外解析开销）
        self.data = self._load_data()
        return self.data.get("channels", {})

    @_serialized(refresh=True)
    def add_channel(self, name: str, info: Dict[str, Any]):
        if "channels" not in self.data:
            self.data["channels"] = {}
        self.data["channels"][name] = info
        self.save_data()

    @_serialized(refresh=True)
    def delete_channel(self, name: str) -> bool:
        if name in self.data.get("channels", {}):
            del self.data["channels"][name]
            self.save_data()
            return True
        return False
        
    @_serialized(refresh=True)
    def update_channel(self, name: str, info: Dict[str, Any]):
         if "channels" not in self.data:
            self.data["channels"] = {}
         self.data["channels"][name] = info
         self.save_data()

# 初始化实例
key_manager = KeyManager()
data_manager = DataManager()
