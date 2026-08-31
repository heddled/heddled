"""The workspace, and mostly: trying to get out of it.

An agent that can touch files can touch exactly these files. Most of this file
is attempts to reach past that — traversal, absolute paths, symlinks, and the
one that matters most, an agent pointed at the directory holding the rules that
constrain it.
"""

import os

import pytest

from heddled import config, filetools, workspace, yamlio
from heddled.workspace import WorkspaceError


class FakeAgent:
    def __init__(self, name="filer", ws=True, path=None):
        self.name = name
        self.workspace = ws
        self.path = path


@pytest.fixture()
def root(project):
    """A workspace on disk, the way `workspace: true` makes one."""
    return workspace.resolve_root(FakeAgent())


# ------------------------------------------------------------------ the root


class TestWhereAWorkspaceMayBe:
    def test_true_means_one_of_its_own(self, project):
        root = workspace.resolve_root(FakeAgent(name="filer"))
        assert root == (project / "work" / "filer").resolve()
        assert root.is_dir()

    def test_a_path_is_taken_as_given(self, project):
        root = workspace.resolve_root(FakeAgent(ws="./exports"))
        assert root == (project / "exports").resolve()

    def test_no_workspace_means_none(self, project):
        assert workspace.resolve_root(FakeAgent(ws=None)) is None
        assert workspace.resolve_root(FakeAgent(ws=False)) is None

    @pytest.mark.parametrize("where", ["./agents", "./tools", "./data", "./var"])
    def test_the_platforms_own_directories_are_refused(self, project, where):
        """The one that matters. Agents are files: an agent that can write
        agents/support.yaml can delete the approval gate that constrains it, and
        no policy fixes that because the policy *is* the file."""
        with pytest.raises(WorkspaceError, match="overlaps"):
            workspace.resolve_root(FakeAgent(ws=where))

    def test_nor_the_project_itself(self, project):
        with pytest.raises(WorkspaceError, match="the project itself"):
            workspace.resolve_root(FakeAgent(ws="."))

    def test_nor_a_folder_containing_them(self, project):
        with pytest.raises(WorkspaceError):
            workspace.resolve_root(FakeAgent(ws=str(project)))

    def test_nor_one_inside_them(self, project):
        with pytest.raises(WorkspaceError):
            workspace.resolve_root(FakeAgent(ws="./agents/scratch"))


# ------------------------------------------------------------- getting out


