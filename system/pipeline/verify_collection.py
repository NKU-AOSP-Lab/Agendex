#!/usr/bin/env python3
"""Independent, offline verification of a completed Agendex Search v2 dataset.

This script does not import the collector, read credentials, or contact GitHub.
Its only write is the JSON report explicitly requested on the command line.
"""

import argparse
import importlib.util
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys


# Keep recorded legacy paths readable without changing frozen experiment files.
if "agendex_workspace_paths" not in sys.modules:
    _WORKSPACE_PATHS_SPEC = importlib.util.spec_from_file_location(
        "agendex_workspace_paths", Path(__file__).resolve().parents[2] / "system" / "workspace_paths.py")
    if _WORKSPACE_PATHS_SPEC is None or _WORKSPACE_PATHS_SPEC.loader is None:
        raise ImportError("Cannot load workspace path resolver")
    WORKSPACE_PATHS = importlib.util.module_from_spec(_WORKSPACE_PATHS_SPEC)
    sys.modules[_WORKSPACE_PATHS_SPEC.name] = WORKSPACE_PATHS
    _WORKSPACE_PATHS_SPEC.loader.exec_module(WORKSPACE_PATHS)
WORKSPACE_PATHS = sys.modules["agendex_workspace_paths"]
resolve_recorded_path = WORKSPACE_PATHS.resolve_recorded_path


AUTHORIZED_QUERIES = (
    "agent in:readme stars:>=1000 fork:true is:public",
    "assistant in:readme stars:>=1000 fork:true is:public",
)
PAGE_SIZE = 100
SEARCH_LIMIT = 1000


class AuditFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AuditFailure(message)


def integer(value):
    return type(value) is int


