"""English presentation text for the web tool, independent of detector prose.

Keep structured evidence and workflow-authored names verbatim. Select English
explanations by type and subtype rather than translating source code or YAML.
"""

import re


RULES = {
    "no_dependency_cache": ("No dependency cache", "Dependency operations have no matching cache configuration."),
    "no_fallback_cache": ("No fallback cache", "The cache has no effective fallback when the primary key misses."),
    "redundant_cache_update": ("Redundant cache update", "Concurrent jobs or matrix instances may write to the same cache key."),
    "cache_position_error": ("Cache position error", "Cache operations are ordered incorrectly relative to their inputs, producers, or consumers."),
    "overlapping_cache_mechanisms": ("Overlapping cache mechanisms", "Independent cache mechanisms manage overlapping directories."),
    "static_cache_key": ("Static cache key", "The cache key has no confirmed component that changes with dependency content."),
    "overly_specific_cache_key": ("Overly specific cache key", "The cache key contains dimensions that may unnecessarily limit reuse."),
    "no_docker_layer_cache": ("No Docker layer cache", "A Docker build has no usable input from a previous layer cache."),
    "deprecated_cache_action": ("Deprecated cache action", "The cache action version relies on a retired cache service."),
    "unreachable_cache_write": ("Unreachable cache write", "A cache save step cannot run under the workflow's conditions."),
    "cache_as_artifact": ("Cache as artifact", "A cache is used to transfer deterministic workflow artifacts."),
}

SUGGESTIONS = {
    "no_dependency_cache": "Configure a matching cache for the dependency operations listed in the evidence.",
    "no_fallback_cache": "Add restore-keys with broader prefixes than the primary cache key.",
    "redundant_cache_update": "Use distinct keys for independent writers, include relevant matrix dimensions, or serialize jobs with needs.",
    "cache_position_error": "Order cache operations around the related input, producer, and consumer steps shown in the evidence.",
    "overlapping_cache_mechanisms": "Use one cache mechanism per directory, or separate the managed paths. Disable built-in setup caching if a manual cache is needed.",
    "static_cache_key": "Include a dependency-content hash such as hashFiles in the key. Fixed versions and OS or architecture partitions do not invalidate caches automatically.",
    "overly_specific_cache_key": "Remove unnecessary per-run identifiers and use dependency-content hashes or stable compatibility dimensions instead.",
    "no_docker_layer_cache": "Configure cache-from (or --cache-from) for the affected build. A local cache source must point to a previously restored directory. Configure cache-to separately when exporting updates.",
    "deprecated_cache_action": "Upgrade to a version supporting the new cache service: at least v3.4.0 for v3 or v4.2.0 for v4. Verify pinned commits and runner compatibility.",
    "unreachable_cache_write": "Make the save condition compatible with workflow triggers and job conditions, or remove the unreachable save step.",
    "cache_as_artifact": "Use actions/upload-artifact and actions/download-artifact to transfer deterministic build outputs within a workflow run.",
}

POSITION_TEXT = {
    "before_cache_input_producer": ("A cache input references an output produced by a later step.", "Move the input-producing step before the cache operation, or change the input source."),
    "before_checkout": ("The cache requires repository files but runs before checkout.", "Check out the repository before the cache or setup step that needs its files."),
    "after_cache_producer": ("The cache is restored after its contents have already been generated.", "Restore the cache before the corresponding content-producing step."),
    "after_dependency_install": ("The cache is restored after the matching dependencies are installed.", "Restore the dependency cache before the associated installation step."),
    "no_consumer": ("The cache is restored after an identified consumer, with no later consumer found.", "Move restoration before the identified consumer and verify that subsequent steps use the restored files."),
}


def finding_text(name, smell):
    """Use structured distinctions without changing research detector outputs."""
    details = smell.details
    message = RULES[name][1]
    suggestion = SUGGESTIONS[name]
    if name == 'cache_position_error':
        message, suggestion = POSITION_TEXT.get(details.get('error_type'), (message, suggestion))
    elif name == 'no_fallback_cache' and details.get('subtype') == 'ineffective_restore_key':
        message = 'The restore-keys entries repeat the primary key without an effective broader prefix.'
        suggestion = 'Replace duplicate restore keys with broader primary-key prefixes.'
    elif name == 'overly_specific_cache_key' and details.get('workflow_commit_evidence') and not details.get('runtime_key_sources'):
        message = 'A dependency-download cache is partitioned by workflow commit SHA, which may unnecessarily fragment reuse.'
        suggestion = 'Remove workflow_sha from the dependency-download cache key while retaining necessary compatibility dimensions and dependency-content hashes.'
    elif name == 'redundant_cache_update' and details.get('conflict_type') == 'matrix_without_var':
        message = 'Multiple matrix instances may write to the same cache key.'
        suggestion = 'Include matrix dimensions that affect cache contents in the cache key.'
    elif name == 'overlapping_cache_mechanisms' and details.get('conflict_reason') == 'overlapping_manual_caches':
        message = 'Two independent manual caches manage overlapping directories.'
        suggestion = 'Consolidate these caches or separate their paths into non-overlapping resources.'
    # Keep existing English detector explanations when available.
    if smell.message and not re.search(r'[\u3400-\u9fff]', smell.message):
        message = smell.message
    if smell.suggestion and not re.search(r'[\u3400-\u9fff]', smell.suggestion):
        suggestion = smell.suggestion
    return message, suggestion


def step_label(value):
    """Translate only the detector's synthetic label; preserve user step names."""
    if value:
        return re.sub(r'^缓存步骤 \(index: (\d+)\)$', r'Cache step (index: \1)', value)
    return value