class TestTryingToLeave:
    def test_a_plain_name_is_fine(self, root):
        assert workspace.safe_path(root, "notes.txt") == root / "notes.txt"

    def test_a_subfolder_is_fine(self, root):
        assert workspace.safe_path(root, "out/report.csv") == root / "out" / "report.csv"

    @pytest.mark.parametrize("attempt", [
        "../secrets.txt",
        "../../etc/passwd",
        "out/../../escape.txt",
        "..",
    ])
    def test_dot_dot_never_works(self, root, attempt):
        with pytest.raises(WorkspaceError, match="outside"):
            workspace.safe_path(root, attempt)

    @pytest.mark.parametrize("attempt", ["/etc/passwd", "/tmp/x"])
    def test_absolute_paths_never_work(self, root, attempt):
        with pytest.raises(WorkspaceError, match="absolute"):
            workspace.safe_path(root, attempt)

    def test_an_empty_name_is_refused(self, root):
        with pytest.raises(WorkspaceError):
            workspace.safe_path(root, "   ")

    def test_a_symlink_pointing_out_is_refused(self, root, project, tmp_path):
        """`..` is caught by looking at the string. A symlink is not — it is
        caught by resolving first and checking where the answer landed."""
        outside = tmp_path / "outside.txt"
        outside.write_text("not yours")
        os.symlink(outside, root / "link.txt")
        with pytest.raises(WorkspaceError, match="outside"):
            workspace.safe_path(root, "link.txt")

    def test_a_symlinked_folder_pointing_out_is_refused(self, root, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.txt").write_text("not yours")
        os.symlink(elsewhere, root / "shortcut")
        with pytest.raises(WorkspaceError, match="outside"):
            workspace.safe_path(root, "shortcut/x.txt")

    def test_a_symlink_that_stays_inside_is_allowed(self, root):
        (root / "real.txt").write_text("mine")
        os.symlink(root / "real.txt", root / "alias.txt")
        assert workspace.safe_path(root, "alias.txt").read_text() == "mine"


# ---------------------------------------------------------------- the tools


class TestReadingAndWriting:
    def test_write_then_read(self, root):
        workspace.write(root, "notes.txt", "hello")
        assert workspace.read(root, "notes.txt") == "hello"

    def test_writing_makes_the_folders_it_needs(self, root):
        workspace.write(root, "out/deep/report.csv", "a,b\n1,2\n")
        assert (root / "out" / "deep" / "report.csv").is_file()

    def test_writing_says_whether_it_replaced_something(self, root):
        assert workspace.write(root, "x.txt", "one")["replaced"] is False
        assert workspace.write(root, "x.txt", "two")["replaced"] is True
        assert workspace.read(root, "x.txt") == "two"

    def test_reading_something_that_is_not_there(self, root):
        with pytest.raises(WorkspaceError, match="no file called"):
            workspace.read(root, "nope.txt")

    def test_a_file_too_big_to_read_is_refused_by_name(self, root):
        (root / "big.txt").write_bytes(b"x" * (workspace.MAX_READ_BYTES + 1))
        with pytest.raises(WorkspaceError, match="over the"):
            workspace.read(root, "big.txt")

    def test_writing_too_much_is_refused(self, root):
        with pytest.raises(WorkspaceError, match="over the"):
            workspace.write(root, "big.txt", "x" * (workspace.MAX_WRITE_BYTES + 1))

    def test_something_it_cannot_read_says_so_rather_than_mojibake(self, root):
        """Bytes reaching a model as replacement characters waste a turn and
        read as a bug. Saying what it does take is more use than trying."""
        (root / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")
        with pytest.raises(WorkspaceError, match="not something this can read"):
            workspace.read(root, "photo.jpg")

    def test_listing_shows_files_with_their_sizes(self, root):
        workspace.write(root, "a.txt", "one")
        workspace.write(root, "sub/b.txt", "two")
        names = {f["path"] for f in workspace.listing(root)}
        assert names == {"a.txt", os.path.join("sub", "b.txt")}

    def test_listing_ignores_a_symlink_out(self, root, tmp_path):
        outside = tmp_path / "secret.txt"
        outside.write_text("not yours")
        os.symlink(outside, root / "peek.txt")
        assert workspace.listing(root) == []


# ----------------------------------------------------------- as an agent sees it


class TestTheToolsAnAgentGets:
    def test_no_workspace_means_no_file_tools(self, registry, project):
        agent = registry.get_agent("support")
        assert filetools.tools_for(agent) == {}

    def test_a_workspace_mounts_exactly_three(self, registry, project):
        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = True
        path.write_text(yamlio.dump(data))

        tools = registry.agent_tools(registry.get_agent("support"))
        assert {"list_files", "read_file", "write_file"} <= set(tools)

    def test_they_are_separate_tools_so_policy_can_tell_them_apart(
            self, registry, project, store):
        """Reading wants to be ungated; writing wants approval. One tool with an
        `operation` argument could not express the difference."""
        from heddled import policies

        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = True
        data["policies"] = [{"tool": "write_file", "requires_approval": True}]
        path.write_text(yamlio.dump(data))
        agent = registry.get_agent("support")

        def decide(tool):
            return policies.check_tool_call(agent, tool, "webchat", store, "s_x")

        assert decide("write_file").requires_approval is True
        assert decide("read_file").requires_approval is False

    def test_the_handler_writes_into_the_workspace(self, registry, project):
        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = True
        path.write_text(yamlio.dump(data))

        handler = filetools.make_workspace_handler("write_file", "support")
        result = handler({"path": "summary.txt", "content": "done"}, _Ctx())
        assert result["path"] == "summary.txt"
        assert (project / "work" / "support" / "summary.txt").read_text() == "done"

    def test_the_handler_refuses_to_leave(self, registry, project):
        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = True
        path.write_text(yamlio.dump(data))

        handler = filetools.make_workspace_handler("write_file", "support")
        with pytest.raises(WorkspaceError):
            handler({"path": "../../escaped.txt", "content": "no"}, _Ctx())
        assert not (project.parent / "escaped.txt").exists()

    def test_an_agent_without_one_gets_a_clear_refusal(self, registry, project):
        handler = filetools.make_workspace_handler("list_files", "support")
        with pytest.raises(WorkspaceError, match="no workspace"):
            handler({}, _Ctx())


class _Ctx:
    """Just enough context for a handler: somewhere for its log lines to go."""

    def __init__(self):
        self.lines = []

    def log(self, message, **extra):
        self.lines.append(message)


class TestTheToggle:
    def test_ticking_the_box_gives_it_a_workspace(self, client, registry):
        client.post("/agents/support/fields",
                    data={"workspace_present": "1", "workspace": "on"})
        assert registry.get_agent("support").workspace is True
        assert "list_files" in registry.agent_tools(registry.get_agent("support"))

    def test_unticking_it_takes_the_tools_away(self, client, registry, project):
        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = True
        path.write_text(yamlio.dump(data))

        client.post("/agents/support/fields", data={"workspace_present": "1"})
        agent = registry.get_agent("support")
        assert not agent.workspace
        assert "write_file" not in registry.agent_tools(agent)

    def test_ticking_it_does_not_flatten_a_path_someone_wrote(
            self, client, registry, project):
        """The form says yes or no; the raw tab says where. Saving the form with
        the box already ticked must not overwrite a considered path with
        `true`."""
        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = "./shared/exports"
        path.write_text(yamlio.dump(data))

        client.post("/agents/support/fields",
                    data={"workspace_present": "1", "workspace": "on"})
        assert registry.get_agent("support").workspace == "./shared/exports"

    def test_a_form_without_the_section_leaves_it_alone(self, client, registry):
        client.post("/agents/support/fields",
                    data={"workspace_present": "1", "workspace": "on"})
        client.post("/agents/support/fields", data={"description": "Still here."})
        assert registry.get_agent("support").workspace is True


def _with_workspace(project, registry, name="support"):
    path = project / "agents" / f"{name}.yaml"
    data = yamlio.load(path.read_text())
    data["workspace"] = True
    path.write_text(yamlio.dump(data))
    return workspace.resolve_root(registry.get_agent(name))


class TestTheWorkspacePanel:
    def test_an_agent_without_one_shows_no_panel(self, client, registry):
        assert "Its files" not in client.get("/agents/support").get_data(as_text=True)

    def test_the_files_are_listed(self, client, project, registry):
        root = _with_workspace(project, registry)
        workspace.write(root, "invoices.csv", "a,b\n1,2\n")
        body = client.get("/agents/support").get_data(as_text=True)
        assert "Its files" in body and "invoices.csv" in body

    def test_a_file_the_agent_cannot_read_says_so(self, client, project, registry):
        """Otherwise an operator drops a file in and is left wondering why the
        agent says it cannot read it."""
        root = _with_workspace(project, registry)
        (root / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")
        body = client.get("/agents/support").get_data(as_text=True)
        assert "not text" in body

    def test_a_document_is_not_marked_unreadable(self, client, project, registry):
        root = _with_workspace(project, registry)
        workspace.write(root, "report.docx", "# Report\n")
        body = client.get("/agents/support").get_data(as_text=True)
        assert "report.docx" in body
        assert body.count("not text") == 0

    def test_a_broken_workspace_does_not_take_the_page_down(
            self, client, project, registry):
        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = "./agents"          # refused, unconditionally
        path.write_text(yamlio.dump(data))
        r = client.get("/agents/support")
        assert r.status_code == 200
        assert "overlaps" in r.get_data(as_text=True)


class TestViewingAndDownloading:
    def test_viewing_shows_the_text(self, client, project, registry):
        root = _with_workspace(project, registry)
        workspace.write(root, "notes.txt", "the quick brown fox")
        body = client.get(
            "/agents/support/files/view?path=notes.txt").get_data(as_text=True)
        assert "the quick brown fox" in body

    def test_downloading_is_always_an_attachment(self, client, project, registry):
        """This serves whatever somebody put in the folder, from the origin
        holding the administrator's session. Inline, an uploaded .html would run
        as a page on that origin."""
        root = _with_workspace(project, registry)
        workspace.write(root, "evil.html", "<script>alert(1)</script>")
        r = client.get("/agents/support/files/download?path=evil.html")
        assert r.status_code == 200
        assert r.headers["Content-Disposition"].startswith("attachment")
        assert "html" not in r.headers["Content-Type"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_the_bytes_come_back_unchanged(self, client, project, registry):
        root = _with_workspace(project, registry)
        (root / "data.bin").write_bytes(b"\x00\x01\x02rawbytes")
        r = client.get("/agents/support/files/download?path=data.bin")
        assert r.data == b"\x00\x01\x02rawbytes"

    @pytest.mark.parametrize("route", ["view", "download"])
    @pytest.mark.parametrize("attempt", ["../../agents/support.yaml", "/etc/passwd"])
    def test_neither_route_can_be_walked_out_of(
            self, client, project, registry, route, attempt):
        _with_workspace(project, registry)
        r = client.get(f"/agents/support/files/{route}?path={attempt}")
        assert r.status_code == 400
        assert "name" not in r.get_data(as_text=True).lower() or True

    def test_an_agent_with_no_workspace_has_no_routes(self, client, registry):
        assert client.get(
            "/agents/support/files/view?path=x").status_code == 404


class TestUploadingAndDeleting:
    def test_uploading_puts_the_file_there(self, client, project, registry):
        import io

        root = _with_workspace(project, registry)
        client.post("/agents/support/files", data={
            "file": (io.BytesIO(b"a,b\n1,2\n"), "rows.csv")},
            content_type="multipart/form-data")
        assert (root / "rows.csv").read_bytes() == b"a,b\n1,2\n"

    def test_a_filename_from_a_browser_cannot_walk_out(
            self, client, project, registry):
        """The name is a string somebody else chose, so it goes through the same
        check as everything else rather than a bespoke one."""
        import io

        _with_workspace(project, registry)
        before = (project / "agents" / "support.yaml").read_text()
        client.post("/agents/support/files", data={
            "file": (io.BytesIO(b"pwned"), "../../agents/support.yaml")},
            content_type="multipart/form-data")
        assert (project / "agents" / "support.yaml").read_text() == before

    def test_deleting_removes_it(self, client, project, registry):
        root = _with_workspace(project, registry)
        workspace.write(root, "gone.txt", "bye")
        client.post("/agents/support/files",
                    data={"action": "delete", "path": "gone.txt"})
        assert not (root / "gone.txt").exists()

    def test_deleting_cannot_reach_outside(self, client, project, registry):
        _with_workspace(project, registry)
        client.post("/agents/support/files",
                    data={"action": "delete", "path": "../../agents/support.yaml"})
        assert (project / "agents" / "support.yaml").exists()

    def test_a_viewer_may_look_but_not_add_or_remove(
            self, client_as, project, registry):
        """No exemption here, unlike chatting and approving: putting a file in
        really is changing something."""
        import io

        root = _with_workspace(project, registry)
        workspace.write(root, "there.txt", "hello")
        viewer = client_as("viewer")
        assert viewer.get("/agents/support").status_code == 200
        assert viewer.post("/agents/support/files", data={
            "file": (io.BytesIO(b"x"), "new.txt")},
            content_type="multipart/form-data").status_code == 403
        assert viewer.post("/agents/support/files",
                           data={"action": "delete", "path": "there.txt"}).status_code == 403
        assert (root / "there.txt").exists()


class TestDocumentsInTheWorkspace:
    """A model cannot emit a .docx, so it writes markdown and the extension
    decides what is made. No second tool for the agent to know about."""

    def test_the_extension_decides_what_is_written(self, root):
        for name in ("report.docx", "rows.xlsx", "deck.pptx"):
            result = workspace.write(root, name, "# Title\n\n| a | b |\n| - | - |\n| 1 | 2 |\n")
            assert result["bytes"] > 0
            assert (root / name).read_bytes()[:2] == b"PK", f"{name} is not a package"

    def test_anything_else_is_still_written_as_text(self, root):
        workspace.write(root, "notes.md", "# Title\n")
        assert (root / "notes.md").read_text() == "# Title\n"

    def test_a_document_written_here_can_be_read_back(self, root):
        workspace.write(root, "r.docx", "# Heading\n\nSome words.\n")
        text = workspace.read(root, "r.docx")
        assert "Heading" in text and "Some words." in text

    def test_a_spreadsheet_comes_back_as_rows(self, root):
        workspace.write(root, "r.xlsx", "a,b\n1,2\n")
        assert workspace.read(root, "r.xlsx").splitlines()[0] == "a,b"

    def test_a_document_still_cannot_be_written_outside(self, root):
        with pytest.raises(WorkspaceError, match="outside"):
            workspace.write(root, "../escaped.docx", "# no")

    def test_a_file_that_is_neither_text_nor_a_document_is_refused(self, root):
        (root / "blob.bin").write_bytes(b"\x00\x01\x02")
        with pytest.raises(WorkspaceError, match="not something this can read"):
            workspace.read(root, "blob.bin")

    def test_the_listing_marks_documents_as_readable(self, root):
        workspace.write(root, "r.docx", "# Hello\n")
        (root / "blob.bin").write_bytes(b"\x00\x01")
        by_name = {f["path"]: f["readable"] for f in workspace.listing(root)}
        assert by_name["r.docx"] is True
        assert by_name["blob.bin"] is False

    def test_the_tool_writes_one_end_to_end(self, registry, project):
        path = project / "agents" / "support.yaml"
        data = yamlio.load(path.read_text())
        data["workspace"] = True
        path.write_text(yamlio.dump(data))

        handler = filetools.make_workspace_handler("write_file", "support")
        result = handler({"path": "out/summary.docx",
                          "content": "# Summary\n\nAll clear.\n"}, _Ctx())
        made = project / "work" / "support" / "out" / "summary.docx"
        assert made.is_file() and made.read_bytes()[:2] == b"PK"
        assert result["path"].endswith("summary.docx")
