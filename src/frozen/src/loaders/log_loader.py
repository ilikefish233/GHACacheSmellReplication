# src/loaders/log_loader.py
import zipfile
import tarfile
import io
from pathlib import Path
from typing import Iterator, Tuple, Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class LogEntry:
    archive_path: str
    file_name: str
    content: str
    repo_owner: Optional[str] = None
    repo_name: Optional[str] = None
    workflow_name: Optional[str] = None
    workflow_hash: Optional[str] = None
    run_id: Optional[int] = None
    job_id: Optional[int] = None

class LogLoader:
    def __init__(self, archive_path: Path):
        self.archive_path = Path(archive_path)
        self._validate()
    
    def _validate(self):
        if not self.archive_path.exists():
            raise FileNotFoundError(f"日志压缩包不存在: {self.archive_path}")
        if self.archive_path.suffix != '.zip':
            raise ValueError(f"不支持的压缩格式: {self.archive_path.suffix}")
    
    def list_tar_files(self) -> list:
        with zipfile.ZipFile(self.archive_path, 'r') as zf:
            return [z for z in zf.namelist() if z.endswith('.tar.gz')]
    
    def count_tar_files(self) -> int:
        return len(self.list_tar_files())
    
    def iter_tar_files(self) -> Iterator[Tuple[str, tarfile.TarFile]]:
        with zipfile.ZipFile(self.archive_path, 'r') as zf:
            for zip_info in zf.namelist():
                if not zip_info.endswith('.tar.gz'):
                    continue
                with zf.open(zip_info) as tar_data:
                    tar_bytes = io.BytesIO(tar_data.read())
                    try:
                        tar = tarfile.open(fileobj=tar_bytes, mode='r:gz')
                        yield zip_info, tar
                    except tarfile.TarError as e:
                        print(f"无法打开 tar 文件 {zip_info}: {e}")
                        continue
    
    def iter_log_entries(self) -> Iterator[LogEntry]:
        for archive_path, tar in self.iter_tar_files():
            metadata = self._parse_path_metadata(archive_path)
            
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                    
                f = tar.extractfile(member)
                if not f:
                    continue
                    
                try:
                    content = f.read().decode('utf-8', errors='ignore')
                    yield LogEntry(
                        archive_path=archive_path,
                        file_name=member.name,
                        content=content,
                        **metadata
                    )
                    del content
                except Exception as e:
                    print(f"读取文件失败 {member.name}: {e}")
                    continue
    
    def iter_batches(self, batch_size: int = 100) -> Iterator[list]:
        batch = []
        for entry in self.iter_log_entries():
            batch.append(entry)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
    
    def _parse_path_metadata(self, path: str) -> Dict[str, Any]:
        parts = path.replace('\\', '/').split('/')
        metadata = {
            'repo_owner': None,
            'repo_name': None,
            'workflow_name': None,
            'workflow_hash': None,
            'run_id': None,
            'job_id': None,
        }
        
        for i, part in enumerate(parts):
            if part == 'logs' and i + 3 < len(parts):
                metadata['repo_owner'] = parts[i + 1]
                metadata['repo_name'] = parts[i + 2]
                
                workflow_part = parts[i + 3] if i + 3 < len(parts) else ''
                if '_' in workflow_part:
                    metadata['workflow_name'] = workflow_part.rsplit('_', 1)[0]
                    metadata['workflow_hash'] = workflow_part.rsplit('_', 1)[-1]
                break
        
        tar_name = Path(path).stem
        match = tar_name.replace('.tar', '')
        if '-' in match:
            ids = match.split('-')
            if len(ids) >= 2:
                try:
                    metadata['run_id'] = int(ids[0])
                    metadata['job_id'] = int(ids[1].replace('.tar', '').replace('.gz', ''))
                except ValueError:
                    pass
        
        return metadata