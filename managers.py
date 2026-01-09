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
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime
from src.common.logger import get_logger
from .utils import save_config_file

logger = get_logger("gemini_drawer")

class KeyManager:
    def __init__(self, keys_file_path: Path = None):
        if keys_file_path is None:
            self.plugin_dir = Path(__file__).parent
            self.data_dir = self.plugin_dir / "data"
            self.data_dir.mkdir(exist_ok=True)
            self.keys_file = self.data_dir / "keys.json"
        else:
            self.keys_file = keys_file_path
            self.plugin_dir = self.keys_file.parent.parent 
            
        self.config = self._load_config()
        self._migrate_legacy_data()

    def _migrate_legacy_data(self):
        migrated = False
        old_keys_file = self.plugin_dir / "keys.json"
        if old_keys_file.exists() and old_keys_file != self.keys_file:
            try:
                with open(old_keys_file, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                    old_keys = old_data.get('keys', [])
                    if old_keys:
                        current_keys = {k['value'] for k in self.config.get('keys', [])}
                        for k in old_keys:
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
        try:
            if not self.keys_file.exists():
                default_config = {"keys": [], "current_index": 0}
                self.save_config(default_config)
                return default_config
            with open(self.keys_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"读取密钥配置失败: {e}")
            return {"keys": [], "current_index": 0}

    def save_config(self, config_data: Dict[str, Any]):
        try:
            with open(self.keys_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"保存密钥配置失败: {e}")

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
                added_count += 1
        self.save_config(self.config)
        return added_count, duplicate_count

    def get_all_keys(self) -> List[Dict[str, Any]]:
        return self.config.get('keys', [])

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
                self.save_config(self.config)
                return

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

class DataManager:
    def __init__(self, data_file_path: Path = None):
        if data_file_path is None:
            self.plugin_dir = Path(__file__).parent
            self.data_dir = self.plugin_dir / "data"
            self.data_dir.mkdir(exist_ok=True)
            self.data_file = self.data_dir / "data.json"
        else:
            self.data_file = data_file_path
            self.plugin_dir = self.data_file.parent.parent
            
        self.data = self._load_data()
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
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load data.json: {e}")
            return {"prompts": {}, "channels": {}}

    def save_data(self):
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save data.json: {e}")

    def _migrate_from_toml(self):
        config_path = self.plugin_dir / "config.toml"
        if not config_path.exists():
            return

        try:
            import toml
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            changed = False
            
            # Migrate Prompts
            if "prompts" in config_data:
                for name, prompt in config_data["prompts"].items():
                    if name not in self.data["prompts"]:
                        self.data["prompts"][name] = prompt
                        changed = True
                del config_data["prompts"]

            # Migrate Channels
            if "channels" in config_data:
                for name, info in config_data["channels"].items():
                    if name not in self.data["channels"]:
                        self.data["channels"][name] = info
                        changed = True
                del config_data["channels"]

            if changed:
                self.save_data()
                save_config_file(config_path, config_data)
                logger.info("Successfully migrated prompts and channels from config.toml to data/data.json")

        except Exception as e:
            logger.error(f"Migration from TOML failed: {e}")

    def get_prompts(self) -> Dict[str, str]:
        return self.data.get("prompts", {})

    def add_prompt(self, name: str, prompt: str):
        if "prompts" not in self.data:
            self.data["prompts"] = {}
        self.data["prompts"][name] = prompt
        self.save_data()

    def delete_prompt(self, name: str) -> bool:
        if name in self.data.get("prompts", {}):
            del self.data["prompts"][name]
            self.save_data()
            return True
        return False

    def get_channels(self) -> Dict[str, Any]:
        return self.data.get("channels", {})

    def add_channel(self, name: str, info: Dict[str, Any]):
        if "channels" not in self.data:
            self.data["channels"] = {}
        self.data["channels"][name] = info
        self.save_data()

    def delete_channel(self, name: str) -> bool:
        if name in self.data.get("channels", {}):
            del self.data["channels"][name]
            self.save_data()
            return True
        return False
        
    def update_channel(self, name: str, info: Dict[str, Any]):
         if "channels" not in self.data:
            self.data["channels"] = {}
         self.data["channels"][name] = info
         self.save_data()

# 初始化实例
key_manager = KeyManager()
data_manager = DataManager()