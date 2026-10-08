#!/usr/bin/env python3
"""
skills_pack.py – the three skills as ZIP files, the way a client installs
them by hand.

One ZIP per skill, the skill's folder as its single top-level item
(`munimentum-case/SKILL.md`): what claude.ai and Claude Desktop take
under Customize › Skills, and what unpacks into `~/.claude/skills/` for
Claude Code. `bundle()` wraps the three in one download for the page's
button, with a README naming the version they belong to; `main()` writes
the three beside the release files for the Build workflow. Standard
library only: the release job runs it on a bare runner, and the app
calls it without an index. The bytes are the same for the same content
(fixed timestamps), so SHA256SUMS can hold them.

A copy installed by hand does not change with the app – the copy the
server serves and gates on (get_guide) always does. Every SKILL.md names
the version it belongs to, and the MCP tool get_version lets Claude
compare.
"""
import io
import sys
import zipfile
from pathlib import Path

PREFIX = "munimentum-"
HERE = Path(__file__).resolve().parent
# One date for every entry: a ZIP carries its files' mtimes, and two
# builds of the same text must give the same bytes.
EPOCH = (1980, 1, 1, 0, 0, 0)


def skill_folders(skills_dir):
    """The skill folders below `skills_dir`, by name."""
    return sorted(p.parent for p in Path(skills_dir).glob("*/SKILL.md"))


def skill_files(folder):
    """The files a skill ships, by path – nothing hidden (.DS_Store, an
    editor's swap file) and no __pycache__: the one list the bundle
    (packaging/app.spec), the download and the release assets share."""
    folder = Path(folder)
    return sorted(p for p in folder.rglob("*") if p.is_file()
                  and not any(part.startswith(".") or part == "__pycache__"
                              for part in p.relative_to(folder).parts))


def zip_name(topic):
    return f"Munimentum-skill-{topic}.zip"


def bundle_name(version):
    return f"Munimentum-skills-{version}.zip"


def _add(z, name, data):
    info = zipfile.ZipInfo(name, date_time=EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    info.create_system = 3       # "Unix" on Windows too, else the bytes differ by platform
    z.writestr(info, data)


def skill_zip(folder):
    """The folder as a ZIP – `<folder>/…` for every file in it. Markdown
    is written with LF whatever the checkout has (Git for Windows may
    give CRLF), as the server serves it."""
    folder = Path(folder)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for file in skill_files(folder):
            data = file.read_bytes()
            if file.suffix == ".md":
                data = data.replace(b"\r\n", b"\n")
            _add(z, f"{folder.name}/{file.relative_to(folder).as_posix()}", data)
    return out.getvalue()


def readme(version, topics):
    files = "\n".join(f"  {zip_name(t)}" for t in topics)
    count = {1: "One skill", 2: "Two skills", 3: "Three skills"}.get(len(topics), f"{len(topics)} skills")
    return f"""Munimentum skills, version {version}

{count} for an MCP client's model, one ZIP each:
{files}

Claude Code: unzip them into ~/.claude/skills/ (for you) or .claude/skills/
of a project. claude.ai and Claude Desktop: upload each ZIP under
Customize > Skills (code execution has to be on).

A copy installed by hand does not change with the app. After an update,
download again: the copy the server serves is always current, every guide
names the version it belongs to, and the MCP tool get_version lets Claude
compare.
"""


def bundle(skills_dir, version):
    """One ZIP holding the per-skill ZIPs and the README."""
    folders = skill_folders(skills_dir)
    topics = [f.name.removeprefix(PREFIX) for f in folders]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for folder, topic in zip(folders, topics, strict=True):
            _add(z, zip_name(topic), skill_zip(folder))
        _add(z, "README.txt", readme(version, topics).encode("utf-8"))
    return out.getvalue()


def main(argv=None):
    """skills_pack.py <out_dir> [skills_dir] – one ZIP per skill into
    <out_dir>, next to the release files."""
    argv = sys.argv[1:] if argv is None else argv
    if not 1 <= len(argv) <= 2:
        sys.stderr.write(__doc__)
        return 2
    out, skills_dir = Path(argv[0]), Path(argv[1]) if len(argv) == 2 else HERE / "skills"
    folders = skill_folders(skills_dir)
    if not folders:
        sys.stderr.write(f"no skills below {skills_dir}\n")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    for folder in folders:
        (out / zip_name(folder.name.removeprefix(PREFIX))).write_bytes(skill_zip(folder))
    return 0


if __name__ == "__main__":
    # Only the script switches its streams: a library that did so would
    # change them for every process that imports it (api_app does).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
