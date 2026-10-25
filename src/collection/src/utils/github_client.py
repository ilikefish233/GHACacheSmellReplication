# src/utils/github_client.py
"""
GitHub API 客户端，封装速率限制、节流、403诊断、重试等逻辑。
从 fetch_workflows.py 中提取，供多个采集脚本复用。
"""

import re
import time
import logging
import threading
import requests
from pathlib import Path
from typing import Optional, Dict, List

from src.utils.env_loader import get_github_token


class GitHubClient:
    """GitHub REST API 客户端，内置令牌桶速率限制管理和错误处理，线程安全"""

    def __init__(self, github_token: str = None, log_file: Path = None):
        if github_token is None:
            github_token = get_github_token()
        if not github_token:
            raise ValueError("未提供 GitHub Token，请设置环境变量 GITHUB_TOKEN 或在 config/.env 中配置")

        self.github_token = github_token
        self.headers = {
            'Authorization': f'token {github_token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        # 速率限制状态
        self.rate_limit_remaining = 5000
        self.rate_limit_reset = None
        self.rate_limit_limit = 5000

        # 令牌桶节流
        self._lock = threading.Lock()
        self._tokens = 25           # 令牌桶容量
        self._max_tokens = 25       # 令牌桶上限
        self._refill_rate = 1.3     # 每秒补充令牌数（~4700/小时，留安全余量）
        self._last_refill = time.monotonic()

        # 统计
        self.api_calls = 0
        self.api_saved = 0

        # 日志
        self.logger = logging.getLogger('GitHubClient')
        self.logger.setLevel(logging.INFO)
        if log_file and not self.logger.handlers:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s - %(message)s')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

    # ─── 速率限制管理 ────────────────────────────────────

    def _update_rate_limit(self, response: requests.Response):
        """从响应头更新速率限制信息，线程安全"""
        with self._lock:
            if 'X-RateLimit-Remaining' in response.headers:
                self.rate_limit_remaining = int(response.headers['X-RateLimit-Remaining'])
            if 'X-RateLimit-Reset' in response.headers:
                self.rate_limit_reset = int(response.headers['X-RateLimit-Reset'])
            if 'X-RateLimit-Limit' in response.headers:
                self.rate_limit_limit = int(response.headers['X-RateLimit-Limit'])

    def _throttle_request(self):
        """
        令牌桶节流，线程安全。
        桶容量25个令牌，每秒补充1.3个（~4700/小时，留安全余量）。
        配额紧张时自动减速，配额充足时允许突发。
        """
        with self._lock:
            now = time.monotonic()
            # 补充令牌
            elapsed = now - self._last_refill
            self._tokens = min(self._max_tokens, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now

            # 配额紧张时降低补充速率
            remaining_ratio = self.rate_limit_remaining / max(self.rate_limit_limit, 1)
            if remaining_ratio <= 0.1:
                # 配额极低，大幅降速
                self._refill_rate = 0.3
            elif remaining_ratio <= 0.2:
                # 配额紧张，适度降速
                time_until_reset = max((self.rate_limit_reset or 0) - time.time(), 60)
                self._refill_rate = max(0.3, self.rate_limit_remaining / time_until_reset * 0.85)
            else:
                self._refill_rate = 1.3

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            # 令牌不足，等待
            wait = (1.0 - self._tokens) / self._refill_rate
            time.sleep(wait)
            self._tokens = 0
            self._last_refill = time.monotonic()

    def _diagnose_403(self, response: requests.Response) -> str:
        """诊断403响应类型，返回 'rate_limit'/'abuse'/或其他原因字符串"""
        try:
            body = response.json()
            message = body.get('message', '')
            msg_lower = message.lower()
            if 'abuse' in msg_lower or 'secondary' in msg_lower:
                return 'abuse'
            if 'too many' in msg_lower or 'rate limit' in msg_lower:
                return 'rate_limit'
            if 'saml' in msg_lower or 'sso' in msg_lower:
                return f'sso/saml: {message[:100]}'
            if 'forbidden' in msg_lower or 'permission' in msg_lower or 'access' in msg_lower:
                return f'permission: {message[:100]}'
            if self.rate_limit_remaining <= 0:
                return 'rate_limit'
            return f'unknown: {message[:100]}'
        except Exception:
            if self.rate_limit_remaining <= 0:
                return 'rate_limit'
            return 'unknown: 无法解析响应体'

    def _wait_for_abuse_limit(self):
        """等待GitHub二级限流(Abuse Rate Limit)解除"""
        wait_time = 120
        print(f"二级限流，等待 {wait_time} 秒...")
        for i in range(wait_time, 0, -5):
            mins, secs = divmod(i, 60)
            print(f"  剩余: {mins:02d}:{secs:02d} (Ctrl+C中断)", end='\r', flush=True)
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print(f"  等待已中断{'':>30}")
                raise
        print(f"  二级限流等待结束，继续处理{'':>15}")

    def _wait_for_rate_limit(self):
        """等待API限流重置，显示实时倒计时（可被Ctrl+C中断）"""
        if self.rate_limit_reset:
            wait_seconds = max(self.rate_limit_reset - time.time(), 0) + 30
            reset_time = time.strftime('%H:%M:%S', time.localtime(self.rate_limit_reset))
            print(f"API限流，重置时间: {reset_time}，等待 {wait_seconds:.0f} 秒...")
            while wait_seconds > 0:
                mins, secs = divmod(int(wait_seconds), 60)
                print(f"  剩余: {mins:02d}:{secs:02d} (Ctrl+C中断)", end='\r', flush=True)
                try:
                    time.sleep(min(5, wait_seconds))
                except KeyboardInterrupt:
                    print(f"  等待已中断{'':>30}")
                    raise
                wait_seconds = max(self.rate_limit_reset - time.time() + 30, 0)
            print(f"  限流已重置，验证配额...{'':>15}")
            self._verify_rate_limit_reset()
        else:
            print(f"API限流，未知重置时间，等待 120 秒...")
            for i in range(120, 0, -5):
                mins, secs = divmod(i, 60)
                print(f"  剩余: {mins:02d}:{secs:02d} (Ctrl+C中断)", end='\r', flush=True)
                try:
                    time.sleep(5)
                except KeyboardInterrupt:
                    print(f"  等待已中断{'':>30}")
                    raise
            print(f"  等待结束，继续处理{'':>20}")

    def _verify_rate_limit_reset(self):
        """限流重置后发探测请求验证配额是否真的恢复"""
        try:
            resp = requests.get('https://api.github.com/rate_limit', headers=self.headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                core = data.get('resources', {}).get('core', {})
                remaining = core.get('remaining', 0)
                limit = core.get('limit', 0)
                reset_ts = core.get('reset', 0)
                self.rate_limit_remaining = remaining
                self.rate_limit_limit = limit
                self.rate_limit_reset = reset_ts
                # 根据配额调整令牌桶补充速率
                with self._lock:
                    self._refill_rate = max(0.5, limit / 3600.0 * 0.8)
                if remaining > 10:
                    print(f"  ✓ 配额已恢复: {remaining}/{limit}")
                else:
                    new_reset = time.strftime('%H:%M:%S', time.localtime(reset_ts))
                    print(f"  ⚠️  配额未恢复(剩余:{remaining})，新重置时间: {new_reset}")
                    if reset_ts > time.time():
                        extra_wait = reset_ts - time.time() + 30
                        print(f"  额外等待 {extra_wait:.0f} 秒...")
                        time.sleep(extra_wait)
                        self._verify_rate_limit_reset()
            else:
                print(f"  ⚠️  探测请求失败({resp.status_code})，假设配额已恢复")
                self.rate_limit_remaining = self.rate_limit_limit
        except Exception as e:
            print(f"  ⚠️  探测请求异常({e})，假设配额已恢复")
            self.rate_limit_remaining = self.rate_limit_limit

    # ─── 核心 API 请求方法 ──────────────────────────────

    def get(self, url: str, params: dict = None, extra_headers: dict = None,
            max_retries: int = 3) -> Optional[requests.Response]:
        """
        发送 GET 请求，自动处理速率限制和重试。

        Returns:
            requests.Response 对象，或 None（网络错误重试耗尽时）
        """
        merged_headers = {**self.headers, **(extra_headers or {})}

        for attempt in range(max_retries):
            if self.rate_limit_remaining < 10:
                self._wait_for_rate_limit()
            elif self.rate_limit_remaining < 100:
                print(f"  ⚠️  API剩余: {self.rate_limit_remaining}")

            self._throttle_request()

            try:
                response = requests.get(url, headers=merged_headers, params=params, timeout=30)
                self.api_calls += 1
                self._update_rate_limit(response)

                if response.status_code == 200:
                    return response

                if response.status_code == 403:
                    reason = self._diagnose_403(response)
                    if reason == 'rate_limit':
                        if attempt >= max_retries - 1:
                            self.logger.error(f"主限流重试{max_retries}次仍失败: {url}")
                            return response
                        print(f"  ⚠️  主限流(剩余:{self.rate_limit_remaining})，等待重置...")
                        self._wait_for_rate_limit()
                        continue
                    elif reason == 'abuse':
                        if attempt >= max_retries - 1:
                            self.logger.error(f"二级限流重试{max_retries}次仍失败: {url}")
                            return response
                        print(f"  ⚠️  触发GitHub二级限流(Abuse Rate Limit)")
                        self._wait_for_abuse_limit()
                        continue
                    else:
                        self.logger.error(f"403 非限流: {reason} - {url}")
                        return response

                if response.status_code == 404:
                    return response

                if response.status_code in (451, 410):
                    self.logger.error(f"资源不可用({response.status_code}): {url}")
                    return response

                if response.status_code >= 500:
                    print(f"  ⚠️  服务器错误 {response.status_code}，重试 {attempt+1}/{max_retries}")
                    time.sleep(2 ** attempt)
                    continue

                return response

            except requests.exceptions.ConnectionError as e:
                print(f"  ⚠️  网络连接失败: {e}，重试 {attempt+1}/{max_retries}")
                time.sleep(2 ** attempt)
                continue
            except requests.exceptions.Timeout as e:
                print(f"  ⚠️  请求超时: {e}，重试 {attempt+1}/{max_retries}")
                time.sleep(2 ** attempt)
                continue
            except Exception as e:
                self.logger.error(f"请求异常: {e} - {url}")
                return None

        self.logger.error(f"重试 {max_retries} 次仍失败: {url}")
        return None

    # ─── 便捷方法 ──────────────────────────────────────

    def get_json(self, url: str, params: dict = None, extra_headers: dict = None) -> Optional[dict]:
        """GET 请求并返回 JSON，失败返回 None"""
        response = self.get(url, params=params, extra_headers=extra_headers)
        if response and response.status_code == 200:
            return response.json()
        return None

    def get_paginated(self, url: str, params: dict = None, extra_headers: dict = None,
                      max_pages: int = None, per_page: int = 100) -> List[dict]:
        """
        分页获取所有结果。

        Args:
            url: API 端点 URL
            params: 额外查询参数
            max_pages: 最大页数限制（None 表示不限制）
            per_page: 每页条数

        Returns:
            所有页的结果列表
        """
        all_items = []
        page = 1
        base_params = {'per_page': per_page}
        if params:
            base_params.update(params)

        while True:
            base_params['page'] = page
            response = self.get(url, params=base_params, extra_headers=extra_headers)
            if not response or response.status_code != 200:
                break

            data = response.json()
            if not data:
                break

            if isinstance(data, list):
                all_items.extend(data)
                if len(data) < per_page:
                    break
            else:
                # 某些端点返回的不是列表
                all_items.append(data)
                break

            page += 1
            if max_pages and page > max_pages:
                break

        return all_items

    def get_count_from_link_header(self, url: str, params: dict = None) -> int:
        """
        通过 Link header 获取资源总数（per_page=1 请求）。
        适用于 /commits, /branches, /releases, /contributors, /pulls 等。
        """
        req_params = {'per_page': 1, 'page': 1}
        if params:
            req_params.update(params)
        response = self.get(url, params=req_params)
        if response and response.status_code == 200:
            link_header = response.headers.get('Link', '')
            if link_header:
                last_match = re.search(r'page=(\d+)>; rel="last"', link_header)
                if last_match:
                    return int(last_match.group(1))
            data = response.json()
            if isinstance(data, list):
                return len(data)
        return 0

    def get_file_content(self, repo: str, path: str, ref: str = None) -> Optional[str]:
        """
        获取仓库文件内容（Base64 解码）。

        Args:
            repo: 仓库全名 (owner/repo)
            path: 文件路径
            ref: Git 引用（分支/SHA），默认为默认分支

        Returns:
            文件内容字符串，失败返回 None
        """
        url = f'https://api.github.com/repos/{repo}/contents/{path}'
        params = {}
        if ref:
            params['ref'] = ref

        response = self.get(url, params=params)
        if not response or response.status_code != 200:
            return None

        import base64
        data = response.json()
        if 'content' not in data:
            return None

        raw_bytes = base64.b64decode(data['content'])
        for encoding in ['utf-8', 'utf-8-sig', 'latin-1']:
            try:
                return raw_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw_bytes.decode('utf-8', errors='replace')

    def get_rate_limit_status(self) -> dict:
        """获取当前速率限制状态"""
        data = self.get_json('https://api.github.com/rate_limit')
        if data:
            core = data.get('resources', {}).get('core', {})
            return {
                'remaining': core.get('remaining', 0),
                'limit': core.get('limit', 0),
                'reset': core.get('reset', 0),
                'reset_time': time.strftime('%Y-%m-%d %H:%M:%S',
                                            time.localtime(core.get('reset', 0))),
            }
        return {}