def canonical(value):
    """Compare JSON types and values while allowing harmless object-key order changes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def timestamp(value, label):
    require(isinstance(value, str), label + ": timestamp is not a string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise AuditFailure(label + ": invalid timestamp") from error
    require(result.tzinfo is not None, label + ": timestamp has no timezone")
    return result


def query_scope(query):
    require(isinstance(query, str), "Journal query is not a string")
    match = re.fullmatch(r"(.+) created:(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\.\.(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", query)
    require(match is not None, "Journal query is not an exact authorized base plus a creation range")
    base, start, end = match.groups()
    require(base in AUTHORIZED_QUERIES, "Unauthorized base query: " + base)
    lower = int(timestamp(start, "query start").timestamp())
    upper = int(timestamp(end, "query end").timestamp())
    require(lower <= upper, "Journal query has a reversed range")
    return base, lower, upper


def read_json(path):
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def read_jsonl(path):
    raw = path.read_bytes()
    require(not raw or raw.endswith(b"\n"), str(path.name) + ": incomplete final line")
    values = []
    for number, line in enumerate(raw.splitlines(), 1):
        require(bool(line.strip()), str(path.name) + ": blank line " + str(number))
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError) as error:
            raise AuditFailure(str(path.name) + ": invalid JSON on line " + str(number)) from error
        require(isinstance(value, dict), str(path.name) + ": non-object on line " + str(number))
        values.append(value)
    return values


def relative_response_path(root, value):
    require(isinstance(value, str) and value.startswith("responses/"), "Invalid response path")
    path = (root / value).resolve()
    require(root in path.parents and path.is_file(), "Response missing or outside dataset: " + value)
    return path


def verify_response_body(root, event):
    path = relative_response_path(root, event.get("response_path"))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    require(digest == event.get("sha256"), "Response SHA256 mismatch: " + str(event.get("response_path")))
    return raw


def verify_repository(item, lower, upper, label):
    require(isinstance(item, dict), label + ": repository is not an object")
    identifier = item.get("id")
    require(integer(identifier) and identifier > 0, label + ": invalid repository ID")
    require(item.get("private") is False, label + ": repository is not public")
    stars = item.get("stargazers_count")
    require(integer(stars) and stars >= 1000, label + ": Star threshold violated")
    created = timestamp(item.get("created_at"), label + " created_at").timestamp()
    require(lower <= created <= upper, label + ": creation time outside response range")
    return identifier


def verify(root, report):
    state = read_json(root / "state.json")
    summary = read_json(root / "summary.json")
    require(state.get("dataset_version") == summary.get("dataset_version") == "v2", "Dataset version is not v2")
    require(state.get("status") == summary.get("status") == "plan_complete", "Dataset is not plan_complete")
    state_queries = state.get("queries")
    summary_queries = summary.get("queries")
    require(isinstance(state_queries, list) and [q.get("base_query") for q in state_queries] == list(AUTHORIZED_QUERIES), "State does not contain exactly the two authorized queries")
    require(isinstance(summary_queries, list) and [q.get("base_query") for q in summary_queries] == list(AUTHORIZED_QUERIES), "Summary queries differ from authorized queries")
    creation = state.get("creation_range")
    require(isinstance(creation, dict) and integer(creation.get("start")) and integer(creation.get("end")), "Invalid root creation range")
    lower, upper = creation["start"], creation["end"]
    require(lower <= upper, "Root creation range is reversed")
    require(summary.get("creation_range") == creation, "State and summary creation ranges differ")
    require(state.get("started_at") == summary.get("started_at"), "State and summary start times differ")
    require(state.get("updated_at") == summary.get("updated_at"), "State and summary completion times differ")
    started = timestamp(summary.get("started_at"), "started_at")
    completed = timestamp(summary.get("updated_at"), "updated_at")
    require(completed >= started, "Completion predates start")
    report["timing"] = {"started_at": summary["started_at"], "completed_at": summary["updated_at"],
                        "elapsed_seconds_including_pauses": round((completed - started).total_seconds(), 3)}
    report["creation_range"] = creation
    report["checks_passed"].append("authorized_queries_and_completion_state")

    journal = read_jsonl(root / "requests.jsonl")
    statuses = Counter(event.get("status") for event in journal)
    require(set(statuses).issubset({"ok", "http_error", "network_error", "invalid_response"}), "Journal contains unknown request status")
    events_by_path = {}
    payloads_by_path = {}
    first_objects = {}
    first_sources = {}
    observed = {base: set() for base in AUTHORIZED_QUERIES}
    successful_times = []
    response_bytes = 0
    for number, event in enumerate(journal, 1):
        base, start, end = query_scope(event.get("query"))
        require(lower <= start <= end <= upper, "Journal query range lies outside frozen root range")
        page = event.get("page")
        require(integer(page) and 1 <= page <= 10, "Invalid journal page number")
        timestamp(event.get("time"), "request time")
        if event.get("response_path") is not None:
            raw = verify_response_body(root, event)
            response_bytes += len(raw)
        else:
            require(event.get("status") in {"network_error", "invalid_response"}, "HTTP response is missing its raw body")
            raw = None
        if event.get("status") != "ok":
            continue
        require(raw is not None, "Successful response has no raw body")
        relative = event["response_path"]
        require(relative not in events_by_path, "A successful raw response is journaled more than once")
        payload = json.loads(raw)
        require(isinstance(payload, dict), "Successful raw response is not an object")
        total, incomplete, items = payload.get("total_count"), payload.get("incomplete_results"), payload.get("items")
        require(integer(total) and total >= 0, "Invalid raw total_count")
        require(type(incomplete) is bool, "Invalid raw incomplete_results")
        require(isinstance(items, list) and len(items) <= PAGE_SIZE, "Invalid raw items list")
        require(event.get("total_count") == total and event.get("incomplete_results") is incomplete, "Journal and raw response accounting differ")
        for item in items:
            identifier = verify_repository(item, start, end, "request " + str(number))
            first_objects.setdefault(identifier, item)
            observed[base].add(identifier)
            key = (base, identifier)
            first_sources.setdefault(key, {"repository_id": identifier, "base_query": base,
                "query": event["query"], "page": page, "response_path": relative,
                "response_sha256": event["sha256"], "observed_at": event["time"]})
        events_by_path[relative] = event
        payloads_by_path[relative] = payload
        successful_times.append(event["time"])
    report["requests"] = {"attempts": len(journal), "successful": statuses["ok"],
        "failed": len(journal) - statuses["ok"], "by_status": dict(statuses),
        "http_error_codes": dict(Counter(str(e.get("http_status")) for e in journal if e.get("status") == "http_error")),
        "first_successful_request_at": successful_times[0] if successful_times else None,
        "last_successful_request_at": successful_times[-1] if successful_times else None}
    report["checks_passed"].append("all_journaled_raw_hashes_and_successful_response_scope")

    validated = {}
    query_reports = []
    for entry, summary_entry in zip(state_queries, summary_queries):
        base = entry["base_query"]
        require(entry.get("pending") == [] and entry.get("unresolved") == [], "A query still has pending or unresolved work")
        require(summary_entry.get("pending_windows") == 0 and summary_entry.get("unresolved_windows") == [], "Summary reports unfinished query work")
        leaves = entry.get("completed")
        require(isinstance(leaves, list) and bool(leaves), "Query has no completed leaves")
        next_start = lower
        all_leaf_ids = set()
        leaf_total = 0
        for leaf in sorted(leaves, key=lambda item: item["start"]):
            start, end = leaf.get("start"), leaf.get("end")
            require(integer(start) and integer(end) and start == next_start and start <= end <= upper, "Completed leaf ranges overlap, have a gap, or exceed root range")
            next_start = end + 1
            expected_total = leaf.get("total_count")
            require(integer(expected_total) and 0 <= expected_total <= SEARCH_LIMIT, "Completed leaf total is invalid or capped")
            sources = leaf.get("responses")
            expected_pages = max(1, (expected_total + PAGE_SIZE - 1) // PAGE_SIZE)
            require(isinstance(sources, list) and len(sources) == expected_pages and len(set(sources)) == len(sources), "Completed leaf has a missing, duplicate or extra page")
            seen = set()
            for page, relative in enumerate(sources, 1):
                require(relative in events_by_path, "Completed leaf references a missing successful journal response")
                event, payload = events_by_path[relative], payloads_by_path[relative]
                event_base, event_start, event_end = query_scope(event["query"])
                require((event_base, event_start, event_end, event["page"]) == (base, start, end, page), "Completed page source/query/range/page mismatch")
                require(payload["incomplete_results"] is False and payload["total_count"] == expected_total, "Completed page is incomplete or its total drifted")
                ids = [item["id"] for item in payload["items"]]
                require(len(set(ids)) == len(ids) and not seen.intersection(ids), "Completed leaf contains duplicate IDs within/across pages")
                expected_page_size = min(PAGE_SIZE, max(0, expected_total - (page - 1) * PAGE_SIZE))
                require(len(ids) == expected_page_size, "Completed page item count differs from expected pagination")
                seen.update(ids)
            require(len(seen) == expected_total, "Completed leaf actual ID count differs from total_count")
            if "actual_count" in leaf:
                require(integer(leaf["actual_count"]) and leaf["actual_count"] == len(seen), "Completed leaf actual_count field differs from raw IDs")
            require(not all_leaf_ids.intersection(seen), "Repository appears in multiple creation leaves of the same query")
            all_leaf_ids.update(seen)
            leaf_total += expected_total
        require(next_start == upper + 1, "Completed leaves do not cover the full frozen root range")
        require(summary_entry.get("completed_windows") == len(leaves), "Summary completed leaf count differs")
        require(summary_entry.get("validated_leaf_total") == leaf_total == len(all_leaf_ids), "Summary validated leaf total differs from raw IDs")
        require(summary_entry.get("unique_repositories_observed") == len(observed[base]), "Summary observed query count differs from journal")
        if "root_total_count" in entry:
            require(summary_entry.get("initial_reported_total") == entry["root_total_count"], "Summary initial query count differs from state")
        validated[base] = all_leaf_ids
        query_reports.append({"base_query": base, "initial_reported_total": summary_entry.get("initial_reported_total"),
            "observed_unique_repositories": len(observed[base]), "validated_unique_repositories": len(all_leaf_ids),
            "observed_outside_validated_leaf_union": len(observed[base] - all_leaf_ids), "completed_leaves": len(leaves),
            "completed_page_responses": sum(len(leaf["responses"]) for leaf in leaves)})
    report["queries"] = query_reports
    report["checks_passed"].append("completed_leaf_coverage_and_every_page_count")

    exported = read_jsonl(root / "repositories.jsonl")
    exported_by_id = {}
    for item in exported:
        identifier = verify_repository(item, lower, upper, "repository export")
        require(identifier not in exported_by_id, "Duplicate repository export ID")
        exported_by_id[identifier] = item
    require(set(exported_by_id) == set(first_objects), "Repository export ID union differs from successful journal responses")
    for identifier, expected in first_objects.items():
        require(canonical(exported_by_id[identifier]) == canonical(expected), "Repository export changed the first raw object for ID " + str(identifier))
    require(state.get("repositories_saved") == summary.get("repositories_saved") == len(exported), "State/summary repository count differs from export")
    report["checks_passed"].append("repository_export_exact_first_raw_objects_and_unique_ids")

    provenance = read_jsonl(root / "provenance.jsonl")
    provenance_keys = set()
    for row in provenance:
        key = (row.get("base_query"), row.get("repository_id"))
        require(key in first_sources, "Provenance relation is absent from successful raw responses")
        require(key not in provenance_keys, "Duplicate provenance base-query/repository relation")
        require(canonical(row) == canonical(first_sources[key]), "Provenance differs from the first successful matching source")
        source_payload = payloads_by_path.get(row.get("response_path"))
        require(source_payload is not None and any(item["id"] == key[1] for item in source_payload["items"]), "Provenance source raw does not contain the repository")
        provenance_keys.add(key)
    require(provenance_keys == set(first_sources), "Provenance does not cover every observed base-query/repository relation")
    require(summary.get("provenance_relations") == len(provenance), "Summary provenance count differs")
    require(summary.get("successful_responses") == statuses["ok"], "Summary successful response count differs")
    report["checks_passed"].append("provenance_exact_first_matching_sources_and_complete_relations")

    observed_sets = list(observed.values())
    validated_sets = list(validated.values())
    report["repositories"] = {"unique_union": len(exported), "observed_query_intersection": len(observed_sets[0] & observed_sets[1]),
        "observed_query_union": len(observed_sets[0] | observed_sets[1]), "validated_query_intersection": len(validated_sets[0] & validated_sets[1]),
        "validated_query_union": len(validated_sets[0] | validated_sets[1]), "provenance_relations": len(provenance)}
    report["sizes_bytes"] = {name: (root / name).stat().st_size for name in (
        "repositories.jsonl", "provenance.jsonl", "requests.jsonl", "state.json", "summary.json")}
    report["sizes_bytes"]["journal_referenced_response_bodies"] = response_bytes
    response_paths = list((root / "responses").glob("*"))
    report["responses_directory"] = {"files": sum(path.is_file() for path in response_paths),
        "bytes": sum(path.stat().st_size for path in response_paths if path.is_file()),
        "journal_referenced_successful_files": len(events_by_path)}
    report["notes"] = ["Verification is offline and independent of the collector implementation.",
        "Observed union includes all successful discovery responses, including parent partitions; validated leaf counts are reported separately.",
        "Plan completion verifies the saved query plan and does not prove a simultaneous snapshot or coverage of all Agent implementations.",
        "Elapsed time includes any pauses between collector invocations."]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_directory", type=Path)
    parser.add_argument("report_path", type=Path)
    args = parser.parse_args()
    root = resolve_recorded_path(args.dataset_directory)
    output = resolve_recorded_path(args.report_path)
    report = {"audit_version": 1, "dataset_directory": str(root), "status": "failed", "checks_passed": [],
              "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # An unsafe output argument must not be opened, even to record a failed audit.
    protected = {root / name for name in ("state.json", "summary.json", "repositories.jsonl", "provenance.jsonl", "requests.jsonl")}
    if output in protected or (root / "responses") in output.parents:
        print("Refusing report path that would overwrite source evidence", file=sys.stderr)
        return 2
    try:
        require(root.is_dir(), "Dataset directory is missing")
        verify(root, report)
        report["status"] = "passed"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    try:
        with output.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except OSError as error:
        print("Could not write audit report: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "report_path": str(output),
                      "repositories": report.get("repositories"), "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
