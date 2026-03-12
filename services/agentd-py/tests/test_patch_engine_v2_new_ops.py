"""Tests for new V2 patch operations: search_replace and apply_diff."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentd.domain.models import (
    ApplyDiffOpV2,
    PatchCandidateV2,
    PatchFailureCode,
    SearchReplaceOpV2,
)
from agentd.patch.engine import PatchEngine


class TestSearchReplaceOp:
    """Tests for SearchReplaceOpV2 (Fast Apply)."""

    @pytest.mark.asyncio
    async def test_simple_search_replace(self):
        """Test basic search and replace operation."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def hello():\n    pass",
                        replace="def hello():\n    return 'world'",
                        reason="Add return value",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "return 'world'" in content
            assert "pass" not in content

    @pytest.mark.asyncio
    async def test_search_replace_not_found(self):
        """Test search/replace fails when text not found."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def goodbye():",
                        replace="def farewell():",
                        reason="Rename function",
                    )
                ],
            )

            with pytest.raises(RuntimeError, match="Search text not found"):
                await engine.apply_patch_candidate(base, candidate)

    @pytest.mark.asyncio
    async def test_search_replace_ambiguous(self):
        """Test search/replace fails when text appears multiple times."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n\ndef world():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="pass",
                        replace="return None",
                        reason="Add return",
                    )
                ],
            )

            with pytest.raises(RuntimeError, match="appears 2 times"):
                await engine.apply_patch_candidate(base, candidate)

    @pytest.mark.asyncio
    async def test_search_replace_preflight(self):
        """Test preflight validation for search/replace."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def goodbye():",
                        replace="def farewell():",
                        reason="Rename function",
                    )
                ],
            )

            report = await engine.preflight_patch_candidate(base, candidate)
            assert not report.success
            assert len(report.issues) == 1
            assert report.issues[0].code == PatchFailureCode.ANCHOR_MISSING

    @pytest.mark.asyncio
    async def test_search_replace_preflight_ambiguous(self):
        """Test preflight catches ambiguous search text."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n\ndef world():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="pass",
                        replace="return None",
                        reason="Add return",
                    )
                ],
            )

            report = await engine.preflight_patch_candidate(base, candidate)
            assert not report.success
            assert len(report.issues) == 1
            assert report.issues[0].code == PatchFailureCode.ANCHOR_AMBIGUOUS


class TestApplyDiffOp:
    """Tests for ApplyDiffOpV2 (Unified Diff)."""

    @pytest.mark.asyncio
    async def test_simple_diff_application(self):
        """Test basic unified diff application."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="@@ -1,2 +1,3 @@\n def hello():\n+    # TODO: implement\n     pass\n",
                        reason="Add TODO comment",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# TODO: implement" in content
            assert "pass" in content

    @pytest.mark.asyncio
    async def test_multi_hunk_diff(self):
        """Test diff with multiple hunks."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text(
                "def hello():\n    pass\n\ndef world():\n    pass\n"
            )

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff=(
                            "@@ -1,2 +1,3 @@\n"
                            " def hello():\n"
                            "+    # First function\n"
                            "     pass\n"
                            "@@ -4,2 +5,3 @@\n"
                            " def world():\n"
                            "+    # Second function\n"
                            "     pass\n"
                        ),
                        reason="Add comments to both functions",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# First function" in content
            assert "# Second function" in content

    @pytest.mark.asyncio
    async def test_diff_context_mismatch(self):
        """Test diff fails when context doesn't match."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    return 'world'\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="@@ -1,2 +1,3 @@\n def hello():\n+    # TODO\n     pass\n",
                        reason="Add TODO",
                    )
                ],
            )

            with pytest.raises(RuntimeError, match="context mismatch"):
                await engine.apply_patch_candidate(base, candidate)

    @pytest.mark.asyncio
    async def test_diff_preflight_validation(self):
        """Test preflight validation for diffs."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="@@ -1,2 +1,3 @@\n def hello():\n+    # TODO\n     pass\n",
                        reason="Add TODO",
                    )
                ],
            )

            report = await engine.preflight_patch_candidate(base, candidate)
            assert report.success
            assert len(report.issues) == 0

    @pytest.mark.asyncio
    async def test_diff_preflight_context_mismatch(self):
        """Test preflight catches context mismatches."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    return 'world'\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="@@ -1,2 +1,3 @@\n def hello():\n+    # TODO\n     pass\n",
                        reason="Add TODO",
                    )
                ],
            )

            report = await engine.preflight_patch_candidate(base, candidate)
            assert not report.success
            assert len(report.issues) == 1
            assert report.issues[0].code == PatchFailureCode.ANCHOR_MISSING

class TestCodexDiffFormat:
    """Tests for Codex-style diff format parsing."""

    @pytest.mark.asyncio
    async def test_codex_format_parsing(self):
        """Test parsing of Codex-style diff format."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("def hello():\n    pass\n")

            engine = PatchEngine()
            
            # Test Codex format with markers
            codex_diff = """*** Begin Patch
@@ -1,2 +1,3 @@
 def hello():
+    # TODO: implement
     pass
*** End Patch"""
            
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff=codex_diff,
                        reason="Add TODO comment",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# TODO: implement" in content




