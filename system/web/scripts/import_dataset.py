#!/usr/bin/env python3
"""导入网站候选数据，或仅同步本地README；不会启动模型或网络采集。"""

import argparse
import json
from pathlib import Path
import sys


WEB_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WEB_ROOT.parents[1]
sys.path.insert(0, str(WEB_ROOT / "backend"))

from agendex.db import DEFAULT_DB_PATH  # noqa: E402
from agendex.importer import import_dataset, import_keyword_dataset, import_fine_dataset, KEYWORD_SCORING_ROOT, FINE_SCREEN_ROOT  # noqa: E402
from agendex.evidence import sync_readme_collection  # noqa: E402


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--dataset", choices=("fine", "keyword", "topic"), default="fine",
                         help="默认fine激活精筛目录；keyword导入初筛来源，topic兼容旧模式")
    command.add_argument("--source", type=Path, help="仓库JSONL，默认fine的kept.jsonl")
    command.add_argument("--classifications", type=Path, help="默认fine的scores.jsonl；其他模式使用各自冻结分数")
    command.add_argument("--database", type=Path, default=DEFAULT_DB_PATH, help="网站 SQLite 数据库")
    command.add_argument("--readmes", type=Path,
                         help="仅追加已有README采集目录或readmes.jsonl，不重导仓库、不调用模型")
    command.add_argument("--threshold", type=float, help="仅topic模式可设，默认0.69；keyword沿用已冻结的保留成员资格")
    return command


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.readmes:
            if any(value is not None for value in (arguments.source, arguments.classifications, arguments.threshold)):
                raise ValueError("README同步不能同时指定仓库导入或阈值参数")
            result = sync_readme_collection(arguments.readmes, arguments.database)
        elif arguments.dataset == "fine":
            if arguments.threshold is not None:
                raise ValueError("fine模式使用已完成精筛的冻结阈值，不接受重新设置")
            result = import_fine_dataset(arguments.source or FINE_SCREEN_ROOT / "kept.jsonl",
                                         arguments.classifications or FINE_SCREEN_ROOT / "scores.jsonl",
                                         arguments.database)
        elif arguments.dataset == "keyword":
            if arguments.threshold is not None:
                raise ValueError("keyword模式按保留清单导入，不接受重新设置数值阈值")
            result = import_keyword_dataset(arguments.source or KEYWORD_SCORING_ROOT / "kept-augmented.jsonl",
                                            arguments.classifications or KEYWORD_SCORING_ROOT / "scores.jsonl",
                                            arguments.database)
        else:
            source_root = PROJECT_ROOT / "experiment" / "01_topic" / "output" / "ai-agents-demo"
            result = import_dataset(arguments.source or source_root / "repositories.jsonl",
                                    arguments.classifications or source_root / "classification-jev" / "classifications.jsonl",
                                    arguments.database, .69 if arguments.threshold is None else arguments.threshold)
    except (ValueError, OSError) as exc:
        print("导入失败：{}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("sync_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
