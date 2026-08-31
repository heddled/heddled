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

    def test_a_binary_file_says_so_rather_than_returning_mojibake(self, root):
        """A PDF reaching a model as replacement characters wastes a turn and
        reads as a bug. Saying what it takes is more use than trying."""
        (root / "invoice.pdf").write_bytes(b"%PDF-1.4\x00\x00binary")
        with pytest.raises(WorkspaceError, match="not a text file"):
            workspace.read(root, "invoice.pdf")

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