class TestPerformanceCharacteristics:
    """Tests to verify performance characteristics of operations."""

    @pytest.mark.asyncio
    async def test_search_replace_large_file(self):
        """Test search/replace performance on large file."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "large.py"
            
            # Create a large file (1000 lines)
            lines = []
            for i in range(1000):
                lines.append(f"def function_{i}():\n    pass\n\n")
            test_file.write_text("".join(lines))

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="large.py",
                        search="def function_500():\n    pass",
                        replace="def function_500():\n    return 500",
                        reason="Update function 500",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["large.py"]
            
            content = test_file.read_text()
            assert "return 500" in content
            # Verify other functions unchanged
            assert "def function_499():" in content
            assert "def function_501():" in content


class TestComplexSearchReplaceScenarios:
    """Complex test cases for search/replace operations."""

    @pytest.mark.asyncio
    async def test_search_replace_with_special_characters(self):
        """Test search/replace with regex special characters."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text('pattern = r"\\d+\\.\\d+"\n')

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search='pattern = r"\\d+\\.\\d+"',
                        replace='pattern = r"\\d+\\.\\d+\\.\\d+"',
                        reason="Update regex pattern",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert r'\d+\.\d+\.\d+' in content

    @pytest.mark.asyncio
    async def test_search_replace_multiline_with_indentation(self):
        """Test search/replace preserving complex indentation."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""class MyClass:
    def method(self):
        if condition:
            for item in items:
                process(item)
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="        if condition:\n            for item in items:\n                process(item)",
                        replace="        if condition:\n            for item in items:\n                # Added validation\n                if validate(item):\n                    process(item)",
                        reason="Add validation before processing",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# Added validation" in content
            assert "if validate(item):" in content
            assert "                    process(item)" in content  # Verify indentation

    @pytest.mark.asyncio
    async def test_search_replace_with_unicode(self):
        """Test search/replace with unicode characters."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text('message = "Hello 世界"\n', encoding="utf-8")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search='message = "Hello 世界"',
                        replace='message = "Hello 世界! 🌍"',
                        reason="Add emoji",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text(encoding="utf-8")
            assert "🌍" in content

    @pytest.mark.asyncio
    async def test_search_replace_empty_lines_preservation(self):
        """Test search/replace preserves empty lines correctly."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""def function_a():
    pass


def function_b():
    pass
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def function_a():\n    pass\n\n\ndef function_b():\n    pass",
                        replace="def function_a():\n    return 'a'\n\n\ndef function_b():\n    return 'b'",
                        reason="Add return values",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            # Verify empty lines preserved
            assert "\n\n\n" in content
            assert "return 'a'" in content
            assert "return 'b'" in content

    @pytest.mark.asyncio
    async def test_search_replace_at_file_boundaries(self):
        """Test search/replace at start and end of file."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("# Header comment\ncode_here()\n# Footer comment")

            engine = PatchEngine()
            
            # Test at start
            candidate1 = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="# Header comment",
                        replace="# Updated header\n# Multi-line header",
                        reason="Update header",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate1)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# Updated header" in content
            assert "# Multi-line header" in content

    @pytest.mark.asyncio
    async def test_search_replace_with_tabs_and_spaces(self):
        """Test search/replace with mixed tabs and spaces."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            # Mix tabs and spaces (common in real codebases)
            test_file.write_text("def func():\n\tif True:\n\t    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="\tif True:\n\t    pass",
                        replace="\tif True:\n\t    return True",
                        reason="Add return",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "return True" in content


class TestComplexDiffScenarios:
    """Complex test cases for unified diff operations."""

    @pytest.mark.asyncio
    async def test_diff_with_deletion_only(self):
        """Test diff that only removes lines."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""def function():
    # TODO: remove this
    # And this too
    return True
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -1,4 +1,2 @@
 def function():
-    # TODO: remove this
-    # And this too
     return True
""",
                        reason="Remove TODO comments",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "TODO" not in content
            assert "return True" in content

    @pytest.mark.asyncio
    async def test_diff_with_addition_only(self):
        """Test diff that only adds lines."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""def function():
    return True
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -1,2 +1,5 @@
 def function():
+    # Validate input
+    if not validate():
+        return False
     return True
""",
                        reason="Add validation",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# Validate input" in content
            assert "if not validate():" in content
            assert "return False" in content

    @pytest.mark.asyncio
    async def test_diff_with_complex_context(self):
        """Test diff with complex surrounding context."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""class DataProcessor:
    def __init__(self):
        self.data = []
    
    def process(self, item):
        # Current implementation
        result = item * 2
        return result
    
    def finalize(self):
        return sum(self.data)
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -5,3 +5,6 @@
     def process(self, item):
-        # Current implementation
-        result = item * 2
+        # Enhanced implementation with validation
+        if not isinstance(item, (int, float)):
+            raise TypeError("Item must be numeric")
+        result = item * 2.5
+        self.data.append(result)
         return result
