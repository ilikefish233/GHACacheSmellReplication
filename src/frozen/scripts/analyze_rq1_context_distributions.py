"""补充当前 RQ1 的异味上下文分布分析。

本脚本只读取当前完整工作流快照和当前活动检测器的逐条扫描结果，生成：

1. 正式异味的工作流级共现矩阵；
2. 异味与工作流触发器的交叉分布；
3. 异味与主要仓库生态的交叉分布。
4. 异味与仓库规模、开发者数、工作流数和运行活跃度等仓库特征的交叉分布。
5. NDC 在检测器支持的依赖安装生态中的机会条件分布。
6. 工作流的主要功能类型和多标签功能角色分布。
7. 仓库内异味工作流的集中程度，以及仓库级和工作流级覆盖率的对照。

脚本不读取归档目录、旧版检测器结果或旧版实验输出。生态分类来自当前完整
数据集的 repo_info.jsonl 中的仓库主语言，并归并为互斥的主要生态类别；它是
工作流所属仓库生态的代理，不声称每个工作流只使用这一种语言。所有交叉比例
都明确以工作流级可用分母计算；触发器是可重叠的，不能把各触发器比例相加。
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.detectors.smell.cache_smell_detector import CacheSmellDetector

FORMAL_SMELL_TYPES = [
    "no_dependency_cache",
    "no_fallback_cache",
    "redundant_cache_update",
    "cache_position_error",
    "overlapping_cache_mechanisms",
    "static_cache_key",
    "overly_specific_cache_key",
    "no_docker_layer_cache",
    "deprecated_cache_action",
    "unreachable_cache_write",
    "cache_as_artifact",
]

# NDC 的检测机会取决于当前检测器识别到的依赖安装命令，不能与其他规则
# 使用同一个“全部成功扫描工作流”分母进行生态横向比较。生态交叉分析因此
# 另外提供一个排除 NDC 的总体指标，其分母对所有仓库主要生态保持一致。
NON_NDC_SMELL_TYPES = [
    smell_type for smell_type in FORMAL_SMELL_TYPES
    if smell_type != "no_dependency_cache"
]

SMELL_ALIASES = {
    "no_restore_keys": "no_fallback_cache",
    "conflicting_cache_sources": "overlapping_cache_mechanisms",
    "disposable_cache_key": "overly_specific_cache_key",
}

TRIGGER_TYPES = [
    "push",
    "pull_request",
    "schedule",
    "workflow_dispatch",
    "release",
    "other",
]

ECOSYSTEM_TYPES = [
    "javascript_typescript",
    "python",
    "java_kotlin",
    "go",
    "rust",
    "c_cpp",
    "dotnet",
    "php",
    "ruby",
    "dart_flutter",
    "swift_objective_c",
    "elixir_erlang",
    "haskell",
    "scala_groovy",
    "shell",
    "container",
    "nix",
    "solidity",
    "other",
    "unknown",
]

ECOSYSTEM_LABELS = {
    "javascript_typescript": "JavaScript/TypeScript",
    "python": "Python",
    "java_kotlin": "Java/Kotlin",
    "go": "Go",
    "rust": "Rust",
    "c_cpp": "C/C++",
    "dotnet": ".NET",
    "php": "PHP",
    "ruby": "Ruby",
    "dart_flutter": "Dart/Flutter",
    "swift_objective_c": "Swift/Objective-C",
    "elixir_erlang": "Elixir/Erlang",
    "haskell": "Haskell",
    "scala_groovy": "Scala/Groovy",
    "shell": "Shell",
    "container": "Container/Docker",
    "nix": "Nix",
    "solidity": "Solidity",
    "other": "Other",
    "unknown": "Unknown",
}

NDC_DEPENDENCY_ECOSYSTEMS = ["npm", "python"]
NDC_DEPENDENCY_ECOSYSTEM_LABELS = {
    "npm": "npm-compatible (npm/yarn/pnpm)",
    "python": "Python-compatible (pip/pipenv/poetry/uv)",
}
NDC_MANAGER_TO_ECOSYSTEM = {
    "npm": "npm",
    "yarn": "npm",
    "pnpm": "npm",
    "python": "python",
}

# Reuse the active detector's auxiliary-install and native-build predicates so
# the RQ1 opportunity denominator follows the same scope as NDC itself.
_ACTIVE_CACHE_DETECTOR = CacheSmellDetector()

LANGUAGE_TO_ECOSYSTEM = {
    "javascript": "javascript_typescript",
    "typescript": "javascript_typescript",
    "jsx": "javascript_typescript",
    "coffeescript": "javascript_typescript",
    "vue": "javascript_typescript",
    "svelte": "javascript_typescript",
    "ejs": "javascript_typescript",
    "rescript": "javascript_typescript",
    "python": "python",
    "jupyter notebook": "python",
    "cython": "python",
    "java": "java_kotlin",
    "kotlin": "java_kotlin",
    "go": "go",
    "rust": "rust",
    "c": "c_cpp",
    "c++": "c_cpp",
    "c#": "dotnet",
    "f#": "dotnet",
    "visual basic .net": "dotnet",
    "php": "php",
    "ruby": "ruby",
    "dart": "dart_flutter",
    "swift": "swift_objective_c",
    "objective-c": "swift_objective_c",
    "objective-c++": "swift_objective_c",
    "elixir": "elixir_erlang",
    "erlang": "elixir_erlang",
    "haskell": "haskell",
    "scala": "scala_groovy",
    "groovy": "scala_groovy",
    "shell": "shell",
    "powershell": "shell",
    "dockerfile": "container",
    "nix": "nix",
    "solidity": "solidity",
}

REPOSITORY_FEATURE_LABELS = {
    "repository_size_kb": "仓库规模（KB）",
    "contributors": "开发者数（contributors）",
    "workflow_count": "工作流数量",
    "runs_90d": "近 90 天运行数",
    "stargazers": "Stars",
    "forks": "Forks",
    "commits": "提交数",
    "issues": "Issue 数",
    "pull_requests": "Pull Request 数",
    "releases": "Release 数",
    "code_lines": "代码行数",
    "branches": "分支数",
    "repository_age_years": "仓库年龄（年）",
}

# 每个区间采用左闭右开；最后一个区间的上界为无穷。
REPOSITORY_FEATURE_BINS = {
    "repository_size_kb": [
        ("<10,000 KB", 0, 10_000),
        ("10,000–99,999 KB", 10_000, 100_000),
        ("100,000–999,999 KB", 100_000, 1_000_000),
        ("≥1,000,000 KB", 1_000_000, None),
    ],
    "contributors": [
        ("<10", 0, 10),
        ("10–49", 10, 50),
        ("50–199", 50, 200),
        ("≥200", 200, None),
    ],
    "workflow_count": [
        ("0–2", 0, 3),
        ("3–5", 3, 6),
        ("6–10", 6, 11),
        (">10", 11, None),
    ],
    "runs_90d": [
        ("<100", 0, 100),
        ("100–299", 100, 300),
        ("300–599", 300, 600),
        ("≥600", 600, None),
    ],
    "stargazers": [
        ("<100", 0, 100),
        ("100–999", 100, 1_000),
        ("1,000–4,999", 1_000, 5_000),
        ("≥5,000", 5_000, None),
    ],
    "forks": [
        ("<50", 0, 50),
        ("50–199", 50, 200),
        ("200–999", 200, 1_000),
        ("≥1,000", 1_000, None),
    ],
    "commits": [
        ("<500", 0, 500),
        ("500–1,999", 500, 2_000),
        ("2,000–4,999", 2_000, 5_000),
        ("≥5,000", 5_000, None),
    ],
    "issues": [
        ("<50", 0, 50),
        ("50–199", 50, 200),
        ("200–999", 200, 1_000),
        ("≥1,000", 1_000, None),
    ],
    "pull_requests": [
        ("<50", 0, 50),
        ("50–199", 50, 200),
        ("200–999", 200, 1_000),
        ("≥1,000", 1_000, None),
    ],
    "releases": [
        ("0", 0, 1),
        ("1–10", 1, 11),
        ("11–50", 11, 51),
        (">50", 51, None),
    ],
    "code_lines": [
        ("<10,000", 0, 10_000),
        ("10,000–49,999", 10_000, 50_000),
        ("50,000–199,999", 50_000, 200_000),
        ("≥200,000", 200_000, None),
    ],
    "branches": [
        ("<5", 0, 5),
        ("5–19", 5, 20),
        ("20–49", 20, 50),
        ("≥50", 50, None),
    ],
    "repository_age_years": [
        ("<2", 0, 2),
        ("2–4.99", 2, 5),
        ("5–9.99", 5, 10),
        ("≥10", 10, None),
    ],
}

REPOSITORY_FEATURE_SOURCE_FIELDS = {
    "repository_size_kb": ("repo", "size"),
    "contributors": ("repo", "contributors"),
    "workflow_count": ("record", "nb_workflows"),
    "runs_90d": ("record", "total_runs_90d"),
    "stargazers": ("repo", "stargazers"),
    "forks": ("repo", "forks"),
    "commits": ("repo", "commits"),
    "issues": ("repo", "totalIssues"),
    "pull_requests": ("repo", "totalPullRequests"),
    "releases": ("repo", "releases"),
    "code_lines": ("repo", "codeLines"),
    "branches": ("repo", "branches"),
}

# These are the ten job-task categories operationalized by Bouzenia and Pradel
# (ICSE 2024).  Their classifier lower-cases job labels, removes parenthesized
# details, and assigns a task when its simplified token is a substring of the
# label.  We retain the same vocabulary and matching principle here.
WORKFLOW_ROLE_TYPES = [
    "test",
    "build",
    "deploy",
    "analyze",
    "lint",
    "linux",
    "release",
    "integration",
    "sync",
    "update",
    "other",
    "mixed",
    "unknown",
]

WORKFLOW_ROLE_LABELS = {
    "test": "Test",
    "build": "Build",
    "deploy": "Deploy",
    "analyze": "Analyze",
    "lint": "Lint",
    "linux": "Linux",
    "release": "Release",
    "integration": "Integration",
    "sync": "Sync",
    "update": "Update",
    "other": "Other",
    "mixed": "Mixed / composite workflow",
    "unknown": "Unknown / unclassified",
}

WORKFLOW_PRIMARY_ROLE_PRIORITY = [
    "test",
    "build",
    "deploy",
    "analyze",
    "lint",
    "linux",
    "release",
    "integration",
    "sync",
    "update",
    "other",
    "mixed",
    "unknown",
]

# The source study uses simplified job-name tokens rather than a semantic
# ontology of workflow files.  ``integrat`` intentionally matches both
# ``integration`` and common abbreviated forms such as ``integrat``.
SOURCE_JOB_TASK_TOKENS = [
    ("test", "test"),
    ("build", "build"),
    ("deploy", "deploy"),
    ("analyze", "analyze"),
    ("lint", "lint"),
    ("linux", "linux"),
    ("release", "release"),
    ("integrat", "integration"),
    ("sync", "sync"),
    ("update", "update"),
]

STUDY_CUTOFF = datetime(2026, 6, 9, 23, 59, 59, tzinfo=timezone.utc)


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"无法解析 {path} 第 {line_number} 行: {exc}") from exc
            if isinstance(value, dict) and len(value) == 1:
                inner = next(iter(value.values()))
                if isinstance(inner, dict):
                    yield inner
                    continue
            if isinstance(value, dict):
                yield value


def normalize_smell_type(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = SMELL_ALIASES.get(value, value)
    return normalized if normalized in FORMAL_SMELL_TYPES else None


def normalize_trigger(value: Any) -> str:
    text = str(value).strip().lower()
    if text in {"push", "pushes"}:
        return "push"
    if text in {"pull_request", "pull_request_target"}:
        return "pull_request"
    if text in {"schedule", "scheduled"}:
        return "schedule"
    if text in {"workflow_dispatch", "workflow_call"}:
        return "workflow_dispatch"
    if text in {"release", "published", "created"}:
        return "release"
    if text in TRIGGER_TYPES:
        return text
    return "other"


def _strip_inline_comment(value: str) -> str:
    """去掉不在引号中的 YAML 行内注释。"""

    quote: Optional[str] = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _inline_trigger_values(value: str) -> List[str]:
    value = _strip_inline_comment(value).strip()
    if not value or value in {"null", "~"}:
        return []
    if value.startswith("[") and "]" in value:
        body = value[1:value.rfind("]")]
        return [token.strip().strip("'\"") for token in body.split(",") if token.strip()]
    if value.startswith("{") and "}" in value:
        body = value[1:value.rfind("}")]
        return re.findall(r"(?:^|,)\s*['\"]?([A-Za-z0-9_-]+)['\"]?\s*:", body)
    return [value.strip().strip("'\"")]


def parse_triggers(path: Path) -> Tuple[Set[str], str]:
    """只解析顶层 ``on`` 字段，返回触发器集合和解析状态。

    交叉分布只需要触发器，不需要解析 YAML 的 jobs、steps 或表达式。这里
    使用顶层缩进扫描，避免对 130,000 余个完整 YAML 做昂贵的通用解析；
    仍然处理 YAML 中未加引号的 ``on``、数组、内联映射和多行映射形式。
    """

    if not path.exists():
        return set(), "missing"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return set(), "parse_error"

    on_index: Optional[int] = None
    on_indent = 0
    inline_value = ""
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(\s*)(?:['\"]on['\"]|on)\s*:\s*(.*)$", line)
        if match and not match.group(1):
            on_index = index
            on_indent = len(match.group(1))
            inline_value = match.group(2).strip()
            break

    if on_index is None:
        return set(), "unspecified"

    values = _inline_trigger_values(inline_value)
    if not values:
        child_indent: Optional[int] = None
        child_lines: List[str] = []
        for line in lines[on_index + 1:]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            indentationless_list_item = indent == on_indent and line.lstrip().startswith("-")
            if indent < on_indent or (indent == on_indent and not indentationless_list_item):
                break
            if child_indent is None:
                child_indent = indent
            if indent == child_indent:
                child_lines.append(line.strip())
        for child in child_lines:
            list_match = re.match(r"^-\s*['\"]?([A-Za-z0-9_-]+)['\"]?\s*$", child)
            if list_match:
                values.append(list_match.group(1))
                continue
            key_match = re.match(r"^(?:['\"]?)([A-Za-z0-9_-]+)(?:['\"]?)\s*:", child)
            if key_match:
                values.append(key_match.group(1))

    values = [value for value in values if value]
    triggers = {normalize_trigger(value) for value in values}
    return triggers, "parsed" if triggers else "unspecified"


def derive_dependency_ecosystem(detector_result: Dict[str, Any]) -> Set[str]:
    """提取当前检测器识别到的依赖生态，作为可选诊断字段。

    该字段仍然只反映当前检测器的高置信度依赖规则，不用于正式的广义生态
    交叉表；正式交叉表使用 repo_info.jsonl 的仓库主语言分类。
    """

    languages: Set[str] = set()
    jobs = ((detector_result.get("metadata") or {}).get("jobs") or {})
    if isinstance(jobs, dict):
        for job_data in jobs.values():
            analysis = (job_data or {}).get("analysis") or {}
            for language in analysis.get("dep_langs") or []:
                if isinstance(language, str):
                    languages.add(language.strip().lower())

    return {language for language in ("npm", "python") if language in languages}


def _ndc_manager_ecosystem(manager: Any) -> Optional[str]:
    if not isinstance(manager, str):
        return None
    return NDC_MANAGER_TO_ECOSYSTEM.get(manager.strip().lower())


def derive_ndc_eligible_ecosystems(detector_result: Dict[str, Any]) -> Set[str]:
    """提取 NDC 实际可评估的依赖生态。

    NDC 只针对当前检测器能够识别的依赖安装操作。这里排除预热缓存工作流、
    可能隐藏缓存行为的自定义 setup action，以及被检测器判定为纯辅助工具安装
    的 Job；因此该集合用于 NDC 专属机会分母，而不是一般生态交叉表的分母。
    """

    metadata = detector_result.get("metadata") or {}
    workflow_mode = metadata.get("workflow_mode") or {}
    if workflow_mode.get("is_warm_cache"):
        return set()

    eligible: Set[str] = set()
    jobs = metadata.get("jobs") or {}
    if not isinstance(jobs, dict):
        return eligible

    for job_data in jobs.values():
        analysis = (job_data or {}).get("analysis") or {}
        dependency_steps = analysis.get("dep_install_steps") or []
        if not dependency_steps or analysis.get("nested_actions"):
            continue

        is_native_build = any(
            _ACTIVE_CACHE_DETECTOR._NATIVE_BUILD_RE.search(content or "")
            for content in analysis.get("all_run_content") or []
        )
        for dependency_step in dependency_steps:
            ecosystem = _ndc_manager_ecosystem(
                dependency_step.get("manager", dependency_step.get("lang"))
            )
            if ecosystem is None:
                continue
            if not _ACTIVE_CACHE_DETECTOR._is_auxiliary_dependency_install(
                dependency_step.get("run", ""),
                is_native_build,
            ):
                eligible.add(ecosystem)

    return eligible


def derive_ndc_positive_ecosystems(detector_result: Dict[str, Any]) -> Set[str]:
    """提取 NDC 警告所涉及的 npm/Python 依赖生态。"""

    positive: Set[str] = set()
    for smell in detector_result.get("smells") or []:
        if not isinstance(smell, dict) or smell.get("type") != "no_dependency_cache":
            continue
        details = smell.get("details") or {}
        for manager in details.get("uncached_managers") or []:
            ecosystem = _ndc_manager_ecosystem(manager)
            if ecosystem is not None:
                positive.add(ecosystem)
    return positive


def normalize_main_language(main_language: Any) -> Tuple[str, str]:
    """将 GitHub 仓库主语言归并为主要生态类别。"""

    if not isinstance(main_language, str) or not main_language.strip():
        return "unknown", "Unknown"
    language = main_language.strip()
    ecosystem = LANGUAGE_TO_ECOSYSTEM.get(language.lower(), "other")
    return ecosystem, language


def _parse_created_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _repository_age_years(created_at: Any) -> Optional[float]:
    parsed = _parse_created_at(created_at)
    if parsed is None:
        return None
    age_days = max(0.0, (STUDY_CUTOFF - parsed).total_seconds() / 86400.0)
    return age_days / 365.2425


def _feature_bin(feature: str, value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    for label, lower, upper in REPOSITORY_FEATURE_BINS[feature]:
        if value >= lower and (upper is None or value < upper):
            return label
    return "unknown"


def load_repository_ecosystems(data_dir: Path) -> Dict[str, Dict[str, Any]]:
    """读取当前完整数据集中的仓库主语言和特征。"""

    path = data_dir / "repo_info.jsonl"
    repositories: Dict[str, Dict[str, str]] = {}
    if not path.exists():
        return repositories

    for item in iter_jsonl(path):
        repository = item.get("_id")
        repo_data = item.get("repo") or {}
        if not isinstance(repository, str) or not repository.strip():
            repository = repo_data.get("name") if isinstance(repo_data, dict) else None
        if not isinstance(repository, str) or not repository.strip():
            continue
        main_language = repo_data.get("mainLanguage") if isinstance(repo_data, dict) else None
        ecosystem, normalized_language = normalize_main_language(main_language)
        feature_values: Dict[str, Any] = {}
        for feature, (source, field) in REPOSITORY_FEATURE_SOURCE_FIELDS.items():
            source_data = item if source == "record" else repo_data
            feature_values[feature] = source_data.get(field) if isinstance(source_data, dict) else None
        feature_values["repository_age_years"] = _repository_age_years(repo_data.get("createdAt"))
        feature_bins = {
            feature: _feature_bin(feature, value)
            for feature, value in feature_values.items()
        }
        repositories[repository.strip().lower()] = {
            "ecosystem": ecosystem,
            "main_language": normalized_language,
            "feature_values": feature_values,
            "feature_bins": feature_bins,
        }
    return repositories


def resolve_workflow_path(data_dir: Path, record: Dict[str, Any]) -> Optional[Path]:
    value = record.get("yml_file")
    if not isinstance(value, str) or not value:
        return None

    candidates = [data_dir / value]
    relative = Path(value)
    if relative.parts and relative.parts[0] == "workflows":
        candidates.append(data_dir / Path(*relative.parts))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _read_workflow_text(path: Optional[Path]) -> Tuple[Optional[str], str]:
    """读取工作流文本作为分类证据。

    工作流已经通过当前扫描器的 YAML 解析并进入扫描结果，因此 RQ1 的功能
    分类不再重复执行一次通用 YAML 解析；只读取 Job ID 和显式 Job 名称，
    按来源研究的 Job-task 规则匹配。这样避免对 13 万余份文件重复构造 YAML
    对象。
    """

    if path is None or not path.exists():
        return None, "missing"
    try:
        return path.read_text(encoding="utf-8"), "read"
    except (OSError, UnicodeDecodeError):
        return None, "read_error"


def _extract_job_label_evidence(content: str) -> List[List[str]]:
    """提取每个 Job 的 ID 和显式 ``name``，与来源研究保持一致。"""

    jobs: List[List[str]] = []
    in_jobs = False
    jobs_indent: Optional[int] = None
    current_job: Optional[List[str]] = None

    for raw_line in content.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = _strip_inline_comment(raw_line).strip()
        if not line:
            continue
        indent = len(raw_line) - len(raw_line.lstrip())

        if not in_jobs:
            if re.match(r"^jobs\s*:\s*(?:$|[|>])", line, re.IGNORECASE):
                in_jobs = True
                jobs_indent = indent
            continue

        if jobs_indent is None:
            continue
        if indent <= jobs_indent:
            break

        if indent == jobs_indent + 2:
            match = re.match(r"^(?:['\"]?)([^:'\"]+)(?:['\"]?)\s*:", line)
            if match:
                current_job = [match.group(1).strip()]
                jobs.append(current_job)
            continue

        if current_job is not None and indent == jobs_indent + 4:
            match = re.match(r"^name\s*:\s*(.*)$", line, re.IGNORECASE)
            if match:
                value = match.group(1).strip().strip("'\"")
                if value:
                    current_job.append(value)

    return jobs


def _normalize_source_job_label(value: str) -> str:
    """按来源研究的规则小写化并移除括号中的矩阵/平台细节。"""

    normalized = str(value or "").lower()
    return re.sub(r"[（(].*?[）)]", "", normalized)


def _match_job_roles(
    job_labels: List[List[str]],
) -> Tuple[Set[str], Dict[str, List[str]], Counter, List[Set[str]]]:
    """按来源研究的简化 Job 名称匹配任务类别。

    来源研究将类别 token 作为 Job 名称的子串进行匹配。这里保留每个
    Job 的匹配集合，便于在工作流层面区分单一功能和复合功能。
    """

    role_counts: Counter = Counter()
    signals: Dict[str, List[str]] = {}
    job_role_sets: List[Set[str]] = []
    for labels in job_labels:
        evidence = " ".join(_normalize_source_job_label(value) for value in labels)
        matched: Set[str] = set()
        for token, role in SOURCE_JOB_TASK_TOKENS:
            if token in evidence:
                matched.add(role)
                signals.setdefault(role, [])
                if token not in signals[role]:
                    signals[role].append(f"job-token:{token}")

        if not matched:
            matched = {"other"}
            signals.setdefault("other", [])
            if "unmatched-job" not in signals["other"]:
                signals["other"].append("unmatched-job")

        job_role_sets.append(matched)
        for role in matched:
            role_counts[role] += 1
    return set(role_counts), signals, role_counts, job_role_sets


def _ordered_roles(roles: Iterable[str]) -> List[str]:
    """按来源类别顺序稳定输出多标签功能集合。"""

    return sorted(
        set(roles),
        key=lambda role: WORKFLOW_PRIMARY_ROLE_PRIORITY.index(role)
        if role in WORKFLOW_PRIMARY_ROLE_PRIORITY
        else len(WORKFLOW_PRIMARY_ROLE_PRIORITY),
    )


def classify_workflow_roles(
    path: Optional[Path],
    workflow_path: str,
    triggers: Set[str],
) -> Tuple[str, List[str], str, Dict[str, List[str]]]:
    """将来源研究的 Job 任务类别聚合到工作流层面。

    来源研究的分析单位是 Job，而本研究的 RQ1 分布单位是工作流。因此，
    一个工作流的 Job 只对应一个来源类别时，工作流继承该类别；包含多个
    不同来源类别时标记为 ``mixed``；所有 Job 均未匹配时标记为 ``other``。
    不使用工作流文件名、Step、Action、shell 命令或触发器推断类别。
    """

    content, status = _read_workflow_text(path)
    if content is None:
        return "unknown", ["unknown"], status, {}

    job_labels = _extract_job_label_evidence(content)
    if not job_labels:
        return "other", ["other"], status, {"other": ["no-job-label"]}

    job_roles, signals, _, job_role_sets = _match_job_roles(job_labels)
    recognized_roles = job_roles - {"other"}
    if not recognized_roles:
        return "other", ["other"], status, signals

    functional_roles = _ordered_roles(recognized_roles)
    if len(recognized_roles) == 1:
        return functional_roles[0], functional_roles, status, signals
    return "mixed", functional_roles, status, signals


def build_records(
    scan_path: Path,
    data_dir: Path,
    parse_triggers_enabled: bool = True,
    classify_workflow_enabled: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    records: List[Dict[str, Any]] = []
    counters = Counter()
    seen_keys: Set[str] = set()
    repository_ecosystems = load_repository_ecosystems(data_dir)
    counters["repository_metadata_records"] = len(repository_ecosystems)

    for record in iter_jsonl(scan_path):
        counters["scan_records_read"] += 1
        detector = ((record.get("detectors") or {}).get("cache_smell") or {})
        if detector.get("error"):
            counters["detector_error_records"] += 1
            continue

        repository = record.get("repository_name") or ""
        workflow_path = record.get("workflow_path") or ""
        workflow_key = record.get("workflow_key")
        if not isinstance(workflow_key, str) or not workflow_key:
            workflow_key = f"{repository}|{workflow_path}"
        if workflow_key in seen_keys:
            counters["duplicate_valid_workflow_keys"] += 1
            continue
        seen_keys.add(workflow_key)

        smell_types: Set[str] = set()
        for smell in detector.get("smells") or []:
            if isinstance(smell, dict):
                normalized = normalize_smell_type(smell.get("type"))
                if normalized:
                    smell_types.add(normalized)

        workflow_file = resolve_workflow_path(data_dir, record)
        if parse_triggers_enabled:
            triggers, trigger_status = parse_triggers(workflow_file) if workflow_file else (set(), "missing")
        else:
            triggers, trigger_status = set(), "not_requested"
        counters[f"trigger_{trigger_status}"] += 1
        if trigger_status == "parsed":
            counters["workflows_with_parsed_triggers"] += 1
        else:
            counters["workflows_without_known_triggers"] += 1

        repository_meta = repository_ecosystems.get(str(repository).strip().lower())
        if repository_meta is None:
            counters["workflows_without_repository_metadata"] += 1
            ecosystem = "unknown"
            main_language = "Unknown"
            feature_bins = {feature: "unknown" for feature in REPOSITORY_FEATURE_BINS}
        else:
            ecosystem = repository_meta["ecosystem"]
            main_language = repository_meta["main_language"]
            feature_bins = repository_meta["feature_bins"]
            if ecosystem == "unknown":
                counters["workflows_with_unknown_main_language"] += 1

        if classify_workflow_enabled:
            role_triggers = triggers
            if not parse_triggers_enabled:
                role_triggers, _ = parse_triggers(workflow_file) if workflow_file else (set(), "missing")
            primary_role, functional_roles, role_status, role_signals = classify_workflow_roles(
                workflow_file,
                workflow_path,
                role_triggers,
            )
            counters[f"workflow_role_{role_status}"] += 1
        else:
            primary_role = "unknown"
            functional_roles = ["unknown"]
            role_status = "not_requested"
            role_signals = {}

        records.append(
            {
                "workflow_key": workflow_key,
                "repository_name": repository,
                "workflow_path": workflow_path,
                "smells": smell_types,
                "triggers": triggers,
                "ecosystem": ecosystem,
                "repository_main_language": main_language,
                "dependency_ecosystems": sorted(derive_dependency_ecosystem(detector)),
                "ndc_eligible_ecosystems": sorted(derive_ndc_eligible_ecosystems(detector)),
                "ndc_positive_ecosystems": sorted(derive_ndc_positive_ecosystems(detector)),
                "feature_bins": feature_bins,
                "primary_role": primary_role,
                "functional_roles": functional_roles,
                "role_status": role_status,
                "role_signals": role_signals,
            }
        )

    counters["valid_workflows"] = len(records)
    counters["valid_repositories"] = len({r["repository_name"] for r in records if r["repository_name"]})
    return records, dict(counters)


def build_concentration_records_from_scan(
    scan_path: Path,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """仅从扫描结果构造仓库集中度所需记录，避免重复解析工作流 YAML。"""

    records: List[Dict[str, Any]] = []
    counters = Counter()
    seen_keys: Set[str] = set()
    for record in iter_jsonl(scan_path):
        counters["scan_records_read"] += 1
        detector = ((record.get("detectors") or {}).get("cache_smell") or {})
        if detector.get("error"):
            counters["detector_error_records"] += 1
            continue
        repository = record.get("repository_name") or ""
        workflow_path = record.get("workflow_path") or ""
        workflow_key = record.get("workflow_key")
        if not isinstance(workflow_key, str) or not workflow_key:
            workflow_key = f"{repository}|{workflow_path}"
        if workflow_key in seen_keys:
            counters["duplicate_valid_workflow_keys"] += 1
            continue
        seen_keys.add(workflow_key)
        smells: Set[str] = set()
        for smell in detector.get("smells") or []:
            if isinstance(smell, dict):
                normalized = normalize_smell_type(smell.get("type"))
                if normalized:
                    smells.add(normalized)
        records.append(
            {
                "workflow_key": workflow_key,
                "repository_name": repository,
                "smells": smells,
            }
        )
    counters["valid_workflows"] = len(records)
    counters["valid_repositories"] = len(
        {record["repository_name"] for record in records if record["repository_name"]}
    )
    return records, dict(counters)


def build_ecosystem_records_from_scan(
    scan_path: Path,
    data_dir: Path,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """从当前扫描结果和仓库元数据快速构造生态分布所需记录。

    生态拆分只需要扫描结果中的 workflow identity、仓库名和异味集合，
    不需要重新读取 13 万份 YAML。因此该路径用于更新生态拆分结果时，
    可以避免重复解析工作流内容，同时保持与完整扫描结果相同的去重和错误
    排除口径。
    """

    repository_ecosystems = load_repository_ecosystems(data_dir)
    records: List[Dict[str, Any]] = []
    counters = Counter()
    seen_keys: Set[str] = set()
    for record in iter_jsonl(scan_path):
        counters["scan_records_read"] += 1
        detector = ((record.get("detectors") or {}).get("cache_smell") or {})
        if detector.get("error"):
            counters["detector_error_records"] += 1
            continue
        repository = record.get("repository_name") or ""
        workflow_path = record.get("workflow_path") or ""
        workflow_key = record.get("workflow_key")
        if not isinstance(workflow_key, str) or not workflow_key:
            workflow_key = f"{repository}|{workflow_path}"
        if workflow_key in seen_keys:
            counters["duplicate_valid_workflow_keys"] += 1
            continue
        seen_keys.add(workflow_key)

        smells: Set[str] = set()
        for smell in detector.get("smells") or []:
            if isinstance(smell, dict):
                normalized = normalize_smell_type(smell.get("type"))
                if normalized:
                    smells.add(normalized)

        repository_meta = repository_ecosystems.get(str(repository).strip().lower())
        ecosystem = repository_meta["ecosystem"] if repository_meta else "unknown"
        records.append(
            {
                "repository_name": repository,
                "ecosystem": ecosystem,
                "smells": smells,
            }
        )

    counters["valid_workflows"] = len(records)
    counters["valid_repositories"] = len(
        {
            record.get("repository_name")
            for record in records
            if record.get("repository_name")
        }
    ) if records and "repository_name" in records[0] else 0
    return records, dict(counters)


def pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 4) if denominator else 0.0


def build_ndc_ecosystem_distribution(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按 NDC 支持的依赖生态计算专属机会分母和覆盖率。

    npm 与 Python 两行不是互斥类别；包含两类依赖的工作流会分别计入两行。
    """

    total = len(records)
    rows: List[Dict[str, Any]] = []
    for ecosystem in NDC_DEPENDENCY_ECOSYSTEMS:
        eligible = sum(
            ecosystem in record.get("ndc_eligible_ecosystems", [])
            for record in records
        )
        positive = sum(
            ecosystem in record.get("ndc_positive_ecosystems", [])
            for record in records
        )
        rows.append(
            {
                "dependency_ecosystem": ecosystem,
                "dependency_ecosystem_label": NDC_DEPENDENCY_ECOSYSTEM_LABELS[ecosystem],
                "detector_eligible_workflow_count": eligible,
                "ndc_workflow_count": positive,
                "ndc_conditional_pct": pct(positive, eligible),
                "ndc_pct_all_workflows": pct(positive, total),
                "eligible_pct_all_workflows": pct(eligible, total),
            }
        )

    return {
        "unit": "successfully scanned logical workflow",
        "workflow_count": total,
        "category_definition": (
            "detector-observable dependency-install ecosystems; npm-compatible and "
            "Python-compatible rows may overlap for mixed-ecosystem workflows"
        ),
        "eligibility_definition": (
            "at least one recognized non-auxiliary dependency-install operation, "
            "excluding warm-cache workflows and jobs containing review-only nested "
            "custom setup actions"
        ),
        "rows": rows,
    }


