"""Resolve recorded workspace paths without rewriting frozen experiment files.

Both former search/ and fetch/ locations, including their intermediate
experiment/ locations, map to the current route-specific directories. Mapping
is deterministic and limited to this checkout. Callers still verify existence
and content hashes; no output files are inspected to infer a destination.
"""

from pathlib import Path
import os
from typing import Optional, Union


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
PathValue = Union[str, os.PathLike]
_TOPIC = Path("experiment/01_topic")
_KEYWORD = Path("experiment/02_keyword")

# Specific output roots override the former stage's generic destination. Use
# path components, not string prefixes: output/v2-extra is not output/v2.
_OUTPUT_PREFIXES = (
    (Path("search/output/ai-agents-demo"), _TOPIC / "output/ai-agents-demo"),
    (Path("fetch/output/ai-agents-readmes-20261031"), _TOPIC / "output/ai-agents-readmes-20261031"),
    (Path("search/output/awesome-seeds-20261031"), _KEYWORD / "output/awesome-seeds-20261031"),
    (Path("search/output/v2"), _KEYWORD / "output/v2"),
    (Path("search"), _TOPIC),
    (Path("fetch"), _KEYWORD),
)
_OUTPUT_PREFIXES = tuple(sorted(_OUTPUT_PREFIXES, key=lambda item: len(item[0].parts), reverse=True))

# Renamed entrypoints are exact matches, so evidence files that happen to share
# a script's basename retain their enclosing output directory.
_FILE_RELOCATIONS = {
    "search/01_collect_topic_repositories.py": _TOPIC / "collect_repositories.py",
    "search/02_classify_agent_repositories.py": _TOPIC / "classify_metadata.py",
    "search/03_calibrate_recall_threshold.py": _TOPIC / "calibrate_recall.py",
    "search/04_compare_threshold_confidence.py": _TOPIC / "compare_thresholds.py",
    "search/plot_jev_score_ecdf.py": _TOPIC / "plot_scores.py",
    "search/plot_repository_stars.py": _TOPIC / "plot_stars.py",
    "search/plot_repository_timeline.py": _TOPIC / "plot_timeline.py",
    "search/05_extract_awesome_candidates.py": _KEYWORD / "extract_awesome_candidates.py",
    "search/06_analyze_awesome_metadata.py": _KEYWORD / "analyze_awesome_metadata.py",
    "search/07_collect_readme_repositories.py": _KEYWORD / "collect_repositories.py",
    "search/08_verify_readme_collection.py": _KEYWORD / "verify_collection.py",
    "search/METHOD.md": _KEYWORD / "METHOD.md",
    "search/discovery-v2.md": _KEYWORD / "METHOD.md",
    "fetch/01_fetch_repository_readmes.py": Path("system/experiment_tools/fetch_readmes.py"),
    "fetch/02_run_readme_collection.py": Path("system/experiment_tools/run_readme_collection.py"),
    "fetch/03_pilot_readme_prescreen.py": _KEYWORD / "prescreen_metadata.py",
    "fetch/04_evaluate_readme_prescreen.py": _KEYWORD / "evaluate_prescreen.py",
    "fetch/05_plot_prescreen_risk_heatmap.py": _KEYWORD / "plot_prescreen_risk.py",
    "fetch/06_fine_screen_readmes.py": _KEYWORD / "fine_screen.py",
    "fetch/07_evaluate_jev_cascade.py": _KEYWORD / "evaluate_cascade.py",
    "fetch/08_evaluate_fine_screen_audit.py": _KEYWORD / "evaluate_reference.py",
    "fetch/plot_screening_tradeoff.py": _KEYWORD / "plot_screening_tradeoff.py",
}


# Numbering changes only the route-root entrypoints, never similarly named evidence files.
_NUMBERED_SCRIPTS = {}
for _route, _names in (
    (_TOPIC, (
        "collect_repositories.py",
        "classify_metadata.py",
        "calibrate_recall.py",
        "compare_thresholds.py",
        "plot_scores.py",
        "plot_stars.py",
        "plot_timeline.py",
    )),
    (_KEYWORD, (
        "collect_repositories.py",
        "verify_collection.py",
        "prescreen_metadata.py",
        "fine_screen.py",
        "evaluate_prescreen.py",
        "evaluate_cascade.py",
        "evaluate_reference.py",
        "plot_prescreen_risk.py",
        "plot_screening_tradeoff.py",
        "extract_awesome_candidates.py",
        "analyze_awesome_metadata.py",
    )),
):
    for _number, _name in enumerate(_names, 1):
        _NUMBERED_SCRIPTS[_route / _name] = _route / f"{_number:02d}_{_name}"

def _relocate(relative: Path) -> Path:
    """Apply known migrations to a checkout-relative path, without file reads."""
    if relative.parts[:2] in (("experiment", "search"), ("experiment", "fetch")):
        relative = Path(*relative.parts[1:])
    exact = _FILE_RELOCATIONS.get(relative.as_posix())
    if exact is not None:
        return exact
    if (len(relative.parts) == 2 and relative.parts[0] == "fetch"
            and relative.name.endswith(".json") and "policy" in relative.name):
        return _KEYWORD / "policies" / relative.name
    for former, current in _OUTPUT_PREFIXES:
        try:
            suffix = relative.relative_to(former)
        except ValueError:
            continue
        return current / suffix
    return relative


def resolve_recorded_path(value: PathValue, base: Optional[PathValue] = None) -> Path:
    """Return a readable current path for an old record or a normal input path.

    Relative inputs retain their usual working-directory behavior unless an
    explicit base is supplied. Mapping does not require the old path to exist.
    It does not modify the record, filesystem, or hash-verification rules.
    """
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = Path(base).expanduser() / path
    # Resolve existing ancestors even when the recorded leaf no longer exists.
    # On macOS the same checkout may be addressed through /var or /private/var.
    path = path.resolve()
    workspace = WORKSPACE_ROOT.resolve()
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        return path.resolve()
    relocated = _relocate(relative)
    return (workspace / _NUMBERED_SCRIPTS.get(relocated, relocated)).resolve()