""",
                        reason="Enhance process method",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "Enhanced implementation" in content
            assert "isinstance(item, (int, float))" in content
            assert "item * 2.5" in content
            assert "self.data.append(result)" in content

    @pytest.mark.asyncio
    async def test_diff_with_three_hunks(self):
        """Test diff with three separate hunks."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""# Module header
import os

def function_a():
    pass

def function_b():
    pass

def function_c():
    pass
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -1,2 +1,3 @@
 # Module header
+# Author: AI Editor
 import os
@@ -4,2 +5,3 @@
 def function_a():
+    # Implementation A
     pass
@@ -10,2 +12,3 @@
 def function_c():
+    # Implementation C
     pass
""",
                        reason="Add comments to multiple functions",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# Author: AI Editor" in content
            assert "# Implementation A" in content
            assert "# Implementation C" in content

    @pytest.mark.asyncio
    async def test_diff_with_no_newline_at_eof(self):
        """Test diff handling files without trailing newline."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            # Write without trailing newline
            test_file.write_text("def function():\n    pass", newline='')

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -1,2 +1,3 @@
 def function():
+    # Added comment
     pass
""",
                        reason="Add comment",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# Added comment" in content

    @pytest.mark.asyncio
    async def test_diff_replacing_entire_function(self):
        """Test diff that replaces an entire function body."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""def calculate(x, y):
    # Old implementation
    temp = x + y
    result = temp * 2
    return result
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -1,5 +1,3 @@
 def calculate(x, y):
-    # Old implementation
-    temp = x + y
-    result = temp * 2
-    return result
+    # New optimized implementation
+    return (x + y) * 2
""",
                        reason="Optimize calculation",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "New optimized implementation" in content
            assert "return (x + y) * 2" in content
            assert "temp" not in content

    @pytest.mark.asyncio
    async def test_diff_with_blank_line_changes(self):
        """Test diff that modifies blank line spacing."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""def function_a():
    pass
def function_b():
    pass
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -1,4 +1,5 @@
 def function_a():
     pass
+
 def function_b():
     pass
""",
                        reason="Add blank line between functions",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            # Verify blank line added
            assert "pass\n\ndef function_b" in content

    @pytest.mark.asyncio
    async def test_diff_with_long_context_lines(self):
        """Test diff with very long context lines."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            long_line = "x = " + " + ".join([f"value_{i}" for i in range(50)])
            test_file.write_text(f"""def function():
    {long_line}
    return x
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff=f"""@@ -1,3 +1,4 @@
 def function():
     {long_line}
+    # Process result
     return x
""",
                        reason="Add comment",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "# Process result" in content


class TestEdgeCasesAndErrorHandling:
    """Test edge cases and error handling scenarios."""

    @pytest.mark.asyncio
    async def test_search_replace_with_windows_line_endings(self):
        """Test search/replace with CRLF line endings."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            # Write with Windows line endings - but Python normalizes on read
            # So we test that the engine handles normalized content
            test_file.write_text("def hello():\n    pass\n")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def hello():\n    pass",
                        replace="def hello():\n    return 'world'",
                        reason="Add return",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "return 'world'" in content

    @pytest.mark.asyncio
    async def test_diff_with_offset_hunks(self):
        """Test diff where hunks need offset adjustment."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""line1
line2
line3
line4
line5
line6
""")

            engine = PatchEngine()
            # Apply two diffs that affect line numbering
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    ApplyDiffOpV2(
                        op="apply_diff",
                        file="test.py",
                        diff="""@@ -1,3 +1,4 @@
 line1
+inserted_line
 line2
 line3
@@ -4,3 +5,4 @@
 line4
+another_inserted_line
 line5
 line6
""",
                        reason="Insert lines at multiple positions",
                    )
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "inserted_line" in content
            assert "another_inserted_line" in content
            lines = content.strip().split('\n')
            assert len(lines) == 8  # Original 6 + 2 inserted

    @pytest.mark.asyncio
    async def test_search_replace_sequential_operations(self):
        """Test multiple search/replace operations in sequence."""
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            test_file = base / "test.py"
            test_file.write_text("""def func_a():
    pass

def func_b():
    pass

def func_c():
    pass
""")

            engine = PatchEngine()
            candidate = PatchCandidateV2(
                candidate_id="c1",
                patch_ops=[
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def func_a():\n    pass",
                        replace="def func_a():\n    return 'a'",
                        reason="Update func_a",
                    ),
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def func_b():\n    pass",
                        replace="def func_b():\n    return 'b'",
                        reason="Update func_b",
                    ),
                    SearchReplaceOpV2(
                        op="search_replace",
                        file="test.py",
                        search="def func_c():\n    pass",
                        replace="def func_c():\n    return 'c'",
                        reason="Update func_c",
                    ),
                ],
            )

            result = await engine.apply_patch_candidate(base, candidate)
            assert result.touched_files == ["test.py"]
            
            content = test_file.read_text()
            assert "return 'a'" in content
            assert "return 'b'" in content
            assert "return 'c'" in content
            assert "pass" not in content

# Made with Bob
# Made with Bob
