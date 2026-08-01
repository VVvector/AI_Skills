import logging

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


class Truncator:
    @staticmethod
    def truncate_diff(diff: str, max_tokens: int, label: str) -> dict:
        estimated = _estimate_tokens(diff)
        if estimated <= max_tokens:
            return {"content": diff, "truncated": False}

        max_chars = max_tokens * 4
        lines = diff.split("\n")
        total_lines = len(lines)

        allowed_lines = max_chars // 50

        if total_lines <= allowed_lines:
            kept = diff[:max_chars]
            return {
                "content": f"{kept}\n... [Output truncated. Content too large ({estimated} tokens). Displaying first {max_chars} chars] ...\n",
                "truncated": True,
            }

        keep_top = allowed_lines // 2
        keep_bottom = allowed_lines // 2

        if keep_top + keep_bottom >= total_lines:
            kept = diff[:max_chars]
            return {
                "content": f"{kept}\n... [Output truncated. Content too large. Displaying first {max_chars} chars] ...\n",
                "truncated": True,
            }

        result_lines = []
        result_lines.extend(lines[:keep_top])

        dropped = total_lines - (keep_top + keep_bottom)
        result_lines.append(
            f"\n... [{label} truncated. Dropped {dropped} lines (lines {keep_top + 1}-{total_lines - keep_bottom})] ...\n"
        )

        result_lines.extend(lines[total_lines - keep_bottom:])

        result = "\n".join(result_lines)

        if _estimate_tokens(result) > max_tokens:
            kept = result[:max_chars]
            return {
                "content": f"{kept}\n... [Output truncated after line filtering. Original size: {estimated} tokens] ...\n",
                "truncated": True,
            }

        return {"content": result, "truncated": True}

    @staticmethod
    def truncate_sequential(content: str, max_tokens: int) -> dict:
        estimated = _estimate_tokens(content)
        if estimated <= max_tokens:
            lines = content.split("\n")
            return {"content": content, "lines_kept": len(lines), "truncated": False}

        lines = content.split("\n")
        total_lines = len(lines)

        low = 0
        high = total_lines
        best_keep = 0

        while low <= high:
            mid = (low + high) // 2
            candidate = "\n".join(lines[:mid])
            cand_tokens = _estimate_tokens(candidate)

            if cand_tokens <= max_tokens:
                best_keep = mid
                low = mid + 1
            else:
                high = mid - 1

        if best_keep == 0:
            max_chars = max_tokens * 4
            kept = content[:max_chars]
            return {
                "content": f"{kept}\n... [Output truncated. Content too large ({estimated} tokens). Displaying first {max_chars} chars] ...\n",
                "lines_kept": 0,
                "truncated": True,
            }

        result = "\n".join(lines[:best_keep]) + "\n"

        warning = f"... [Output truncated. Dropped {total_lines - best_keep} lines. Original size: {estimated} tokens] ...\n"

        while best_keep > 0:
            candidate = result + warning
            if _estimate_tokens(candidate) <= max_tokens:
                return {
                    "content": candidate,
                    "lines_kept": best_keep,
                    "truncated": True,
                }
            best_keep -= 1
            result = "\n".join(lines[:best_keep])
            if best_keep > 0:
                result += "\n"

        max_chars = max_tokens * 4
        kept = content[:max_chars]
        return {
            "content": f"{kept}\n... [Output truncated. Content too large ({estimated} tokens). Displaying first {max_chars} chars] ...\n",
            "lines_kept": 0,
            "truncated": True,
        }