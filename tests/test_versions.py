"""versions.py: every write into the archive keeps what it replaces and is
journaled for the evidence step."""

import json

import pytest

import versions


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A data folder with an export folder in it, and the environment the
    app hands every step."""
    (tmp_path / "onedrive_export").mkdir()
    monkeypatch.setenv("MUNIMENTUM_VERSIONS_DIR", str(tmp_path / "versions"))
    monkeypatch.setenv("MUNIMENTUM_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("KEEP_VERSIONS", "1")
    monkeypatch.setenv("VERSIONS_MAX_MB", "1")
    monkeypatch.setitem(versions._JOURNAL, "path", None)
    return tmp_path


def journal(data):
    lines = []
    for p in sorted((data / "evidence" / "pending").glob("*.jsonl")):
        lines += [json.loads(z) for z in p.read_text(encoding="utf-8").splitlines()]
    return lines


def test_a_replaced_file_keeps_its_earlier_version(data):
    path = data / "onedrive_export" / "plan.md"
    versions.write_text(path, "draft 1")
    versions.write_text(path, "draft 2")
    assert path.read_text(encoding="utf-8") == "draft 2"
    old = versions.sha256_bytes(b"draft 1")
    kept = data / "versions" / "onedrive_export" / "plan.md" / f"{old[:16]}.md"
    assert kept.read_text(encoding="utf-8") == "draft 1"
    first, second = journal(data)
    assert first["op"] == "write" and first["rel"] == "onedrive_export/plan.md" and "was" not in first
    assert second["was"] == old and second["kept"] is True
    assert second["sha256"] == versions.sha256_bytes(b"draft 2") and second["size"] == 7


def test_the_same_bytes_again_write_nothing(data):
    path = data / "onedrive_export" / "plan.md"
    versions.write_text(path, "same")
    stamp = path.stat().st_mtime_ns
    versions.write_text(path, "same")
    assert path.stat().st_mtime_ns == stamp
    assert len(journal(data)) == 1
    assert not (data / "versions").exists()
    assert not list(path.parent.glob("*.tmp"))


def test_a_large_file_keeps_only_its_checksum(data):
    path = data / "onedrive_export" / "big.bin"
    versions.write_bytes(path, b"x" * (2 * 1024 * 1024))
    versions.write_bytes(path, b"y" * 10)
    assert not (data / "versions").exists()
    assert journal(data)[-1]["kept"] is False
    assert journal(data)[-1]["was"] == versions.sha256_bytes(b"x" * (2 * 1024 * 1024))


def test_keeping_off_still_journals(data, monkeypatch):
    monkeypatch.setenv("KEEP_VERSIONS", "0")
    path = data / "onedrive_export" / "plan.md"
    versions.write_text(path, "a")
    versions.write_text(path, "b")
    assert not (data / "versions").exists()
    assert journal(data)[-1]["kept"] is False


def test_a_conversation_is_written_without_a_kept_copy(data):
    """A Teams conversation keeps its history per message, not per file."""
    path = data / "onedrive_export" / "chat.html"
    versions.write_text(path, "<p>1</p>", keep=False)
    versions.write_text(path, "<p>1</p><p>2</p>", keep=False)
    assert not (data / "versions").exists()


def test_a_missing_folder_is_an_error_as_with_a_plain_write(data):
    with pytest.raises(OSError):
        versions.write_bytes(data / "onedrive_export" / "nope" / "a.txt", b"x")


def test_outside_the_data_folder_it_is_a_plain_write(tmp_path, monkeypatch):
    monkeypatch.delenv("MUNIMENTUM_VERSIONS_DIR", raising=False)
    monkeypatch.delenv("MUNIMENTUM_EVIDENCE_DIR", raising=False)
    monkeypatch.setitem(versions._JOURNAL, "path", None)
    path = tmp_path / "a.txt"
    versions.write_text(path, "one")
    versions.write_text(path, "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


def test_a_removed_file_is_kept_and_names_its_successor(data):
    old = data / "onedrive_export" / "old name.ics"
    new = data / "onedrive_export" / "new name.ics"
    versions.write_text(old, "BEGIN:VEVENT")
    versions.write_text(new, "BEGIN:VEVENT\nSUMMARY:moved")
    assert versions.remove(old, successor=new)
    assert not old.exists()
    sha = versions.sha256_bytes(b"BEGIN:VEVENT")
    assert (data / "versions" / "onedrive_export" / "old name.ics" / f"{sha[:16]}.ics").is_file()
    last = journal(data)[-1]
    assert last == {**last, "op": "removed", "rel": "onedrive_export/old name.ics",
                    "to": "onedrive_export/new name.ics", "kept": True, "sha256": sha}
    assert versions.remove(old) is False           # nothing there any more


def test_a_move_is_journaled_file_by_file(data):
    folder = data / "onedrive_export" / "Anhaenge" / "a"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text("x", encoding="utf-8")
    target = data / "onedrive_export" / "Anhaenge" / "b"
    folder.replace(target)
    versions.moved(folder, target)
    assert journal(data) == [{**journal(data)[0], "op": "moved",
                              "rel": "onedrive_export/Anhaenge/a/x.txt",
                              "to": "onedrive_export/Anhaenge/b/x.txt"}]


def test_find_and_stored_name_the_kept_bytes(data):
    path = data / "onedrive_export" / "plan.md"
    versions.write_text(path, "v1")
    versions.write_text(path, "v2")
    sha = versions.sha256_bytes(b"v1")
    found = versions.find(data / "versions", ["other.md", "onedrive_export/plan.md"], sha)
    assert found is not None and found.read_text(encoding="utf-8") == "v1"
    assert versions.find(data / "versions", ["onedrive_export/plan.md"], "0" * 64) is None


def test_the_diff_gives_the_new_text_back_and_names_what_went():
    old = "Defects: three open points. Approval pending."
    new = "Defects: none. Approval  given."
    ops = versions.diff(old, new)
    assert "".join(t for op, t in ops if op != "-") == new
    assert ["-", "three open points."] in ops and ["+", "none."] in ops
    # Whitespace that only moved is no change
    assert not any(op != "=" and not t.strip() for op, t in ops)
    assert versions.diff("same", "same") == [["=", "same"]]


def test_plain_text_takes_the_markup_off_a_page():
    html = ("<html><head><title>x</title><style>p{}</style></head><body><h1>Acceptance</h1>"
            "<p>Defects: <b>none</b>.</p><div>Approval &amp; sign-off</div></body></html>")
    text = versions.plain_text(html.encode(), "page.html")
    assert "Acceptance" in text and "Defects: none ." in text and "Approval & sign-off" in text
    assert "p{}" not in text and "<" not in text
    assert versions.plain_text(b"BEGIN:VCARD\r\nFN:Bob\r\n", "bob.vcf") == "BEGIN:VCARD\nFN:Bob\n"
    assert versions.is_text("a.ics") and not versions.is_text("a.docx")


# ---------------------------------------------------------------------------
# quickXorHash
# ---------------------------------------------------------------------------
def _reference_quick_xor(data):
    """Microsoft's published C# QuickXorHash, line by line: every byte,
    no shortcut – the fast one must agree with it however the bytes come."""
    width, shift, last_bits = 160, 11, 32
    cells = [0, 0, 0]
    mask = (1 << 64) - 1
    shift_so_far, length = 0, 0

    def core(array):
        nonlocal shift_so_far, length
        size = len(array)
        index, offset = shift_so_far // 64, shift_so_far % 64
        for i in range(min(size, width)):
            last = index == len(cells) - 1
            bits = last_bits if last else 64
            if offset <= bits - 8:
                for j in range(i, size, width):
                    cells[index] ^= (array[j] << offset) & mask
            else:
                index2 = 0 if last else index + 1
                low = bits - offset
                xored = 0
                for j in range(i, size, width):
                    xored ^= array[j]
                cells[index] ^= (xored << offset) & mask
                cells[index2] ^= xored >> low
            offset += shift
            while offset >= bits:
                index = 0 if last else index + 1
                offset -= bits
        shift_so_far = (shift_so_far + shift * (size % width)) % width
        length += size

    for start in range(0, len(data), 997):
        core(data[start:start + 997])
    out = bytearray(20)
    out[0:8] = cells[0].to_bytes(8, "little")
    out[8:16] = cells[1].to_bytes(8, "little")
    out[16:20] = (cells[2] & 0xFFFFFFFF).to_bytes(4, "little")
    for i, b in enumerate(length.to_bytes(8, "little")):
        out[12 + i] ^= b
    import base64
    return base64.b64encode(bytes(out)).decode("ascii")


def test_quick_xor_agrees_with_the_published_algorithm():
    import random
    rnd = random.Random(7)
    assert versions.QuickXor().b64() == "AAAAAAAAAAAAAAAAAAAAAAAAAAA="
    # One byte: it sits at bit 0, the length is XORed in at byte 12
    assert versions.QuickXor().update(b"J").b64() == "SgAAAAAAAAAAAAAAAQAAAAAAAAA="
    for size in (1, 7, 159, 160, 161, 320, 1000, 5000, 70001):
        data = bytes(rnd.randrange(256) for _ in range(size))
        expected = _reference_quick_xor(data)
        assert versions.QuickXor().update(data).b64() == expected, size
        # However the bytes arrive
        q = versions.QuickXor()
        pos = 0
        while pos < size:
            step = rnd.randrange(1, 400)
            q.update(data[pos:pos + step])
            pos += step
        assert q.b64() == expected, size


def test_quick_xor_of_a_file(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"munimentum" * 1000)
    assert versions.quick_xor_file(path) == _reference_quick_xor(b"munimentum" * 1000)


def test_extra_fields_travel_into_the_journal(data):
    path = data / "onedrive_export" / "plan.md"
    versions.write_bytes(path, b"x", extra={"quickxor": "q", "ms_quickxor": "m", "ms_match": False})
    entry = journal(data)[-1]
    assert (entry["quickxor"], entry["ms_quickxor"], entry["ms_match"]) == ("q", "m", False)
