# -*- coding: utf-8 -*-
"""
代理检测和配置模块
自动检测系统代理设置并应用到 Google API 请求
"""

import os
import sys
import platform
import logging
from typing import Optional, Dict
from urllib.request import getproxies

logger = logging.getLogger(__name__)


def get_windows_proxy() -> Optional[str]:
    """
    从 Windows 注册表获取系统代理设置
    返回格式: http://host:port 或 None
    """
    if platform.system() != 'Windows':
        return None
    
    try:
        import winreg
        
        # 打开 Internet Settings 注册表项
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',
            0,
            winreg.KEY_READ
        )
        
        try:
            # 检查代理是否启用
            proxy_enable, _ = winreg.QueryValueEx(key, 'ProxyEnable')
            if not proxy_enable:
                return None
            
            # 获取代理服务器地址
            proxy_server, _ = winreg.QueryValueEx(key, 'ProxyServer')
            
            if proxy_server:
                # 处理可能的格式：host:port 或 http=host:port;https=host:port
                if '=' in proxy_server:
                    # 解析多协议代理设置
                    for part in proxy_server.split(';'):
                        if part.startswith('http=') or part.startswith('https='):
                            addr = part.split('=', 1)[1]
                            if not addr.startswith('http'):
                                addr = f'http://{addr}'
                            return addr
                else:
                    if not proxy_server.startswith('http'):
                        proxy_server = f'http://{proxy_server}'
                    return proxy_server
                    
        finally:
            winreg.CloseKey(key)
            
    except (ImportError, OSError, FileNotFoundError) as e:
        logger.debug(f"无法读取 Windows 注册表代理设置: {e}")
    
    return None


