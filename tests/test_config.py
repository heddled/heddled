"""Project root resolution.

A pip-installed `heddled` must find *your* project, not the directory it happens to
be installed into — otherwise `heddled init` scaffolds agents into site-packages.
"""

from heddled import config


class TestFindRoot:
    def test_a_directory_holding_agents_is_the_root(self, tmp_path):
        (tmp_path / "agents").mkdir()
        assert config.find_root(tmp_path) == tmp_path

    def test_the_search_walks_up_from_a_subdirectory(self, tmp_path):
        (tmp_path / "agents").mkdir()
        deep = tmp_path / "tools" / "lookup_invoice"
        deep.mkdir(parents=True)
        assert config.find_root(deep) == tmp_path

    def test_an_empty_directory_is_its_own_root(self, tmp_path):
        """What `heddled init` in a fresh folder needs."""
        empty = tmp_path / "fresh"
        empty.mkdir()
        assert config.find_root(empty) == empty

    def test_the_nearest_project_wins(self, tmp_path):
        (tmp_path / "agents").mkdir()
        inner = tmp_path / "nested"
        (inner / "agents").mkdir(parents=True)
        assert config.find_root(inner) == inner

    def test_the_root_is_never_the_package_directory(self, tmp_path):
        """Regression: the root used to be derived from the installed package
        location, so a real `pip install` wrote agents into site-packages."""
        empty = tmp_path / "elsewhere"
        empty.mkdir()
        package_dir = __import__("pathlib").Path(config.__file__).resolve().parent
        assert config.find_root(empty) != package_dir

    def test_paths_are_resolved_absolute(self, tmp_path):
        (tmp_path / "agents").mkdir()
        assert config.find_root(tmp_path).is_absolute()
