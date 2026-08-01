import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts import (
    ToolBox,
    Truncator,
    GitToolContext,
    validate_path,
    glob_to_regex,
    format_git_grep_output,
    get_priority_score,
)


def _create_test_repo():
    tmpdir = tempfile.TemporaryDirectory()
    repo_path = Path(tmpdir.name)

    def run_git(args):
        subprocess.run(
            ["git"] + args,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )

    run_git(["init"])
    run_git(["config", "user.name", "Test User"])
    run_git(["config", "user.email", "test@example.com"])
    run_git(["commit", "--allow-empty", "-m", "Initial commit"])

    (repo_path / "README.md").write_text(
            "# Test Project\n" + "TestProject\n" + "\n".join([f"line {i}" for i in range(1, 200)]) + "\n",
            encoding="utf-8",
        )
    (repo_path / "src").mkdir(exist_ok=True)
    (repo_path / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_path / "src" / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
    (repo_path / "src" / "worker").mkdir(exist_ok=True)
    (repo_path / "src" / "worker" / "prompts.py").write_text(
        "# " + "\n".join([f"line {i}" for i in range(3000)]), encoding="utf-8"
    )

    run_git(["add", "."])
    run_git(["commit", "-m", "Add project files"])

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    ).stdout.strip()

    return tmpdir, repo_path, sha


class TestTruncator(unittest.TestCase):
    def test_truncate_diff(self):
        diff = "line1\nline2\nline3\nline4\nline5\nline6"
        res = Truncator.truncate_diff(diff, 5, "Diff")
        self.assertTrue("Diff truncated" in res["content"])
        self.assertTrue(res["truncated"])

    def test_truncate_diff_long_line(self):
        long_line = "a" * 1000
        res = Truncator.truncate_diff(long_line, 20, "Diff")
        self.assertTrue(len(res["content"]) < 300)
        self.assertTrue("Output truncated" in res["content"])
        self.assertTrue(res["content"].startswith("aaaa"))
        self.assertTrue(res["truncated"])

    def test_truncate_sequential(self):
        content = "\n".join([f"line {i}" for i in range(100)])
        res = Truncator.truncate_sequential(content, 50)
        self.assertTrue("line 0" in res["content"])
        self.assertTrue("Output truncated. Dropped" in res["content"])
        self.assertFalse("line 99" in res["content"])
        self.assertTrue(res["truncated"])
        self.assertTrue(res["lines_kept"] > 0)
        self.assertTrue(res["lines_kept"] < 100)

    def test_truncate_diff_precise_range(self):
        diff = "\n".join([f"diff line {i} padding text" for i in range(1, 21)])
        res = Truncator.truncate_diff(diff, 80, "Diff")
        self.assertTrue(res["truncated"])
        self.assertTrue("Diff truncated. Dropped 14 lines (lines 4-17)" in res["content"])
        self.assertTrue("diff line 1" in res["content"])
        self.assertTrue("diff line 3" in res["content"])
        self.assertTrue("diff line 18" in res["content"])
        self.assertTrue("diff line 20" in res["content"])

    def test_truncate_diff_no_truncation(self):
        diff = "short diff\n"
        res = Truncator.truncate_diff(diff, 100, "Test")
        self.assertFalse(res["truncated"])
        self.assertEqual(res["content"], "short diff\n")


class TestUtils(unittest.TestCase):
    def test_glob_to_regex(self):
        self.assertEqual(glob_to_regex("*.rs"), "^.*\\.rs$")
        self.assertEqual(glob_to_regex("test?"), "^test.$")
        self.assertEqual(glob_to_regex("src/**/mod.rs"), "^src/.*.*/mod\\.rs$")

    def test_get_priority_score(self):
        self.assertEqual(get_priority_score("fs/ext4/inline.c", ["fs/ext4/inline.c"]), 1)
        self.assertEqual(get_priority_score("fs/ext4/dir.c", ["fs/ext4/inline.c"]), 2)
        self.assertEqual(get_priority_score("include/linux/fs.h", ["fs/ext4/inline.c"]), 3)
        self.assertEqual(get_priority_score("drivers/net/eth.c", ["fs/ext4/inline.c"]), 4)
        self.assertEqual(get_priority_score("any/file.c", []), 4)

    def test_validate_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "subdir").mkdir()
            (base / "subdir" / "file.txt").touch()

            result = validate_path("subdir/file.txt", base)
            self.assertTrue(str(result).startswith(str(base)))

            with self.assertRaises(ValueError):
                validate_path("../outside", base)

            with self.assertRaises(ValueError):
                validate_path("/etc/passwd", base)

    def test_format_git_grep_output_summary_header(self):
        stdout = "HEAD:fs/ext4/inline.c:1518:if (x)\nHEAD:fs/ext4/ext4.h:2489:static inline\nHEAD:fs/ext4/dir.c:91:if (y)\nHEAD:fs/ext4/dir.c:95:else"
        active_files = ["fs/ext4/inline.c"]
        formatted = format_git_grep_output(stdout, "HEAD", active_files)
        self.assertTrue(
            formatted.startswith(
                "Matches found across 3 files (4 total matches): fs/ext4/inline.c (1 match), fs/ext4/dir.c (2 matches), fs/ext4/ext4.h (1 match)"
            )
        )
        self.assertTrue("[file: fs/ext4/inline.c]" in formatted)

    def test_format_git_grep_output_summary_header_truncation(self):
        lines = [f"HEAD:file_{i}.c:1:match" for i in range(1, 16)]
        stdout = "\n".join(lines)
        active_files = []
        formatted = format_git_grep_output(stdout, "HEAD", active_files)
        self.assertTrue(formatted.startswith("Matches found across 15 files (15 total matches):"))
        self.assertTrue(", ... and 5 more files" in formatted)