def get_macos_proxy() -> Optional[str]:
    """
    从 macOS 系统偏好设置获取代理
    """
    if platform.system() != 'Darwin':
        return None
    
    try:
        import subprocess
        
        # 获取当前活动的网络服务
        result = subprocess.run(
            ['networksetup', '-listnetworkserviceorder'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        # 尝试获取 HTTP 代理
        for service in ['Wi-Fi', 'Ethernet', 'USB 10/100/1000 LAN']:
            try:
                result = subprocess.run(
                    ['networksetup', '-getwebproxy', service],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')
                    enabled = False
                    server = None
                    port = None
                    
                    for line in lines:
                        if 'Enabled: Yes' in line:
                            enabled = True
                        elif line.startswith('Server:'):
                            server = line.split(':', 1)[1].strip()
                        elif line.startswith('Port:'):
                            port = line.split(':', 1)[1].strip()
                    
                    if enabled and server and port:
                        return f'http://{server}:{port}'
                        
            except subprocess.TimeoutExpired:
                continue
                
    except Exception as e:
        logger.debug(f"无法读取 macOS 代理设置: {e}")
    
    return None


def get_env_proxy() -> Optional[str]:
    """
    从环境变量获取代理设置
    检查常见的代理环境变量
    """
    # 按优先级检查环境变量
    proxy_vars = [
        'HTTPS_PROXY',
        'https_proxy',
        'HTTP_PROXY',
        'http_proxy',
        'ALL_PROXY',
        'all_proxy',
    ]
    
    for var in proxy_vars:
        proxy = os.environ.get(var)
        if proxy:
            return proxy
    
    return None


def get_urllib_proxy() -> Optional[str]:
    """
    使用 urllib 获取系统代理（跨平台）
    """
    proxies = getproxies()
    
    # 优先返回 HTTPS 代理，其次 HTTP
    if 'https' in proxies:
        return proxies['https']
    if 'http' in proxies:
        return proxies['http']
    
    return None


def detect_system_proxy() -> Optional[str]:
    """
    自动检测系统代理设置
    按以下优先级检测：
    1. 环境变量（用户可能已手动设置）
    2. 操作系统特定方法（Windows 注册表 / macOS 系统偏好）
    3. urllib.request.getproxies()（通用方法）
    
    返回: 代理地址字符串 (如 "http://127.0.0.1:7890") 或 None
    """
    # 1. 先检查环境变量
    proxy = get_env_proxy()
    if proxy:
        logger.info(f"从环境变量检测到代理: {proxy}")
        return proxy
    
    # 2. 操作系统特定检测
    system = platform.system()
    
    if system == 'Windows':
        proxy = get_windows_proxy()
        if proxy:
            logger.info(f"从 Windows 注册表检测到代理: {proxy}")
            return proxy
    
    elif system == 'Darwin':
        proxy = get_macos_proxy()
        if proxy:
            logger.info(f"从 macOS 系统偏好检测到代理: {proxy}")
            return proxy
    
    # 3. 通用方法
    proxy = get_urllib_proxy()
    if proxy:
        logger.info(f"通过 urllib 检测到代理: {proxy}")
        return proxy
    
    logger.info("未检测到系统代理设置")
    return None


def apply_proxy_to_environment(proxy: Optional[str] = None, auto_detect: bool = True) -> Dict[str, Optional[str]]:
    """
    将代理设置应用到环境变量，使 google-generativeai 库可以使用代理
    
    参数:
        proxy: 手动指定的代理地址，如 "http://127.0.0.1:7890"
        auto_detect: 如果 proxy 为 None，是否自动检测系统代理
    
    返回:
        包含已设置代理信息的字典
    """
    result = {
        'detected_proxy': None,
        'applied': False,
        'message': ''
    }
    
    # 确定要使用的代理
    if proxy:
        proxy_to_use = proxy
        result['message'] = f"使用手动指定的代理: {proxy}"
    elif auto_detect:
        proxy_to_use = detect_system_proxy()
        if proxy_to_use:
            result['message'] = f"自动检测到代理: {proxy_to_use}"
        else:
            result['message'] = "未检测到系统代理，将直接连接"
            return result
    else:
        result['message'] = "代理功能已禁用"
        return result
    
    result['detected_proxy'] = proxy_to_use
    
    # 设置环境变量
    # google-generativeai 使用 httpx，它会读取这些环境变量
    os.environ['HTTP_PROXY'] = proxy_to_use
    os.environ['HTTPS_PROXY'] = proxy_to_use
    os.environ['http_proxy'] = proxy_to_use
    os.environ['https_proxy'] = proxy_to_use
    
    # 对于 gRPC（如果使用的话）
    os.environ['GRPC_PROXY'] = proxy_to_use
    
    result['applied'] = True
    logger.info(result['message'])
    
    return result


def clear_proxy_environment():
    """
    清除代理相关的环境变量
    """
    proxy_vars = [
        'HTTP_PROXY', 'HTTPS_PROXY', 'GRPC_PROXY',
        'http_proxy', 'https_proxy', 'grpc_proxy',
        'ALL_PROXY', 'all_proxy'
    ]
    
    for var in proxy_vars:
        if var in os.environ:
            del os.environ[var]
    
    logger.info("已清除代理环境变量")


def get_proxy_status() -> Dict[str, Optional[str]]:
    """
    获取当前代理状态信息
    """
    return {
        'http_proxy': os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy'),
        'https_proxy': os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy'),
        'detected_system_proxy': detect_system_proxy(),
    }


# 便捷函数：程序启动时调用
def setup_proxy(proxy: Optional[str] = None, verbose: bool = True) -> bool:
    """
    便捷函数：设置代理
    
    参数:
        proxy: 手动指定代理，None 则自动检测
        verbose: 是否打印日志
    
    返回:
        是否成功设置了代理
    """
    result = apply_proxy_to_environment(proxy, auto_detect=True)
    
    if verbose and result['message']:
        print(f"[代理] {result['message']}")
    
    return result['applied']


if __name__ == '__main__':
    # 测试代理检测
    logging.basicConfig(level=logging.DEBUG)
    
    print("=" * 50)
    print("代理检测测试")
    print("=" * 50)
    
    print(f"\n操作系统: {platform.system()}")
    
    print("\n1. 环境变量代理:")
    env_proxy = get_env_proxy()
    print(f"   结果: {env_proxy or '未设置'}")
    
    print("\n2. 系统代理:")
    sys_proxy = detect_system_proxy()
    print(f"   结果: {sys_proxy or '未检测到'}")
    
    print("\n3. 应用代理到环境变量:")
    result = apply_proxy_to_environment()
    print(f"   {result['message']}")
    print(f"   已应用: {result['applied']}")
    
    print("\n4. 当前代理状态:")
    status = get_proxy_status()
    for key, value in status.items():
        print(f"   {key}: {value or '未设置'}")
