# src/utils/env_loader.py
import os
from pathlib import Path
from typing import Optional


def load_env_file(env_file: str = None) -> dict:
    """
    加载 .env 文件中的环境变量
    
    Args:
        env_file: .env 文件路径，默认为 config/.env
    
    Returns:
        包含环境变量的字典
    """
    if env_file is None:
        env_file = Path(__file__).parent.parent.parent / 'config' / '.env'
    else:
        env_file = Path(env_file)
    
    env_vars = {}
    
    if not env_file.exists():
        print(f"警告: .env 文件不存在: {env_file}")
        return env_vars
    
    with open(env_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            
            # 跳过空行和注释
            if not line or line.startswith('#'):
                continue
            
            # 解析 KEY=VALUE
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                
                # 移除引号
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                
                env_vars[key] = value
                os.environ[key] = value
    
    return env_vars


def get_github_token() -> Optional[str]:
    """
    获取 GitHub Token
    
    优先级：
    1. 环境变量 GITHUB_TOKEN
    2. config/.env 文件中的 GITHUB_TOKEN
    
    Returns:
        GitHub Token 或 None
    """
    # 先检查环境变量
    token = os.environ.get('GITHUB_TOKEN')
    
    if token and token != 'your_github_token_here':
        return token
    
    # 尝试从 .env 文件加载
    env_vars = load_env_file()
    token = env_vars.get('GITHUB_TOKEN')
    
    if token and token != 'your_github_token_here':
        return token
    
    return None