class TestVirtualizeRef(unittest.TestCase):
    def setUp(self):
        self.toolbox = ToolBox(".")

    def test_without_virtual_head(self):
        self.assertEqual(self.toolbox.virtualize_ref("HEAD"), "HEAD")
        self.assertEqual(self.toolbox.virtualize_ref("HEAD~1"), "HEAD~1")
        self.assertEqual(self.toolbox.virtualize_ref("origin/HEAD"), "origin/HEAD")

    def test_with_virtual_head(self):
        self.toolbox.set_virtual_head("abc123e")
        self.assertEqual(self.toolbox.virtualize_ref("HEAD"), "abc123e")
        self.assertEqual(self.toolbox.virtualize_ref("HEAD~1"), "abc123e~1")
        self.assertEqual(self.toolbox.virtualize_ref("HEAD^"), "abc123e^")
        self.assertEqual(self.toolbox.virtualize_ref("baseline..HEAD"), "baseline..abc123e")
        self.assertEqual(self.toolbox.virtualize_ref("HEAD..baseline"), "abc123e..baseline")
        self.assertEqual(self.toolbox.virtualize_ref("HEAD:file.c"), "abc123e:file.c")

    def test_non_replacements(self):
        self.toolbox.set_virtual_head("abc123e")
        self.assertEqual(self.toolbox.virtualize_ref("origin/HEAD"), "origin/HEAD")
        self.assertEqual(self.toolbox.virtualize_ref("origin/HEAD~1"), "origin/HEAD~1")
        self.assertEqual(self.toolbox.virtualize_ref("refs/remotes/origin/HEAD"), "refs/remotes/origin/HEAD")
        self.assertEqual(self.toolbox.virtualize_ref("FOREHEAD"), "FOREHEAD")
        self.assertEqual(self.toolbox.virtualize_ref("my-HEAD-branch"), "my-HEAD-branch")
        self.assertEqual(self.toolbox.virtualize_ref("HEAD-fixes"), "HEAD-fixes")


class TestGitLs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_git_ls(self):
        result = self.toolbox.call("git_ls", {"revision": "HEAD", "path": "."})
        entries = result.get("entries", [])
        names = [e["name"] for e in entries]
        self.assertIn("README.md", names)
        self.assertIn("src", names)

    def test_git_ls_subdir(self):
        result = self.toolbox.call("git_ls", {"revision": "HEAD", "path": "src"})
        entries = result.get("entries", [])
        names = [e["name"] for e in entries]
        self.assertIn("main.py", names)
        self.assertIn("utils.py", names)


class TestGitReadFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_read_files_readme(self):
        result = self.toolbox.call("git_read_files", {
            "revision": "HEAD",
            "files": [{"path": "README.md", "start_line": 1, "end_line": 5}],
        })
        results = result.get("results", [])
        self.assertEqual(len(results), 1)
        content = results[0].get("content", "")
        self.assertTrue(len(content) > 0)
        self.assertTrue("TestProject" in content)

    def test_read_files_multiple(self):
        result = self.toolbox.call("git_read_files", {
            "revision": "HEAD",
            "files": [
                {"path": "README.md"},
                {"path": "src/main.py"},
            ],
        })
        results = result.get("results", [])
        self.assertEqual(len(results), 2)
        self.assertTrue(len(results[0].get("content", "")) > 0)
        self.assertTrue(len(results[1].get("content", "")) > 0)


class TestGitLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_git_log(self):
        result = self.toolbox.call("git_log", {"range": "HEAD", "limit": 1})
        output = result.get("output", "")
        self.assertTrue("commit" in output)


