"""
工作流解析缓存

将解析结果持久化到磁盘，支持增量扫描。
"""

import json
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime, timedelta


class WorkflowCache:
    """工作流解析缓存

    缓存已解析的工作流，避免重复解析。
    支持基于文件内容的哈希验证和 TTL 过期。
    """

    def __init__(self, cache_dir: Optional[Path] = None, ttl_hours: int = 24):
        """初始化缓存

        Args:
            cache_dir: 缓存目录
            ttl_hours: 缓存过期时间（小时）
        """
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl = timedelta(hours=ttl_hours)
        self._memory_cache: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str, file_hash: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取缓存

        Args:
            key: 缓存键
            file_hash: 文件哈希（用于验证）

        Returns:
            缓存数据或 None
        """
        # 先查内存缓存
        if key in self._memory_cache:
            data = self._memory_cache[key]
            if self._is_valid(data, file_hash):
                return data
            else:
                del self._memory_cache[key]

        # 查磁盘缓存
        if not self.cache_dir:
            return None

        cache_file = self.cache_dir / f"{key}.json"
        if not cache_file.exists():
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 验证哈希和过期时间
            if self._is_valid(data, file_hash):
                self._memory_cache[key] = data
                return data
            else:
                # 缓存无效，删除
                cache_file.unlink()

        except (IOError, json.JSONDecodeError):
            pass

        return None

    def set(self, key: str, data: Dict[str, Any], file_hash: Optional[str] = None):
        """设置缓存

        Args:
            key: 缓存键
            data: 要缓存的数据
            file_hash: 文件哈希
        """
        # 添加元数据
        cache_data = {
            'cached_at': datetime.now().isoformat(),
            'file_hash': file_hash,
            'data': data,
        }

        # 更新内存缓存
        self._memory_cache[key] = cache_data

        # 持久化到磁盘
        if not self.cache_dir:
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"{key}.json"

        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except (IOError, TypeError):
            pass

    def _is_valid(self, cache_data: Dict[str, Any], file_hash: Optional[str] = None) -> bool:
        """验证缓存是否有效

        Args:
            cache_data: 缓存数据
            file_hash: 期望的文件哈希

        Returns:
            是否有效
        """
        # 检查哈希
        if file_hash and cache_data.get('file_hash') != file_hash:
            return False

        # 检查过期时间
        cached_at = cache_data.get('cached_at')
        if cached_at:
            try:
                cached_time = datetime.fromisoformat(cached_at)
                if datetime.now() - cached_time > self.ttl:
                    return False
            except (ValueError, TypeError):
                return False

        return True

    def clear(self):
        """清空缓存"""
        self._memory_cache.clear()

    def clear_persistent(self):
        """清空持久化缓存"""
        self.clear()
        if self.cache_dir and self.cache_dir.exists():
            import shutil
            shutil.rmtree(self.cache_dir, ignore_errors=True)

    def compute_hash(self, content: str) -> str:
        """计算内容哈希

        Args:
            content: 文件内容

        Returns:
            哈希值（前12位）
        """
        return hashlib.md5(content.encode('utf-8')).hexdigest()[:12]

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        stats = {
            'memory_count': len(self._memory_cache),
            'persistent_enabled': self.cache_dir is not None,
        }

        if self.cache_dir and self.cache_dir.exists():
            json_files = list(self.cache_dir.glob('*.json'))
            stats['persistent_count'] = len(json_files)
            total_size = sum(f.stat().st_size for f in json_files)
            stats['persistent_size_bytes'] = total_size
            stats['persistent_size_mb'] = round(total_size / 1024 / 1024, 2)

        return stats
