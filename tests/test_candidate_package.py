"""A removed editable header cannot reappear from the setter's starter tree."""

from __future__ import annotations

import shutil

from cxxprobe_worker.candidate_package import sanitize_attachments


def test_only_explicit_readonly_headers_remain_in_staged_package(tmp_path):
    original = tmp_path / "original"
    public = original / "candidate"
    public.mkdir(parents=True)
    (original / "problem.yaml").write_text(
        "version: 2\nname: Example\nattachments:\n  dir: candidate\n  reference_files: [api.hpp]\n"
    )
    (public / "starter.hpp").write_text("#define OLD_STUB 1\n")
    (public / "api.hpp").write_text("int answer();\n")
    staged = tmp_path / "staged"
    shutil.copytree(original, staged)

    sanitize_attachments(staged)

    assert not (staged / "candidate/starter.hpp").exists()
    assert (staged / "candidate/api.hpp").read_text() == "int answer();\n"
    assert (original / "candidate/starter.hpp").is_file()
