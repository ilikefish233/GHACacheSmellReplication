import json
import requests
import base64
import hashlib
import time
import logging
import re
import tempfile
import os
from pathlib import Path
from typing import Dict, Set, Optional, List
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.env_loader import get_github_token
from config.settings import settings


class WorkflowFetcher:
    """获取并管理工作流文件"""
    
    def __init__(self, github_token: str, output_dir: Path):
        self.github_token = github_token
        self.headers = {
            'Authorization': f'token {github_token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        self.output_dir = output_dir
        self.workflows_dir = output_dir / 'workflows'
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        
        self.log_file = output_dir / 'workflow_fetch.log'
        self._setup_logging()
        
        self.workflow_hashes: Dict[str, Path] = {}
        self.run_workflow_map: Dict[str, str] = {}
        self.workflow_runs_map: Dict[str, Set[str]] = {}
        
        # 缓存已获取的工作流内容（repo+path+sha -> content）
        self.workflow_cache: Dict[str, str] = {}
        
        self.rate_limit_remaining = 5000
        self.rate_limit_reset = None
        self.rate_limit_limit = 5000
        self.last_request_time = 0
        self.min_request_interval = 0.5
        
        self.stats = {
            'total_runs': 0,
            'unique_workflows': 0,
            'api_calls': 0,
            'api_saved': 0,
            'errors': 0,
            'skipped': 0,
            'offset': 0,
            'already_mapped': 0,
            'retried': 0,
            'retry_success': 0
        }
        
        self.failed_runs: Dict[str, Dict] = {}
        
        self._load_existing_mappings()
    
    def _setup_logging(self):
        """设置日志"""
        self.logger = logging.getLogger('WorkflowFetcher')
        self.logger.setLevel(logging.INFO)
        
        if not self.logger.handlers:
            file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
            file_handler.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s - %(message)s')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
    
    def log(self, message: str, level: str = 'info'):
        """同时输出到控制台和日志文件"""
        print(message)
        if level == 'error':
            self.logger.error(message)
        else:
            self.logger.info(message)
    
    def _load_existing_mappings(self):
        """加载已有映射（断点续传）"""
        run_map_file = self.output_dir / 'run_workflow_mapping.json'
        workflow_map_file = self.output_dir / 'workflow_runs_mapping.json'
        stats_file = self.output_dir / 'workflow_fetch_stats.json'
        failed_runs_file = self.output_dir / 'failed_runs.json'
        
        if run_map_file.exists():
            with open(run_map_file, 'r', encoding='utf-8') as f:
                self.run_workflow_map = json.load(f)
            print(f"加载已有run映射: {len(self.run_workflow_map)} 条")
        
        if workflow_map_file.exists():
            with open(workflow_map_file, 'r', encoding='utf-8') as f:
                loaded_map = json.load(f)
                self.workflow_runs_map = {k: set(v) for k, v in loaded_map.items()}
            print(f"加载已有workflow映射: {len(self.workflow_runs_map)} 条")
        
        if stats_file.exists():
            with open(stats_file, 'r', encoding='utf-8') as f:
                loaded_stats = json.load(f)
                self.stats.update(loaded_stats)
            print(f"加载已有统计: 已处理 {self.stats.get('total_runs', 0)} 条")
        
        if failed_runs_file.exists():
            try:
                with open(failed_runs_file, 'r', encoding='utf-8') as f:
                    self.failed_runs = json.load(f)
                print(f"加载失败记录: {len(self.failed_runs)} 条")
            except json.JSONDecodeError as e:
                print(f"警告: failed_runs.json 文件损坏，将重新开始记录")
                print(f"  错误位置: line {e.lineno}, column {e.colno}")
                backup_file = failed_runs_file.with_suffix('.json.bak')
                if backup_file.exists():
                    backup_file.unlink()
                failed_runs_file.rename(backup_file)
                print(f"  已备份损坏文件到: {backup_file}")
                self.failed_runs = {}
        
        for workflow_file in self.workflows_dir.glob('*.yml'):
            content_hash = workflow_file.stem
            self.workflow_hashes[content_hash] = workflow_file
        
        if self.workflow_hashes:
            print(f"加载已有工作流文件: {len(self.workflow_hashes)} 个")
    
    def get_content_hash(self, content: str) -> str:
        """计算内容的hash（32位，碰撞概率极低）"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]
    
    def get_workflow_content(self, repo: str, workflow_path: str, ref: str, _retry_count: int = 0) -> Optional[str]:
        """通过GitHub API获取工作流文件内容"""
        max_retries = 3

        cache_key = f"{repo}/{workflow_path}@{ref}"
        if cache_key in self.workflow_cache:
            self.stats['api_saved'] += 1
            return self.workflow_cache[cache_key]

        if self.rate_limit_remaining < 10:
            self._wait_for_rate_limit()
        elif self.rate_limit_remaining < 100:
            print(f"  ⚠️  API剩余: {self.rate_limit_remaining}")

        self._throttle_request()

        url = f"https://api.github.com/repos/{repo}/contents/{workflow_path}"
        params = {'ref': ref}
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            self.stats['api_calls'] += 1
            
            self._update_rate_limit(response)
            
            if response.status_code == 200:
                content = response.json()
                if 'content' in content:
                    raw_bytes = base64.b64decode(content['content'])
                    
                    # 尝试多种编码（优先UTF-8）
                    encodings = ['utf-8', 'utf-8-sig', 'latin-1']
                    for encoding in encodings:
                        try:
                            decoded_content = raw_bytes.decode(encoding)
                            # 缓存结果
                            self.workflow_cache[cache_key] = decoded_content
                            return decoded_content
                        except UnicodeDecodeError:
                            continue
                    
                    # 所有编码都失败，使用replace模式
                    decoded_content = raw_bytes.decode('utf-8', errors='replace')
                    self.workflow_cache[cache_key] = decoded_content
                    return decoded_content
            
            elif response.status_code == 403:
                rate_limit_info = self._diagnose_403(response)
                if rate_limit_info == 'rate_limit':
                    if _retry_count >= max_retries:
                        msg = f"主限流重试{max_retries}次仍失败: {repo}/{workflow_path}@{ref}"
                        print(f"  ❌ {msg}")
                        self.logger.error(msg)
                        return '__NETWORK_ERROR__'
                    print(f"  ⚠️  主限流(剩余:{self.rate_limit_remaining})，等待重置...")
                    self._wait_for_rate_limit()
                    return self.get_workflow_content(repo, workflow_path, ref, _retry_count + 1)
                elif rate_limit_info == 'abuse':
                    if _retry_count >= max_retries:
                        msg = f"二级限流重试{max_retries}次仍失败: {repo}/{workflow_path}@{ref}"
                        print(f"  ❌ {msg}")
                        self.logger.error(msg)
                        return '__NETWORK_ERROR__'
                    print(f"  ⚠️  触发GitHub二级限流(Abuse Rate Limit)")
                    self._wait_for_abuse_limit()
                    return self.get_workflow_content(repo, workflow_path, ref, _retry_count + 1)
                else:
                    msg = f"访问被拒绝(403非限流): {repo}/{workflow_path}@{ref} - {rate_limit_info}"
                    print(f"  ❌ {msg}")
                    self.logger.error(msg)
                    return '__NOT_FOUND__'
            
            elif response.status_code == 404:
                msg = f"工作流文件不存在: {repo}/{workflow_path}@{ref}"
                print(f"  ❌ {msg}")
                self.logger.error(msg)
                return '__NOT_FOUND__'
            elif response.status_code == 451:
                msg = f"仓库因法律原因不可用(451): {repo}/{workflow_path}@{ref}"
                print(f"  ❌ {msg}")
                self.logger.error(msg)
                return '__NOT_FOUND__'
            elif response.status_code == 410:
                msg = f"资源已永久移除(410): {repo}/{workflow_path}@{ref}"
                print(f"  ❌ {msg}")
                self.logger.error(msg)
                return '__NOT_FOUND__'
            else:
                msg = f"API错误 {response.status_code}: {repo}/{workflow_path}@{ref}"
                print(f"  ❌ {msg}")
                self.logger.error(msg)
        except requests.exceptions.ConnectionError as e:
            msg = f"网络连接失败: {repo}/{workflow_path}@{ref} - {e}"
            print(f"  ❌ {msg}")
            self.logger.error(msg)
            return '__NETWORK_ERROR__'
        except requests.exceptions.Timeout as e:
            msg = f"请求超时: {repo}/{workflow_path}@{ref} - {e}"
            print(f"  ❌ {msg}")
            self.logger.error(msg)
            return '__NETWORK_ERROR__'
        except Exception as e:
            msg = f"获取工作流出错: {repo}/{workflow_path}@{ref} - {e}"
            print(f"  ❌ {msg}")
            self.logger.error(msg)
        
        return None
    
    def _update_rate_limit(self, response):
        """更新rate limit信息"""
        if 'X-RateLimit-Remaining' in response.headers:
            self.rate_limit_remaining = int(response.headers['X-RateLimit-Remaining'])
        if 'X-RateLimit-Reset' in response.headers:
            self.rate_limit_reset = int(response.headers['X-RateLimit-Reset'])
        if 'X-RateLimit-Limit' in response.headers:
            self.rate_limit_limit = int(response.headers['X-RateLimit-Limit'])
            self.min_request_interval = max(0.5, 3600.0 / self.rate_limit_limit * 0.8)

    def _throttle_request(self):
        """自适应请求节流，均匀消耗API配额"""
        if self.rate_limit_reset and self.rate_limit_remaining > 0:
            time_until_reset = max(self.rate_limit_reset - time.time(), 1)
            adaptive_interval = time_until_reset / max(self.rate_limit_remaining, 1) * 0.9
            interval = max(self.min_request_interval, min(adaptive_interval, 2.0))
        else:
            interval = self.min_request_interval
        elapsed = time.time() - self.last_request_time
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self.last_request_time = time.time()

    def _diagnose_403(self, response) -> str:
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
        print(f"\n二级限流，等待 {wait_time} 秒...")
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
            print(f"\nAPI限流，重置时间: {reset_time}，等待 {wait_seconds:.0f} 秒...")
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
            print(f"\nAPI限流，未知重置时间，等待 120 秒...")
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
                self.min_request_interval = max(0.5, 3600.0 / limit * 0.8)
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
    
    def save_workflow(self, content: str, content_hash: str) -> Path:
        """保存工作流文件（如果不存在）"""
        if content_hash in self.workflow_hashes:
            return self.workflow_hashes[content_hash]
        
        workflow_file = self.workflows_dir / f"{content_hash}.yml"
        workflow_file.write_text(content, encoding='utf-8')
        
        self.workflow_hashes[content_hash] = workflow_file
        self.stats['unique_workflows'] += 1
        
        return workflow_file
    
    def find_reusable_workflows(self, content: str, repo: str) -> List[str]:
        """从yml内容中提取本地可复用工作流引用路径"""
        local_refs = []
        pattern = re.compile(r"^\s+uses:\s+['\"]?(\.\/\.github\/workflows\/[^'\"\s#]+)['\"]?", re.MULTILINE)
        for match in pattern.finditer(content):
            ref_path = match.group(1)
            if ref_path not in local_refs:
                local_refs.append(ref_path)
        return local_refs

    def fetch_reusable_workflows(self, content: str, repo: str, ref: str, parent_run_id: str):
        """递归获取本地可复用工作流引用"""
        local_refs = self.find_reusable_workflows(content, repo)
        if not local_refs:
            return []
        
        fetched = []
        for ref_path in local_refs:
            cache_key = f"{repo}/{ref_path}@{ref}"
            if cache_key in self.workflow_cache:
                fetched.append(ref_path)
                continue
            
            print(f"  → 获取引用: {ref_path}")
            ref_content = self.get_workflow_content(repo, ref_path, ref)
            if ref_content:
                ref_hash = self.get_content_hash(ref_content)
                if ref_hash not in self.workflow_hashes:
                    self.save_workflow(ref_content, ref_hash)
                    print(f"    保存引用: {ref_hash}.yml")
                
                ref_workflow_path = str(self.workflow_hashes[ref_hash].relative_to(self.output_dir))
                if ref_workflow_path not in self.workflow_runs_map:
                    self.workflow_runs_map[ref_workflow_path] = set()
                self.workflow_runs_map[ref_workflow_path].add(f"ref:{parent_run_id}:{ref_path}")
                fetched.append(ref_path)
                
                nested_refs = self.find_reusable_workflows(ref_content, repo)
                if nested_refs:
                    self.fetch_reusable_workflows(ref_content, repo, ref, parent_run_id)
        
        return fetched

    def process_run(self, run: Dict) -> Optional[str]:
        """处理单个run，获取并保存工作流"""
        run_id = run['_id']
        
        self.stats['total_runs'] += 1
        
        if run_id in self.run_workflow_map:
            self.stats['already_mapped'] += 1
            return self.run_workflow_map[run_id]
        
        repo = run['repository_name']
        workflow_path = run['workflow_path']
        head_sha = run['metadata']['head_sha']
        
        print(f"\n[{self.stats['total_runs']}] {run_id[:50]}...")
        print(f"  仓库: {repo} | 工作流: {workflow_path}")
        
        content = self.get_workflow_content(repo, workflow_path, head_sha)
        
        if content == '__NOT_FOUND__':
            self.stats['errors'] += 1
            self.failed_runs[run_id] = {
                'repo': repo,
                'workflow_path': workflow_path,
                'head_sha': head_sha,
                'run_data': run,
                'fail_type': 'not_found',
            }
            return None
        
        if content == '__NETWORK_ERROR__':
            self.stats['errors'] += 1
            self.failed_runs[run_id] = {
                'repo': repo,
                'workflow_path': workflow_path,
                'head_sha': head_sha,
                'run_data': run,
                'fail_type': 'network',
            }
            return None
        
        if not content:
            self.stats['errors'] += 1
            self.failed_runs[run_id] = {
                'repo': repo,
                'workflow_path': workflow_path,
                'head_sha': head_sha,
                'run_data': run,
                'fail_type': 'unknown',
            }
            return None
        
        content_hash = self.get_content_hash(content)
        
        # 检查是否已存在（跳过重复）
        if content_hash in self.workflow_hashes:
            workflow_file = self.workflow_hashes[content_hash]
            self.stats['skipped'] += 1
            print(f"  跳过（已存在）: {workflow_file.name}")
        else:
            workflow_file = self.save_workflow(content, content_hash)
            print(f"  保存: {workflow_file.name}")
        
        workflow_path_str = str(workflow_file.relative_to(self.output_dir))
        self.run_workflow_map[run_id] = workflow_path_str
        
        if workflow_path_str not in self.workflow_runs_map:
            self.workflow_runs_map[workflow_path_str] = set()
        self.workflow_runs_map[workflow_path_str].add(run_id)
        
        self.fetch_reusable_workflows(content, repo, head_sha, run_id)
        
        return workflow_path_str
    
    def _count_total_runs(self, input_file: Path) -> int:
        """快速统计数据集总run数"""
        with open(input_file, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
        
        if not first_line.startswith('['):
            count = 0
            with open(input_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        count += 1
            return count
        
        count = 0
        buffer = ''
        brace_count = 0
        in_string = False
        escape_next = False
        started = False
        
        with open(input_file, 'r', encoding='utf-8') as f:
            for line in f:
                for char in line:
                    if escape_next:
                        escape_next = False
                        if started:
                            buffer += char
                        continue
                    if char == '\\':
                        escape_next = True
                        if started:
                            buffer += char
                        continue
                    if char == '"' and not escape_next:
                        in_string = not in_string
                        if started:
                            buffer += char
                        continue
                    if in_string:
                        if started:
                            buffer += char
                        continue
                    if char == '{':
                        if not started:
                            started = True
                        brace_count += 1
                        buffer += char
                    elif char == '}':
                        brace_count -= 1
                        buffer += char
                        if brace_count == 0 and started:
                            count += 1
                            buffer = ''
                            started = False
                    elif started:
                        buffer += char
        return count

    def _stream_items(self, input_file: Path):
        """流式遍历数据集，兼容JSON数组和JSONL格式"""
        with open(input_file, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
        
        if not first_line.startswith('['):
            with open(input_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            return
        
        buffer = ''
        brace_count = 0
        in_string = False
        escape_next = False
        started = False
        
        with open(input_file, 'r', encoding='utf-8') as f:
            for line in f:
                for char in line:
                    if escape_next:
                        escape_next = False
                        if started:
                            buffer += char
                        continue
                    if char == '\\':
                        escape_next = True
                        if started:
                            buffer += char
                        continue
                    if char == '"' and not escape_next:
                        in_string = not in_string
                        if started:
                            buffer += char
                        continue
                    if in_string:
                        if started:
                            buffer += char
                        continue
                    if char == '{':
                        if not started:
                            started = True
                        brace_count += 1
                        buffer += char
                    elif char == '}':
                        brace_count -= 1
                        buffer += char
                        if brace_count == 0 and started:
                            try:
                                yield json.loads(buffer)
                            except json.JSONDecodeError:
                                pass
                            buffer = ''
                            started = False
                    elif started:
                        buffer += char

    def process_dataset(self, input_file: Path, limit: int = None, offset: int = 0):
        """流式处理数据集，基于已有映射智能续传"""
        already_mapped = len(self.run_workflow_map)
        print(f"已有映射: {already_mapped} 条")
        
        print("统计数据集总量...")
        total = self._count_total_runs(input_file)
        print(f"数据集总量: {total} 条")
        
        need_process = total - already_mapped - len(self.failed_runs)
        print(f"待处理: {need_process} 条")
        
        if need_process <= 0:
            print("所有 run 已处理完毕，无需继续")
            self.print_stats()
            return
        
        count = 0
        skipped_mapped = 0
        skipped_failed = 0
        processed = 0
        start_time = time.time()
        
        for run in self._stream_items(input_file):
            count += 1
            
            run_id = run.get('_id')
            if run_id and run_id in self.run_workflow_map:
                skipped_mapped += 1
                continue
            
            if run_id and run_id in self.failed_runs:
                skipped_failed += 1
                continue
            
            processed += 1
            self.process_run(run)
            
            if processed % 50 == 0:
                elapsed = time.time() - start_time
                speed = processed / elapsed if elapsed > 0 else 0
                eta = (need_process - processed) / speed if speed > 0 else 0
                print(f"\n进度: {already_mapped + len(self.failed_runs) + processed}/{total} ({(already_mapped + len(self.failed_runs) + processed)/total*100:.1f}%) | "
                      f"速度: {speed:.1f} runs/s | "
                      f"剩余: ~{eta/60:.0f}m | "
                      f"API剩余: {self.rate_limit_remaining}")
                
                if processed % 500 == 0:
                    self.save_mappings()
                    print("  ✓ 检查点已保存")
            
            if limit and processed >= limit:
                break
        
        self.save_mappings()
        self.print_stats()
        print(f"\n遍历: {count} | 跳过(已映射): {skipped_mapped} | 跳过(已失败): {skipped_failed} | 新处理: {processed}")
    
    def _atomic_json_dump(self, data, target_file: Path):
        """原子写入JSON文件，防止中断导致文件损坏"""
        target_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=str(target_file.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(target_file))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def save_mappings(self):
        """保存映射关系"""
        run_map_file = self.output_dir / 'run_workflow_mapping.json'
        self._atomic_json_dump(self.run_workflow_map, run_map_file)
        print(f"\n保存run映射: {run_map_file}")

        workflow_map_file = self.output_dir / 'workflow_runs_mapping.json'
        workflow_runs = {k: list(v) for k, v in self.workflow_runs_map.items()}
        self._atomic_json_dump(workflow_runs, workflow_map_file)
        print(f"保存workflow映射: {workflow_map_file}")

        stats_file = self.output_dir / 'workflow_fetch_stats.json'
        self._atomic_json_dump(self.stats, stats_file)
        print(f"保存统计: {stats_file}")

        failed_runs_file = self.output_dir / 'failed_runs.json'
        self._atomic_json_dump(self.failed_runs, failed_runs_file)
        print(f"保存失败记录: {failed_runs_file} ({len(self.failed_runs)} 条)")
    
    def print_stats(self):
        """打印统计信息"""
        print("\n" + "=" * 80)
        print("处理完成!")
        print("=" * 80)
        print(f"总run数: {self.stats['total_runs']}")
        print(f"已有映射跳过: {self.stats.get('already_mapped', 0)}")
        print(f"当前偏移: {self.stats.get('offset', 0)}")
        print(f"唯一工作流数: {self.stats['unique_workflows']}")
        print(f"跳过重复: {self.stats['skipped']}")
        print(f"\nAPI统计:")
        print(f"  实际调用: {self.stats['api_calls']}")
        print(f"  节省调用: {self.stats['api_saved']} (缓存命中)")
        print(f"  错误数: {self.stats['errors']}")
        if self.stats.get('retried', 0) > 0:
            print(f"\n重试统计:")
            print(f"  重试数: {self.stats['retried']}")
            print(f"  重试成功: {self.stats['retry_success']}")
        print(f"\n工作流文件保存在: {self.workflows_dir}")
        print(f"映射文件: {self.output_dir / 'run_workflow_mapping.json'}")
        print(f"映射文件: {self.output_dir / 'workflow_runs_mapping.json'}")
        
        if len(self.failed_runs) > 0:
            print(f"\n失败记录: {len(self.failed_runs)} 条")
            print(f"   重试命令: python scripts/fetch_workflows.py --retry-failed")
        
        if self.stats.get('offset', 0) > 0:
            print(f"\n续传提示: 如需继续处理，在config/settings.py中设置:")
            print(f"   workflow_fetch_offset = {self.stats['offset']}")


    def _load_run_ids_set(self) -> Set[str]:
        """仅加载run_workflow_map的key集合，用于去重检查"""
        run_map_file = self.output_dir / 'run_workflow_mapping.json'
        if not run_map_file.exists():
            return set()
        run_ids = set()
        with open(run_map_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            run_ids = set(data.keys())
        print(f"加载已有run ID集合: {len(run_ids)} 条 (仅去重用)")
        return run_ids

    def retry_failed(self, retry_all: bool = False):
        """重试失败的记录
        
        Args:
            retry_all: 如果为True，也重试not_found和unknown类型的失败
        """
        if not self.failed_runs:
            print("没有失败记录需要重试")
            return
        
        existing_run_ids = self._load_run_ids_set()
        
        print(f"\n开始重试 {len(self.failed_runs)} 条失败记录...")
        
        retry_count = 0
        success_count = 0
        skip_mapped_count = 0
        
        if retry_all:
            retryable = dict(self.failed_runs)
        else:
            retryable = {k: v for k, v in self.failed_runs.items() if v.get('fail_type', 'network') == 'network'}
            non_retryable = {k: v for k, v in self.failed_runs.items() if v.get('fail_type', 'network') != 'network'}
            
            not_found_count = sum(1 for v in non_retryable.values() if v.get('fail_type') == 'not_found')
            unknown_count = sum(1 for v in non_retryable.values() if v.get('fail_type') != 'not_found')
            print(f"失败分类: 网络错误 {len(retryable)} 条 (可重试), 文件不存在 {not_found_count} 条, 其他 {unknown_count} 条")
            print(f"提示: 使用 --retry-all 可重试所有类型")
        
        if not retryable:
            print("无可重试的记录")
            return
        
        interrupted = False
        def _handle_interrupt(sig, frame):
            nonlocal interrupted
            interrupted = True
            raise KeyboardInterrupt
        
        import signal
        old_handler = signal.signal(signal.SIGINT, _handle_interrupt)
        
        try:
            for run_id, fail_info in list(retryable.items()):
                if interrupted:
                    break
                
                if run_id in existing_run_ids:
                    skip_mapped_count += 1
                    del self.failed_runs[run_id]
                    continue
                
                retry_count += 1
                self.stats['retried'] += 1
                
                print(f"\n[重试 {retry_count}/{len(retryable)}] {run_id[:50]}...")
                print(f"  仓库: {fail_info['repo']} | 工作流: {fail_info['workflow_path']}")
                
                content = self.get_workflow_content(
                    fail_info['repo'],
                    fail_info['workflow_path'],
                    fail_info['head_sha']
                )
                
                if content == '__NOT_FOUND__':
                    self.failed_runs[run_id] = {
                        'repo': fail_info['repo'],
                        'workflow_path': fail_info['workflow_path'],
                        'head_sha': fail_info['head_sha'],
                        'fail_type': 'not_found',
                    }
                    print(f"  文件不存在，跳过")
                    continue
                
                if content == '__NETWORK_ERROR__':
                    self.failed_runs[run_id] = {
                        'repo': fail_info['repo'],
                        'workflow_path': fail_info['workflow_path'],
                        'head_sha': fail_info['head_sha'],
                        'fail_type': 'network',
                    }
                    msg = f"仍然失败(网络): {fail_info['repo']}/{fail_info['workflow_path']}@{fail_info['head_sha']}"
                    print(f"  {msg}")
                    self.logger.error(msg)
                    continue
                
                if not content:
                    self.failed_runs[run_id] = {
                        'repo': fail_info['repo'],
                        'workflow_path': fail_info['workflow_path'],
                        'head_sha': fail_info['head_sha'],
                        'fail_type': 'unknown',
                    }
                    msg = f"仍然失败(未知): {fail_info['repo']}/{fail_info['workflow_path']}@{fail_info['head_sha']}"
                    print(f"  {msg}")
                    self.logger.error(msg)
                    continue
                
                content_hash = self.get_content_hash(content)
                
                if content_hash in self.workflow_hashes:
                    workflow_file = self.workflow_hashes[content_hash]
                    self.stats['skipped'] += 1
                    print(f"  文件已存在，复用: {workflow_file.name}")
                else:
                    workflow_file = self.save_workflow(content, content_hash)
                    print(f"  保存: {workflow_file.name}")
                
                workflow_path_str = str(workflow_file.relative_to(self.output_dir))
                self.run_workflow_map[run_id] = workflow_path_str
                
                if workflow_path_str not in self.workflow_runs_map:
                    self.workflow_runs_map[workflow_path_str] = set()
                self.workflow_runs_map[workflow_path_str].add(run_id)
                
                del self.failed_runs[run_id]
                success_count += 1
                self.stats['retry_success'] += 1
                self.stats['errors'] -= 1
                
                if retry_count % 20 == 0:
                    print(f"\n重试进度: {retry_count}/{len(retryable)} | 成功: {success_count} | 已映射跳过: {skip_mapped_count}")
                    self.save_mappings()
        finally:
            signal.signal(signal.SIGINT, old_handler)
            self.save_mappings()
        
        print(f"\n重试完成: 成功 {success_count}/{retry_count} | 已映射跳过: {skip_mapped_count}")
        if interrupted:
            print(f"⚠️ 被中断，已保存进度，再次运行可继续")
        if self.failed_runs:
            print(f"仍有 {len(self.failed_runs)} 条记录失败")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='获取工作流文件')
    parser.add_argument('--retry-failed', action='store_true', help='重试失败的记录')
    parser.add_argument('--retry-all', action='store_true', help='重试所有类型的失败记录(包括not_found)')
    args = parser.parse_args()
    
    github_token = get_github_token()
    
    if not github_token:
        print("错误: 请在 config/.env 文件中设置 GITHUB_TOKEN")
        exit(1)
    
    fetcher = WorkflowFetcher(github_token, settings.workflow_fetch_output_dir)
    
    if args.retry_failed or args.retry_all:
        fetcher.retry_failed(retry_all=args.retry_all)
    else:
        fetcher.process_dataset(
            input_file=settings.workflow_fetch_input,
            limit=settings.workflow_fetch_limit
        )


if __name__ == "__main__":
    main()