class TestGitShow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_git_show_head(self):
        result = self.toolbox.call("git_show", {"object": "HEAD"})
        content = result.get("content", "")
        self.assertTrue("commit" in content)

    def test_git_show_file_full(self):
        result = self.toolbox.call("git_show", {"object": "HEAD:README.md"})
        content = result.get("content", "")
        self.assertTrue("TestProject" in content)

    def test_git_show_file_range(self):
        result = self.toolbox.call("git_show", {
            "object": "HEAD:README.md",
            "start_line": 1,
            "end_line": 5,
        })
        content = result.get("content", "")
        end_line = result.get("end_line")
        start_line = result.get("start_line")
        self.assertEqual(start_line, 1)
        self.assertEqual(end_line, 5)
        lines_count = len(content.split("\n"))
        self.assertEqual(lines_count, 5)

    def test_git_show_file_default_limit(self):
        result = self.toolbox.call("git_show", {
            "object": "HEAD:README.md",
            "start_line": 10,
        })
        end_line = result.get("end_line")
        start_line = result.get("start_line")
        self.assertEqual(start_line, 10)
        self.assertEqual(end_line, 110)


class TestGitBlame(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_git_blame_readme(self):
        result = self.toolbox.call("git_blame", {
            "revision": "HEAD",
            "path": "README.md",
            "start_line": 1,
            "end_line": 3,
        })
        content = result.get("content", "")
        self.assertTrue(len(content) > 0)


class TestGitGrep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_git_grep_relative_path(self):
        result = self.toolbox.call("git_grep", {
            "revision": "HEAD",
            "pattern": "TestProject",
            "path": "README.md",
        })
        content = result.get("content", "")
        self.assertTrue(len(content) > 0)
        for line in content.split("\n"):
            self.assertFalse(line.startswith("/"), f"Line starts with absolute path: {line}")


class TestGitDiff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_git_diff(self):
        result = self.toolbox.call("git_diff", {
            "base_revision": "HEAD~1",
            "target_revision": "HEAD",
        })
        self.assertIn("content", result)


class TestCaching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_git_show_raw_caching(self):
        self.toolbox.cache.clear()
        self.assertEqual(len(self.toolbox.cache), 0)

        result1 = self.toolbox.call("git_show", {
            "object": "HEAD:README.md",
            "start_line": 1,
            "end_line": 5,
        })
        self.assertEqual(result1.get("start_line"), 1)
        self.assertEqual(result1.get("end_line"), 5)

        # Check git_show raw cache key (internal to the tool)
        raw_key = "git_show_raw:HEAD:README.md:false:None"
        self.assertIn(raw_key, self.toolbox.cache)

        # Check ToolBox-level cache key. Key format is now:
        # "git_show:<repo_key>:<virtual_head>:<args>" — repo_key is the resolved repo path.
        tb_cache_keys = [
            k for k in self.toolbox.cache
            if k.startswith("git_show:") and ":none:" in k and not k.startswith("git_show_raw:")
        ]
        self.assertTrue(len(tb_cache_keys) > 0, "ToolBox cache should have entries")

        result2 = self.toolbox.call("git_show", {
            "object": "HEAD:README.md",
            "start_line": 10,
            "end_line": 15,
        })
        self.assertEqual(result2.get("start_line"), 10)
        self.assertEqual(result2.get("end_line"), 15)

        # Should have: git_show_raw (1) + git_show TB cache (2) + new git_show_raw for second call (reuses first)
        # Actually git_show_raw should be reused, so total = 1 raw + 2 TB keys = 3
        self.assertEqual(len(self.toolbox.cache), 3)


class TestToolRegistry(unittest.TestCase):
    def test_get_declarations(self):
        toolbox = ToolBox(".")
        declarations = toolbox.get_declarations_generic()
        self.assertTrue(len(declarations) > 0)
        names = [d["name"] for d in declarations]
        self.assertIn("git_ls", names)
        self.assertIn("git_read_files", names)
        self.assertIn("git_show", names)
        self.assertIn("git_blame", names)
        self.assertIn("git_log", names)
        self.assertIn("git_diff", names)
        self.assertIn("git_grep", names)
        self.assertIn("git_find_files", names)

    def test_invalid_tool(self):
        toolbox = ToolBox(".")
        result = toolbox.call("nonexistent_tool", {})
        self.assertIn("error", result)


class TestGitVirtualHead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir, cls.repo_path, cls.sha = _create_test_repo()
        cls.toolbox = ToolBox(str(cls.repo_path))

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_virtual_head_git_show(self):
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        ).stdout.strip()

        self.toolbox.set_virtual_head(sha)

        result = self.toolbox.call("git_show", {"object": "HEAD"})
        content = result.get("content", "")
        self.assertTrue(sha in content)

        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotIn(current_head, content)


