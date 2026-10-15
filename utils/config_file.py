"""TOML 配置文件的读写修复。

- fix_broken_toml_config(): 修复 TOML 配置文件中未加引号的中文键名
- save_config_file(): 统一的配置文件保存入口，确保中文 Key 正确处理

两者都使用「同目录唯一临时文件 + 原子替换」写入：写入中断、磁盘满或并发写入
不会留下损坏的配置。保存失败会向调用者抛出异常，由调用者决定如何向用户报告，
不得吞掉异常后误报成功。
"""
import contextlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

from .log import logger


def _atomic_write_text(file_path: Path, text: str) -> None:
    """在同目录写唯一临时文件后原子替换，避免固定临时文件被并发覆盖。"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = None
    with contextlib.suppress(OSError):
        original_mode = file_path.stat().st_mode & 0o777
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{file_path.name}.", suffix=".tmp", dir=str(file_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, original_mode if original_mode is not None else 0o644)
        os.replace(tmp_name, file_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def fix_broken_toml_config(file_path: Path):
    """
    读取配置文件原始文本，使用正则强制修复未加引号的中文键名。
    专门解决框架自动生成时 key 不带引号导致 Empty key 报错的问题。
    """
    if not file_path.exists():
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        fixed_lines = []
        modified = False
        
        # 匹配规则：行首是非引号、非注释、非方括号的字符，且包含中文，后接等号
        pattern = re.compile(r'^([^#\n"\'\[]*[一-龥][^#\n"\'\[]*?)\s*=')
        
        # 简单的状态机，用于处理 admins 列表
        in_admins_block = False
        
        for line in lines:
            stripped = line.strip()
            
            # 1. 修复中文键名 (现有逻辑)
            match = pattern.match(line)
            if match:
                key = match.group(1).strip()
                parts = line.split('=', 1)
                if len(parts) == 2:
                    new_line = f'"{key}" ={parts[1]}'
                    fixed_lines.append(new_line)
                    modified = True
                    continue
            
            # 2. 修复 admins 列表中的纯数字 (新增逻辑)
            if stripped.startswith('admins = ['):
                # 单行数组（admins = [123]）不进入块模式，避免误处理后续行。
                if ']' not in stripped.split('[', 1)[1]:
                    in_admins_block = True
                fixed_lines.append(line)
            elif in_admins_block and re.match(r'^\]\s*,?\s*(#.*)?$', stripped):
                in_admins_block = False
                fixed_lines.append(line)
            elif in_admins_block:
                # 检查是否是纯数字（可能带逗号）
                # 匹配: 空白 + 数字 + 可选逗号 + 空白
                digit_match = re.match(r'^(\s*)(\d+)(\s*,?\s*)$', line)
                if digit_match:
                    # 给数字加上双引号
                    prefix, number, suffix = digit_match.groups()
                    fixed_lines.append(f'{prefix}"{number}"{suffix}')
                    modified = True
                else:
                    fixed_lines.append(line)
            else:
                fixed_lines.append(line)
        
        if modified:
            _atomic_write_text(file_path, "".join(fixed_lines))
            logger.info("配置文件格式已自动修复（中文Key引号/Admins列表格式）。")
            
    except Exception as e:
        logger.error(f"尝试自动修复配置文件失败: {e}")


def save_config_file(config_path: Path, config_data: Dict[str, Any]):
    """
    统一的保存入口，保存前先转为字符串并二次处理，确保中文Key有引号。

    Raises:
        OSError: 写入失败（权限、磁盘空间等）时原样抛出，调用方不得误报成功。
    """
    import toml
    # 1. 先生成标准 TOML 字符串
    content = toml.dumps(config_data)

    # 2. 再次进行正则修复
    lines = content.splitlines()
    final_lines = []
    for line in lines:
        stripped = line.strip()
        if '=' in stripped and not stripped.startswith('#') and not stripped.startswith('['):
            key_part, rest = stripped.split('=', 1)
            key_clean = key_part.strip()
            # 如果包含非ASCII且没引号
            if any(ord(c) > 127 for c in key_clean) and not (key_clean.startswith('"') or key_clean.startswith("'")):
                line = f'"{key_clean}" ={rest}'
        final_lines.append(line)
        
    _atomic_write_text(config_path, "\n".join(final_lines))
