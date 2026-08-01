"""git-toolbox CLI 入口。

直接通过命令行调用本 skill 自带的工具脚本，无需 MCP 服务器。

用法：
    python -m scripts.cli <tool_name> --repo <repo_path> [--args '<json>']
    echo '<json>' | python -m scripts.cli <tool_name> --repo <repo_path>

示例：
    python -m scripts.cli git_log --repo D:\\linux\\linux_kernel_v7.2 --args '{\"range\":\"HEAD\",\"limit\":3}'
    python -m scripts.cli git_show --repo D:\\repo --args '{\"object\":\"HEAD:README.md\"}'

输出：单个 JSON 对象到 stdout（含 content/metadata/error 等字段）。
退出码：0 表示成功（含工具内部 error）；1 表示 CLI 参数/运行错误。
"""
import argparse
import json
import sys
from pathlib import Path

# 确保父目录在 sys.path 中，使 `from scripts.toolbox import ToolBox` 可用
_THIS_DIR = Path(__file__).resolve().parent          # .../scripts
_SKILL_DIR = _THIS_DIR.parent                        # .../git-toolbox
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from scripts.toolbox import ToolBox  # noqa: E402


def _read_args(args_json: str | None) -> dict:
    """优先 --args，其次 stdin，最后空 dict。"""
    if args_json:
        try:
            return json.loads(args_json)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[cli] --args 不是合法 JSON: {e}")
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise SystemExit(f"[cli] stdin 不是合法 JSON: {e}")
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.cli",
        description="直接调用 git-toolbox skill 的工具脚本",
        usage="python -m scripts.cli <tool_name> --repo <path> [--args '<json>']",
    )
    parser.add_argument("tool", help="工具名，如 git_log / git_show / git_diff ...")
    parser.add_argument("--repo", required=True, help="目标 Git 仓库绝对路径")
    parser.add_argument(
        "--args",
        default=None,
        help='工具参数(JSON 字符串)。也可通过 stdin 传入。例: \'{"range":"HEAD","limit":5}\'',
    )
    args = parser.parse_args()

    try:
        tool_args = _read_args(args.args)
    except SystemExit as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1

    repo_path = str(Path(args.repo).resolve())
    if not Path(repo_path).exists():
        print(json.dumps({"error": f"repo path does not exist: {repo_path}"}, ensure_ascii=False))
        return 0

    box = ToolBox()
    try:
        result = box.call(args.tool, tool_args, repo_path=repo_path)
    except Exception as e:  # 兜底：任何未预期异常都转成 JSON error
        result = {"error": f"cli execution failed: {e}"}

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
