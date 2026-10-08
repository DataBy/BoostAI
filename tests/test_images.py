from __future__ import annotations

from pathlib import Path

from boost_ai.orchestrator import Harness
from boost_ai.runtimes.base import UsageBook
from boost_ai.task import find_images

from .conftest import FakeRuntime, ScriptedUI, writes

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def img(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    return path


def test_find_images_forms(tmp_path):
    a = img(tmp_path / "shots" / "login error.png")
    b = img(tmp_path / "b.jpg")
    c = img(tmp_path / "c d.webp")
    text = (f"Fix the layout shown in '{a}' and {b}, also file://{str(c).replace(' ', '%20')}. "
            f"Ignore {tmp_path}/missing.png and notes.txt and {b} again.")
    assert find_images(text, tmp_path) == [a.resolve(), b.resolve(), c.resolve()]


def test_relative_and_escaped_paths(tmp_path):
    a = img(tmp_path / "docs" / "my shot.png")
    assert find_images("see docs/my\\ shot.png", tmp_path) == [a.resolve()]
    assert find_images("no images here, just text.png mentions", tmp_path) == []


async def test_images_attached_and_passed_to_implementer_only(repo, cfg, tmp_path):
    shot = img(tmp_path / "Screenshot from 2026.png")
    codex = FakeRuntime("codex", [writes("auth.py")])
    claude = FakeRuntime("claude", [lambda req: __import__("boost_ai.runtimes.base", fromlist=["x"]).ExecutionResult(
        ok=True, duration=1, final={"verdict": "approve", "summary": "ok", "findings": []})])
    ui = ScriptedUI(["No"])
    h = Harness(repo, cfg, ui, {"codex": codex, "claude": claude}, usage=UsageBook(tmp_path / "u.json"),
                metrics_path=tmp_path / "m.jsonl")
    task = await h.run_task(f"Fix the login token bug shown in '{shot}'")
    copied = repo / ".boost-ai" / "state" / "attachments" / "T-0001" / "01-Screenshot from 2026.png"
    assert task.attachments == [str(copied)] and copied.read_bytes() == PNG
    assert codex.requests[0].images == [copied]
    assert copied.parent in codex.requests[0].read_dirs
    assert claude.requests[0].images == []              # reviewer does not get the images
    assert "Attached: Screenshot from 2026.png" in ui.text()



# ── any file type ──────────────────────────────────────────────────────────
from boost_ai.task import find_attachments  # noqa: E402


def test_any_file_type_outside_the_project(tmp_path, repo):
    ext = tmp_path / "inbox"
    ext.mkdir()
    for name in ("spec.pdf", "data.csv", "app.log", "notes.docx"):
        (ext / name).write_bytes(b"%PDF-1.4 x" if name.endswith(".pdf") else b"x")
    text = f"Revisa {ext}/spec.pdf, {ext}/data.csv y '{ext}/app.log' y file://{ext}/notes.docx"
    assert [p.name for p in find_attachments(text, repo)] == ["spec.pdf", "data.csv", "app.log", "notes.docx"]


def test_tracked_project_files_are_not_copied(repo):
    (repo / "README.md").write_text("tracked\n")          # tracked by the fixture's first commit
    (repo / "scratch.txt").write_text("untracked\n")
    found = find_attachments("mira README.md y scratch.txt y .boost-ai/config.yaml", repo)
    assert [p.name for p in found] == ["scratch.txt"]


def test_size_limit(tmp_path, repo, monkeypatch):
    import boost_ai.task as task_mod
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * 2048)
    monkeypatch.setattr(task_mod, "MAX_ATTACHMENT_BYTES", 1024)
    assert find_attachments(str(big), repo) == []


async def test_attachments_listed_in_prompt_and_images_native(repo, cfg, tmp_path):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    shot = img(tmp_path / "ui.png")
    codex = FakeRuntime("codex", [writes("a.py")])
    ui = ScriptedUI(["No"])
    h = Harness(repo, cfg, ui, {"codex": codex}, usage=UsageBook(tmp_path / "u.json"), metrics_path=tmp_path / "m")
    await h.run_task(f"Implementa lo de {pdf} como en {shot}")
    req = codex.requests[0]
    assert "## Attached files" in req.prompt and "01-spec.pdf" in req.prompt and "02-ui.png" in req.prompt
    assert [p.name for p in req.images] == ["02-ui.png"]                 # only images via the native flag


async def test_image_moves_conversation_off_text_only_runtime(repo, cfg, tmp_path):
    shot = img(tmp_path / "bug.png")

    class TextOnly(FakeRuntime):
        vision = False

    cfg["routing"]["policy"]["L2"] = ["deepseek", "claude"]
    ds = TextOnly("deepseek", [])
    claude = FakeRuntime("claude", [writes("fix.py")])
    ui = ScriptedUI(["No"])
    h = Harness(repo, cfg, ui, {"deepseek": ds, "claude": claude}, usage=UsageBook(tmp_path / "u.json"),
                metrics_path=tmp_path / "m")
    await h.run_task(f"Arregla el layout que se ve en {shot}")
    assert ds.requests == [] and claude.requests and h.task.runtime == "claude"
    assert "cannot read images or PDFs" in ui.text()