class TestMultiRepo(unittest.TestCase):
    """验证多 repo 支持：单个 ToolBox 实例按 repo_path 隔离服务多个 repo。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir1, cls.repo_path1, cls.sha1 = _create_test_repo()
        cls._tmpdir2, cls.repo_path2, cls.sha2 = _create_test_repo()

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir1.cleanup()
        cls._tmpdir2.cleanup()

    def test_no_default_repo_without_repo_path(self):
        """无默认 repo 且不传 repo_path → 返回 error。"""
        tb = ToolBox()
        result = tb.call("git_ls", {"revision": "HEAD", "path": "."})
        self.assertIn("error", result)
        self.assertIn("repo_path", result["error"])

    def test_no_default_repo_with_repo_path(self):
        """无默认 repo，传 repo_path → 动态创建 context 并成功。"""
        tb = ToolBox()
        result = tb.call(
            "git_ls",
            {"revision": "HEAD", "path": "."},
            repo_path=str(self.repo_path1),
        )
        self.assertNotIn("error", result)
        names = [e["name"] for e in result.get("entries", [])]
        self.assertIn("README.md", names)

    def test_multi_repo_isolation(self):
        """同一 ToolBox 实例，两个 repo 都能正常工作且结果独立。"""
        tb = ToolBox()
        r1 = tb.call(
            "git_log", {"range": "HEAD", "limit": 1}, repo_path=str(self.repo_path1)
        )
        r2 = tb.call(
            "git_log", {"range": "HEAD", "limit": 1}, repo_path=str(self.repo_path2)
        )
        self.assertNotIn("error", r1)
        self.assertNotIn("error", r2)
        # 两个独立创建的 repo，HEAD commit SHA 不同
        self.assertNotEqual(r1.get("output", ""), r2.get("output", ""))

    def test_repo_path_not_exist(self):
        """repo_path 指向不存在的路径 → 返回 error。"""
        tb = ToolBox()
        result = tb.call(
            "git_ls",
            {"revision": "HEAD", "path": "."},
            repo_path="/nonexistent/path/xxx",
        )
        self.assertIn("error", result)
        self.assertIn("does not exist", result["error"])

    def test_cache_isolation_across_repos(self):
        """不同 repo 的 cache 隔离：调用 repo1 不应污染 repo2 的 cache。"""
        tb = ToolBox()
        tb.call(
            "git_show",
            {"object": "HEAD:README.md"},
            repo_path=str(self.repo_path1),
        )
        ctx1 = tb._get_context(str(self.repo_path1))
        ctx2 = tb._get_context(str(self.repo_path2))
        self.assertIsNot(ctx1.cache, ctx2.cache)
        self.assertGreater(len(ctx1.cache), 0)
        self.assertEqual(len(ctx2.cache), 0)

    def test_virtual_head_isolation(self):
        """virtual_head 按 repo 隔离：repo1 设置不影响 repo2。"""
        tb = ToolBox()
        tb.set_virtual_head("abc123e", repo_path=str(self.repo_path1))
        self.assertEqual(
            tb.virtualize_ref("HEAD", repo_path=str(self.repo_path1)), "abc123e"
        )
        # repo2 未设置 virtual_head，HEAD 应保持原样
        self.assertEqual(
            tb.virtualize_ref("HEAD", repo_path=str(self.repo_path2)), "HEAD"
        )

    def test_context_reuse_for_same_repo(self):
        """同一 repo_path 多次调用应复用同一 context。"""
        tb = ToolBox()
        tb.call(
            "git_ls", {"revision": "HEAD", "path": "."}, repo_path=str(self.repo_path1)
        )
        ctx_a = tb._get_context(str(self.repo_path1))
        tb.call(
            "git_log", {"range": "HEAD", "limit": 1}, repo_path=str(self.repo_path1)
        )
        ctx_b = tb._get_context(str(self.repo_path1))
        self.assertIs(ctx_a, ctx_b)

    def test_backward_compat_default_repo(self):
        """向后兼容：传入 worktree_path 作为默认 repo，不传 repo_path 也能用。"""
        tb = ToolBox(str(self.repo_path1))
        result = tb.call("git_ls", {"revision": "HEAD", "path": "."})
        self.assertNotIn("error", result)
        # 同时也支持显式传 repo_path 访问其他 repo
        result2 = tb.call(
            "git_ls",
            {"revision": "HEAD", "path": "."},
            repo_path=str(self.repo_path2),
        )
        self.assertNotIn("error", result2)


if __name__ == "__main__":
    unittest.main()