def build_non_ndc_ecosystem_distribution(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算排除 NDC 后的生态总体异味分布。

    这里的“任一非 NDC 异味”是其余正式异味集合的并集。所有生态类别都
    使用成功扫描工作流数作为分母，因此该指标可用于生态之间的描述性比较；
    它不把 NDC 的检测机会差异混入总体比例。
    """

    total = len(records)
    rows: List[Dict[str, Any]] = []
    for ecosystem in ECOSYSTEM_TYPES:
        eligible = sum(record["ecosystem"] == ecosystem for record in records)
        non_ndc_count = sum(
            record["ecosystem"] == ecosystem
            and any(smell in record["smells"] for smell in NON_NDC_SMELL_TYPES)
            for record in records
        )
        rows.append(
            {
                "ecosystem": ecosystem,
                "ecosystem_label": ECOSYSTEM_LABELS[ecosystem],
                "workflow_denominator": eligible,
                "non_ndc_smell_workflow_count": non_ndc_count,
                "non_ndc_smell_conditional_pct": pct(non_ndc_count, eligible),
                "non_ndc_smell_pct_all_workflows": pct(non_ndc_count, total),
            }
        )

    return {
        "unit": "successfully scanned logical workflow",
        "workflow_count": total,
        "excluded_smell_type": "no_dependency_cache",
        "included_smell_types": NON_NDC_SMELL_TYPES,
        "category_definition": (
            "mutually exclusive normalization of repo_info.jsonl repo.mainLanguage; "
            "the denominator is all successfully scanned workflows in each ecosystem"
        ),
        "rows": rows,
    }


def build_cooccurrence(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(records)
    counts = {
        smell: sum(smell in record["smells"] for record in records)
        for smell in FORMAL_SMELL_TYPES
    }
    exact_counts = Counter()
    for record in records:
        n = len(record["smells"])
        exact_counts[str(n if n <= 3 else "4+")] += 1

    pairs: List[Dict[str, Any]] = []
    for smell_a, smell_b in itertools.combinations(FORMAL_SMELL_TYPES, 2):
        co = sum(
            smell_a in record["smells"] and smell_b in record["smells"]
            for record in records
        )
        union = counts[smell_a] + counts[smell_b] - co
        pairs.append(
            {
                "smell_a": smell_a,
                "smell_b": smell_b,
                "cooccurrence_workflow_count": co,
                "cooccurrence_pct_all_workflows": pct(co, total),
                "jaccard": round(co / union, 6) if union else 0.0,
                "conditional_pct_b_given_a": pct(co, counts[smell_a]),
                "conditional_pct_a_given_b": pct(co, counts[smell_b]),
            }
        )

    pairs.sort(key=lambda row: (-row["cooccurrence_workflow_count"], row["smell_a"], row["smell_b"]))
    return {
        "unit": "successfully scanned logical workflow",
        "workflow_count": total,
        "smell_workflow_counts": counts,
        "smell_workflow_pct": {smell: pct(count, total) for smell, count in counts.items()},
        "exact_number_of_formal_smells": dict(sorted(exact_counts.items(), key=lambda item: item[0])),
        "pairwise": pairs,
    }


def build_context_distribution(
    records: List[Dict[str, Any]],
    dimension: str,
    categories: List[str],
) -> Dict[str, Any]:
    total = len(records)
    eligible_counts = {
        category: sum(category in record[dimension] if dimension == "triggers" else record[dimension] == category for record in records)
        for category in categories
    }

    rows: List[Dict[str, Any]] = []
    for category in categories:
        eligible = eligible_counts[category]
        any_count = sum(
            category in record[dimension] and bool(record["smells"])
            if dimension == "triggers"
            else record[dimension] == category and bool(record["smells"])
            for record in records
        )
        rows.append(
            {
                "category": category,
                "eligible_workflow_count": eligible,
                "any_smell_workflow_count": any_count,
                "any_smell_conditional_pct": pct(any_count, eligible),
                "any_smell_pct_all_workflows": pct(any_count, total),
            }
        )
        for smell in FORMAL_SMELL_TYPES:
            smell_count = sum(
                category in record[dimension] and smell in record["smells"]
                if dimension == "triggers"
                else record[dimension] == category and smell in record["smells"]
                for record in records
            )
            rows.append(
                {
                    "category": category,
                    "smell_type": smell,
                    "eligible_workflow_count": eligible,
                    "smell_workflow_count": smell_count,
                    "conditional_pct": pct(smell_count, eligible),
                    "pct_all_workflows": pct(smell_count, total),
                }
            )

    return {
        "unit": "successfully scanned logical workflow",
        "dimension": dimension,
        "category_definition": (
            "trigger categories are not mutually exclusive"
            if dimension == "triggers"
            else "ecosystem is a mutually exclusive normalization of repo_info.jsonl repo.mainLanguage; it is a repository-level ecosystem proxy rather than a claim that every workflow uses only one language"
        ),
        "workflow_count": total,
        "category_workflow_counts": eligible_counts,
        "rows": rows,
    }


def build_repository_feature_distributions(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按仓库特征分箱，计算工作流级异味分布。"""

    total = len(records)
    distributions: Dict[str, Any] = {}
    for feature, bins in REPOSITORY_FEATURE_BINS.items():
        categories = [label for label, _, _ in bins] + ["unknown"]
        eligible_counts = {
            category: sum(
                record["feature_bins"].get(feature) == category
                for record in records
            )
            for category in categories
        }
        rows: List[Dict[str, Any]] = []
        for category in categories:
            eligible = eligible_counts[category]
            in_category = [
                record for record in records
                if record["feature_bins"].get(feature) == category
            ]
            any_count = sum(bool(record["smells"]) for record in in_category)
            rows.append(
                {
                    "category": category,
                    "eligible_workflow_count": eligible,
                    "any_smell_workflow_count": any_count,
                    "any_smell_conditional_pct": pct(any_count, eligible),
                    "any_smell_pct_all_workflows": pct(any_count, total),
                }
            )
            for smell in FORMAL_SMELL_TYPES:
                smell_count = sum(smell in record["smells"] for record in in_category)
                rows.append(
                    {
                        "category": category,
                        "smell_type": smell,
                        "eligible_workflow_count": eligible,
                        "smell_workflow_count": smell_count,
                        "conditional_pct": pct(smell_count, eligible),
                        "pct_all_workflows": pct(smell_count, total),
                    }
                )

        distributions[feature] = {
            "label": REPOSITORY_FEATURE_LABELS[feature],
            "source": (
                "repo_info.jsonl repo metadata"
                if feature not in {"workflow_count", "runs_90d"}
                else "repo_info.jsonl record-level metadata"
            ),
            "bin_definition": [
                {"category": label, "lower_inclusive": lower, "upper_exclusive": upper}
                for label, lower, upper in bins
            ],
            "unknown_category": "unknown",
            "unit": "successfully scanned logical workflow",
            "workflow_count": total,
            "category_workflow_counts": eligible_counts,
            "rows": rows,
        }
    return distributions


def build_workflow_role_distributions(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算互斥主要功能和可重叠功能角色下的异味分布。"""

    total = len(records)
    distributions: Dict[str, Any] = {}
    for dimension, definition in (
        (
            "primary_role",
            "primary_role is mutually exclusive and selected by a fixed priority",
        ),
        (
            "functional_role",
            "functional_roles is multi-label; one workflow may contribute to several roles",
        ),
    ):
        rows: List[Dict[str, Any]] = []
        category_counts: Dict[str, int] = {}
        for role in WORKFLOW_ROLE_TYPES:
            if dimension == "primary_role":
                eligible_records = [
                    record for record in records if record.get("primary_role") == role
                ]
            else:
                eligible_records = [
                    record for record in records if role in (record.get("functional_roles") or [])
                ]
            eligible = len(eligible_records)
            category_counts[role] = eligible
            any_count = sum(bool(record.get("smells")) for record in eligible_records)
            rows.append(
                {
                    "role_type": role,
                    "role_label": WORKFLOW_ROLE_LABELS[role],
                    "eligible_workflow_count": eligible,
                    "any_smell_workflow_count": any_count,
                    "any_smell_conditional_pct": pct(any_count, eligible),
                    "any_smell_pct_all_workflows": pct(any_count, total),
                }
            )
            for smell in FORMAL_SMELL_TYPES:
                smell_count = sum(
                    smell in record.get("smells", set())
                    for record in eligible_records
                )
                rows.append(
                    {
                        "role_type": role,
                        "role_label": WORKFLOW_ROLE_LABELS[role],
                        "smell_type": smell,
                        "eligible_workflow_count": eligible,
                        "smell_workflow_count": smell_count,
                        "conditional_pct": pct(smell_count, eligible),
                        "pct_all_workflows": pct(smell_count, total),
                    }
                )

        distributions[dimension] = {
            "unit": "successfully scanned logical workflow",
            "dimension": dimension,
            "category_definition": definition,
            "workflow_count": total,
            "category_workflow_counts": category_counts,
            "rows": rows,
        }

    status_counts = Counter(record.get("role_status", "unknown") for record in records)
    return {
        "classification_version": "rq1_workflow_tasks_v1_bouzenia_pradel_job_name",
        "unit": "successfully scanned logical workflow",
        "workflow_count": total,
        "role_definitions": WORKFLOW_ROLE_LABELS,
        "primary_role_priority": WORKFLOW_PRIMARY_ROLE_PRIORITY,
        "classification_quality": dict(status_counts),
        "primary_role": distributions["primary_role"],
        "functional_role": distributions["functional_role"],
    }


def _quantile(values: List[float], q: float) -> float:
    """使用线性插值计算一个小型数值列表的分位数。"""

    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    value = ordered[lower] * (1 - weight) + ordered[upper] * weight
    return round(value, 6)


def _repository_smell_profiles(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """聚合每个仓库的工作流数量、异味工作流数量和异味比例。"""

    profiles: Dict[str, Dict[str, Any]] = {}
    for record in records:
        repository = record.get("repository_name") or "<unknown>"
        profile = profiles.setdefault(
            repository,
            {
                "workflow_count": 0,
                "smelly_workflow_count": 0,
                "smells": set(),
            },
        )
        profile["workflow_count"] += 1
        if record.get("smells"):
            profile["smelly_workflow_count"] += 1
            profile["smells"].update(record["smells"])
    for profile in profiles.values():
        profile["smell_fraction"] = (
            profile["smelly_workflow_count"] / profile["workflow_count"]
            if profile["workflow_count"]
            else 0.0
        )
    return profiles


def build_repository_concentration_distributions(
    records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """分析仓库级暴露与仓库内异味工作流集中程度。

    仓库覆盖率使用“仓库中至少一个工作流有异味”的 OR 聚合定义；工作流覆盖率
    使用所有成功扫描工作流作为分母。两者不应混为同一个 prevalence 指标。
    """

    profiles = _repository_smell_profiles(records)
    repository_count = len(profiles)
    workflow_count = len(records)
    smelly_workflow_count = sum(
        profile["smelly_workflow_count"] for profile in profiles.values()
    )
    exposed_profiles = [
        profile for profile in profiles.values() if profile["smelly_workflow_count"] > 0
    ]

    profile_rows: List[Dict[str, Any]] = []
    for label, predicate in (
        ("no_smelly_workflow", lambda p: p["smelly_workflow_count"] == 0),
        ("exactly_one_smelly_workflow", lambda p: p["smelly_workflow_count"] == 1),
        ("multiple_smelly_workflows", lambda p: p["smelly_workflow_count"] >= 2),
        ("partial_exposure", lambda p: 0 < p["smelly_workflow_count"] < p["workflow_count"]),
        ("all_workflows_smelly", lambda p: p["workflow_count"] > 0 and p["smelly_workflow_count"] == p["workflow_count"]),
    ):
        matching = [profile for profile in profiles.values() if predicate(profile)]
        profile_rows.append(
            {
                "profile": label,
                "repository_count": len(matching),
                "repository_pct": pct(len(matching), repository_count),
                "workflow_count": sum(profile["workflow_count"] for profile in matching),
                "smelly_workflow_count": sum(
                    profile["smelly_workflow_count"] for profile in matching
                ),
            }
        )

    def workflow_count_category(count: int) -> str:
        if count == 1:
            return "1"
        if count <= 3:
            return "2–3"
        if count <= 5:
            return "4–5"
        if count <= 10:
            return "6–10"
        if count <= 20:
            return "11–20"
        return ">20"

    workflow_count_groups: Dict[str, List[Dict[str, Any]]] = {}
    for profile in profiles.values():
        workflow_count_groups.setdefault(
            workflow_count_category(profile["workflow_count"]), []
        ).append(profile)
    workflow_count_order = ["1", "2–3", "4–5", "6–10", "11–20", ">20"]
    workflow_count_rows: List[Dict[str, Any]] = []
    for category in workflow_count_order:
        group = workflow_count_groups.get(category, [])
        group_workflows = sum(profile["workflow_count"] for profile in group)
        group_smelly_workflows = sum(
            profile["smelly_workflow_count"] for profile in group
        )
        group_exposed_repositories = sum(
            profile["smelly_workflow_count"] > 0 for profile in group
        )
        workflow_count_rows.append(
            {
                "observed_workflow_count_category": category,
                "repository_count": len(group),
                "exposed_repository_count": group_exposed_repositories,
                "repository_coverage_pct": pct(group_exposed_repositories, len(group)),
                "workflow_count": group_workflows,
                "smelly_workflow_count": group_smelly_workflows,
                "workflow_coverage_pct": pct(group_smelly_workflows, group_workflows),
                "mean_within_repository_smell_fraction": round(
                    sum(profile["smell_fraction"] for profile in group) / len(group), 6
                )
                if group
                else 0.0,
            }
        )

    smell_rows: List[Dict[str, Any]] = []
    for smell in FORMAL_SMELL_TYPES:
        per_smell = []
        for record in records:
            if smell not in record.get("smells", set()):
                continue
            per_smell.append(record.get("repository_name") or "<unknown>")
        per_smell_counts = Counter(per_smell)
        smell_exposed_profiles = [
            profile for profile in profiles.values() if smell in profile["smells"]
        ]
        one_workflow_repositories = sum(
            per_smell_counts.get(repository, 0) == 1
            for repository in per_smell_counts
        )
        multiple_workflow_repositories = sum(
            per_smell_counts.get(repository, 0) >= 2
            for repository in per_smell_counts
        )
        all_workflows_repositories = sum(
            per_smell_counts.get(repository, 0) == profile["workflow_count"]
            for repository, profile in profiles.items()
            if repository in per_smell_counts
        )
        partial_repositories = sum(
            0 < per_smell_counts.get(repository, 0) < profile["workflow_count"]
            for repository, profile in profiles.items()
            if repository in per_smell_counts
        )
        fractions = [
            per_smell_counts[repository] / profile["workflow_count"]
            for repository, profile in profiles.items()
            if repository in per_smell_counts and profile["workflow_count"]
        ]
        smell_rows.append(
            {
                "smell_type": smell,
                "repository_count": len(smell_exposed_profiles),
                "repository_coverage_pct": pct(len(smell_exposed_profiles), repository_count),
                "smelly_workflow_count": sum(per_smell_counts.values()),
                "workflow_coverage_pct": pct(sum(per_smell_counts.values()), workflow_count),
                "repositories_with_exactly_one_smelly_workflow": one_workflow_repositories,
                "repositories_with_multiple_smelly_workflows": multiple_workflow_repositories,
                "repositories_with_all_workflows_smelly": all_workflows_repositories,
                "repositories_with_partial_exposure": partial_repositories,
                "mean_within_exposed_repository_smell_fraction": round(
                    sum(fractions) / len(fractions), 6
                )
                if fractions
                else 0.0,
                "median_within_exposed_repository_smell_fraction": _quantile(fractions, 0.5),
            }
        )

    fraction_bins = [
        ("0", lambda fraction: fraction == 0),
        ("(0, 0.25]", lambda fraction: 0 < fraction <= 0.25),
        ("(0.25, 0.50]", lambda fraction: 0.25 < fraction <= 0.50),
        ("(0.50, 0.75]", lambda fraction: 0.50 < fraction <= 0.75),
        ("(0.75, 1)", lambda fraction: 0.75 < fraction < 1),
        ("1", lambda fraction: fraction == 1),
    ]
    fraction_rows = []
    for label, predicate in fraction_bins:
        matching = [
            profile for profile in profiles.values() if predicate(profile["smell_fraction"])
        ]
        fraction_rows.append(
            {
                "within_repository_smell_fraction_category": label,
                "repository_count": len(matching),
                "repository_pct": pct(len(matching), repository_count),
            }
        )

    return {
        "unit": "successfully scanned logical workflow and observed repository",
        "definitions": {
            "repository_exposure": "a repository is exposed if at least one successfully scanned workflow contains at least one formal smell",
            "workflow_exposure": "a workflow is exposed if it contains at least one formal smell",
            "within_repository_smell_fraction": "smelly workflow count divided by successfully scanned workflow count within the repository",
            "workflow_count_strata": "number of successfully scanned workflows observed per repository, not repo_info metadata workflow_count",
        },
        "repository_count": repository_count,
        "workflow_count": workflow_count,
        "exposed_repository_count": len(exposed_profiles),
        "repository_coverage_pct": pct(len(exposed_profiles), repository_count),
        "smelly_workflow_count": smelly_workflow_count,
        "workflow_coverage_pct": pct(smelly_workflow_count, workflow_count),
        "exposed_repository_within_workflow_fraction_summary": {
            "mean": round(
                sum(profile["smell_fraction"] for profile in exposed_profiles)
                / len(exposed_profiles),
                6,
            )
            if exposed_profiles
            else 0.0,
            "median": _quantile(
                [profile["smell_fraction"] for profile in exposed_profiles], 0.5
            ),
            "p25": _quantile(
                [profile["smell_fraction"] for profile in exposed_profiles], 0.25
            ),
            "p75": _quantile(
                [profile["smell_fraction"] for profile in exposed_profiles], 0.75
            ),
        },
        "repository_profile_rows": profile_rows,
        "workflow_count_strata_rows": workflow_count_rows,
        "within_repository_smell_fraction_rows": fraction_rows,
        "smell_rows": smell_rows,
    }


def write_repository_concentration_outputs(
    output_dir: Path,
    distributions: Dict[str, Any],
) -> None:
    """写出仓库集中度分析的长格式 CSV 结果。"""

    write_csv(
        output_dir / "rq1_repository_concentration_profiles.csv",
        distributions["repository_profile_rows"],
        [
            "profile",
            "repository_count",
            "repository_pct",
            "workflow_count",
            "smelly_workflow_count",
        ],
    )
    write_csv(
        output_dir / "rq1_repository_concentration_by_workflow_count.csv",
        distributions["workflow_count_strata_rows"],
        [
            "observed_workflow_count_category",
            "repository_count",
            "exposed_repository_count",
            "repository_coverage_pct",
            "workflow_count",
            "smelly_workflow_count",
            "workflow_coverage_pct",
            "mean_within_repository_smell_fraction",
        ],
    )
    write_csv(
        output_dir / "rq1_repository_concentration_by_smell.csv",
        distributions["smell_rows"],
        [
            "smell_type",
            "repository_count",
            "repository_coverage_pct",
            "smelly_workflow_count",
            "workflow_coverage_pct",
            "repositories_with_exactly_one_smelly_workflow",
            "repositories_with_multiple_smelly_workflows",
            "repositories_with_all_workflows_smelly",
            "repositories_with_partial_exposure",
            "mean_within_exposed_repository_smell_fraction",
            "median_within_exposed_repository_smell_fraction",
        ],
    )
    write_csv(
        output_dir / "rq1_repository_within_smell_fraction.csv",
        distributions["within_repository_smell_fraction_rows"],
        [
            "within_repository_smell_fraction_category",
            "repository_count",
            "repository_pct",
        ],
    )


def build_trigger_combinations(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts = Counter()
    for record in records:
        triggers = sorted(record["triggers"])
        for trigger_a, trigger_b in itertools.combinations(triggers, 2):
            counts[f"{trigger_a}+{trigger_b}"] += 1
    return [
        {"triggers": combination, "workflow_count": count}
        for combination, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="补充当前 RQ1 的异味共现、上下文、仓库特征和工作流功能角色分布"
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--scan-path", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--reuse-context",
        type=Path,
        help="复用当前已有结果中的触发器分布；生态、仓库特征和工作流角色仍重新计算",
    )
    parser.add_argument(
        "--concentration-only",
        action="store_true",
        help="仅从扫描结果快速更新仓库内异味集中度，不重复解析工作流 YAML",
    )
    parser.add_argument(
        "--ecosystem-split-only",
        action="store_true",
        help="仅从扫描结果快速更新非 NDC 与 NDC 分开的生态分布，不重复解析工作流 YAML",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.ecosystem_split_only:
        if args.reuse_context is None:
            parser.error("--ecosystem-split-only 需要同时提供 --reuse-context")
        with args.reuse_context.open("r", encoding="utf-8") as handle:
            output = json.load(handle)
        records, split_coverage = build_ecosystem_records_from_scan(
            args.scan_path,
            args.data_dir,
        )
        non_ndc_distribution = build_non_ndc_ecosystem_distribution(records)
        output["non_ndc_ecosystem_distribution"] = non_ndc_distribution
        output.setdefault("data_boundary", {})["non_ndc_ecosystem_distribution"] = {
            "scan_path": str(args.scan_path),
            "method": "scan-result and repo_info aggregation without re-parsing workflow YAML",
            "excluded_smell_type": "no_dependency_cache",
            "included_smell_types": NON_NDC_SMELL_TYPES,
        }
        output.setdefault("coverage", {}).update(
            {
                "ecosystem_split_scan_records_read": split_coverage.get(
                    "scan_records_read", 0
                ),
                "ecosystem_split_detector_error_records": split_coverage.get(
                    "detector_error_records", 0
                ),
                "ecosystem_split_duplicate_valid_workflow_keys": split_coverage.get(
                    "duplicate_valid_workflow_keys", 0
                ),
                "ecosystem_split_valid_workflows": split_coverage.get(
                    "valid_workflows", 0
                ),
            }
        )
        with (args.output_dir / "rq1_context_distributions.json").open(
            "w", encoding="utf-8"
        ) as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)
        write_csv(
            args.output_dir / "rq1_non_ndc_smell_ecosystem_distribution.csv",
            non_ndc_distribution["rows"],
            [
                "ecosystem",
                "ecosystem_label",
                "workflow_denominator",
                "non_ndc_smell_workflow_count",
                "non_ndc_smell_conditional_pct",
                "non_ndc_smell_pct_all_workflows",
            ],
        )
        print(
            json.dumps(
                {
                    "coverage": split_coverage,
                    "output": str(
                        args.output_dir / "rq1_non_ndc_smell_ecosystem_distribution.csv"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.concentration_only:
        if args.reuse_context is None:
            parser.error("--concentration-only 需要同时提供 --reuse-context")
        with args.reuse_context.open("r", encoding="utf-8") as handle:
            output = json.load(handle)
        concentration_records, concentration_coverage = build_concentration_records_from_scan(
            args.scan_path
        )
        concentration = build_repository_concentration_distributions(concentration_records)
        output["repository_concentration_distributions"] = concentration
        output.setdefault("data_boundary", {})["repository_concentration"] = {
            "scan_path": str(args.scan_path),
            "method": "scan-result aggregation without re-parsing workflow YAML",
        }
        output.setdefault("coverage", {}).update(
            {
                "repository_concentration_scan_records_read": concentration_coverage.get(
                    "scan_records_read", 0
                ),
                "repository_concentration_detector_error_records": concentration_coverage.get(
                    "detector_error_records", 0
                ),
                "repository_concentration_valid_workflows": concentration_coverage.get(
                    "valid_workflows", 0
                ),
                "repository_concentration_valid_repositories": concentration_coverage.get(
                    "valid_repositories", 0
                ),
            }
        )
        with (args.output_dir / "rq1_context_distributions.json").open(
            "w", encoding="utf-8"
        ) as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)
        write_repository_concentration_outputs(args.output_dir, concentration)
        print(
            json.dumps(
                {
                    "coverage": concentration_coverage,
                    "repository_concentration": {
                        "repository_count": concentration["repository_count"],
                        "workflow_count": concentration["workflow_count"],
                        "repository_coverage_pct": concentration["repository_coverage_pct"],
                        "workflow_coverage_pct": concentration["workflow_coverage_pct"],
                    },
                    "output": str(args.output_dir / "rq1_context_distributions.json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    records, coverage = build_records(
        args.scan_path,
        args.data_dir,
        parse_triggers_enabled=args.reuse_context is None,
    )
    cooccurrence = build_cooccurrence(records)
    if args.reuse_context is None:
        trigger_distribution = build_context_distribution(records, "triggers", TRIGGER_TYPES)
        trigger_distribution["combinations"] = build_trigger_combinations(records)
    else:
        with args.reuse_context.open("r", encoding="utf-8") as handle:
            previous_context = json.load(handle)
        trigger_distribution = previous_context["trigger_distribution"]
        previous_coverage = previous_context.get("coverage") or {}
        for key in list(coverage):
            if key.startswith("trigger_") or key in {
                "workflows_with_parsed_triggers",
                "workflows_without_known_triggers",
                "unknown_trigger_workflows",
            }:
                coverage.pop(key, None)
        for key, value in previous_coverage.items():
            if key.startswith("trigger_") or key in {
                "workflows_with_parsed_triggers",
                "workflows_without_known_triggers",
                "unknown_trigger_workflows",
            }:
                coverage[key] = value
    ecosystem_distribution = build_context_distribution(records, "ecosystem", ECOSYSTEM_TYPES)
    non_ndc_ecosystem_distribution = build_non_ndc_ecosystem_distribution(records)
    ndc_ecosystem_distribution = build_ndc_ecosystem_distribution(records)
    repository_feature_distributions = build_repository_feature_distributions(records)
    workflow_role_distributions = build_workflow_role_distributions(records)
    repository_concentration_distributions = build_repository_concentration_distributions(records)

    unknown_trigger_records = sum(
        coverage.get(f"trigger_{status}", 0)
        for status in ("missing", "parse_error", "unspecified")
    )
    output = {
        "analysis": "current_rq1_context_distributions",
        "data_boundary": {
            "data_dir": str(args.data_dir),
            "scan_path": str(args.scan_path),
            "current_detector_only": True,
            "formal_smell_types": FORMAL_SMELL_TYPES,
            "ecosystem_source": str(args.data_dir / "repo_info.jsonl"),
            "ecosystem_classification": "primary repository ecosystem normalized from repo.mainLanguage",
            "ecosystem_labels": ECOSYSTEM_LABELS,
            "ndc_ecosystem_classification": (
                "detector-observable dependency ecosystem derived from recognized npm/yarn/pnpm "
                "and pip/pipenv/poetry/uv installation operations"
            ),
            "ndc_eligibility_definition": ndc_ecosystem_distribution["eligibility_definition"],
            "ndc_ecosystem_labels": NDC_DEPENDENCY_ECOSYSTEM_LABELS,
            "non_ndc_ecosystem_distribution": {
                "excluded_smell_type": "no_dependency_cache",
                "included_smell_types": NON_NDC_SMELL_TYPES,
                "output": str(args.output_dir / "rq1_non_ndc_smell_ecosystem_distribution.csv"),
            },
            "repository_feature_labels": REPOSITORY_FEATURE_LABELS,
            "repository_feature_bins": {
                feature: [
                    {"category": label, "lower_inclusive": lower, "upper_exclusive": upper}
                    for label, lower, upper in bins
                ]
                for feature, bins in REPOSITORY_FEATURE_BINS.items()
            },
            "workflow_role_classification": {
                "version": workflow_role_distributions["classification_version"],
                "method": (
                    "adapted from Bouzenia and Pradel's job-task classification: lowercase "
                    "Job ID/name labels, remove parenthesized details, and match the "
                    "simplified task token as a substring"
                ),
                "primary_role_definition": (
                    "workflow-level aggregation of Job task categories; a single recognized "
                    "category is inherited, multiple recognized categories yield mixed, and "
                    "unmatched Jobs yield other when no recognized category exists"
                ),
                "functional_role_definition": (
                    "set of distinct recognized Job task categories in the workflow; this is "
                    "a workflow-level adaptation of the source Job analysis"
                ),
                "role_labels": WORKFLOW_ROLE_LABELS,
                "primary_role_priority": WORKFLOW_PRIMARY_ROLE_PRIORITY,
            },
            "repository_concentration": {
                "method": (
                    "aggregate successfully scanned workflows by repository; compare the "
                    "repository-level any-smell indicator with the workflow-level indicator, "
                    "and stratify by observed workflows per repository"
                ),
                "repository_exposure_definition": (
                    "at least one successfully scanned workflow in the repository contains "
                    "at least one formal smell"
                ),
                "within_repository_fraction_definition": (
                    "smelly successfully scanned workflows divided by successfully scanned "
                    "workflows in the repository"
                ),
            },
            "trigger_distribution_reused": str(args.reuse_context) if args.reuse_context else None,
        },
        "coverage": {
            **coverage,
            "unknown_trigger_workflows": unknown_trigger_records,
        },
        "cooccurrence": cooccurrence,
        "trigger_distribution": trigger_distribution,
        "ecosystem_distribution": ecosystem_distribution,
        "non_ndc_ecosystem_distribution": non_ndc_ecosystem_distribution,
        "ndc_ecosystem_distribution": ndc_ecosystem_distribution,
        "repository_feature_distributions": repository_feature_distributions,
        "workflow_role_distributions": workflow_role_distributions,
        "repository_concentration_distributions": repository_concentration_distributions,
    }

    json_path = args.output_dir / "rq1_context_distributions.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)

    write_csv(
        args.output_dir / "rq1_smell_cooccurrence.csv",
        cooccurrence["pairwise"],
        [
            "smell_a",
            "smell_b",
            "cooccurrence_workflow_count",
            "cooccurrence_pct_all_workflows",
            "jaccard",
            "conditional_pct_b_given_a",
            "conditional_pct_a_given_b",
        ],
    )
    write_csv(
        args.output_dir / "rq1_smell_trigger_distribution.csv",
        trigger_distribution["rows"],
        [
            "category",
            "smell_type",
            "eligible_workflow_count",
            "any_smell_workflow_count",
            "any_smell_conditional_pct",
            "any_smell_pct_all_workflows",
            "smell_workflow_count",
            "conditional_pct",
            "pct_all_workflows",
        ],
    )
    write_csv(
        args.output_dir / "rq1_smell_ecosystem_distribution.csv",
        ecosystem_distribution["rows"],
        [
            "category",
            "smell_type",
            "eligible_workflow_count",
            "any_smell_workflow_count",
            "any_smell_conditional_pct",
            "any_smell_pct_all_workflows",
            "smell_workflow_count",
            "conditional_pct",
            "pct_all_workflows",
        ],
    )
    write_csv(
        args.output_dir / "rq1_non_ndc_smell_ecosystem_distribution.csv",
        non_ndc_ecosystem_distribution["rows"],
        [
            "ecosystem",
            "ecosystem_label",
            "workflow_denominator",
            "non_ndc_smell_workflow_count",
            "non_ndc_smell_conditional_pct",
            "non_ndc_smell_pct_all_workflows",
        ],
    )
    write_csv(
        args.output_dir / "rq1_ndc_dependency_ecosystem_distribution.csv",
        ndc_ecosystem_distribution["rows"],
        [
            "dependency_ecosystem",
            "dependency_ecosystem_label",
            "detector_eligible_workflow_count",
            "ndc_workflow_count",
            "ndc_conditional_pct",
            "ndc_pct_all_workflows",
            "eligible_pct_all_workflows",
        ],
    )

    feature_rows: List[Dict[str, Any]] = []
    for feature, distribution in repository_feature_distributions.items():
        for row in distribution["rows"]:
            feature_rows.append(
                {
                    "feature": feature,
                    "feature_label": distribution["label"],
                    **row,
                }
            )
    write_csv(
        args.output_dir / "rq1_smell_repository_features.csv",
        feature_rows,
        [
            "feature",
            "feature_label",
            "category",
            "smell_type",
            "eligible_workflow_count",
            "any_smell_workflow_count",
            "any_smell_conditional_pct",
            "any_smell_pct_all_workflows",
            "smell_workflow_count",
            "conditional_pct",
            "pct_all_workflows",
        ],
    )

    role_rows: List[Dict[str, Any]] = []
    for dimension in ("primary_role", "functional_role"):
        distribution = workflow_role_distributions[dimension]
        for row in distribution["rows"]:
            role_rows.append(
                {
                    "role_dimension": dimension,
                    **row,
                }
            )
    write_csv(
        args.output_dir / "rq1_workflow_role_distribution.csv",
        role_rows,
        [
            "role_dimension",
            "role_type",
            "role_label",
            "smell_type",
            "eligible_workflow_count",
            "any_smell_workflow_count",
            "any_smell_conditional_pct",
            "any_smell_pct_all_workflows",
            "smell_workflow_count",
            "conditional_pct",
            "pct_all_workflows",
        ],
    )

    write_csv(
        args.output_dir / "rq1_repository_concentration_profiles.csv",
        repository_concentration_distributions["repository_profile_rows"],
        [
            "profile",
            "repository_count",
            "repository_pct",
            "workflow_count",
            "smelly_workflow_count",
        ],
    )
    write_csv(
        args.output_dir / "rq1_repository_concentration_by_workflow_count.csv",
        repository_concentration_distributions["workflow_count_strata_rows"],
        [
            "observed_workflow_count_category",
            "repository_count",
            "exposed_repository_count",
            "repository_coverage_pct",
            "workflow_count",
            "smelly_workflow_count",
            "workflow_coverage_pct",
            "mean_within_repository_smell_fraction",
        ],
    )
    write_csv(
        args.output_dir / "rq1_repository_concentration_by_smell.csv",
        repository_concentration_distributions["smell_rows"],
        [
            "smell_type",
            "repository_count",
            "repository_coverage_pct",
            "smelly_workflow_count",
            "workflow_coverage_pct",
            "repositories_with_exactly_one_smelly_workflow",
            "repositories_with_multiple_smelly_workflows",
            "repositories_with_all_workflows_smelly",
            "repositories_with_partial_exposure",
            "mean_within_exposed_repository_smell_fraction",
            "median_within_exposed_repository_smell_fraction",
        ],
    )
    write_csv(
        args.output_dir / "rq1_repository_within_smell_fraction.csv",
        repository_concentration_distributions["within_repository_smell_fraction_rows"],
        [
            "within_repository_smell_fraction_category",
            "repository_count",
            "repository_pct",
        ],
    )

    print(json.dumps({"coverage": output["coverage"], "output": str(json_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
